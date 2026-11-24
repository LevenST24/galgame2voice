"""Deleting a character-pack voice moves its folder to the backup folder so it stays deleted."""

from __future__ import annotations

from pathlib import Path
import shutil

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
    finally:
        CharacterManager.reset_instance()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_a_folder_that_cannot_move_keeps_the_voice(tmp_path: Path, isolate_test_database, monkeypatch):
    await _init_isolated_env(monkeypatch, tmp_path, isolate_test_database)
    CharacterManager.reset_instance()
    try:
        _install_character_package(tmp_path, "ann", "Ann")
        async with _app_http() as http:
            assert (await http.post("/api/characters/rescan")).status_code == 200
            ann = await _ann(http)

            def locked(*_args, **_kwargs):
                raise PermissionError(32, "The process cannot access the file")

            monkeypatch.setattr(shutil, "move", locked)
            res = await http.delete(f"/api/voice/profiles/{ann['id']}")
            assert res.status_code == 409
            assert "没能移到备份文件夹" in res.json()["detail"]
            assert await _ann(http) is not None
            assert (tmp_path / "characters" / "ann" / "manifest.json").is_file()
    finally:
        CharacterManager.reset_instance()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_a_custom_voice_deletes_without_touching_packages(tmp_path: Path, isolate_test_database, monkeypatch):
    await _init_isolated_env(monkeypatch, tmp_path, isolate_test_database)
    CharacterManager.reset_instance()
    try:
        _install_character_package(tmp_path, "ann", "Ann")
        from tests.test_character_voice_catalog import _create_dummy_voice_assets, _create_voice_profile

        async with _app_http() as http:
            assert (await http.post("/api/characters/rescan")).status_code == 200
            created = await _create_voice_profile(http, "自定义", _create_dummy_voice_assets(tmp_path, "c1"), "テスト")
            custom_id = created.json()["profile"]["id"] if "profile" in created.json() else created.json()["id"]
            res = await http.delete(f"/api/voice/profiles/{custom_id}")
            assert res.status_code == 200, res.text
            assert res.json()["moved_to"] is None
            assert (tmp_path / "characters" / "ann" / "manifest.json").is_file()
            assert not (get_settings().data_dir / "deleted_characters").exists()
    finally:
        CharacterManager.reset_instance()
        get_settings.cache_clear()
