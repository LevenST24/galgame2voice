"""
Unit & Integration tests for:
1. Path Traversal & File Boundary Guard (galgame2voice.utils.path_guard)
2. Persistent FFmpeg Discovery, Caching & Format Short-Circuiting (galgame2voice.utils.audio_converter)
3. In-Memory Disk Cache Metadata Tracking in TtsCacheManager (galgame2voice.services.tts_cache_manager)
"""

import io
import os
import struct
import tempfile
import wave
from pathlib import Path

import pytest

from galgame2voice.config import get_settings
from galgame2voice.utils.path_guard import (
    PathTraversalError,
    contains_traversal_payload,
    is_safe_filename,
    is_windows_device_name,
    safe_resolve_audio_path,
    validate_path_containment,
    validate_voice_profile_paths,
)
from galgame2voice.utils.audio_converter import (
    convert_ogg_to_wav,
    find_ffmpeg,
    is_ffmpeg_available,
    is_target_wav_pcm,
    reset_ffmpeg_cache,
)
from galgame2voice.services.tts_cache_manager import TtsCacheManager


def _create_wav(sample_rate: int = 16000, channels: int = 1, sampwidth: int = 2, duration_s: float = 0.1) -> bytes:
    """Helper creating raw WAV PCM bytes."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        num_frames = int(sample_rate * duration_s)
        wf.writeframes(struct.pack(f"<{num_frames * channels}h", *([0] * (num_frames * channels))))
    return buf.getvalue()


# ============================================================================
# 1. Path Guard Tests
# ============================================================================

def test_windows_device_names():
    assert is_windows_device_name("CON")
    assert is_windows_device_name("prn.txt")
    assert is_windows_device_name("aux.wav")
    assert is_windows_device_name("NUL")
    assert is_windows_device_name("COM1")
    assert is_windows_device_name("lpt9.bin")
    assert not is_windows_device_name("console.wav")
    assert not is_windows_device_name("gentle.ogg")


def test_contains_traversal_payload():
    assert contains_traversal_payload("../etc/passwd")
    assert contains_traversal_payload("..\\windows\\system32")
    assert contains_traversal_payload("audio/../../secret.txt")
    assert contains_traversal_payload("%2e%2e/passwd")
    assert contains_traversal_payload("audio\x00/file.wav")
    assert contains_traversal_payload(r"\\remote-server\share\audio.wav")
    assert contains_traversal_payload("//remote-server/share/audio.wav")
    assert not contains_traversal_payload("audio/references/natsume/gentle.ogg")
    assert not contains_traversal_payload("nat002_032.ogg")


def test_is_safe_filename():
    assert is_safe_filename("audio_123.wav")
    assert is_safe_filename("gentle.ogg")
    assert not is_safe_filename("../audio.wav")
    assert not is_safe_filename("sub/audio.wav")
    assert not is_safe_filename("aux.wav")
    assert not is_safe_filename("")


def test_validate_path_containment_valid():
    settings = get_settings()
    ref_audio = settings.project_root / "audio" / "references" / "natsume" / "gentle.ogg"
    if ref_audio.is_file():
        resolved = validate_path_containment("audio/references/natsume/gentle.ogg")
        assert resolved == ref_audio.resolve()


def test_validate_path_containment_rejection():
    with pytest.raises(PathTraversalError):
        validate_path_containment("../../windows/system32/cmd.exe")

    with pytest.raises(PathTraversalError):
        validate_path_containment(r"\\192.168.1.100\share\evil.wav")

    with pytest.raises(PathTraversalError):
        validate_path_containment("CON")


def test_validate_voice_profile_paths_rejection():
    with pytest.raises(PathTraversalError):
        validate_voice_profile_paths(
            gpt_weights_path="../../secret.ckpt",
            sovits_weights_path="models/model.pth",
        )


# ============================================================================
# 2. Audio Converter & FFmpeg Discovery Tests
# ============================================================================

def test_find_ffmpeg_caching():
    reset_ffmpeg_cache()
    found1 = find_ffmpeg()
    found2 = find_ffmpeg()
    assert found1 == found2
    if found1:
        assert Path(found1).is_file() or Path(f"{found1}.exe").is_file()


def test_is_target_wav_pcm():
    wav_16k = _create_wav(sample_rate=16000, channels=1, sampwidth=2)
    wav_24k = _create_wav(sample_rate=24000, channels=1, sampwidth=2)
    wav_stereo = _create_wav(sample_rate=16000, channels=2, sampwidth=2)

    assert is_target_wav_pcm(wav_16k, sample_rate=16000, channels=1)
    assert not is_target_wav_pcm(wav_24k, sample_rate=16000, channels=1)
    assert not is_target_wav_pcm(wav_stereo, sample_rate=16000, channels=1)
    assert not is_target_wav_pcm(b"OggS\x00\x02dummy_ogg_data")
    assert not is_target_wav_pcm(b"")


@pytest.mark.asyncio
async def test_convert_ogg_to_wav_short_circuit():
    """Verifies that an audio byte stream already in target format bypasses ffmpeg subprocess."""
    wav_16k = _create_wav(sample_rate=16000, channels=1, sampwidth=2)
    # Even if an invalid ffmpeg_path is provided, short-circuiting must return wav_16k without error
    result = await convert_ogg_to_wav(wav_16k, sample_rate=16000, channels=1, ffmpeg_path="non_existent_ffmpeg_bin")
    assert result == wav_16k


# ============================================================================
# 3. TTS Cache In-Memory Tracking Tests
# ============================================================================

@pytest.mark.asyncio
async def test_tts_cache_manager_in_memory_tracking(tmp_path):
    cache_dir = tmp_path / "cache"
    db_path = tmp_path / "test.db"
    
    # Initialize DB
    from galgame2voice.database.session import init_db
    await init_db(db_path)

    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_cache_mb=10, max_entries=50)

    # Initial stats uninitialized
    assert mgr._disk_bytes_total is None
    assert mgr._stats_initialized is False

    # Initialize stats via get_stats
    await mgr.get_stats()
    assert mgr._disk_bytes_total == 0
    assert mgr._disk_files_total == 0
    assert mgr._stats_initialized is True

    # Store first item
    data1 = b"RIFF" + b"\x00" * 40
    url1, path1, size1 = await mgr.put(
        cache_key="key1",
        text="text1",
        clean_text="text1",
        voice_profile_id=1,
        params_hash="hash1",
        audio_bytes=data1,
    )
    assert mgr._disk_bytes_total == len(data1)
    assert mgr._disk_files_total == 1

    # Store second item
    data2 = b"RIFF" + b"\x00" * 100
    url2, path2, size2 = await mgr.put(
        cache_key="key2",
        text="text2",
        clean_text="text2",
        voice_profile_id=1,
        params_hash="hash2",
        audio_bytes=data2,
    )
    assert mgr._disk_bytes_total == len(data1) + len(data2)
    assert mgr._disk_files_total == 2

    # Clear cache
    await mgr.clear()
    assert mgr._disk_bytes_total == 0
    assert mgr._disk_files_total == 0
    assert mgr._stats_initialized is True
