import asyncio
import logging
import httpx
import pytest

from galgame2voice.services.gpt_sovits_client import GptSovitsClient

MOCK_WAV = (
    b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00"
    + (b"\x00\x7f" * 1024)
)


@pytest.mark.asyncio
async def test_old_client_drains_promptly_when_new_client_has_inflight_requests(caplog):
    """When base URL is changed while requests are in-flight, old HTTP client

    pool should close promptly once its own in-flight requests drain to 0,
    even if the new client continues to have active in-flight requests.
    """
    caplog.set_level(logging.WARNING)

    # 1. Setup old client with an ongoing in-flight request
    old_started = asyncio.Event()
    old_release = asyncio.Event()

    async def old_handler(request: httpx.Request) -> httpx.Response:
        old_started.set()
        await old_release.wait()
        return httpx.Response(200, content=MOCK_WAV, headers={"Content-Type": "audio/wav"})

    old_transport = httpx.MockTransport(old_handler)
    old_client = httpx.AsyncClient(transport=old_transport)

    client = GptSovitsClient(base_url="http://engine-old.test:9880", client=old_client)
    client._model_state_uncertain = False
    client.current_refer_audio = "dummy.wav"

    old_task = asyncio.create_task(
        client.synthesize("旧客户端消息", options={"ref_audio_path": "dummy.wav"})
    )
    await asyncio.wait_for(old_started.wait(), timeout=2.0)
    assert client._inflight_requests == 1

    # 2. Hot-reload to new base URL while old request is still in-flight
    await client.set_base_url("http://engine-new.test:9880")
    assert client._client is None

    # 3. Setup new client with a hanging in-flight request
    new_started = asyncio.Event()
    new_release = asyncio.Event()

    async def new_handler(request: httpx.Request) -> httpx.Response:
        new_started.set()
        await new_release.wait()
        return httpx.Response(200, content=MOCK_WAV, headers={"Content-Type": "audio/wav"})

    new_transport = httpx.MockTransport(new_handler)
    new_client = httpx.AsyncClient(transport=new_transport)
    client._client = new_client
    client._model_state_uncertain = False
    client.current_refer_audio = "dummy.wav"

    # Finish old request so old_client has 0 in-flight requests
    old_release.set()
    await asyncio.wait_for(old_task, timeout=2.0)

    # Immediately start new request on new_client
    new_task = asyncio.create_task(
        client.synthesize("新客户端消息", options={"ref_audio_path": "dummy.wav"})
    )
    await asyncio.wait_for(new_started.wait(), timeout=2.0)
    assert client._inflight_requests == 1

    try:
        # 4. Old client's in-flight requests are 0.
        # It should close within ~0.5s without waiting for new_client or hitting grace period.
        await asyncio.sleep(0.6)
        assert old_client.is_closed, (
            "old_client should be closed promptly because its own in-flight requests "
            "are 0, but was blocked by new client's in-flight request"
        )
        assert not any(
            "Force-closing stale GPT-SoVITS connection pool" in r.message
            for r in caplog.records
        ), "Should not emit force-closing warning for old client"
    finally:
        new_release.set()
        await asyncio.wait_for(new_task, timeout=2.0)
        await client.aclose()
        if not new_client.is_closed:
            await new_client.aclose()


@pytest.mark.asyncio
async def test_old_client_drains_promptly_during_new_client_streaming(caplog):
    """When base URL changes and a stream runs on new client, old client should

    drain and close once its old requests finish, without being blocked by new stream.
    """
    caplog.set_level(logging.WARNING)

    # Old client with an in-flight request
    old_started = asyncio.Event()
    old_release = asyncio.Event()

    async def old_handler(request: httpx.Request) -> httpx.Response:
        old_started.set()
        await old_release.wait()
        return httpx.Response(200, content=MOCK_WAV, headers={"Content-Type": "audio/wav"})

    old_client = httpx.AsyncClient(transport=httpx.MockTransport(old_handler))
    client = GptSovitsClient(base_url="http://engine-old.test:9880", client=old_client)
    client._model_state_uncertain = False
    client.current_refer_audio = "dummy.wav"

    old_task = asyncio.create_task(
        client.synthesize("旧客户端消息", options={"ref_audio_path": "dummy.wav"})
    )
    await asyncio.wait_for(old_started.wait(), timeout=2.0)

    # Swap URL
    await client.set_base_url("http://engine-new.test:9880")

    # New client streaming response
    stream_started = asyncio.Event()
    stream_proceed = asyncio.Event()

    async def stream_generator():
        stream_started.set()
        yield MOCK_WAV[:512]
        await stream_proceed.wait()
        yield MOCK_WAV[512:]

    async def streaming_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=stream_generator(),
            headers={"Content-Type": "audio/wav"},
        )

    new_client = httpx.AsyncClient(transport=httpx.MockTransport(streaming_handler))
    client._client = new_client
    client._model_state_uncertain = False
    client.current_refer_audio = "dummy.wav"

    # Finish old request
    old_release.set()
    await asyncio.wait_for(old_task, timeout=2.0)

    async def consume_stream():
        chunks = []
        async for chunk in client.stream_tts("ストリーミングテスト", options={"ref_audio_path": "dummy.wav"}):
            chunks.append(chunk)
        return chunks

    stream_task = asyncio.create_task(consume_stream())
    await asyncio.wait_for(stream_started.wait(), timeout=2.0)

    # New stream is in flight
    assert client._inflight_requests > 0

    try:
        await asyncio.sleep(0.6)
        assert old_client.is_closed, (
            "old_client should be closed promptly during new stream, but was blocked by new client"
        )
        assert not any(
            "Force-closing stale GPT-SoVITS connection pool" in r.message
            for r in caplog.records
        )
    finally:
        stream_proceed.set()
        await asyncio.wait_for(stream_task, timeout=2.0)
        await client.aclose()
        if not new_client.is_closed:
            await new_client.aclose()
