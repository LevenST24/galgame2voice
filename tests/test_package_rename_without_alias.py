"""A package whose manifest renames the character (same folder, no alias for the old
name) keeps its voice row, so sessions, affection and memories stay with it."""
import json

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.database.session import init_db
from galgame2voice.main import create_app
from galgame2voice.services.character_manager import CharacterManager
from test_character_rescan import _install_package


def _rename(pkg_dir, name, **extra):
    manifest = pkg_dir / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data.update(name=name, **extra)
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    CharacterManager.reset_instance()
    return tmp_path


async def _client():
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8080")


@pytest.mark.asyncio
async def test_rename_without_alias_keeps_the_voice_row(project):
    pkg_dir = _install_package(project / "characters", "char_01", "夏目")
    await init_db()
    async with await _client() as client:
        assert (await client.post("/api/characters/rescan")).status_code == 200
        [before] = (await client.get("/api/characters")).json()["characters"]
        session = await client.post("/api/chat/sessions", json={"id": "s1", "title": "t", "voice_profile_id": before["id"]})
        assert session.status_code == 200

        _rename(pkg_dir, "四季夏目")
        assert (await client.post("/api/characters/rescan")).status_code == 200

        chars = (await client.get("/api/characters")).json()["characters"]
        assert [(c["id"], c["name"]) for c in chars] == [(before["id"], "四季夏目")]

        # A second rescan changes nothing.
        assert (await client.post("/api/characters/rescan")).status_code == 200
        assert [(c["id"], c["name"]) for c in (await client.get("/api/characters")).json()["characters"]] == [
            (before["id"], "四季夏目")
        ]


async def _rescan_profiles(client):
    assert (await client.post("/api/characters/rescan")).status_code == 200
    return {p["id"]: p for p in (await client.get("/api/voice/profiles")).json()["profiles"]}

@pytest.mark.asyncio
async def test_deleted_package_voice_does_not_hijack_custom_voice_reusing_audio(project):
    pkg_dir = _install_package(project / "characters", "char_01", "夏目")
    await init_db()
    async with await _client() as client:
        before_id = next(iter(await _rescan_profiles(client)))
        created = await client.post("/api/voice/profiles", json={
            "name": "夏目（自制）", "gpt_weights_path": str(pkg_dir / "gpt.ckpt"), "sovits_weights_path": str(pkg_dir / "sovits.pth"),
            "ref_audio_path": str(pkg_dir / "gentle.wav"), "system_prompt": "我的自定义人设"})
        assert created.status_code == 201 and (await client.delete(f"/api/voice/profiles/{before_id}")).status_code == 200
        profiles, cid = await _rescan_profiles(client), created.json()["id"]
        assert (profiles[cid]["name"], profiles[cid]["system_prompt"]) == ("夏目（自制）", "我的自定义人设") and any(p["id"] != cid and p["name"] == "夏目" for p in profiles.values())

@pytest.mark.asyncio
async def test_different_package_id_in_same_folder_does_not_take_over_old_row(project):
    pkg_dir = _install_package(project / "characters", "char_01", "夏目")
    await init_db()
    async with await _client() as client:
        before_id = next(iter(await _rescan_profiles(client)))
        _rename(pkg_dir, "栞那", id="char_02", system_prompt="Persona for 栞那")
        profiles = await _rescan_profiles(client)
        assert (profiles[before_id]["name"], profiles[before_id]["system_prompt"]) == ("夏目", "Persona for 夏目") and any(p["id"] != before_id and p["name"] == "栞那" for p in profiles.values())

@pytest.mark.asyncio
async def test_conflicting_name_row_is_not_claimed_by_package_id_fallback(project):
    alice_dir = _install_package(project / "characters", "alice", "shared")
    await init_db()
    async with await _client() as client:
        before_id = next(iter(await _rescan_profiles(client)))
        _rename(alice_dir, "Alice", aliases=["shared"])
        _install_package(project / "characters", "bella", "Bella", aliases=["shared"])
        profiles = await _rescan_profiles(client)
        assert profiles[before_id]["name"] == "shared" and {"shared", "Alice", "Bella"} <= {p["name"] for p in profiles.values()}
