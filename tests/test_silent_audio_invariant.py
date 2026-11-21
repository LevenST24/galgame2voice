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
        await client.synthesize("テスト", options={"ref_audio_path": "mock.wav", "prompt_text": "mock"})


@pytest.mark.asyncio
async def test_stream_tts_raises_before_yielding_on_silent_audio():
    client = GptSovitsClient(base_url="http://mock", server=SilentTtsStubServer(build_wav(b"\x00\x00" * 1600)))
    chunks = []
    with pytest.raises(RuntimeError, match="all-zero silent audio"):
        async for chunk in client.stream_tts("テスト", options={"ref_audio_path": "mock.wav", "prompt_text": "mock"}):
            chunks.append(chunk)
    assert chunks == []


@pytest.mark.asyncio
async def test_normal_audio_passes_through():
    payload = build_wav(struct.pack("<h", 8000) * 1600)
    client = GptSovitsClient(base_url="http://mock", server=SilentTtsStubServer(payload))
    result = await client.synthesize("テスト", options={"ref_audio_path": "mock.wav", "prompt_text": "mock"})
    assert result == payload


@pytest.mark.asyncio
async def test_tts_pipeline_surfaces_chinese_fp32_guidance_for_managed_and_external_engine(monkeypatch):
    """When synthesis returns an all-zero WAV, the UI-facing error is Chinese FP32 guidance, not English."""
    from types import SimpleNamespace

    from galgame2voice.services.chat_pipelines.tts_pipeline import TtsStreamPipeline
    from galgame2voice.services import engine_supervisor
    from galgame2voice.utils.audio_spec import (
        SILENT_AUDIO_USER_GUIDANCE_EXTERNAL,
        SILENT_AUDIO_USER_GUIDANCE_MANAGED,
    )

    silent_wav = build_wav(b"\x00\x00" * 1600)
    client = GptSovitsClient(base_url="http://127.0.0.1:9880", server=SilentTtsStubServer(silent_wav))

    class _PipelineTtsService:
        def __init__(self, inner_client):
            self.client = inner_client

        async def synthesize_to_file(self, text, options=None, **kwargs):
            await self.client.synthesize(text, options=options)
            raise AssertionError("Silent WAV must raise before writing an audio file")

    pipe = TtsStreamPipeline(_PipelineTtsService(client), "gen-silent")
    opts = {"ref_audio_path": "mock.wav", "prompt_text": "mock"}

    # Case 1: app-managed live engine -> instruct switching in 语音服务 -> 计算方式 -> FP32 单精度 · 兼容模式
    live_launch = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(
        engine_supervisor,
        "get_engine_supervisor",
        lambda request=None: SimpleNamespace(get_managed_launch=lambda ep: live_launch),
    )
    chunk_managed, err_managed = await pipe.synthesize_chunk("テスト", 0, dict(opts))
    assert chunk_managed is None
    assert err_managed == SILENT_AUDIO_USER_GUIDANCE_MANAGED
    assert "语音服务" in err_managed and "计算方式" in err_managed and "FP32 单精度" in err_managed and "运行状态" in err_managed

    # Case 2: user-started external engine -> instruct restarting with FP32 (is_half=False)
    monkeypatch.setattr(
        engine_supervisor,
        "get_engine_supervisor",
        lambda request=None: SimpleNamespace(get_managed_launch=lambda ep: None),
    )
    chunk_ext, err_ext = await pipe.synthesize_chunk("テスト", 1, dict(opts))
    assert chunk_ext is None
    assert err_ext == SILENT_AUDIO_USER_GUIDANCE_EXTERNAL
    assert "FP32" in err_ext and "is_half=False" in err_ext
