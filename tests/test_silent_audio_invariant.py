"""
Tests for the silent-audio invariant: an all-zero WAV is a FAILED synthesis.

Guards against the MX450/TU117 FP16 NaN-clamping failure mode where the engine
returns HTTP 200 with a well-formed but completely silent WAV, which used to be
cached and played back as silence with no error surfaced anywhere.
"""

import io
import struct

import pytest

from galgame2voice.services.gpt_sovits_client import (
    GptSovitsClient,
    SILENT_AUDIO_ERROR,
    wav_is_silent,
    wav_peak_amplitude,
)


def build_wav(samples: bytes, bits: int = 16, audio_format: int = 1, sample_rate: int = 32000, channels: int = 1) -> bytes:
    """Builds a minimal RIFF/WAVE payload from raw sample bytes."""
    block_align = channels * (bits // 8)
    fmt = struct.pack(
        "<HHIIHH", audio_format, channels, sample_rate,
        sample_rate * block_align, block_align, bits,
    )
    chunk = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunk += b"data" + struct.pack("<I", len(samples)) + samples
    if len(samples) % 2:
        chunk += b"\x00"
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WAVE" + chunk


class SilentTtsStubServer:
    """Mock engine that returns 200 OK with an all-zero (NaN-clamped) WAV."""

    def __init__(self, payload: bytes):
        self.payload = payload

    async def handle_request(self, method, path, json_data=None, params=None):
        import httpx
        if path == "/tts":
            return httpx.Response(status_code=200, content=self.payload)
        return httpx.Response(status_code=200, json={"status": "running"})


def test_wav_is_silent_pcm16_all_zero():
    wav = build_wav(b"\x00\x00" * 1600)
    assert wav_peak_amplitude(wav) == 0.0
    assert wav_is_silent(wav) is True


def test_wav_is_silent_float32_all_zero():
    wav = build_wav(struct.pack("<%sf" % 800, *([0.0] * 800)), bits=32, audio_format=3)
    assert wav_peak_amplitude(wav) == 0.0
    assert wav_is_silent(wav) is True


def test_wav_nonzero_samples_not_silent():
    wav = build_wav(struct.pack("<h", 16384) * 1600)
    assert wav_peak_amplitude(wav) == pytest.approx(0.5, abs=1e-6)
    assert wav_is_silent(wav) is False

    wav_f32 = build_wav(struct.pack("<%sf" % 800, *([0.25] * 800)), bits=32, audio_format=3)
    assert wav_is_silent(wav_f32) is False


def test_wav_unparseable_or_empty_not_flagged_silent():
    # Degenerate mocks (empty data chunk) must NOT be treated as silent failures
    assert wav_peak_amplitude(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00data\x00\x00\x00\x00") is None
    assert wav_peak_amplitude(b"not a wav at all") is None
    assert wav_is_silent(b"") is False


@pytest.mark.asyncio
async def test_synthesize_raises_on_silent_audio():
    client = GptSovitsClient(base_url="http://mock", server=SilentTtsStubServer(build_wav(b"\x00\x00" * 1600)))
    with pytest.raises(RuntimeError, match="all-zero silent audio"):
        await client.synthesize("テスト", options={"ref_audio_path": "", "prompt_text": ""})


@pytest.mark.asyncio
async def test_stream_tts_raises_before_yielding_on_silent_audio():
    client = GptSovitsClient(base_url="http://mock", server=SilentTtsStubServer(build_wav(b"\x00\x00" * 1600)))
    chunks = []
    with pytest.raises(RuntimeError, match="all-zero silent audio"):
        async for chunk in client.stream_tts("テスト", options={"ref_audio_path": "", "prompt_text": ""}):
            chunks.append(chunk)
    assert chunks == []


@pytest.mark.asyncio
async def test_normal_audio_passes_through():
    payload = build_wav(struct.pack("<h", 8000) * 1600)
    client = GptSovitsClient(base_url="http://mock", server=SilentTtsStubServer(payload))
    result = await client.synthesize("テスト", options={"ref_audio_path": "", "prompt_text": ""})
    assert result == payload
