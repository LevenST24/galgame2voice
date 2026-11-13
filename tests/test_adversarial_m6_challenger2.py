"""Adversarial audio-converter cases that still reach ffmpeg.

Short and known-bad payloads are covered by the hardening and remediation
suites. These tests keep the sizes and headers that pass the early rejects
and must fail inside the transcoder.
"""

import os

import pytest

from galgame2voice.utils.audio_converter import convert_ogg_to_wav


class TestAudioConverterAdversarial:
    """Random and corrupt inputs that are long enough to reach ffmpeg."""

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload_size", [100, 1024, 65536])
    async def test_convert_ogg_to_wav_random_noise_fuzzing(self, payload_size):
        """Random buffers longer than 44 bytes must fail as ValueError, not crash."""
        random_bytes = os.urandom(payload_size)

        with pytest.raises(ValueError) as exc_info:
            await convert_ogg_to_wav(random_bytes)
        assert len(str(exc_info.value)) > 0

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_convert_ogg_to_wav_truncated_magic_headers(self):
        """An OggS prefix plus a long corrupt tail must reach ffmpeg and fail."""
        corrupted_oggs = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00" + os.urandom(100)
        with pytest.raises(ValueError):
            await convert_ogg_to_wav(corrupted_oggs)
