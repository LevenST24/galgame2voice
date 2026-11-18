"""
Unit and Integration Tests for GptSovitsClient and Audio Probing.
Verifies:
1. async_probe_audio_duration_seconds async execution via asyncio.to_thread on miss and fast-path on hit.
2. validate_reference_audio 3~10s duration boundaries.
"""

import asyncio
import io
import struct
import wave
from pathlib import Path

import pytest

from galgame2voice.services.gpt_sovits_client import (
    GptSovitsClient,
    SLICING_METHODS,
    TTS_PRESETS,
    async_probe_audio_duration_seconds,
    probe_audio_duration_seconds,
    resolve_tts_options,
    validate_reference_audio,
)


def _make_test_wav(file_path: Path, duration_sec: float = 4.0, sample_rate: int = 16000) -> Path:
    """Helper to create a valid mono PCM16 WAV file with actual non-zero audio."""
    n_frames = int(sample_rate * duration_sec)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_frames):
            val = 4000 if (i // 100) % 2 == 0 else -4000
            frames.extend(struct.pack("<h", val))
        wf.writeframes(frames)
    file_path.write_bytes(buf.getvalue())
    return file_path


def test_probe_audio_duration_seconds_none_or_missing(tmp_path):
    """Verifies that invalid or non-existent paths safely return None."""
    assert probe_audio_duration_seconds(None) is None
    assert probe_audio_duration_seconds("") is None
    assert probe_audio_duration_seconds(str(tmp_path / "non_existent_audio.wav")) is None


@pytest.mark.asyncio
async def test_async_probe_audio_duration_seconds(tmp_path):
    """Verifies async_probe_audio_duration_seconds returns float and offloads miss to thread."""
    wav_path = _make_test_wav(tmp_path / "async_probe.wav", duration_sec=5.0)

    # Initial async call
    dur = await async_probe_audio_duration_seconds(wav_path)
    assert dur is not None
    assert abs(dur - 5.0) < 0.1

    # Fast path: already in stat cache
    dur_cached = await async_probe_audio_duration_seconds(wav_path)
    assert dur_cached == dur

    # Missing / empty
    assert await async_probe_audio_duration_seconds(None) is None
    assert await async_probe_audio_duration_seconds("") is None
    assert await async_probe_audio_duration_seconds(tmp_path / "does_not_exist.wav") is None


def test_validate_reference_audio(tmp_path):
    """Verifies 3.0s~10.0s reference audio validation rules."""
    short_wav = _make_test_wav(tmp_path / "short.wav", duration_sec=2.0)
    valid_wav = _make_test_wav(tmp_path / "valid.wav", duration_sec=5.0)
    long_wav = _make_test_wav(tmp_path / "long.wav", duration_sec=12.0)

    # Short (< 3.0s)
    ok_short, msg_short = validate_reference_audio(str(short_wav))
    assert ok_short is False
    assert "outside the 3~10s range" in msg_short

    # Valid (3.0s - 10.0s)
    ok_valid, msg_valid = validate_reference_audio(str(valid_wav))
    assert ok_valid is True
    assert msg_valid == ""

    # Long (> 10.0s)
    ok_long, msg_long = validate_reference_audio(str(long_wav))
    assert ok_long is False
    assert "outside the 3~10s range" in msg_long

    # Missing file
    ok_missing, msg_missing = validate_reference_audio(str(tmp_path / "ghost.wav"))
    assert ok_missing is False
    assert "not found" in msg_missing


def test_audio_spec_module_reexport_and_direct_import(tmp_path):
    """Verifies that audio spec symbols can be imported directly from utils.audio_spec as well as gpt_sovits_client."""
    import galgame2voice.utils.audio_spec as u_spec
    import galgame2voice.services.gpt_sovits_client as c_spec
    import galgame2voice.services.tts_options as t_opts

    assert u_spec.REFERENCE_AUDIO_MIN_SECONDS == 3.0
    assert u_spec.REFERENCE_AUDIO_MAX_SECONDS == 10.0
    assert c_spec.REFERENCE_AUDIO_MIN_SECONDS == 3.0
    assert c_spec.REFERENCE_AUDIO_MAX_SECONDS == 10.0

    assert u_spec._AUDIO_SPEC_CACHE is c_spec._AUDIO_SPEC_CACHE
    assert u_spec._AUDIO_SPEC_CACHE is c_spec._GLOBAL_AUDIO_SPEC_CACHE
    assert u_spec.AudioSpec is c_spec.AudioSpec
    assert u_spec.AudioSpecCache is c_spec.AudioSpecCache

    # Verify silence detection re-exports
    assert u_spec.SILENT_AUDIO_ERROR == c_spec.SILENT_AUDIO_ERROR
    assert u_spec.wav_peak_amplitude is c_spec.wav_peak_amplitude
    assert u_spec.is_silent_audio is c_spec.is_silent_audio
    assert u_spec.wav_is_silent is c_spec.wav_is_silent

    # Verify TTS options re-exports
    assert t_opts.SLICING_METHODS is c_spec.SLICING_METHODS
    assert t_opts.TTS_PRESETS is c_spec.TTS_PRESETS
    assert t_opts._TTS_NUMERIC_RANGES is c_spec._TTS_NUMERIC_RANGES
    assert t_opts._TTS_STRING_MAXLEN is c_spec._TTS_STRING_MAXLEN
    assert t_opts.validate_user_tts_options is c_spec.validate_user_tts_options
    assert t_opts.resolve_tts_options is c_spec.resolve_tts_options
    assert t_opts.VoiceProfileWeightSpec is c_spec.VoiceProfileWeightSpec
    assert t_opts._extract_weight_spec is c_spec._extract_weight_spec


def test_validate_reference_audio_bytes(tmp_path):
    """Verifies validation when passing raw audio bytes (short, valid, long, empty, exotic)."""
    short_wav = _make_test_wav(tmp_path / "short_bytes.wav", duration_sec=2.0).read_bytes()
    valid_wav = _make_test_wav(tmp_path / "valid_bytes.wav", duration_sec=5.0).read_bytes()
    long_wav = _make_test_wav(tmp_path / "long_bytes.wav", duration_sec=12.0).read_bytes()

    # Short (< 3.0s) bytes
    ok_short, msg_short = validate_reference_audio(short_wav)
    assert ok_short is False
    assert "outside the 3~10s range" in msg_short

    # Valid (3.0s - 10.0s) bytes
    ok_valid, msg_valid = validate_reference_audio(valid_wav)
    assert ok_valid is True
    assert msg_valid == ""

    # Long (> 10.0s) bytes
    ok_long, msg_long = validate_reference_audio(long_wav)
    assert ok_long is False
    assert "outside the 3~10s range" in msg_long

    # Empty bytes
    ok_empty, msg_empty = validate_reference_audio(b"")
    assert ok_empty is False
    assert "empty reference audio data" in msg_empty

    # Exotic undecodable bytes should allow engine to decide
    ok_exotic, msg_exotic = validate_reference_audio(b"EXOTIC_AUDIO_FORMAT_DATA_STREAM")
    assert ok_exotic is True
    assert msg_exotic == ""


def test_probe_audio_spec_bytes_and_raw_riff_fallback(tmp_path, monkeypatch):
    """Verifies byte probing with standard libraries and raw WAV RIFF header parsing fallback."""
    from galgame2voice.utils.audio_spec import probe_audio_spec, _probe_wav_riff_header

    wav_path = _make_test_wav(tmp_path / "riff_test.wav", duration_sec=4.0)
    data = wav_path.read_bytes()

    # Normal byte probe
    spec = probe_audio_spec(data)
    assert spec is not None
    assert abs(spec.duration_s - 4.0) < 0.1
    assert spec.sample_rate == 16000
    assert spec.channels == 1

    # Raw RIFF header parser directly
    riff_spec = _probe_wav_riff_header(data)
    assert riff_spec is not None
    assert abs(riff_spec.duration_s - 4.0) < 0.1
    assert riff_spec.sample_rate == 16000
    assert riff_spec.channels == 1

    # Fallback path when soundfile and wave fail
    import sys
    monkeypatch.setitem(sys.modules, "soundfile", None)
    monkeypatch.setitem(sys.modules, "wave", None)

    fallback_spec = probe_audio_spec(data)
    assert fallback_spec is not None
    assert abs(fallback_spec.duration_s - 4.0) < 0.1
    assert fallback_spec.sample_rate == 16000


@pytest.mark.asyncio
async def test_synthesize_empty_after_cleaning_raises_value_error(mock_gpt_sovits, sample_voice_profile):
    """Verifies real GptSovitsClient.synthesize rejects stage-direction-only text before calling /tts."""
    client = GptSovitsClient(server=mock_gpt_sovits)
    await client.switch_voice_profile(sample_voice_profile)
    calls_before = len(mock_gpt_sovits.call_history)

    with pytest.raises(ValueError, match="empty after cleaning"):
        await client.synthesize("（笑）（ため息）（静寂）")

    assert len(mock_gpt_sovits.call_history) == calls_before
    assert not any(call["endpoint"] == "/tts" for call in mock_gpt_sovits.call_history)


@pytest.mark.parametrize("cut_option", ["cut0", "cut1", "cut2", "cut3", "cut4", "cut5"])
@pytest.mark.asyncio
async def test_synthesize_cut_options_map_to_text_split_method(mock_gpt_sovits, sample_voice_profile, cut_option):
    """Verifies all 6 cut_option values map to payload text_split_method on real GptSovitsClient.synthesize."""
    client = GptSovitsClient(server=mock_gpt_sovits)
    await client.switch_voice_profile(sample_voice_profile)

    audio = await client.synthesize("テスト", options={"cut_option": cut_option})

    assert audio.startswith(b"RIFF")
    assert mock_gpt_sovits.call_history[-1]["endpoint"] == "/tts"
    assert mock_gpt_sovits.call_history[-1]["payload"]["text_split_method"] == cut_option


@pytest.mark.parametrize("language", ["ja", "zh", "en"])
@pytest.mark.asyncio
async def test_synthesize_multilingual_text_language_option(mock_gpt_sovits, sample_voice_profile, language):
    """Verifies text_language option (ja/zh/en) maps to payload text_lang on real GptSovitsClient.synthesize."""
    client = GptSovitsClient(server=mock_gpt_sovits)
    await client.switch_voice_profile(sample_voice_profile)

    audio = await client.synthesize("Hello/你好/こんにちは", options={"text_language": language})

    assert audio.startswith(b"RIFF")
    assert mock_gpt_sovits.call_history[-1]["endpoint"] == "/tts"
    assert mock_gpt_sovits.call_history[-1]["payload"]["text_lang"] == language


class TestPresetResolution:
    """Test suite for TTS preset options and slicing configurations."""

    def test_preset_definitions_exist(self):
        assert "high_quality" in TTS_PRESETS
        assert "balanced" in TTS_PRESETS
        assert "low_latency" in TTS_PRESETS

    def test_slicing_methods_coverage(self):
        for cut in ["cut0", "cut1", "cut2", "cut3", "cut4", "cut5"]:
            assert cut in SLICING_METHODS

    def test_resolve_preset_high_quality(self):
        opts = resolve_tts_options({"preset": "high_quality"})
        assert opts["top_k"] == 20
        assert opts["temperature"] == 0.8
        assert opts["text_split_method"] == "cut5"

    def test_resolve_preset_low_latency(self):
        opts = resolve_tts_options({"preset": "low_latency"})
        assert opts["top_k"] == 5
        assert opts["temperature"] == 0.5
        assert opts["speed"] == 1.2
        assert opts["text_split_method"] == "cut5"

    def test_custom_options_override_preset(self):
        opts = resolve_tts_options({
            "preset": "balanced",
            "speed": 1.5,
            "top_k": 30,
            "text_split_method": "cut3",
        })
        assert opts["speed"] == 1.5
        assert opts["top_k"] == 30
        assert opts["text_split_method"] == "cut3"

    def test_preset_application_with_extreme_values(self):
        """Tests resolve_tts_options across extreme boundary values."""
        # Case 1: Extreme low vs extreme high speed
        low_speed = resolve_tts_options({"preset": "high_quality", "speed": 0.1})
        assert low_speed["speed"] == 0.1
        assert low_speed["top_k"] == 20  # from high_quality preset

        high_speed = resolve_tts_options({"preset": "low_latency", "speed": 3.0})
        assert high_speed["speed"] == 3.0
        assert high_speed["top_k"] == 5  # from low_latency preset

        # Case 2: Extreme temperature 0.0 vs 2.0
        zero_temp = resolve_tts_options({"temperature": 0.0})
        assert zero_temp["temperature"] == 0.0

        high_temp = resolve_tts_options({"temperature": 2.0})
        assert high_temp["temperature"] == 2.0

        # Case 3: Extreme top_k 1 vs 100
        min_topk = resolve_tts_options({"top_k": 1})
        assert min_topk["top_k"] == 1

        max_topk = resolve_tts_options({"top_k": 100})
        assert max_topk["top_k"] == 100

        # Case 4: Extreme top_p 0.01 vs 1.0
        low_topp = resolve_tts_options({"top_p": 0.01})
        assert low_topp["top_p"] == 0.01

        max_topp = resolve_tts_options({"top_p": 1.0})
        assert max_topp["top_p"] == 1.0

    def test_invalid_or_unknown_preset_fallback(self):
        """Verifies non-existent preset name falls back to 'balanced' without raising exception."""
        resolved = resolve_tts_options({"preset": "non_existent_preset_xyz"})
        assert resolved["speed"] == TTS_PRESETS["balanced"]["speed"]
        assert resolved["top_k"] == TTS_PRESETS["balanced"]["top_k"]
        assert resolved["temperature"] == TTS_PRESETS["balanced"]["temperature"]


class TestGptSovitsClient:
    """Test suite for GptSovitsClient endpoints, rollback, streaming, and serialization."""

    @pytest.mark.asyncio
    async def test_client_endpoints_and_health(self, mock_gpt_sovits):
        client = GptSovitsClient(server=mock_gpt_sovits)
        health = await client.check_health()
        assert health["connected"] is True
        assert health["status"] == "running"

        control_res = await client.control("restart")
        assert control_res["status"] == "running"

    @pytest.mark.asyncio
    async def test_set_individual_weights(self, mock_gpt_sovits):
        client = GptSovitsClient(server=mock_gpt_sovits)

        ok_gpt = await client.set_gpt_weights("weights/custom.ckpt")
        assert ok_gpt is True
        assert mock_gpt_sovits.current_gpt_weights == "weights/custom.ckpt"

        ok_sovits = await client.set_sovits_weights("weights/custom.pth")
        assert ok_sovits is True
        assert mock_gpt_sovits.current_sovits_weights == "weights/custom.pth"

        ok_ref = await client.set_refer_audio("ref/custom.wav", "カスタムテキスト", "ja")
        assert ok_ref is True
        assert mock_gpt_sovits.current_refer_audio == "ref/custom.wav"

    @pytest.mark.asyncio
    async def test_rollback_on_step2_sovits_failure(self, mock_gpt_sovits):
        client = GptSovitsClient(server=mock_gpt_sovits)
        profile_a = {
            "name": "ProfileA",
            "gpt_weights_path": "weights/a.ckpt",
            "sovits_weights_path": "weights/a.pth",
            "refer_audio_path": "ref/a.wav",
            "refer_text": "テキストA",
        }
        await client.switch_voice_profile(profile_a)
        assert mock_gpt_sovits.current_gpt_weights == "weights/a.ckpt"

        profile_b = {
            "name": "ProfileB",
            "gpt_weights_path": "weights/b.ckpt",
            "sovits_weights_path": "weights/b.pth",
            "refer_audio_path": "ref/b.wav",
            "refer_text": "テキストB",
        }
        mock_gpt_sovits.fail_on_step("set_sovits_weights")
        success = await client.switch_voice_profile(profile_b)
        assert success is False
        # Rollback check: GPT weights restored to profile A
        assert mock_gpt_sovits.current_gpt_weights == "weights/a.ckpt"

    @pytest.mark.asyncio
    async def test_rollback_on_step3_refer_audio_failure(self, mock_gpt_sovits):
        client = GptSovitsClient(server=mock_gpt_sovits)
        profile_a = {
            "name": "ProfileA",
            "gpt_weights_path": "weights/a.ckpt",
            "sovits_weights_path": "weights/a.pth",
            "refer_audio_path": "ref/a.wav",
            "refer_text": "テキストA",
        }
        await client.switch_voice_profile(profile_a)

        profile_b = {
            "name": "ProfileB",
            "gpt_weights_path": "weights/b.ckpt",
            "sovits_weights_path": "weights/b.pth",
            "refer_audio_path": "ref/b.wav",
            "refer_text": "テキストB",
        }
        mock_gpt_sovits.fail_on_step("set_refer_audio")
        success = await client.switch_voice_profile(profile_b)
        assert success is False
        # Rollback check: Both GPT and SoVITS weights restored to profile A
        assert mock_gpt_sovits.current_gpt_weights == "weights/a.ckpt"
        assert mock_gpt_sovits.current_sovits_weights == "weights/a.pth"

    @pytest.mark.asyncio
    async def test_synthesize_and_streaming(self, mock_gpt_sovits, sample_voice_profile):
        client = GptSovitsClient(server=mock_gpt_sovits)
        await client.switch_voice_profile(sample_voice_profile)

        # Full synthesize
        audio_bytes = await client.synthesize("（笑顔で）こんにちは、先生！")
        assert audio_bytes.startswith(b"RIFF")
        assert len(audio_bytes) > 44

        # Stream TTS
        chunks = []
        async for chunk in client.stream_tts("（優しく）今日もお疲れ様でした。", chunk_size=512):
            chunks.append(chunk)

        combined = b"".join(chunks)
        assert combined.startswith(b"RIFF")
        assert len(chunks) > 1

    @pytest.mark.asyncio
    async def test_inference_mutex_serialization(self, mock_gpt_sovits):
        client = GptSovitsClient(server=mock_gpt_sovits)
        mock_gpt_sovits.simulate_latency_s = 0.05

        p1 = {"name": "P1", "gpt_weights_path": "w1.ckpt", "sovits_weights_path": "w1.pth", "refer_audio_path": "r1.wav", "refer_text": "t1"}
        p2 = {"name": "P2", "gpt_weights_path": "w2.ckpt", "sovits_weights_path": "w2.pth", "refer_audio_path": "r2.wav", "refer_text": "t2"}

        res1, res2 = await asyncio.gather(
            client.switch_voice_profile(p1),
            client.switch_voice_profile(p2),
        )
        assert res1 is True
        assert res2 is True
        assert mock_gpt_sovits.max_concurrent_seen == 1

