"""Concurrent chat turns must not interleave one session's persisted history."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_service import ChatService


@pytest.fixture
async def concurrent_chat(tmp_path):
    database = tmp_path / "concurrent-chat.db"
    await init_db(database)
    entered = {name: asyncio.Event() for name in ("A", "B")}
    release_a = asyncio.Event()
    contexts = {}

    async def make_reply(messages):
        history = [(message.role, message.content) for message in messages if message.role != "system"]
        prompt = [content for role, content in history if role == "user"][-1]
        contexts[prompt] = history
        entered[prompt].set()
        if prompt == "A":
            await release_a.wait()
        return json.dumps({"chinese": f"answer to {prompt}", "japanese": ""})

    async def stream_tokens(messages, **kwargs):
        yield await make_reply(messages)

    async def sync_reply(**kwargs):
        return await make_reply(kwargs["messages"]), 0

    service = ChatService(db_path=database)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(stream_chat=stream_tokens, api_key="synthetic"), "test", "custom",
    ))
    service._execute_sync_llm = AsyncMock(side_effect=sync_reply)
    service._synthesize_sync_audio = AsyncMock(return_value=("", 0, 0, 0))
    service._extract_memory_safe = AsyncMock()

    async def run(mode, prompt, session):
        if mode == "stream":
            return [event async for event in service.stream_chat(prompt, session_id=session)]
        return await service.chat_sync(prompt, session_id=session)

    async def rows(session):
        async with get_db(database) as conn:
            result = await (await conn.execute(
                "SELECT role, content_chinese FROM messages WHERE session_id=? ORDER BY id", (session,),
            )).fetchall()
        return [(row[0], row[1]) for row in result]

    yield SimpleNamespace(
        service=service, entered=entered, release_a=release_a, contexts=contexts, run=run, rows=rows,
    )
    release_a.set()
    await service.aclose()


@pytest.mark.parametrize("first_mode,second_mode", [
    ("sync", "sync"), ("sync", "stream"), ("stream", "sync"), ("stream", "stream"),
])
async def test_same_session_turns_do_not_overlap_or_misorder(concurrent_chat, first_mode, second_mode):
    stack = concurrent_chat
    first = asyncio.create_task(stack.run(first_mode, "A", "shared"))
    second = None
    try:
        await asyncio.wait_for(stack.entered["A"].wait(), 5)
        second = asyncio.create_task(stack.run(second_mode, "B", "shared"))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(stack.entered["B"].wait(), 0.3)
        assert await stack.rows("shared") == [("user", "A")]
        stack.release_a.set()
        await asyncio.wait_for(asyncio.gather(first, second), 10)
        assert await stack.rows("shared") == [
            ("user", "A"), ("assistant", "answer to A"),
            ("user", "B"), ("assistant", "answer to B"),
        ]
        assert [role for role, _ in stack.contexts["B"]] == ["user", "assistant", "user"]
    finally:
        stack.release_a.set()
        for task in (first, second):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)


@pytest.mark.parametrize("first_mode,second_mode", [("sync", "stream"), ("stream", "sync")])
async def test_cancelled_turn_releases_same_session_after_cleanup(concurrent_chat, first_mode, second_mode):
    stack = concurrent_chat
    first = asyncio.create_task(stack.run(first_mode, "A", "shared"))
    second = None
    try:
        await asyncio.wait_for(stack.entered["A"].wait(), 5)
        second = asyncio.create_task(stack.run(second_mode, "B", "shared"))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(stack.entered["B"].wait(), 0.3)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.wait_for(second, 10)
        assert await stack.rows("shared") == [("user", "B"), ("assistant", "answer to B")]
        assert stack.contexts["B"] == [("user", "B")]
    finally:
        stack.release_a.set()
        for task in (first, second):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)


async def test_different_sessions_are_allowed_to_run_concurrently(concurrent_chat):
    stack = concurrent_chat
    first = asyncio.create_task(stack.run("stream", "A", "one"))
    second = None
    try:
        await asyncio.wait_for(stack.entered["A"].wait(), 5)
        second = asyncio.create_task(stack.run("sync", "B", "two"))
        await asyncio.wait_for(stack.entered["B"].wait(), 5)
        await asyncio.wait_for(second, 10)
        assert not first.done()
    finally:
        stack.release_a.set()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)


async def test_cancelling_waiter_does_not_cancel_the_active_turn(concurrent_chat):
    stack = concurrent_chat
    first = asyncio.create_task(stack.run("stream", "A", "shared"))
    second = None
    try:
        await asyncio.wait_for(stack.entered["A"].wait(), 5)
        second = asyncio.create_task(stack.run("sync", "B", "shared"))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(stack.entered["B"].wait(), 0.3)
        second.cancel()
        assert isinstance((await asyncio.gather(second, return_exceptions=True))[0], asyncio.CancelledError)
        assert not first.done()
        stack.release_a.set()
        await asyncio.wait_for(first, 10)
        await asyncio.wait_for(stack.run("sync", "B", "shared"), 10)
        assert await stack.rows("shared") == [
            ("user", "A"), ("assistant", "answer to A"),
            ("user", "B"), ("assistant", "answer to B"),
        ]
    finally:
        stack.release_a.set()
        for task in (first, second):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)


async def test_unconsumed_stream_does_not_reserve_its_session(concurrent_chat):
    stack = concurrent_chat
    unconsumed = stack.service.stream_chat("A", session_id="shared")
    try:
        await asyncio.wait_for(stack.run("sync", "B", "shared"), 10)
        assert stack.entered["B"].is_set()
    finally:
        await unconsumed.aclose()


async def test_waiting_stream_stops_when_cancel_event_is_set(concurrent_chat):
    stack = concurrent_chat
    first = asyncio.create_task(stack.run("sync", "A", "shared"))
    cancelled = asyncio.Event()
    stream = stack.service.stream_chat("B", session_id="shared", cancel_event=cancelled)

    async def consume():
        return [event async for event in stream]

    second = None
    try:
        await asyncio.wait_for(stack.entered["A"].wait(), 5)
        second = asyncio.create_task(consume())
        await asyncio.sleep(0.1)
        cancelled.set()
        await asyncio.wait_for(second, 2)
        assert not stack.entered["B"].is_set()
        assert await stack.rows("shared") == [("user", "A")]
        stack.release_a.set()
        await asyncio.wait_for(first, 10)
        await asyncio.wait_for(stack.run("sync", "B", "shared"), 10)
        assert await stack.rows("shared") == [
            ("user", "A"), ("assistant", "answer to A"),
            ("user", "B"), ("assistant", "answer to B"),
        ]
    finally:
        stack.release_a.set()
        for task in (first, second):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)
        await stream.aclose()


async def test_waiting_stream_sends_keepalive_and_closes_owned_waiter(concurrent_chat):
    from galgame2voice.services.chat_pipelines.stream_coordinator import SseKeepAlive

    stack = concurrent_chat
    first = asyncio.create_task(stack.run("sync", "A", "shared"))
    stream = stack.service.stream_chat("B", session_id="shared")
    try:
        await asyncio.wait_for(stack.entered["A"].wait(), 5)
        event = await asyncio.wait_for(stream.__anext__(), 7)
        assert isinstance(event, SseKeepAlive)
        assert str(event) == ": keep-alive\n\n"
        assert not stack.entered["B"].is_set()
        # Let the waiter potentially acquire ownership while the generator is
        # suspended at its heartbeat, then close without resuming the stream.
        stack.release_a.set()
        await asyncio.wait_for(first, 10)
        await asyncio.sleep(0.05)
        await stream.aclose()
        await asyncio.wait_for(stack.run("sync", "B", "shared"), 10)
        assert await stack.rows("shared") == [
            ("user", "A"), ("assistant", "answer to A"),
            ("user", "B"), ("assistant", "answer to B"),
        ]
    finally:
        stack.release_a.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await stream.aclose()


@pytest.mark.parametrize("same_database", [True, False])
async def test_turn_ownership_is_shared_by_database_and_session(concurrent_chat, tmp_path, same_database):
    stack = concurrent_chat
    database = stack.service.db_path if same_database else tmp_path / "other.db"
    if not same_database:
        await init_db(database)
    other = ChatService(db_path=database)
    other._get_active_llm_adapter = stack.service._get_active_llm_adapter
    other._execute_sync_llm = stack.service._execute_sync_llm
    other._synthesize_sync_audio = stack.service._synthesize_sync_audio
    other._extract_memory_safe = AsyncMock()
    first = asyncio.create_task(stack.run("stream", "A", "shared"))
    second = None
    try:
        await asyncio.wait_for(stack.entered["A"].wait(), 5)
        second = asyncio.create_task(other.chat_sync("B", session_id="shared"))
        if same_database:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(stack.entered["B"].wait(), 0.3)
            stack.release_a.set()
            await asyncio.wait_for(asyncio.gather(first, second), 10)
            assert await stack.rows("shared") == [
                ("user", "A"), ("assistant", "answer to A"),
                ("user", "B"), ("assistant", "answer to B"),
            ]
        else:
            await asyncio.wait_for(stack.entered["B"].wait(), 5)
            await asyncio.wait_for(second, 10)
            assert not first.done()
    finally:
        stack.release_a.set()
        for task in (first, second):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)
        await other.aclose()
