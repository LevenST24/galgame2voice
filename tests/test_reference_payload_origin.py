"""Tests verifying that final GPT-SoVITS payload retains selected reference audio and metadata."""

import json
import struct
import wave
from pathlib import Path

import httpx
import pytest

from galgame2voice.config import get_settings
from galgame2voice.services.character_manager import CharacterManager, get_character_manager
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.voice_resolver import get_voice_resolver


def _create_wav(path: Path, duration_sec: float = 4.0, sample_rate: int = 16000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = int(sample_rate * duration_sec)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_frames):
            val = int(2000 * (1 if (i // 80) % 2 == 0 else -1))
            frames.extend(struct.pack("<h", val))
        w.writeframes(frames)
    return path


@pytest.fixture
def character_env(tmp_path, monkeypatch):
    root = tmp_path / "app_root"
    chars_dir = root / "characters"
    default_pkg_dir = chars_dir / "default_heroine"

    # Default package has its own gentle.wav under refs/
    gentle_wav = _create_wav(default_pkg_dir / "refs" / "gentle.wav", duration_sec=4.0)

    gpt_weights = default_pkg_dir / "weights" / "model.ckpt"
    sovits_weights = default_pkg_dir / "weights" / "model.pth"
    gpt_weights.parent.mkdir(parents=True, exist_ok=True)
    gpt_weights.write_bytes(b"dummy gpt weights")
    sovits_weights.write_bytes(b"dummy sovits weights")

    manifest = {
        "id": "default_heroine",
        "name": "默认女主角",
        "version": "1.0.0",
        "is_default": True,
        "gpt_weights": "weights/model.ckpt",
        "sovits_weights": "weights/model.pth",
        "emotions": {
            "gentle": {
                "audio": "refs/gentle.wav",
                "text": "默认角色的提示词",
                "lang": "ja",
            }
        },
    }
    (default_pkg_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(root))
    monkeypatch.setenv("GALGAME2VOICE_CHARACTERS_DIR", str(chars_dir))
    monkeypatch.setenv("GALGAME2VOICE_AUDIO_DIR", str(root / "audio"))
    monkeypatch.setenv("GALGAME2VOICE_SKIP_MEM_CHECK", "1")
    get_settings.cache_clear()
    CharacterManager.reset_instance()
    get_voice_resolver().invalidate()

    mgr = get_character_manager(characters_dir=chars_dir)
    discovered = mgr.discover_characters()
    assert len(discovered) == 1
    assert discovered[0].is_valid

    yield {
        "root": root,
        "default_pkg_dir": default_pkg_dir,
        "default_gentle": gentle_wav,
    }

    CharacterManager.reset_instance()
    get_voice_resolver().invalidate()


@pytest.mark.asyncio
async def test_resolved_profile_reference_not_hijacked_by_default_package(character_env, tmp_path):
    captured_payloads = []

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tts":
            data = json.loads(request.content.decode("utf-8"))
            captured_payloads.append(data)
            wav_path = tmp_path / "mock_response.wav"
            _create_wav(wav_path, duration_sec=1.0)
            return httpx.Response(200, content=wav_path.read_bytes())
        return httpx.Response(200, json={"code": 0})

    transport = httpx.MockTransport(mock_transport_handler)
    client = GptSovitsClient(
        base_url="http://127.0.0.1:9880",
        server=None,
        client=httpx.AsyncClient(transport=transport),
    )

    # Set stale engine state to ensure client defaults are not borrowed
    client.current_refer_audio = "/engine/stale_engine_ref.wav"
    client.current_refer_text = "stale engine prompt"
    client.current_refer_language = "en"

    # Selected profile specifies unknown remote_sub reference path with valid empty prompt
    selected_ref = "refs/remote_sub/gentle.wav"
    options = {
        "ref_audio_path": selected_ref,
        "prompt_text": "",
        "prompt_lang": "ja",
        "_profile_prompt_resolved": True,
    }

    try:
        # 1. Verify _build_tts_payload directly
        payload = client._build_tts_payload("こんにちは", options=options)
        assert payload["ref_audio_path"] == selected_ref, (
            f"Expected ref_audio_path to remain '{selected_ref}', but got '{payload['ref_audio_path']}'"
        )
        assert payload["prompt_text"] == ""
        assert payload["prompt_lang"] == "ja"

        # 2. Verify full synthesize() through MockTransport
        audio = await client.synthesize("こんにちは", options=options)
        assert audio
        assert len(captured_payloads) == 1
        captured = captured_payloads[0]
        assert captured["ref_audio_path"] == selected_ref, (
            f"Expected captured ref_audio_path to be '{selected_ref}', but got '{captured['ref_audio_path']}'"
        )
        assert captured["prompt_text"] == ""
        assert captured["prompt_lang"] == "ja"
    finally:
        await client.aclose()


def test_authoritative_reference_resolves_valid_local_custom_audio(character_env):
    client = GptSovitsClient(server=None)
    custom_path = character_env["root"] / "custom" / "custom_voice.wav"
    _create_wav(custom_path, duration_sec=4.0)

    options = {
        "ref_audio_path": "custom/custom_voice.wav",
        "prompt_text": "カスタム音声です",
        "prompt_lang": "ja",
        "_profile_prompt_resolved": True,
    }
    payload = client._build_tts_payload("テスト", options=options)
    assert payload["ref_audio_path"] == str(custom_path.resolve())
    assert payload["prompt_text"] == "カスタム音声です"
    assert payload["prompt_lang"] == "ja"


def test_legacy_unpinned_fallback_preserves_engine_defaults(character_env):
    client = GptSovitsClient(server=None)
    client.current_refer_audio = "/engine/stale_engine_ref.wav"
    client.current_refer_text = "stale engine prompt"
    client.current_refer_language = "zh"

    # Legacy caller passes no selector and no profile_resolved marker
    options = {}
    payload = client._build_tts_payload("テスト", options=options)
    assert payload["ref_audio_path"] == "/engine/stale_engine_ref.wav"
    assert payload["prompt_text"] == "stale engine prompt"
    assert payload["prompt_lang"] == "zh"


def test_legacy_unpinned_missing_reference_resolves_via_fuzzy_fallback(character_env):
    client = GptSovitsClient(server=None)
    # Legacy caller without profile_resolved marker passes refs/gentle.wav
    # which should resolve via legacy package search to default_heroine/refs/gentle.wav
    options = {
        "ref_audio_path": "refs/gentle.wav",
    }
    payload = client._build_tts_payload("テスト", options=options)
    assert payload["ref_audio_path"] == str(character_env["default_gentle"].resolve())


def test_authoritative_missing_ref_raises_without_borrowing_engine_ref(character_env):
    client = GptSovitsClient(server=None)
    client.current_refer_audio = "/engine/stale_engine_ref.wav"
    client.current_refer_text = "stale engine prompt"

    options = {
        "ref_audio_path": "",
        "_profile_prompt_resolved": True,
    }
    with pytest.raises(ValueError, match="尚未配置角色包或参考音频，请在「设置中心 → 角色与会话」导入角色包或创建自定义音色后重试。"):
        client._build_tts_payload("テスト", options=options)


@pytest.mark.parametrize("options", [None, {}, {"ref_audio_path": ""}])
def test_build_tts_payload_without_reference_audio_raises_chinese_guidance(options):
    client = GptSovitsClient(server=None)
    with pytest.raises(ValueError, match="尚未配置角色包或参考音频，请在「设置中心 → 角色与会话」导入角色包或创建自定义音色后重试。"):
        client._build_tts_payload("テスト", options=options)


@pytest.mark.asyncio
async def test_tts_stream_pipeline_chunk_synthesis_without_voice_returns_chinese_guidance(isolate_test_database):
    import re
    from galgame2voice.services.tts_service import TtsService
    from galgame2voice.services.chat_pipelines.tts_pipeline import TtsStreamPipeline

    client = GptSovitsClient(server=None)
    service = TtsService(client=client, db_path=isolate_test_database)
    pipeline = TtsStreamPipeline(service, "generation_fresh_install")

    chunk_data, err_str = await pipeline.synthesize_chunk(
        sentence="你好，这是测试台词。",
        chunk_index=0,
        options={},
    )
    assert chunk_data is None
    expected_msg = "尚未配置角色包或参考音频，请在「设置中心 → 角色与会话」导入角色包或创建自定义音色后重试。"
    assert err_str == expected_msg
    # Ensure frontend request.js:getErrorMessage regex (/[\u3400-\u9fff]/) recognizes it as Chinese guidance
    assert re.search(r"[\u3400-\u9fff]", err_str) is not None


