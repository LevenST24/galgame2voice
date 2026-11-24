"""A character pack with a Chinese reference clip still voices its Japanese lines as Japanese."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from galgame2voice.config import get_settings
from galgame2voice.services.character_manager import CharacterManager
from tests.test_character_voice_catalog import _app_http, _init_isolated_env, _install_character_package


@pytest.mark.asyncio
async def test_a_chinese_reference_clip_keeps_japanese_lines(tmp_path: Path, isolate_test_database, monkeypatch):
    await _init_isolated_env(monkeypatch, tmp_path, isolate_test_database)
    CharacterManager.reset_instance()
    try:
        package = _install_character_package(tmp_path, "mei", "Mei")
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        manifest["emotions"]["gentle"]["lang"] = "zh"
        (package / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        async with _app_http() as http:
            assert (await http.post("/api/characters/rescan")).status_code == 200
            mei = next(row for row in (await http.get("/api/voice/profiles")).json()["profiles"] if row["name"] == "Mei")
        assert mei["prompt_lang"] == "zh"
        assert mei["text_lang"] == "ja"
    finally:
        CharacterManager.reset_instance()
        get_settings.cache_clear()
