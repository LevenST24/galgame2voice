"""A 200 reply from the engine that is not WAV audio is an error, not audio to save."""
from __future__ import annotations

import functools
import io
import math
import struct
import wave

import httpx
import pytest

from galgame2voice.services import gpt_sovits_client as mod
from galgame2voice.services.gpt_sovits_client import GptSovitsClient


def _tone_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(i / 8))) for i in range(8000)))
    return buf.getvalue()


@pytest.fixture
def engine_body(monkeypatch):
    body = {"content": b""}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tts":
            return httpx.Response(200, content=body["content"])
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        mod.httpx, "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )
    return body


@pytest.mark.parametrize(
    "content",
    [
        b"<!DOCTYPE html><html><body>Login required</body></html>",
        b'{"message": "tts failed", "exception": "CUDA out of memory"}',
        b"RIFF\x00\x00\x00\x00AVI LIST",
    ],
)
async def test_non_wav_reply_is_rejected(engine_body, content):
    engine_body["content"] = content
    client = GptSovitsClient(base_url="http://engine.invalid:9880")
    try:
        with pytest.raises(RuntimeError, match="不是音频"):
            await client.synthesize("こんにちは。", options={"ref_audio_path": "dummy.wav"})
    finally:
        await client.aclose()


async def test_wav_reply_still_returned(engine_body):
    engine_body["content"] = _tone_wav()
    client = GptSovitsClient(base_url="http://engine.invalid:9880")
    try:
        assert await client.synthesize("こんにちは。", options={"ref_audio_path": "dummy.wav"}) == engine_body["content"]
    finally:
        await client.aclose()


async def _collect_stream(client: GptSovitsClient) -> bytes:
    out = bytearray()
    async for chunk in client.stream_tts("こんにちは。", options={"ref_audio_path": "dummy.wav"}, chunk_size=64):
        out.extend(chunk)
    return bytes(out)


@pytest.mark.parametrize(
    "content",
    [
        # Short page: used to be passed through (and cached) as audio.
        b"<!DOCTYPE html><html><body>Login required</body></html>",
        # Long page: used to fail with the misleading FP16 silent-audio advice.
        b"<html>" + b"x" * 200_000 + b"</html>",
    ],
)
async def test_streamed_non_wav_reply_is_rejected(engine_body, content):
    engine_body["content"] = content
    client = GptSovitsClient(base_url="http://engine.invalid:9880")
    try:
        with pytest.raises(RuntimeError, match="不是音频"):
            await _collect_stream(client)
    finally:
        await client.aclose()


async def test_streamed_wav_reply_still_returned(engine_body):
    engine_body["content"] = _tone_wav()
    client = GptSovitsClient(base_url="http://engine.invalid:9880")
    try:
        assert await _collect_stream(client) == engine_body["content"]
    finally:
        await client.aclose()
