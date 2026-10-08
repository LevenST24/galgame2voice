"""
Unit and Integration Tests for TtsService.
Verifies:
1. get_audio_duration stat-based caching and cache invalidation on file modification.
2. async_get_audio_duration non-blocking thread delegation and fast-path hit.
3. clear_tts_profile_cache clearing all in-memory caches.
4. _populate_voice_profile_opts using async duration checks and fallback logic.
5. synthesize / stream basic contract handling.
"""

import asyncio
import io
import struct
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from galgame2voice.services.tts_service import (
    TtsService,
    _AUDIO_DURATION_CACHE,
    _AUDIO_STAT_DURATION_CACHE,
    async_get_audio_duration,
    clear_tts_profile_cache,
)
from galgame2voice.utils.audio_spec import AudioSpecCache, _AUDIO_SPEC_CACHE


def _make_test_wav(file_path: Path, duration_sec: float = 4.0, sample_rate: int = 16000) -> Path:
    """Creates a mono PCM16 WAV file of specified duration."""
    n_frames = int(sample_rate * duration_sec)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_frames):
            val = 3000 if (i // 100) % 2 == 0 else -3000
            frames.extend(struct.pack("<h", val))
        wf.writeframes(frames)
    file_path.write_bytes(buf.getvalue())
    return file_path


def setup_function():
    clear_tts_profile_cache()


def test_get_audio_duration_stat_cache(tmp_path):
    """Verifies get_audio_duration uses (path, mtime_ns, size) stat caching."""
    wav_path = _make_test_wav(tmp_path / "duration_test.wav", duration_sec=4.5)

    # First probe: miss, disk inspection
    dur1 = TtsService.get_audio_duration(wav_path)
    assert dur1 is not None
    assert abs(dur1 - 4.5) < 0.1

    # Stat cache should have recorded the entry
    st = wav_path.stat()
    stat_key = (str(wav_path.resolve()), st.st_mtime_ns, st.st_size)
    assert stat_key in _AUDIO_SPEC_CACHE._cache
    assert stat_key in _AUDIO_STAT_DURATION_CACHE

    # Second probe: should hit stat cache directly without calling AudioSpecCache._probe
    with patch.object(AudioSpecCache, "_probe") as mock_probe:
        dur2 = TtsService.get_audio_duration(wav_path)
        assert dur2 == dur1
        mock_probe.assert_not_called()

    # File modification: changes size/mtime, causing cache re-probe
    _make_test_wav(wav_path, duration_sec=6.0)
    dur3 = TtsService.get_audio_duration(wav_path)
    assert dur3 is not None
    assert abs(dur3 - 6.0) < 0.1


def test_get_audio_duration_missing_or_none(tmp_path):
    """Verifies get_audio_duration handles invalid paths safely."""
    assert TtsService.get_audio_duration(None) is None
    assert TtsService.get_audio_duration("") is None
    assert TtsService.get_audio_duration(tmp_path / "missing.wav") is None


@pytest.mark.asyncio
async def test_async_get_audio_duration(tmp_path):
    """Verifies async_get_audio_duration runs asynchronously without blocking the loop."""
    wav_path = _make_test_wav(tmp_path / "async_test.wav", duration_sec=5.0)

    # Probe via module-level function
    dur = await async_get_audio_duration(wav_path)
    assert dur is not None
    assert abs(dur - 5.0) < 0.1

    # Probe via class staticmethod
    dur2 = await TtsService.async_get_audio_duration(wav_path)
    assert dur2 == dur

    # Cached metadata remains off the event loop, and decoding is reused.
    real_to_thread = asyncio.to_thread
    with patch("asyncio.to_thread", wraps=real_to_thread) as mock_thread:
        dur3 = await async_get_audio_duration(wav_path)
        assert dur3 == dur
        mock_thread.assert_awaited_once()

    # Invalid cases
    assert await async_get_audio_duration(None) is None
    assert await async_get_audio_duration("") is None
    assert await async_get_audio_duration(tmp_path / "ghost.wav") is None


def test_clear_tts_profile_cache(tmp_path):
    """Verifies clear_tts_profile_cache clears both duration and stat caches."""
    wav_path = _make_test_wav(tmp_path / "cache_clear.wav", duration_sec=3.5)
    TtsService.get_audio_duration(wav_path)

    assert len(_AUDIO_SPEC_CACHE._cache) > 0
    assert len(_AUDIO_DURATION_CACHE) > 0
    assert len(_AUDIO_STAT_DURATION_CACHE) > 0

    clear_tts_profile_cache()

    assert len(_AUDIO_SPEC_CACHE._cache) == 0
    assert len(_AUDIO_DURATION_CACHE) == 0
    assert len(_AUDIO_STAT_DURATION_CACHE) == 0


@pytest.mark.asyncio
async def test_populate_voice_profile_opts_async_duration(tmp_path):
    """Verifies _populate_voice_profile_opts correctly evaluates audio duration asynchronously."""
    valid_wav = _make_test_wav(tmp_path / "ref_valid.wav", duration_sec=4.0)
    invalid_wav = _make_test_wav(tmp_path / "ref_invalid.wav", duration_sec=1.5)

    mock_client = MagicMock()
    service = TtsService(client=mock_client, audio_dir=tmp_path)

    mock_ctx = MagicMock()
    mock_ctx.ref_audio_path = tmp_path / "fallback_ref.wav"
    mock_ctx.prompt_text = "fallback prompt"
    mock_ctx.prompt_lang = "ja"
    mock_ctx.text_lang = "ja"
    mock_ctx.profile_id = 1
    mock_ctx.get_emotion_ref.return_value = None

    with patch("galgame2voice.services.voice_resolver.get_voice_resolver") as mock_get_res:
        mock_res = MagicMock()
        mock_res.resolve_context = AsyncMock(return_value=mock_ctx)
        mock_get_res.return_value = mock_res

        # 1. Valid reference audio (within 3.0s - 10.0s) -> retained
        opts_valid = {
            "ref_audio_path": str(valid_wav),
            "voice_profile_id": 1,
            "ai_adaptive_voice": True,
            "emotion": "happy",
        }
        res_valid = await service._populate_voice_profile_opts(opts_valid)
        assert res_valid.get("ref_audio_path") == str(valid_wav)

        # 2. Invalid reference audio (< 3.0s) -> fallback triggered
        opts_invalid = {
            "ref_audio_path": str(invalid_wav),
            "voice_profile_id": 1,
            "ai_adaptive_voice": True,
            "emotion": "happy",
        }
        res_invalid = await service._populate_voice_profile_opts(opts_invalid)
        assert res_invalid.get("ref_audio_path") == str(tmp_path / "fallback_ref.wav")
