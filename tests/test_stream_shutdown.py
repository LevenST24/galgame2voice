"""Cancelled/failed turns persist once and finish owned asynchronous cleanup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest

from galgame2voice.database.session import get_db, init_db
from galgame2voice.routers.chat import sse_event_formatter
from galgame2voice.routers import chat as chat_router
from starlette.requests import ClientDisconnect
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.chat_pipelines import stream_coordinator
from galgame2voice.services.chat_pipelines.stream_coordinator import StreamCoordinator
from galgame2voice.services.chat_pipelines.llm_pipeline import LlmStreamPipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "error"])
async def test_partial_reply_is_persisted_exactly_once(tmp_path, ending):
    database = tmp_path / "chat.db"
    await init_db(database)
    release = asyncio.Event()

    async def tokens(*args, **kwargs):
        yield '{"chinese": "已经收到的内容'
        await release.wait()
        raise RuntimeError("synthetic provider disconnect")

    service = ChatService(db_path=database)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(stream_chat=tokens, api_key="synthetic"), "test", "custom",
    ))
    service._extract_memory_safe = AsyncMock()
    cancel = asyncio.Event()
    stream = service.stream_chat("问题", session_id="partial", cancel_event=cancel)
    try:
        first = await asyncio.wait_for(stream.__anext__(), 2)
        assert first["event"] == "text"
        if ending == "cancel":
            cancel.set()
        else:
            release.set()
        await asyncio.wait_for(_consume(stream), 2)
        await service.aclose()
        async with get_db(database) as conn:
            rows = await (await conn.execute(
                "SELECT role, content_chinese FROM messages WHERE session_id='partial' ORDER BY id"
            )).fetchall()
        assert [(row[0], row[1]) for row in rows] == [
            ("user", "问题"), ("assistant", "已经收到的内容"),
        ]
    finally:
        release.set()
        await stream.aclose()
        await service.aclose()


async def _consume(stream):
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_stop_waits_for_upstream_finally_without_cancelling_it_twice(tmp_path):
    database = tmp_path / "chat.db"
    await init_db(database)
    cleanup_started, release_cleanup, cleanup_done, cancelled_twice = (
        asyncio.Event() for _ in range(4)
    )

    async def tokens(*args, **kwargs):
        try:
            yield '{"chinese": "部分回复'
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                cancelled_twice.set()
                raise
            cleanup_done.set()

    service = ChatService(db_path=database)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(stream_chat=tokens, api_key="synthetic"), "test", "custom",
    ))
    service._extract_memory_safe = AsyncMock()
    cancel = asyncio.Event()
    stream = service.stream_chat("问题", session_id="cleanup", cancel_event=cancel)
    consumer = None
    try:
        assert (await asyncio.wait_for(stream.__anext__(), 2))["event"] == "text"
        cancel.set()
        consumer = asyncio.create_task(_consume(stream))
        await asyncio.wait_for(cleanup_started.wait(), 2)
        # A loop barrier allows the coordinator to reach teardown while cleanup is gated.
        for _ in range(5):
            await asyncio.sleep(0)
        assert not consumer.done(), "stream returned while its upstream cleanup was unfinished"
        assert not cancelled_twice.is_set(), "a second cancel interrupted the upstream finally"
        release_cleanup.set()
        await asyncio.wait_for(consumer, 2)
        assert cleanup_done.is_set()
    finally:
        release_cleanup.set()
        if consumer is not None:
            await asyncio.gather(consumer, return_exceptions=True)
        await stream.aclose()
        await service.aclose()


@pytest.mark.asyncio
async def test_sse_close_closes_source_and_disconnect_watcher():
    source_closed, watcher_closed = asyncio.Event(), asyncio.Event()

    async def source():
        try:
            yield {"event": "text", "data": {"delta_chinese": "你好"}}
            await asyncio.Event().wait()
        finally:
            source_closed.set()

    async def watcher():
        try:
            await asyncio.Event().wait()
        finally:
            watcher_closed.set()

    iterator = source()
    watch_task = asyncio.create_task(watcher())
    await asyncio.sleep(0)
    cancel = asyncio.Event()
    formatted = sse_event_formatter(iterator, cancel, watch_task)
    try:
        await formatted.__anext__()
        await formatted.aclose()
        assert cancel.is_set()
        assert source_closed.is_set()
        assert watcher_closed.is_set()
    finally:
        await iterator.aclose()
        watch_task.cancel()
        await asyncio.gather(watch_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("watcher_stops_first", [False, True])
async def test_teardown_reaps_cancelled_tasks_and_preserves_their_finally(monkeypatch, watcher_stops_first):
    started, cleanup_started, release, cleanup_done = (asyncio.Event() for _ in range(4))
    lock = asyncio.Lock()

    async def producer():
        async with lock:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release.wait()
                cleanup_done.set()

    task = asyncio.create_task(producer())
    await started.wait()
    cancel = asyncio.Event()
    cancel.set()
    run = SimpleNamespace(
        stream_gen_id="test", producer_task=task, worker_task=None,
        cancel_monitor=None, cancel_event=cancel, cancel_requested=set(),
        tts_queue=asyncio.Queue(), event_queue=asyncio.Queue(),
        profiler=SimpleNamespace(print_waterfall=lambda: None),
    )
    coordinator = StreamCoordinator.__new__(StreamCoordinator)
    coordinator.persisted_assistant, coordinator.user_msg = True, None
    monkeypatch.setattr(stream_coordinator, "get_tts_scheduler", lambda: SimpleNamespace(cancel_generation=lambda _: None))
    teardown = None
    try:
        if watcher_stops_first:
            await coordinator._watch_cancel(run)
            await cleanup_started.wait()
        teardown = asyncio.create_task(coordinator._teardown(run))
        await asyncio.wait_for(cleanup_started.wait(), 1)
        for _ in range(5):
            await asyncio.sleep(0)
        assert not teardown.done(), "teardown must wait until inference cleanup releases its lock"
        assert lock.locked()
        release.set()
        await asyncio.wait_for(teardown, 1)
        assert task.done()
        assert cleanup_done.is_set(), "a repeated cancellation must not interrupt the finally block"
        assert not lock.locked()
    finally:
        release.set()
        if teardown is not None:
            await asyncio.gather(teardown, return_exceptions=True)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_event_pump_leaves_no_queue_or_event_waiters():
    entered = asyncio.Event()

    class ObservedQueue(asyncio.Queue):
        async def get(self):
            entered.set()
            return await super().get()

    existing = asyncio.all_tasks()
    run = SimpleNamespace(event_queue=ObservedQueue(), cancel_event=asyncio.Event())
    coordinator = StreamCoordinator.__new__(StreamCoordinator)
    pump = coordinator._pump_events(run)
    consumer = asyncio.create_task(pump.__anext__())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await pump.aclose()
        leaked = asyncio.all_tasks() - existing - {consumer}
        assert not leaked, f"stream left queue/event waiters running: {leaked}"
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await pump.aclose()
        remaining = asyncio.all_tasks() - existing
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)


@pytest.mark.asyncio
async def test_llm_pipeline_owns_upstream_close_after_cancel():
    cleanup_started, release, closed = (asyncio.Event() for _ in range(3))

    async def upstream(*args, **kwargs):
        try:
            yield "token"
        finally:
            cleanup_started.set()
            await release.wait()
            closed.set()

    cancel = asyncio.Event()
    pipeline = LlmStreamPipeline(
        adapter=SimpleNamespace(stream_chat=upstream), messages=[], model_name="test", cancel_event=cancel,
    )
    stream = pipeline.stream_tokens()
    await stream.__anext__()
    cancel.set()
    closing = asyncio.create_task(stream.aclose())
    try:
        await cleanup_started.wait()
        assert not closing.done(), "upstream close must not be abandoned in an unowned task"
        release.set()
        await asyncio.wait_for(closing, 1)
        assert closed.is_set()
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.asyncio
async def test_sse_close_cancellation_still_reaps_disconnect_watcher():
    watcher_done = asyncio.Event()

    async def source():
        try:
            yield {"event": "text", "data": {}}
        finally:
            raise asyncio.CancelledError()

    async def watcher():
        try:
            await asyncio.Event().wait()
        finally:
            watcher_done.set()

    task = asyncio.create_task(watcher())
    await asyncio.sleep(0)
    formatted = sse_event_formatter(source(), asyncio.Event(), task)
    try:
        await formatted.__anext__()
        with pytest.raises(asyncio.CancelledError):
            await formatted.aclose()
        assert task.done()
        assert watcher_done.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_real_response_disconnect_preserves_partial_history(tmp_path, monkeypatch, spec_version):
    database = tmp_path / "http-disconnect.db"
    await init_db(database)
    disconnect = asyncio.Event()
    upstream_closed = asyncio.Event()

    async def tokens(*args, **kwargs):
        try:
            yield '{"chinese": "断连前的回复'
            await asyncio.Event().wait()
        finally:
            upstream_closed.set()

    service = ChatService(db_path=database)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(stream_chat=tokens, api_key="synthetic"), "test", "custom",
    ))
    service._extract_memory_safe = AsyncMock()
    monkeypatch.setattr(chat_router, "get_chat_service", lambda: service)
    response = await chat_router.chat_stream_endpoint(
        chat_router.ChatRequest(prompt="问题", session_id="http-disconnect"), None,
    )

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and b"event: text" in message.get("body", b""):
            disconnect.set()
            if spec_version == "2.4":
                raise OSError("synthetic disconnected socket")

    try:
        try:
            await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": spec_version}}, receive, send), 3)
        except ClientDisconnect:
            assert spec_version == "2.4"
        assert disconnect.is_set()
        assert upstream_closed.is_set(), "response returned before closing the provider connection"
        async with get_db(database) as conn:
            rows = await (await conn.execute(
                "SELECT role, content_chinese FROM messages WHERE session_id='http-disconnect' ORDER BY id"
            )).fetchall()
        assert [(row[0], row[1]) for row in rows] == [("user", "问题"), ("assistant", "断连前的回复")]
    finally:
        await response.body_iterator.aclose()
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["resolving-provider", "waiting-for-first-token"])
async def test_request_scope_cancel_before_reply_prunes_only_its_user_message(tmp_path, phase):
    database = tmp_path / "empty-reply.db"
    await init_db(database)
    entered = asyncio.Event()

    async def tokens(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
        yield "never reached"

    async def adapter(*args, **kwargs):
        if phase == "resolving-provider":
            entered.set()
            await asyncio.Event().wait()
        return SimpleNamespace(stream_chat=tokens), "test", "custom"

    service = ChatService(db_path=database)
    service._get_active_llm_adapter = adapter
    service._extract_memory_safe = AsyncMock()
    stream = service.stream_chat("不应留下孤立消息", session_id="empty-reply", cancel_event=asyncio.Event())
    try:
        async with anyio.create_task_group() as group:
            group.start_soon(_consume, stream)
            await entered.wait()
            group.cancel_scope.cancel()
        async with get_db(database) as conn:
            count = (await (await conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id='empty-reply'"
            )).fetchone())[0]
        assert count == 0
    finally:
        await stream.aclose()
        await service.aclose()
