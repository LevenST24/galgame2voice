"""Concurrent chat turns must not interleave one session's persisted history."""

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult
from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.routers.chat import ChatRequest, chat_stream_endpoint, set_chat_service
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.voice_manager import VoiceManager


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

    service = ChatService(db_path=database)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(stream_chat=stream_tokens, api_key="synthetic"), "test", "custom",
    ))
    service._extract_memory_safe = AsyncMock()

    async def run(prompt, session):
        return [event async for event in service.stream_chat(prompt, session_id=session)]

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


_DEFAULT_SECOND = object()


@asynccontextmanager
async def _blocked_shared_turn(stack, second_factory=_DEFAULT_SECOND, *, expect_blocked=True):
    """Hold turn A on session 'shared', optionally start turn B, and cancel/gather both on exit."""
    first = asyncio.create_task(stack.run("A", "shared"))
    second = None
    try:
        await asyncio.wait_for(stack.entered["A"].wait(), 5)
        if second_factory is not None:
            coro = stack.run("B", "shared") if second_factory is _DEFAULT_SECOND else second_factory()
            second = asyncio.create_task(coro)
            if expect_blocked:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(stack.entered["B"].wait(), 0.3)
        yield first, second
    finally:
        stack.release_a.set()
        for task in (first, second):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)


async def test_first_turn_creates_session(concurrent_chat):
    stack = concurrent_chat
    await asyncio.wait_for(stack.run("B", "new"), 10)
    assert await stack.rows("new") == [("user", "B"), ("assistant", "answer to B")]


async def test_queued_turn_does_not_recreate_deleted_session(concurrent_chat):
    stack = concurrent_chat
    async with _blocked_shared_turn(stack) as (first, second):
        async with get_db(stack.service.db_path) as conn:
            assert await crud.delete_session(conn, "shared")
        stack.release_a.set()
        _, result = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 10)
        assert any(event["event"] == "error" and "对话已被删除" in event["data"]["error"] for event in result)
        async with get_db(stack.service.db_path) as conn:
            assert await crud.get_session(conn, "shared") is None
        assert await stack.rows("shared") == []
        assert not stack.entered["B"].is_set()


async def test_same_session_turns_do_not_overlap_or_misorder(concurrent_chat):
    stack = concurrent_chat
    async with _blocked_shared_turn(stack) as (first, second):
        assert await stack.rows("shared") == [("user", "A")]
        stack.release_a.set()
        await asyncio.wait_for(asyncio.gather(first, second), 10)
        assert await stack.rows("shared") == [
            ("user", "A"), ("assistant", "answer to A"),
            ("user", "B"), ("assistant", "answer to B"),
        ]
        assert [role for role, _ in stack.contexts["B"]] == ["user", "assistant", "user"]


async def test_cancelled_turn_releases_same_session_after_cleanup(concurrent_chat):
    stack = concurrent_chat
    async with _blocked_shared_turn(stack) as (first, second):
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.wait_for(second, 10)
        assert await stack.rows("shared") == [("user", "B"), ("assistant", "answer to B")]
        assert stack.contexts["B"] == [("user", "B")]


async def test_different_sessions_are_allowed_to_run_concurrently(concurrent_chat):
    stack = concurrent_chat
    first = asyncio.create_task(stack.run("A", "one"))
    second = None
    try:
        await asyncio.wait_for(stack.entered["A"].wait(), 5)
        second = asyncio.create_task(stack.run("B", "two"))
        await asyncio.wait_for(stack.entered["B"].wait(), 5)
        await asyncio.wait_for(second, 10)
        assert not first.done()
    finally:
        stack.release_a.set()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)


async def test_cancelling_waiter_does_not_cancel_the_active_turn(concurrent_chat):
    stack = concurrent_chat
    async with _blocked_shared_turn(stack) as (first, second):
        second.cancel()
        assert isinstance((await asyncio.gather(second, return_exceptions=True))[0], asyncio.CancelledError)
        assert not first.done()
        stack.release_a.set()
        await asyncio.wait_for(first, 10)
        await asyncio.wait_for(stack.run("B", "shared"), 10)
        assert await stack.rows("shared") == [
            ("user", "A"), ("assistant", "answer to A"),
            ("user", "B"), ("assistant", "answer to B"),
        ]


async def test_unconsumed_stream_does_not_reserve_its_session(concurrent_chat):
    stack = concurrent_chat
    unconsumed = stack.service.stream_chat("A", session_id="shared")
    try:
        await asyncio.wait_for(stack.run("B", "shared"), 10)
        assert stack.entered["B"].is_set()
    finally:
        await unconsumed.aclose()


