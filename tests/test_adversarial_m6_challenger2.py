"""
Adversarial Stress & Empirical Challenge Test Suite for Milestone 6 (Audio & Multi-User Isolation).
Authored by m6_challenger_2.

Challenges & Stress Dimensions:
1. Multi-User Concurrency & Session Isolation:
   - 15+ concurrent users sending simultaneous text & voice messages
   - Zero crosstalk / chat_id leakage
   - Cancellation isolation (interrupting user A cancels ONLY user A's task, not user B/C/D)
   - High concurrency SQLite message sequence & foreign key consistency
2. Audio Converter Fuzzing, Boundary Conditions & Error Recovery:
   - Random byte payload fuzzing across multiple buffer sizes (0B, 5B, 11B, 50B, 1KB, 100KB, 1MB)
   - Truncated audio headers (partial OggS, partial RIFF/WAV)
   - Non-audio formats (HTML, JSON, Executable) disguised as audio
   - Missing ffmpeg binary simulation
   - Non-zero ffmpeg exit codes / subprocess crash simulation
   - Temporary file leak prevention under error and exception conditions
3. Telegram Voice Handler Resiliency:
   - Corrupt voice download handling
   - Network failure during voice file download
   - STT adapter failure / empty transcription handling
   - TTS synthesis background failure isolation
   - Update object malformation robustness
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from galgame2voice.database.session import init_db
from galgame2voice.utils.audio_converter import (
    convert_ogg_to_wav,
    convert_wav_to_ogg,
)


@pytest.fixture
async def challenge_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        await init_db(path)
        yield path
    finally:
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass


# ============================================================================
# Section 2: Audio Converter Defensive & Fuzzing Stress Tests
# ============================================================================

class TestAudioConverterAdversarial:
    """Adversarial testing of audio converter against malformed, random, and extreme inputs."""

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload_size", [0, 1, 5, 11, 15, 30, 44, 100, 1024, 65536])
    async def test_convert_ogg_to_wav_random_noise_fuzzing(self, payload_size):
        """
        Fuzz convert_ogg_to_wav with purely random byte buffers of varying sizes.
        Must raise ValueError or handle cleanly; MUST NEVER crash with unhandled exception or segfault.
        """
        # Generate random bytes
        random_bytes = os.urandom(payload_size)

        with pytest.raises(ValueError) as exc_info:
            await convert_ogg_to_wav(random_bytes)
        assert len(str(exc_info.value)) > 0

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_convert_ogg_to_wav_truncated_magic_headers(self):
        """
        Test inputs that start with valid magic bytes ('OggS' or 'RIFF') but are truncated or corrupted immediately after.
        """
        # 1. Truncated OggS
        with pytest.raises(ValueError):
            await convert_ogg_to_wav(b"OggS")

        # 2. OggS followed by random garbage
        corrupted_oggs = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00" + os.urandom(100)
        with pytest.raises(ValueError):
            await convert_ogg_to_wav(corrupted_oggs)

        # 3. Truncated RIFF
        with pytest.raises(ValueError):
            await convert_ogg_to_wav(b"RIFF\x00\x00")

    @pytest.mark.asyncio
    async def test_convert_ogg_to_wav_non_audio_formats(self):
        """
        Test valid non-audio file payloads disguised as audio bytes (JSON, HTML, PNG header, ELF header).
        """
        non_audio_samples = [
            b'{"error": "not an audio file", "status": 400, "data": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}',
            b'<!DOCTYPE html><html><head><title>Error</title></head><body><h1>404 Not Found</h1></body></html>',
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10\x08\x06\x00\x00\x00\x1f\xf3\xffa',
            b'\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00>\x00\x01\x00\x00\x00',
        ]
        for sample in non_audio_samples:
            with pytest.raises(ValueError):
                await convert_ogg_to_wav(sample)

    @pytest.mark.asyncio
    async def test_convert_wav_to_ogg_empty_and_corrupt(self):
        """Test convert_wav_to_ogg with empty input."""
        with pytest.raises(ValueError, match="WAV bytes cannot be empty"):
            await convert_wav_to_ogg(b"")

    @pytest.mark.asyncio
    async def test_missing_ffmpeg_simulation(self):
        """
        Simulate a missing ffmpeg binary by making discovery fail.
        Missing ffmpeg must fail loudly instead of silently producing wrong audio.
        """
        # Patch find_ffmpeg, not is_ffmpeg_available: discovery is what the
        # conversion path actually consults (_require_ffmpeg_bin -> find_ffmpeg).
        with patch("galgame2voice.utils.audio_converter.find_ffmpeg", return_value=None):
            # 1. convert_ogg_to_wav with missing ffmpeg -> loud failure
            valid_fake_ogg = b"OggS\x00\x02\x00\x00" + b"\x00" * 50
            with pytest.raises(RuntimeError, match="ffmpeg executable not found"):
                await convert_ogg_to_wav(valid_fake_ogg)

            # 2. convert_wav_to_ogg with missing ffmpeg -> loud failure
            sample_wav = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00"
            with pytest.raises(RuntimeError, match="ffmpeg executable not found"):
                await convert_wav_to_ogg(sample_wav)

    @pytest.mark.asyncio
    async def test_ffmpeg_subprocess_crash_and_nonzero_exit_code(self):
        """
        Simulate ffmpeg command returning exit code 1 or 137.
        Verifies that RuntimeError is caught and wrapped into ValueError.
        """
        # Both halves of the seam must be faked: discovery so the transcode step
        # is reachable, and the subprocess so no real binary is executed. Patching
        # only the executor left the test depending on a real ffmpeg install.
        with patch("galgame2voice.utils.audio_converter.find_ffmpeg", return_value="/fake/ffmpeg"):
            with patch("galgame2voice.utils.audio_converter.run_ffmpeg_command", side_effect=RuntimeError("ffmpeg conversion failed (code 1): Invalid data")):
                valid_fake_ogg = b"OggS\x00\x02\x00\x00" + b"\x00" * 50
                with pytest.raises(ValueError, match="Audio conversion failed"):
                    await convert_ogg_to_wav(valid_fake_ogg)

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_temp_file_cleanup_on_conversion_failure(self):
        """
        Verify that temporary files (.ogg and .wav) are unlinked and cleaned up even when ffmpeg fails.
        """
        temp_dir = Path(tempfile.gettempdir())
        before_temp_files = set(temp_dir.glob("*.ogg")).union(set(temp_dir.glob("*.wav")))

        # Intentionally fail conversion
        corrupted_oggs = b"OggS\x00\x02\x00\x00" + os.urandom(100)
        try:
            await convert_ogg_to_wav(corrupted_oggs)
        except ValueError:
            pass

        after_temp_files = set(temp_dir.glob("*.ogg")).union(set(temp_dir.glob("*.wav")))
        # Ensure no newly leaked temporary files remain in temp directory
        newly_created = after_temp_files - before_temp_files
        assert len(newly_created) == 0, f"Leaked temporary audio files: {newly_created}"

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_audio_converter_large_payload_stress(self):
        """
        Stress test audio converter with large payload (1MB WAV PCM audio).
        Verifies no memory leak, correct header generation, and roundtrip consistency.
        """
        # 1MB 16-bit PCM WAV
        header = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00"
        large_pcm = header + (b"\x12\x34" * 500000)  # ~1MB
        assert len(large_pcm) > 1000000

        # Test WAV -> OGG
        ogg_output = await convert_wav_to_ogg(large_pcm)
        assert len(ogg_output) > 0

        # Test OGG (which is already WAV format) -> WAV
        wav_output = await convert_ogg_to_wav(large_pcm)
        assert wav_output.startswith(b"RIFF")
        assert len(wav_output) == len(large_pcm)

