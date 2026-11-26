"""Deleting a character-pack voice moves its folder to the backup folder so it stays deleted."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from galgame2voice.config import get_settings
from galgame2voice.services.character_manager import CharacterManager
from tests.test_character_voice_catalog import _app_http, _init_isolated_env, _install_character_package


async def _ann(http):
    profiles = (await http.get("/api/voice/profiles")).json()["profiles"]
    return next((row for row in profiles if row["name"] == "Ann"), None)


@pytest.mark.asyncio
async def test_a_deleted_package_voice_stays_deleted_after_a_rescan(tmp_path: Path, isolate_test_database, monkeypatch):
    await _init_isolated_env(monkeypatch, tmp_path, isolate_test_database)
    CharacterManager.reset_instance()
    try:
        _install_character_package(tmp_path, "ann", "Ann")
        _install_character_package(tmp_path, "bea", "Bea")
        async with _app_http() as http:
            assert (await http.post("/api/characters/rescan")).status_code == 200
            ann = await _ann(http)

            res = await http.delete(f"/api/voice/profiles/{ann['id']}")
            assert res.status_code == 200, res.text
            moved_to = res.json()["moved_to"]
            backup = tmp_path / moved_to
            assert backup.parent == get_settings().data_dir / "deleted_characters"
            assert backup.name.startswith("ann-")
            assert (backup / "manifest.json").is_file()
            assert not (tmp_path / "characters" / "ann").exists()

            assert (await http.post("/api/characters/rescan")).status_code == 200
            assert await _ann(http) is None
            assert CharacterManager.get_instance().get_character("Ann") is None
            scanned = (await http.get("/api/voice/scan-models")).json()
            paths = [x["path"] for k in ("gpt_weights", "sovits_weights", "audio_files") for x in scanned[k]]
            assert paths and all("deleted_characters" not in p.replace("\\", "/") for p in paths)
            bea = next(r for r in (await http.get("/api/voice/profiles")).json()["profiles"] if r["name"] == "Bea")
            in_sync, release, orig_sync = asyncio.Event(), asyncio.Event(), CharacterManager.sync_with_db
            async def slow_sync(self, conn):
                in_sync.set()
                await release.wait()
                return await orig_sync(self, conn)
            monkeypatch.setattr(CharacterManager, "sync_with_db", slow_sync)
            rescan_task = asyncio.create_task(http.post("/api/characters/rescan"))
            try:
                await in_sync.wait()
                dels = [asyncio.create_task(http.delete(f"/api/voice/profiles/{bea['id']}")) for _ in range(2)]
                await asyncio.sleep(0.05)
                assert all(not task.done() for task in dels)
            finally:
                release.set()
            r_scan, r1, r2 = await asyncio.gather(rescan_task, *dels)
            assert r_scan.status_code == 200 and sorted((r1.status_code, r2.status_code)) == [200, 404]
    finally:
        CharacterManager.reset_instance()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_a_folder_that_cannot_move_keeps_the_voice(tmp_path: Path, isolate_test_database, monkeypatch):
    await _init_isolated_env(monkeypatch, tmp_path, isolate_test_database)
    CharacterManager.reset_instance()
    try:
        _install_character_package(tmp_path, "ann", "Ann")
        ann_dir = tmp_path / "characters" / "ann"
        before_files = sorted(p.relative_to(ann_dir).as_posix() for p in ann_dir.rglob("*"))
        async with _app_http() as http:
            assert (await http.post("/api/characters/rescan")).status_code == 200
            ann = await _ann(http)

            def locked(*_args, **_kwargs):
                raise PermissionError(32, "The process cannot access the file")

            monkeypatch.setattr(os, "rename", locked)
            res = await http.delete(f"/api/voice/profiles/{ann['id']}")
            assert res.status_code == 409
            assert "没能移到备份文件夹" in res.json()["detail"]
            assert await _ann(http) is not None
            assert (ann_dir / "manifest.json").is_file()
            assert sorted(p.relative_to(ann_dir).as_posix() for p in ann_dir.rglob("*")) == before_files
    finally:
        CharacterManager.reset_instance()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_a_custom_voice_deletes_without_touching_packages(tmp_path: Path, isolate_test_database, monkeypatch):
    await _init_isolated_env(monkeypatch, tmp_path, isolate_test_database)
    CharacterManager.reset_instance()
    try:
        _install_character_package(tmp_path, "ann", "安娜")
        from tests.test_character_voice_catalog import _create_dummy_voice_assets, _create_voice_profile

        async with _app_http() as http:
            assert (await http.post("/api/characters/rescan")).status_code == 200
            assets = _create_dummy_voice_assets(tmp_path, "c1")
            for name in ("自定义", "ann", "安娜 (ann)", "default"):
                cid = (await _create_voice_profile(http, name, assets, "テスト")).json()["id"]
                res = await http.delete(f"/api/voice/profiles/{cid}")
                assert res.status_code == 200 and res.json()["moved_to"] is None, res.text
            assert (tmp_path / "characters" / "ann" / "manifest.json").is_file()
            assert not (get_settings().data_dir / "deleted_characters").exists()
    finally:
        CharacterManager.reset_instance()
        get_settings.cache_clear()
