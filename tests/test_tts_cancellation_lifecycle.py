"""Cancellation must release TTS capacity even with a stalled upstream or full buffer."""

import asyncio
import gc
from unittest.mock import AsyncMock, MagicMock

import pytest

from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.tts_scheduler import (
    SingleFlightCoordinator,
    TtsScheduler,
    _safe_put_chunk,
)


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_cancel_full_buffer_put_leaves_no_helper_tasks():
    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait(b"full")
    stop = asyncio.Event()
    original_tasks = asyncio.all_tasks()
    producer = asyncio.create_task(_safe_put_chunk(queue, b"next", stop))
    await _settle()
    producer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await producer
    await _settle()
    leaked = asyncio.all_tasks() - original_tasks
    try:
        assert not leaked, "cancelled enqueue retained put()/wait() helper tasks"
    finally:
        stop.set()
        for task in leaked:
            task.cancel()
        await asyncio.gather(*leaked, return_exceptions=True)


async def test_cancel_stalled_stream_releases_worker():
    scheduler = TtsScheduler()
    closed = asyncio.Event()

    async def stalled_stream():
        try:
            yield b"first"
            await asyncio.Event().wait()
        finally:
            closed.set()

    stream = scheduler.schedule_stream(stalled_stream, generation_id="old")
    try:
        assert await anext(stream) == b"first"
        assert scheduler.cancel_generation("old") == 1
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=1)
        assert await asyncio.wait_for(
            scheduler.schedule(lambda: asyncio.sleep(0, result="new")), timeout=1
        ) == "new"
        assert closed.is_set()
        assert scheduler.active_streams == 0
    finally:
        await stream.aclose()
        await scheduler.aclose()


async def test_cancel_active_caller_releases_worker():
    scheduler = TtsScheduler()
    started = asyncio.Event()
    closed = asyncio.Event()

    async def stalled_job():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    caller = asyncio.create_task(scheduler.schedule(stalled_job))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert await asyncio.wait_for(
            scheduler.schedule(lambda: asyncio.sleep(0, result="next")), timeout=1
        ) == "next"
        assert closed.is_set()
    finally:
        await scheduler.aclose()


async def test_shutdown_wakes_all_waiters_and_allows_restart():
    scheduler = TtsScheduler()
    started = asyncio.Event()

    async def blocker():
        started.set()
        await asyncio.Event().wait()

    async def unused_stream():
        yield b"must not execute"

    active = asyncio.create_task(scheduler.schedule(blocker))
    await asyncio.wait_for(started.wait(), timeout=1)
    queued = asyncio.create_task(scheduler.schedule(blocker, generation_id="queued"))
    stream = scheduler.schedule_stream(unused_stream, generation_id="queued")
    reader = asyncio.create_task(anext(stream))
    await _settle()
    try:
        assert scheduler.get_queue_depth() == 2
        await asyncio.wait_for(scheduler.aclose(), timeout=1)
        await _settle()
        assert active.cancelled()
        assert queued.cancelled(), "shutdown left a queued synthesis waiting forever"
        assert reader.done(), "shutdown left a queued stream waiting forever"
        with pytest.raises(StopAsyncIteration):
            await reader
        assert scheduler.get_queue_depth() == 0
        assert not scheduler._tasks_by_id
        assert not scheduler._tasks_by_gen
        assert await scheduler.schedule(lambda: asyncio.sleep(0, result="restarted")) == "restarted"
    finally:
        for task in (active, queued, reader):
            task.cancel()
        await asyncio.gather(active, queued, reader, return_exceptions=True)
        await stream.aclose()
        await scheduler.aclose()


async def test_cancel_upstream_producer_with_full_buffer():
    client = GptSovitsClient(server="http://127.0.0.1:9880")
    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait(b"full")
    reading = asyncio.Event()
    closed = asyncio.Event()

    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.set()

        async def aiter_bytes(self, **kwargs):
            reading.set()
            yield b"next"

    http_client = MagicMock()
    http_client.stream.return_value = Response()
    producer = asyncio.create_task(client._produce_stream_tts_chunks(
        client=http_client, url="http://127.0.0.1:9880/tts", payload={},
        queue=queue, sentinel=object(), profiler=None, chunk_size=4096,
    ))
    try:
        await asyncio.wait_for(reading.wait(), timeout=1)
        producer.cancel()
        done, _ = await asyncio.wait([producer], timeout=1)
        assert producer in done, "cancelled producer blocked writing an error or EOF to a full queue"
        assert producer.cancelled()
        assert closed.is_set()
        assert not client.lock.locked()
        assert client._inflight_requests == 0
    finally:
        # Free the queue on a failing implementation so teardown itself cannot hang.
        while not producer.done():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            await asyncio.sleep(0)
        await asyncio.gather(producer, return_exceptions=True)


@pytest.mark.parametrize("finish", ["complete", "cancel", "close"])
async def test_service_caches_only_complete_audio_and_closes_upstream(finish, monkeypatch, tmp_path):
    scheduler = TtsScheduler()
    monkeypatch.setattr("galgame2voice.services.tts_service.get_tts_scheduler", lambda: scheduler)
    closed = asyncio.Event()

    async def upstream(*args, **kwargs):
        try:
            yield b"first"
            if finish == "complete":
                yield b"second"
            else:
                await asyncio.Event().wait()
        finally:
            closed.set()

    client = MagicMock()
    client.stream_tts = upstream
    cache = MagicMock()
    cache.compute_cache_key.return_value = ("key", "テスト", "params")
    cache.get = AsyncMock(return_value=None)
    async def cache_miss(*args, **kwargs):
        if False:
            yield b""
    cache.stream_cached = cache_miss
    cache.put = AsyncMock()
    service = TtsService(client=client, cache_manager=cache, audio_dir=tmp_path)
    stream = service.stream_tts("テスト", options={"_pre_resolved": True, "_generation_id": "turn"})
    try:
        assert await anext(stream) == b"first"
        if finish == "complete":
            assert [chunk async for chunk in stream] == [b"second"]
            cache.put.assert_awaited_once()
            assert cache.put.call_args.kwargs["audio_bytes"] == b"firstsecond"
        else:
            if finish == "cancel":
                scheduler.cancel_generation("turn")
                with pytest.raises(StopAsyncIteration):
                    await asyncio.wait_for(anext(stream), timeout=1)
            else:
                await stream.aclose()
            cache.put.assert_not_awaited()
        await _settle()
        assert closed.is_set(), "service left its upstream generator alive after closing"
    finally:
        await stream.aclose()
        await scheduler.aclose()


@pytest.mark.parametrize("kind", ["single_flight", "stream"])
async def test_propagated_failure_does_not_emit_unretrieved_future(kind):
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    reports = []
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    scheduler = TtsScheduler()

    async def fail():
        raise ValueError("synthesis failed")

    async def fail_stream():
        await fail()
        yield b"unreachable"

    try:
        with pytest.raises(ValueError, match="synthesis failed"):
            if kind == "single_flight":
                await SingleFlightCoordinator().execute("key", fail)
            else:
                async for _ in scheduler.schedule_stream(fail_stream):
                    pass
        await scheduler.aclose()
        await _settle()
        gc.collect()
        await _settle()
        assert not reports, f"already propagated failure leaked into the event loop: {reports}"
    finally:
        await scheduler.aclose()
        loop.set_exception_handler(original_handler)