async def test_waiting_stream_stops_when_cancel_event_is_set(concurrent_chat):
    stack = concurrent_chat
    cancelled = asyncio.Event()
    stream = stack.service.stream_chat("B", session_id="shared", cancel_event=cancelled)

    async def consume():
        return [event async for event in stream]

    try:
        async with _blocked_shared_turn(stack, consume, expect_blocked=False) as (first, second):
            await asyncio.sleep(0.1)
            cancelled.set()
            await asyncio.wait_for(second, 2)
            assert not stack.entered["B"].is_set()
            assert await stack.rows("shared") == [("user", "A")]
            stack.release_a.set()
            await asyncio.wait_for(first, 10)
            await asyncio.wait_for(stack.run("B", "shared"), 10)
            assert await stack.rows("shared") == [
                ("user", "A"), ("assistant", "answer to A"),
                ("user", "B"), ("assistant", "answer to B"),
            ]
    finally:
        await stream.aclose()


def _sse_frame(frame):
    if frame.lstrip().startswith(":"):
        return ":keep-alive", None
    name = "message"
    data = None
    for line in frame.splitlines():
        if line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data = json.loads(line.split(":", 1)[1].strip())
    return name, data


async def test_queued_stream_reports_progress_before_taking_the_lock(concurrent_chat, monkeypatch):
    """The second /api/chat/stream on a busy session must report queued progress before it runs."""
    monkeypatch.setattr(
        "galgame2voice.services.chat_pipelines.turn_ownership._KEEPALIVE_SECONDS",
        0.05,
    )
    stack = concurrent_chat
    set_chat_service(stack.service)
    response = None
    try:
        async with _blocked_shared_turn(stack, None):
            response = await chat_stream_endpoint(ChatRequest(prompt="B", session_id="shared"), None)
            iterator = response.body_iterator
            frame = await asyncio.wait_for(iterator.__anext__(), 2)
            name, data = _sse_frame(frame)
            assert name == "progress" and data == {"stage": "queued"}, frame
            assert "answer to" not in frame
            assert not stack.entered["B"].is_set()
            stack.release_a.set()
            rest = [chunk async for chunk in iterator]
            events = [_sse_frame(chunk) for chunk in rest]
            text = "".join(
                item.get("delta_chinese", "")
                for event_name, item in events
                if event_name == "text" and isinstance(item, dict)
            )
            done = [item for event_name, item in events if event_name == "done" and isinstance(item, dict)]
            assert "answer to B" in text
            assert done and done[-1]["chinese"] == "answer to B"
            assert all(event_name != "error" for event_name, _item in events)
    finally:
        set_chat_service(None)
        if response is not None:
            await response.body_iterator.aclose()


async def test_waiting_stream_sends_queued_progress_and_closes_owned_waiter(concurrent_chat):
    stack = concurrent_chat
    stream = stack.service.stream_chat("B", session_id="shared")
    try:
        async with _blocked_shared_turn(stack, None) as (first, _):
            event = await asyncio.wait_for(stream.__anext__(), 7)
            assert event.get("event") == "progress"
            assert event.get("data") == {"stage": "queued"}
            assert not stack.entered["B"].is_set()
            # Let the waiter potentially acquire ownership while the generator is
            # suspended at its heartbeat, then close without resuming the stream.
            stack.release_a.set()
            await asyncio.wait_for(first, 10)
            await asyncio.sleep(0.05)
            await stream.aclose()
            await asyncio.wait_for(stack.run("B", "shared"), 10)
            assert await stack.rows("shared") == [
                ("user", "A"), ("assistant", "answer to A"),
                ("user", "B"), ("assistant", "answer to B"),
            ]
    finally:
        await stream.aclose()


@pytest.mark.parametrize("same_database", [True, False])
async def test_turn_ownership_is_shared_by_database_and_session(concurrent_chat, tmp_path, same_database):
    stack = concurrent_chat
    database = stack.service.db_path if same_database else tmp_path / "other.db"
    if not same_database:
        await init_db(database)
    other = ChatService(db_path=database)
    other._get_active_llm_adapter = stack.service._get_active_llm_adapter
    other._extract_memory_safe = AsyncMock()

    async def consume_other():
        return [event async for event in other.stream_chat("B", session_id="shared")]

    try:
        async with _blocked_shared_turn(stack, consume_other, expect_blocked=same_database) as (first, second):
            if same_database:
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
        await other.aclose()


