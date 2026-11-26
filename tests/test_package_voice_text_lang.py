"""A character pack with a Chinese reference clip still voices its Japanese lines as Japanese."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.services.character_manager import CharacterManager
from tests.test_character_voice_catalog import (
    _app_http, _create_dummy_voice_assets, _init_isolated_env, _install_character_package,
)


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


@pytest.mark.asyncio
async def test_create_voice_profile_preserves_prompt_lang_without_refer_language(
    tmp_path: Path, isolate_test_database, monkeypatch
):
    await _init_isolated_env(monkeypatch, tmp_path, isolate_test_database)
    gpt_path, sovits_path, wav_path = _create_dummy_voice_assets(tmp_path, "custom_zh")
    try:
        async with _app_http() as http:
            resp = await http.post("/api/voice/profiles", json={
                "name": "CustomZh", "gpt_weights_path": gpt_path, "sovits_weights_path": sovits_path,
                "ref_audio_path": wav_path, "prompt_text": "你好", "prompt_lang": "zh", "text_lang": "ja",
            })
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["profile"]["prompt_lang"] == "zh" and body["profile"]["text_lang"] == "ja"
        async with get_db(isolate_test_database) as conn:
            stored = await crud.get_voice_profile(conn, body["id"])
        assert stored is not None and stored.prompt_lang == "zh" and stored.text_lang == "ja"
    finally:
        get_settings.cache_clear()
