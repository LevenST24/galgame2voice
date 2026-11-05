"""Requests without an explicit timeout must inherit the pooled client's TTS_TIMEOUT."""
from __future__ import annotations

import functools
import io
import math
import struct
import wave

import httpx
import pytest

from galgame2voice.services import gpt_sovits_client as mod
from galgame2voice.services.gpt_sovits_client import (
    SWITCH_TIMEOUT,
    TTS_TIMEOUT,
    GptSovitsClient,
)


def _tone_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(i / 8))) for i in range(8000)
        ))
    return buf.getvalue()


@pytest.fixture
def seen_timeouts(monkeypatch):
    """Real GptSovitsClient + real pooled httpx client; only the transport is faked."""
    seen: list[tuple[str, dict]] = []
    wav = _tone_wav()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.extensions["timeout"])))
        if request.url.path == "/tts":
            return httpx.Response(200, content=wav)
        return httpx.Response(200, json={"ok": True})

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        mod.httpx, "AsyncClient",
        functools.partial(real_async_client, transport=httpx.MockTransport(handler)),
    )
    return seen


async def test_non_streaming_synthesis_uses_tts_timeout(seen_timeouts):
    client = GptSovitsClient(base_url="http://engine.invalid:9880")
    try:
        await client.synthesize("こんにちは。", options={"ref_audio_path": "dummy.wav"})
    finally:
        await client.aclose()

    assert seen_timeouts == [("/tts", {
        "connect": TTS_TIMEOUT.connect, "read": TTS_TIMEOUT.read,
        "write": TTS_TIMEOUT.write, "pool": TTS_TIMEOUT.pool,
    })]
    assert seen_timeouts[0][1]["read"] == 300.0


async def test_control_command_uses_client_default_timeout(seen_timeouts):
    client = GptSovitsClient(base_url="http://engine.invalid:9880")
    try:
        await client.control("restart")
    finally:
        await client.aclose()

    assert seen_timeouts[0][1]["read"] == TTS_TIMEOUT.read


async def test_explicit_timeout_is_still_passed_through(seen_timeouts):
    client = GptSovitsClient(base_url="http://engine.invalid:9880")
    try:
        await client._request("GET", "/set_gpt_weights", timeout=SWITCH_TIMEOUT)
    finally:
        await client.aclose()

    assert seen_timeouts == [("/set_gpt_weights", {
        "connect": SWITCH_TIMEOUT.connect, "read": SWITCH_TIMEOUT.read,
        "write": SWITCH_TIMEOUT.write, "pool": SWITCH_TIMEOUT.pool,
    })]
