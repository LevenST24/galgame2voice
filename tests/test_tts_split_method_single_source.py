"""Regression tests for single-source default text_split_method resolution across pipeline, service, options, and cache."""

from __future__ import annotations

import io
import struct
import wave
from typing import Any

import httpx
import pytest

from galgame2voice.database.session import init_db
from galgame2voice.services.chat_pipelines.tts_pipeline import TtsStreamPipeline
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_cache_manager import TtsCacheManager, _build_canonical_params_dict
from galgame2voice.services.tts_options import resolve_tts_options
from galgame2voice.services.tts_scheduler import TtsScheduler
from galgame2voice.services.tts_service import TtsService


def _make_audible_wav_bytes(duration_sec: float = 0.2, sample_rate: int = 32000) -> bytes:
    """Generates non-silent 16-bit PCM mono WAV bytes passing wav_is_silent."""
    buf = io.BytesIO()
    n_frames = int(sample_rate * duration_sec)
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_frames):
            val = int(3000 * (1 if (i // 100) % 2 == 0 else -1))
            frames.extend(struct.pack("<h", val))
        w.writeframes(frames)
    return buf.getvalue()


class _CapturingMockServer:
    """Minimal GPT-SoVITS mock server recording /tts request JSON payloads."""

    def __init__(self) -> None:
        self.tts_payloads: list[dict[str, Any]] = []
        self._wav = _make_audible_wav_bytes()

    async def handle_request(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        if path == "/tts":
            self.tts_payloads.append(dict(json_data or {}))
            return httpx.Response(200, content=self._wav)
        if path in ("/set_gpt_weights", "/set_sovits_weights"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"status": "ok"})


@pytest.fixture
async def tts_stack(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    db_path = tmp_path / "test_split.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db_path))
    await init_db(db_path)

    ref_wav = tmp_path / "ref.wav"
    ref_wav.write_bytes(_make_audible_wav_bytes(duration_sec=3.5))
    gpt_ckpt = tmp_path / "model.ckpt"
    gpt_ckpt.write_bytes(b"gpt")
    sovits_pth = tmp_path / "model.pth"
    sovits_pth.write_bytes(b"sovits")

    server = _CapturingMockServer()
    client = GptSovitsClient(base_url="http://127.0.0.1:19880", server=server)
    scheduler = TtsScheduler()
    monkeypatch.setattr("galgame2voice.services.tts_service.get_tts_scheduler", lambda: scheduler)
    cache = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(db_path))
    service = TtsService(client=client, cache_manager=cache, audio_dir=tmp_path)

    profile_opts = {
        "_pre_resolved": True,
        "voice_profile_id": 1,
        "gpt_weights_path": str(gpt_ckpt),
        "sovits_weights_path": str(sovits_pth),
        "ref_audio_path": str(ref_wav),
        "prompt_text": "こんにちは。",
        "prompt_lang": "ja",
        "text_lang": "ja",
    }

    yield service, cache, server, client, profile_opts

    await scheduler.aclose()
    await cache.aclose()
    await client.aclose()


def _canonical_split_from_cache(opts: dict[str, Any], clean_text: str) -> str:
    prof_fields = {
        "voice_profile_id": 1,
        "gpt_weights": "model.ckpt",
        "sovits_weights": "model.pth",
        "ref_audio": "ref.wav",
        "prompt_text": "こんにちは。",
        "prompt_lang": "ja",
        "text_lang": "ja",
    }
    return _build_canonical_params_dict(opts, prof_fields, clean_text)["text_split_method"]


# ============================================================================
# Bug 1: Explicit None / "" / whitespace must not leak "None" / "" to /tts
# ============================================================================


@pytest.mark.parametrize("empty_value", [None, "", "   "])
@pytest.mark.asyncio
async def test_bug1_null_or_blank_split_method_falls_back_to_cut0_in_service_and_cache(
    tts_stack, empty_value
):
    service, cache, server, _client, profile_opts = tts_stack
    text = "テストです。"
    opts = {**profile_opts, "text_split_method": empty_value}

    await service.synthesize(text, options=opts)

    assert len(server.tts_payloads) == 1
    assert server.tts_payloads[0]["text_split_method"] == "cut0"

    # Cache key with explicit null/blank split option must match omitted option and hit cache.
    k_empty, _, p_empty = cache.compute_cache_key(text, options=opts)
    k_default, _, p_default = cache.compute_cache_key(text, options=profile_opts)
    assert k_empty == k_default
    assert p_empty == p_default

    await service.synthesize(text, options=profile_opts)
    assert len(server.tts_payloads) == 1, "cache miss occurred because empty split option diverged from default"


@pytest.mark.parametrize("key", ["text_split_method", "cut_option", "how_to_cut"])
@pytest.mark.parametrize("empty_value", [None, "", "   "])
def test_bug1_resolve_tts_options_never_emits_none_or_blank(key, empty_value):
    resolved = resolve_tts_options({key: empty_value})
    assert resolved["text_split_method"] == "cut5"
    assert resolved["text_split_method"] not in ("None", "", "   ")


# ============================================================================
# Bug 2: Long stage directions (>80 raw, <=80 normalized) must select cut0
# ============================================================================


@pytest.mark.asyncio
async def test_bug2_long_stage_directions_with_short_dialogue_unifies_on_cut0_and_shares_cache(tts_stack):
    service, cache, server, _client, profile_opts = tts_stack
    stage_cues_text = "（微笑みながら）（深呼吸して）（恥ずかしそうに）" + "あ" * 60
    plain_text = "あ" * 60
    assert len(stage_cues_text.strip()) > 80

    pipeline = TtsStreamPipeline(tts_service=service, generation_id="gen-bug2")
    pipe_opts = pipeline.prepare_chunk_options(profile_opts, None, 0, stage_cues_text)
    svc_opts = service._sanitize_dynamic_voice_options(dict(profile_opts), text=stage_cues_text)
    k_cues, clean_cues, p_cues = cache.compute_cache_key(stage_cues_text, options=profile_opts)
    k_plain, clean_plain, p_plain = cache.compute_cache_key(plain_text, options=profile_opts)

    assert clean_cues == clean_plain == plain_text
    assert pipe_opts["text_split_method"] == "cut0"
    assert svc_opts["text_split_method"] == "cut0"
    assert k_cues == k_plain
    assert p_cues == p_plain

    # End-to-end synthesis with stage directions sends cut0 and populates cache for plain dialogue.
    await service.synthesize(stage_cues_text, options=profile_opts)
    assert len(server.tts_payloads) == 1
    assert server.tts_payloads[0]["text_split_method"] == "cut0"

    await service.synthesize(plain_text, options=profile_opts)
    assert len(server.tts_payloads) == 1, "plain dialogue failed to reuse cut0 cache entry from stage-cue line"


# ============================================================================
# Bug 3 & Full Consistency Matrix across Pipeline / Service / Cache / Options
# ============================================================================


@pytest.mark.parametrize(
    "text,clean_text,options,expected_split",
    [
        ("", "", {}, "cut0"),
        ("こんにちは。", "こんにちは。", {}, "cut0"),
        ("あ" * 80, "あ" * 80, {}, "cut0"),
        ("あ" * 81, "あ" * 81, {}, "cut2"),
        ("（微笑みながら）" + "あ" * 80, "あ" * 80, {}, "cut0"),
        ("（微笑みながら）" + "あ" * 81, "あ" * 81, {}, "cut2"),
        ("あ" * 10, "あ" * 10, {"text_split_method": None}, "cut0"),
        ("あ" * 10, "あ" * 10, {"text_split_method": ""}, "cut0"),
        ("あ" * 10, "あ" * 10, {"text_split_method": "   "}, "cut0"),
        ("あ" * 85, "あ" * 85, {"cut_option": None, "how_to_cut": ""}, "cut2"),
        ("あ" * 10, "あ" * 10, {"text_split_method": "", "cut_option": "cut3", "how_to_cut": "cut1"}, "cut3"),
        ("あ" * 10, "あ" * 10, {"text_split_method": None, "cut_option": "  ", "how_to_cut": "cut1"}, "cut1"),
        ("あ" * 10, "あ" * 10, {"how_to_cut": "cut1", "cut_option": "cut3"}, "cut3"),
        ("あ" * 10, "あ" * 10, {"text_split_method": "cut4", "cut_option": "cut3", "how_to_cut": "cut1"}, "cut4"),
        ("あ" * 10, "あ" * 10, {"text_split_method": " CUT3 "}, "cut3"),
        ("あ" * 10, "あ" * 10, {"cut_option": " CUT1 "}, "cut1"),
        ("あ" * 10, "あ" * 10, {"how_to_cut": " Cut4 "}, "cut4"),
    ],
)
def test_matrix_three_sites_agree_on_text_split_method(
    text: str,
    clean_text: str,
    options: dict[str, Any],
    expected_split: str,
    tmp_path,
):
    dummy_service = TtsService(
        client=GptSovitsClient(base_url="http://127.0.0.1:19880"),
        cache_manager=TtsCacheManager(cache_dir=tmp_path / "c", db_path=str(tmp_path / "m.db")),
        audio_dir=tmp_path,
    )
    pipeline = TtsStreamPipeline(tts_service=dummy_service, generation_id="gen-matrix")

    pipe_res = pipeline.prepare_chunk_options(dict(options), None, 0, text)["text_split_method"]
    svc_res = dummy_service._sanitize_dynamic_voice_options(dict(options), text=text)["text_split_method"]
    cache_res = _canonical_split_from_cache(dict(options), clean_text)

    assert pipe_res == expected_split
    assert svc_res == expected_split
    assert cache_res == expected_split


def test_resolve_tts_options_alias_precedence_and_normalization():
    assert resolve_tts_options({"text_split_method": "", "cut_option": "cut3", "how_to_cut": "cut1"})["text_split_method"] == "cut3"
    assert resolve_tts_options({"text_split_method": None, "cut_option": "  ", "how_to_cut": "cut1"})["text_split_method"] == "cut1"
    assert resolve_tts_options({"how_to_cut": "cut1", "cut_option": "cut3"})["text_split_method"] == "cut3"
    assert resolve_tts_options({"text_split_method": " CUT4 ", "cut_option": "cut3", "how_to_cut": "cut1"})["text_split_method"] == "cut4"
    assert resolve_tts_options({"preset": "balanced", "text_split_method": None})["text_split_method"] == "cut5"
