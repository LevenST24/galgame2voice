"""Hot character discovery keeps a complete catalog available to readers."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import wave

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import CharacterAffectionUpdate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.services.character_manager import CharacterManager, CharacterManifest, CharacterPackage
from galgame2voice.services.voice_resolver import get_voice_resolver


def _manager(tmp_path, monkeypatch):
    root = tmp_path / "characters"
    root.mkdir()
    for name in ("alpha", "beta"):
        folder = root / name
        folder.mkdir()
        (folder / "manifest.json").write_text("{}", encoding="utf-8")
    manager = CharacterManager(root)

    def load(folder, manifest_path):
        return CharacterPackage(folder, CharacterManifest(id=folder.name, name=folder.name))

    monkeypatch.setattr(manager, "_load_and_validate_package", load)
    manager.discover_characters()
    return manager, load


def test_hot_scan_keeps_old_portrait_and_aliases_until_complete(tmp_path, monkeypatch):
    manager, load = _manager(tmp_path, monkeypatch)
    original = manager.get_character("beta")
    entered, release = threading.Event(), threading.Event()

    def paused_load(folder, manifest_path):
        if folder.name == "beta":
            entered.set()
            assert release.wait(5), "test did not release the scan"
        package = load(folder, manifest_path)
        package.manifest.name = f"new {folder.name}"
        return package

    monkeypatch.setattr(manager, "_load_and_validate_package", paused_load)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(manager.discover_characters)
        try:
            assert entered.wait(5), "scan never reached beta"
            assert manager.get_character("beta") is original
            assert manager.get_active_character_manifest("beta") is original.manifest
            assert {p.name for p in manager.get_available_characters()} == {"alpha", "beta"}
        finally:
            release.set()
        pending.result(timeout=5)
    assert manager.get_character("new beta").name == "new beta"
    assert {p.name for p in manager.get_available_characters()} == {"new alpha", "new beta"}


def test_failed_scan_preserves_previous_catalog(tmp_path, monkeypatch):
    manager, load = _manager(tmp_path, monkeypatch)
    original = manager.get_character("beta")

    def failed_load(folder, manifest_path):
        if folder.name == "beta":
            raise OSError("temporary read failure")
        return load(folder, manifest_path)

    monkeypatch.setattr(manager, "_load_and_validate_package", failed_load)
    with pytest.raises(OSError, match="temporary read failure"):
        manager.discover_characters()
    assert manager.get_character("beta") is original
    assert {p.name for p in manager.get_available_characters()} == {"alpha", "beta"}


def test_empty_install_does_not_scan_again_on_every_lookup(tmp_path, monkeypatch):
    manager = CharacterManager(tmp_path / "no-characters")
    discover = manager.discover_characters
    calls = 0

    def counted():
        nonlocal calls
        calls += 1
        return discover()

    monkeypatch.setattr(manager, "discover_characters", counted)
    assert manager.get_character("missing") is None
    assert manager.get_default_character() is None
    assert manager.get_available_characters() == []
    assert calls == 1


@pytest.mark.asyncio
async def test_rescans_serialize_scan_and_db_sync_per_app(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    events = []

    class Manager:
        def discover_characters(self):
            events.append("scan")
            return []

        async def sync_with_db(self, conn):
            events.append("sync-start")
            if events.count("sync-start") == 1:
                entered.set()
                await release.wait()
            events.append("sync-end")
            return 0

    monkeypatch.setattr("galgame2voice.routers.characters.get_character_manager", lambda: Manager())
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(client.post("/api/characters/rescan"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(client.post("/api/characters/rescan"))
        try:
            await asyncio.sleep(0.02)
            assert events == ["scan", "sync-start"]
        finally:
            release.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert [r.status_code for r in results] == [200, 200]
    assert events == ["scan", "sync-start", "sync-end", "scan", "sync-start", "sync-end"]


def _install_package(characters_dir: Path, package_id: str, name: str, aliases: list[str] | None = None) -> Path:
    pkg = characters_dir / package_id
    pkg.mkdir(parents=True, exist_ok=True)
    with wave.open(str(pkg / "gentle.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x01\x00" * 8000 * 4)
    (pkg / "gpt.ckpt").write_bytes(b"x" * 5000)
    (pkg / "sovits.pth").write_bytes(b"x" * 5000)
    manifest = {
        "id": package_id,
        "name": name,
        "aliases": aliases or [],
        "gpt_weights": "gpt.ckpt",
        "sovits_weights": "sovits.pth",
        "system_prompt": f"Persona for {name}",
        "emotions": {"gentle": {"audio": "gentle.wav", "text": name, "lang": "ja"}},
    }
    (pkg / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return pkg


@pytest.mark.asyncio
async def test_rescan_package_rename_with_alias_updates_profile_and_preserves_associations(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    CharacterManager.reset_instance()

    chars_dir = tmp_path / "characters"
    pkg_dir = _install_package(chars_dir, "char_01", "夏目", aliases=[])
    await init_db()

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Initial scan creates profile for 夏目
        res = await client.post("/api/characters/rescan")
        assert res.status_code == 200
        assert res.json()["characters"] == ["夏目"]

        listed = (await client.get("/api/characters")).json()
        assert listed["total"] == 1
        initial_profile = listed["characters"][0]
        original_id = initial_profile["id"]
        assert initial_profile["name"] == "夏目"

        # Bind a conversation session and an affection score to profile id
        session_res = await client.post(
            "/api/chat/sessions",
            json={"id": "s_natsume", "title": "夏目的对话", "voice_profile_id": original_id},
        )
        assert session_res.status_code == 200

        async with get_db() as conn:
            await crud.get_or_create_character_affection(conn, "default_user", original_id)
            await crud.update_character_affection(
                conn, "default_user", original_id, CharacterAffectionUpdate(affection_score=42)
            )
            await conn.commit()

        # Prime VoiceProfileResolver cache with the original name
        resolver = get_voice_resolver()
        ctx_before = await resolver.resolve_context(character_name="夏目")
        assert ctx_before is not None
        assert ctx_before.name == "夏目"
        assert ctx_before.profile_id == original_id

        # Rename character package in manifest: name="四季夏目", aliases=["夏目"]
        manifest_path = pkg_dir / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["name"] = "四季夏目"
        data["aliases"] = ["夏目"]
        manifest_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        # Rescan via real API
        rescan2 = await client.post("/api/characters/rescan")
        assert rescan2.status_code == 200
        assert rescan2.json()["characters"] == ["四季夏目"]

        # Check listed character: profile name should be updated to "四季夏目" and id must be unchanged
        listed2 = (await client.get("/api/characters")).json()
        assert listed2["total"] == 1
        renamed_profile = listed2["characters"][0]
        assert renamed_profile["id"] == original_id
        assert renamed_profile["name"] == "四季夏目"

        # Check VoiceProfileResolver resolves both new name and alias, with cache invalidated
        ctx_new = await resolver.resolve_context(character_name="四季夏目")
        assert ctx_new is not None
        assert ctx_new.name == "四季夏目"
        assert ctx_new.profile_id == original_id
        ctx_alias = await resolver.resolve_context(character_name="夏目")
        assert ctx_alias is not None
        assert ctx_alias.name == "四季夏目"
        assert ctx_alias.profile_id == original_id

        # Now clean up aliases in manifest: aliases=[]
        data["aliases"] = []
        manifest_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        # Rescan again via real API
        rescan3 = await client.post("/api/characters/rescan")
        assert rescan3.status_code == 200

        # Assert resolver now resolves the clean name
        ctx_after_cleanup = await resolver.resolve_context(character_name="四季夏目")
        assert ctx_after_cleanup is not None
        assert ctx_after_cleanup.name == "四季夏目"
        assert ctx_after_cleanup.profile_id == original_id

        # Assert no duplicate profile was created, and id is still original_id
        listed3 = (await client.get("/api/characters")).json()
        assert listed3["total"] == 1
        final_profile = listed3["characters"][0]
        assert final_profile["id"] == original_id
        assert final_profile["name"] == "四季夏目"

        # Assert the session is still bound to original_id
        sess_get = (await client.get("/api/chat/sessions")).json()
        saved_sess = next((s for s in sess_get["sessions"] if s["id"] == "s_natsume"), None)
        assert saved_sess is not None
        assert saved_sess["voice_profile_id"] == original_id

        # Assert character affection is still associated with original_id
        async with get_db() as conn:
            aff = await crud.get_character_affection(conn, "default_user", original_id)
            assert aff is not None
            assert aff.affection_score == 42
            assert aff.affection_level == 3