class _ConcurrentSseMockAdapter(BaseLLMAdapter):
    """Configurable/dynamic mock adapter for concurrent SSE streaming tests."""

    def __init__(self, custom_tokens=None):
        super().__init__(api_key="sk-dynamic", base_url="https://api.openai.com/v1")
        self.custom_tokens = custom_tokens

    async def chat(self, messages: list[ChatMessage], model: str, temperature: float = 1.0, **kwargs) -> LLMResponse:
        if self.custom_tokens is not None:
            return LLMResponse(content="".join(self.custom_tokens))
        user_msg = messages[-1].content if messages else ""
        return LLMResponse(content=json.dumps({"chinese": f"回复: {user_msg}", "japanese": f"応答: {user_msg}。"}, ensure_ascii=False))

    async def stream_chat(self, messages: list[ChatMessage], model: str, temperature: float = 1.0, **kwargs):
        if self.custom_tokens is not None:
            tokens = self.custom_tokens
        else:
            user_msg = messages[-1].content if messages else ""
            tokens = [
                '{"chinese": "',
                f"回复: {user_msg}",
                '", "japanese": "',
                f"応答: {user_msg}。",
                '"}',
            ]
        for token in tokens:
            await asyncio.sleep(0.001)
            yield token

    async def test_connection(self, model=None) -> TestResult:
        return TestResult(success=True, message="Dynamic connection OK", latency_ms=10.0)

    async def list_models(self):
        return ["dynamic-model"]


@pytest.mark.asyncio
async def test_high_concurrency_multi_client_sse_streaming(
    isolate_test_database, configured_voice, mock_gpt_sovits,
):
    """10 concurrent client sessions streaming multi-sentence bilingual responses with monotonic chunks."""
    voice_manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=isolate_test_database)
    chat_service = ChatService(tts_service=voice_manager.tts_service, db_path=isolate_test_database)

    async def run_client(client_id: int):
        events = []
        async for event in chat_service.stream_chat(
            prompt=f"Client {client_id} prompt",
            session_id=f"concurrent_session_{client_id}",
        ):
            events.append(event)
        return events

    with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=_ConcurrentSseMockAdapter()):
        tasks = [run_client(i) for i in range(10)]
        results = await asyncio.gather(*tasks)

    assert len(results) == 10
    for i, events in enumerate(results):
        done_events = [e for e in events if e["event"] == "done"]
        assert len(done_events) == 1, f"Client {i} missing done event"
        done_data = done_events[0]["data"]
        assert f"Client {i} prompt" in done_data["chinese"]
        assert f"Client {i} prompt" in done_data["japanese"]

        audio_chunks = [e for e in events if e["event"] == "audio_chunk"]
        assert len(audio_chunks) >= 1
        for idx, ch in enumerate(audio_chunks):
            assert ch["data"]["index"] == idx


@pytest.mark.asyncio
async def test_early_client_disconnect_cancellation_stress(
    isolate_test_database, configured_voice, mock_gpt_sovits,
):
    """10 concurrent clients cancelling mid-stream leave backend clean for subsequent requests."""
    voice_manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=isolate_test_database)
    chat_service = ChatService(tts_service=voice_manager.tts_service, db_path=isolate_test_database)

    def cancellation_adapter(*args, **kwargs):
        tokens = [
            '{"chinese": "正在开始...", ',
            '"japanese": "始まりました。長い文です。' + "続く。" * 20 + '"}',
        ]
        return _ConcurrentSseMockAdapter(custom_tokens=tokens)

    async def run_cancelled_client(client_id: int):
        cancel_event = asyncio.Event()
        received = 0
        async for _event in chat_service.stream_chat(
            prompt=f"Abort prompt {client_id}",
            session_id=f"abort_session_{client_id}",
            cancel_event=cancel_event,
        ):
            received += 1
            if received >= 1:
                cancel_event.set()

    with patch("galgame2voice.services.chat_service.get_llm_adapter", side_effect=cancellation_adapter):
        tasks = [run_cancelled_client(i) for i in range(10)]
        await asyncio.gather(*tasks)

    normal_adapter = _ConcurrentSseMockAdapter(custom_tokens=[
        '{"chinese": "正常测试", "japanese": "正常テスト"}'
    ])
    with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=normal_adapter):
        events = [e async for e in chat_service.stream_chat("Normal after cancel", session_id="post_cancel_session")]
        done = next(e for e in events if e["event"] == "done")
        assert done["data"]["chinese"] == "正常测试"
