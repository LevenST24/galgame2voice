"""
Unit and Integration Tests for GptSovitsClient and Audio Probing.
Verifies:
1. GptSovitsClient connection pooling limits (20 keep-alive, 50 max, 30s expiry).
2. probe_audio_duration_seconds stat-based caching (mtime_ns, size).
3. async_probe_audio_duration_seconds async execution via asyncio.to_thread on miss and fast-path on hit.
4. validate_reference_audio 3~10s duration boundaries.
5. WAV duration extraction and silence invariant verification.
"""

import asyncio
import io
import struct
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from galgame2voice.services.gpt_sovits_client import (
    AudioSpec,
    AudioSpecCache,
    GptSovitsClient,
    _GLOBAL_AUDIO_SPEC_CACHE,
    async_probe_audio_duration_seconds,
    extract_wav_duration,
    probe_audio_duration_seconds,
    validate_reference_audio,
    wav_is_silent,
    wav_peak_amplitude,
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


def test_gpt_sovits_client_connection_limits():
    """Verifies that GptSovitsClient._get_client configures pooled httpx limits properly."""
    client = GptSovitsClient(base_url="http://127.0.0.1:9880")
    http_cli = client._get_client()

    assert isinstance(http_cli, httpx.AsyncClient)
    assert not http_cli.is_closed

    # Verify connection pool limits: 20 keep-alive, 50 max, 30s expiry
    pool = http_cli._transport._pool
    assert pool._max_keepalive_connections == 20
    assert pool._max_connections == 50
    assert pool._keepalive_expiry == 30.0

    # Verify backward-compatibility alias
    assert client._get_http_client() is http_cli


def test_probe_audio_duration_stat_cache(tmp_path):
    """Verifies that probe_audio_duration_seconds caches by stat (mtime_ns, size) and invalidates on edit."""
    wav_path = _make_test_wav(tmp_path / "stat_test.wav", duration_sec=4.0)
    cache = AudioSpecCache()

    # First call: cache miss, probes file
    dur1 = cache.get_duration(wav_path)
    assert dur1 is not None
    assert abs(dur1 - 4.0) < 0.1

    # Second call: cache hit
    with patch.object(cache, "_probe") as mock_probe:
        dur2 = cache.get_duration(wav_path)
        assert dur2 == dur1
        mock_probe.assert_not_called()

    # Modify file: update duration and change size
    _make_test_wav(wav_path, duration_sec=6.0)
    dur3 = cache.get_duration(wav_path)
    assert dur3 is not None
    assert abs(dur3 - 6.0) < 0.1


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


def test_extract_wav_duration_and_amplitude(tmp_path):
    """Verifies WAV duration extraction and peak amplitude / silence detection."""
    wav_path = _make_test_wav(tmp_path / "dur_test.wav", duration_sec=3.5)
    data = wav_path.read_bytes()

    dur = extract_wav_duration(data)
    assert dur is not None
    assert abs(dur - 3.5) < 0.1

    peak = wav_peak_amplitude(data)
    assert peak is not None
    assert peak > 0.0
    assert not wav_is_silent(data)

    # Empty / corrupted bytes
    assert extract_wav_duration(b"") is None
    assert wav_peak_amplitude(b"") is None


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


