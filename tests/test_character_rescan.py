"""Hot character discovery keeps a complete catalog available to readers."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.main import create_app
from galgame2voice.services.character_manager import CharacterManager, CharacterManifest, CharacterPackage


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
