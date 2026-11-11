import io
import struct
import wave
import httpx
import pytest

from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.utils.audio_spec import SILENT_AUDIO_ERROR


def make_wav_bytes(leading_silence_s: float, voice_s: float, sample_rate: int = 32000) -> bytes:
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        silence_samples = int(sample_rate * leading_silence_s)
        if silence_samples > 0:
            wf.writeframes(b"\x00\x00" * silence_samples)
        voice_samples = int(sample_rate * voice_s)
        if voice_samples > 0:
            wf.writeframes(struct.pack(f"<{voice_samples}h", *([10000] * voice_samples)))
    return bio.getvalue()


class AsyncChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def create_streaming_client(wav_data: bytes, chunk_size: int = 4096) -> GptSovitsClient:
    chunks = [wav_data[i:i + chunk_size] for i in range(0, len(wav_data), chunk_size)]

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "audio/wav"},
            stream=AsyncChunkStream(chunks),
        )

    transport = httpx.MockTransport(handle)
    http_client = httpx.AsyncClient(transport=transport)
    client = GptSovitsClient(base_url="http://mock-engine:9880", client=http_client)
    return client


@pytest.mark.asyncio
async def test_stream_tts_natural_leading_silence_passes_through():
    """
    Verifies that a stream with 0.6s of natural leading silence followed by 1.0s of normal
    speech is fully streamed to the caller without being misidentified as silent audio.
    """
    wav_data = make_wav_bytes(leading_silence_s=0.6, voice_s=1.0, sample_rate=32000)
    client = create_streaming_client(wav_data, chunk_size=4096)

    received_chunks = []
    async for chunk in client.stream_tts(
        "こんにちは",
        options={"_pre_resolved": True, "ref_audio_path": "dummy.wav", "prompt_text": "dummy"},
        chunk_size=4096,
    ):
        received_chunks.append(chunk)

    assembled = b"".join(received_chunks)
    assert len(received_chunks) > 0
    assert assembled == wav_data


@pytest.mark.asyncio
async def test_stream_tts_pure_silence_raises_silent_audio_error():
    """
    Verifies that a completely all-zero silent audio stream (e.g. GPU FP16 NaN clamping)
    is correctly detected and raises SILENT_AUDIO_ERROR before yielding chunks.
    """
    wav_data = make_wav_bytes(leading_silence_s=1.0, voice_s=0.0, sample_rate=32000)
    client = create_streaming_client(wav_data, chunk_size=4096)

    received_chunks = []
    with pytest.raises(RuntimeError) as exc_info:
        async for chunk in client.stream_tts(
            "こんにちは",
            options={"_pre_resolved": True, "ref_audio_path": "dummy.wav", "prompt_text": "dummy"},
            chunk_size=4096,
        ):
            received_chunks.append(chunk)

    assert str(exc_info.value) == SILENT_AUDIO_ERROR
    assert len(received_chunks) == 0
