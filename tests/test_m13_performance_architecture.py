# -*- coding: utf-8 -*-
"""
Tests for Milestone M13: Performance & Architecture Simplification.
Verifies:
1. Agile first-sentence splitting (TTFA < 1s) and conversational interjections.
2. StreamingBilingualParser immediate early dispatch on first clause.
3. TtsService DB decoupling and voice profile option pre-resolution.
4. Decoupled Provider Protocol (LLMProvider, ProviderError, Registry, Providers).
5. Character Manifest v2 and character packaging/validation.
6. ChatTurnProfiler latency waterfall metrics and reporting.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from galgame2voice.utils.text_splitter import (
    is_short_salutation,
    is_natural_clause_boundary,
    split_japanese_sentences,
)
from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.services.tts_service import (
    TtsService,
    clear_tts_profile_cache,
)
from galgame2voice.providers import (
    LLMProvider,
    BaseLLMProvider,
    ProviderError,
    OpenAIProvider,
    GeminiProvider,
    AnthropicProvider,
    DeepSeekProvider,
    XAIProvider,
    create_provider,
    get_provider_class,
    list_registered_providers,
)
from galgame2voice.schemas.character_manifest import (
    CharacterManifestV2,
    validate_character_package,
)
from galgame2voice.utils.profiler import (
    ChatTurnProfiler,
    is_profiling_enabled,
    set_profiling_enabled,
)


# ============================================================================
# 1. Agile First-Sentence Splitting & Interjection Tests
# ============================================================================

def test_is_short_salutation():
    assert is_short_salutation("はい、")
    assert is_short_salutation("ええ、")
    assert is_short_salutation("うん、")
    assert is_short_salutation("あの、")
    assert is_short_salutation("そうですね、")
    assert is_short_salutation("大丈夫、")
    assert not is_short_salutation("今日はとてもいい天気ですね。")
    assert not is_short_salutation("")


def test_split_japanese_sentences_agile_first_chunk():
    # Conversational opener with comma should split immediately on first chunk
    text = "はい、今日もお疲れ様でした。明日も頑張りましょう。"
    sents_first = split_japanese_sentences(text, min_chars=12, is_first_chunk=True)
    assert len(sents_first) >= 2
    assert "はい" in sents_first[0]

    # Non-first chunk respects normal sentence boundary
    sents_normal = split_japanese_sentences(text, min_chars=12, is_first_chunk=False)
    assert len(sents_normal) >= 1


def test_streaming_parser_early_first_chunk():
    parser = StreamingBilingualParser()
    chunk1 = '{"tts": {"speed": 1.0, "emotion": "gentle"}, "chinese": "好啊，", "japanese": "はい、今日もお疲れ様でした。"}'
    delta_ch, sents = parser.feed_chunk(chunk1)
    assert delta_ch == "好啊，"
    # First sentence should be extracted immediately
    assert len(sents) >= 1
    assert "はい" in sents[0]


# ============================================================================
# 2. TTS Service SQLite Critical Path Decoupling
# ============================================================================

@pytest.mark.asyncio
async def test_tts_service_bypasses_sqlite_when_pre_resolved():
    clear_tts_profile_cache()
    svc = TtsService()
    opts = {
        "_pre_resolved": True,
        "ref_audio_path": "refs/gentle.ogg",
        "prompt_text": "テストです",
        "prompt_lang": "ja",
    }
    # Mock database to ensure get_db is NEVER called
    with patch("galgame2voice.database.session.get_db") as mock_get_db:
        res = await svc._populate_voice_profile_opts(opts)
        mock_get_db.assert_not_called()
        assert res.get("ref_audio_path") == "refs/gentle.ogg"
        assert res.get("prompt_text") == "テストです"
        assert res.get("prompt_lang") == "ja"


@pytest.mark.asyncio
async def test_tts_service_memoizes_db_lookups():
    clear_tts_profile_cache()
    svc = TtsService()
    fake_profile = MagicMock(
        id=42,
        name="TestChar",
        prompt_text="テストプロンプト",
        prompt_lang="ja",
        text_lang="ja",
        ref_audio_path="refs/test.ogg",
        speed=1.0,
        temperature=0.8,
    )
    with patch("galgame2voice.database.session.get_db") as mock_get_db:
        mock_conn = AsyncMock()
        mock_get_db.return_value.__aenter__.return_value = mock_conn
        with patch("galgame2voice.database.crud.get_voice_profile", new_callable=AsyncMock) as mock_get_prof:
            mock_get_prof.return_value = fake_profile

            # First call: hits DB and caches in memo
            opts1 = {"voice_profile_id": 42}
            res1 = await svc._populate_voice_profile_opts(opts1)
            assert res1.get("prompt_text") == "テストプロンプト"
            assert mock_get_prof.call_count == 1

            # Second call: hits in-memory memo, 0 DB calls!
            opts2 = {"voice_profile_id": 42}
            res2 = await svc._populate_voice_profile_opts(opts2)
            assert res2.get("prompt_text") == "テストプロンプト"
            assert mock_get_prof.call_count == 1  # Still 1!


# ============================================================================
# 3. Decoupled Provider Protocol & Registry Tests
# ============================================================================

def test_provider_registry_and_factory():
    registered = list_registered_providers()
    assert "openai" in registered
    assert "gemini" in registered
    assert "anthropic" in registered
    assert "deepseek" in registered
    assert "xai" in registered

    openai_p = create_provider("openai", api_key="sk-test")
    assert isinstance(openai_p, LLMProvider)
    assert isinstance(openai_p, OpenAIProvider)

    gemini_p = create_provider("gemini", api_key="sk-test")
    assert isinstance(gemini_p, LLMProvider)
    assert isinstance(gemini_p, GeminiProvider)

    anthropic_p = create_provider("anthropic", api_key="sk-test")
    assert isinstance(anthropic_p, LLMProvider)
    assert isinstance(anthropic_p, AnthropicProvider)


def test_provider_error_normalization():
    p = create_provider("openai", api_key="sk-test")

    # 401 Auth error
    mock_resp_401 = MagicMock(status_code=401)
    err_401 = Exception("Unauthorized")
    err_401.response = mock_resp_401
    norm_401 = p.normalize_error(err_401)
    assert isinstance(norm_401, ProviderError)
    assert norm_401.code == "AUTHENTICATION_FAILED"
    assert norm_401.status_code == 401
    assert not norm_401.retryable

    # 429 Rate limit error
    mock_resp_429 = MagicMock(status_code=429)
    err_429 = Exception("Too Many Requests")
    err_429.response = mock_resp_429
    norm_429 = p.normalize_error(err_429)
    assert norm_429.code == "RATE_LIMIT_EXCEEDED"
    assert norm_429.status_code == 429
    assert norm_429.retryable


# ============================================================================
# 4. Character Package Standard (Manifest v2 & Packager)
# ============================================================================

def test_character_manifest_v2_model():
    manifest_data = {
        "id": "test_heroine",
        "name": "测试女主角",
        "manifest_version": "2.0",
        "default_voice_params": {"speed": 1.05, "temperature": 0.85},
        "emotions": {
            "gentle": {
                "audio": "refs/gentle.ogg",
                "text": "こんにちは、テストです。",
                "lang": "ja",
            }
        },
    }
    m = CharacterManifestV2.model_validate(manifest_data)
    assert m.id == "test_heroine"
    assert m.manifest_version == "2.0"
    assert "gentle" in m.emotions
    assert m.default_voice_params.speed == 1.05


def test_validate_character_package_existing_natsume():
    natsume_dir = Path("characters/四季夏目")
    if natsume_dir.exists():
        is_valid, errors = validate_character_package(natsume_dir)
        assert is_valid, f"Validation failed with errors: {errors}"
        assert len(errors) == 0


# ============================================================================
# 5. Profiling Mode (ChatTurnProfiler) Tests
# ============================================================================

def test_profiler_waterfall_generation():
    profiler = ChatTurnProfiler(turn_id="test-turn-1", enabled=True)
    profiler.record_llm_first_token()
    profiler.record_first_sentence()
    profiler.record_tts_dispatch(chunk_index=0)
    profiler.record_tts_inference(chunk_index=0, cached=False)
    profiler.record_tts_inference(chunk_index=1, cached=True)
    profiler.record_first_audio()

    report = profiler.generate_report()
    assert "Chat Turn #test-turn-1" in report
    assert "LLM TTFT:" in report
    assert "First Sentence:" in report
    assert "TTS Dispatch:" in report
    assert "TTS Inference:" in report
    assert "TTFA:" in report
    assert "Cache Hit:" in report


def test_profiler_env_toggle():
    set_profiling_enabled(False)
    assert not is_profiling_enabled()

    set_profiling_enabled(True)
    assert is_profiling_enabled()
    set_profiling_enabled(False)


# ============================================================================
# 6. Character Packager CLI & Web Audio Gapless Configuration Tests
# ============================================================================

def test_character_packager_pack_and_install():
    from scripts.tools.character_packager import cmd_validate, cmd_pack, cmd_install

    natsume_dir = Path("characters/四季夏目")
    if not natsume_dir.exists():
        pytest.skip("characters/四季夏目 not present")

    # 1. Validate
    assert cmd_validate(natsume_dir) == 0

    with tempfile.TemporaryDirectory() as tmpdir:
        out_zip = Path(tmpdir) / "natsume_test.zip"
        # 2. Pack
        ret_pack = cmd_pack(natsume_dir, out_zip)
        assert ret_pack == 0
        assert out_zip.is_file()
        assert out_zip.stat().st_size > 0

        # 3. Install
        install_target = Path(tmpdir) / "installed_chars"
        ret_inst = cmd_install(out_zip, install_target)
        assert ret_inst == 0
        installed_manifest = install_target / "四季夏目" / "manifest.json"
        assert installed_manifest.is_file()


def test_web_audio_crossfade_and_interrupt_constants():
    # Verify static audio_player.js defines 12ms micro-fade and 40ms interrupt ramp
    js_path = Path("galgame2voice/static/js/audio_player.js")
    assert js_path.is_file()
    content = js_path.read_text(encoding="utf-8")
    assert "0.012" in content  # 12ms micro-fade default
    assert "0.04" in content   # 40ms linear interruption fade-out

