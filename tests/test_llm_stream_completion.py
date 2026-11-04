import asyncio
import httpx
import pytest

from galgame2voice.adapters.base import ChatMessage
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter
from galgame2voice.database import crud
from galgame2voice.database.session import get_db

pytest_plugins = ["tests.test_stream_reply_lines"]


def _create_mock_adapter(responses_sse: list[str], requests_log: list, clients_log: list):
    idx = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal idx
        requests_log.append(request)
        if idx < len(responses_sse):
            content = responses_sse[idx]
            idx += 1
        else:
            content = ""
        return httpx.Response(200, text=content, headers={"Content-Type": "text/event-stream"})

    def create_client(**kwargs):
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, trust_env=False)
        clients_log.append(client)
        return client

    adapter = OpenAICompatibleLLMAdapter(
        api_key="synthetic-key",
        base_url="https://synthetic.invalid/v1",
        allow_private=True,
    )
    return adapter, create_client


@pytest.mark.asyncio
async def test_openai_stream_early_eof_without_terminal_signals(coordinator_factory, monkeypatch):
    """
    Case A: HTTP 200 SSE yields partial content and cleanly ends without finish_reason or [DONE].
    Must:
      - Preserve partial content.
      - Make exactly 1 upstream request (no duplicate billing/auto-retry).
      - Close httpx client.
      - Emit Chinese incomplete/retry guidance error and final done with truncated=True.
      - Never award favorability (affection_calls == 0).
    """
    requests_log = []
    clients_log = []
    sse_body = 'data: {"choices":[{"delta":{"content":"{\\"chinese\\":\\"已经收到但未结束"}}]}\n\n'

    adapter, client_factory = _create_mock_adapter([sse_body], requests_log, clients_log)
    monkeypatch.setattr("galgame2voice.adapters.llm.openai_adapter.create_async_client", client_factory)

    coordinator, _, database = coordinator_factory()
    coordinator.adapter = adapter
    messages = [ChatMessage(role="user", content="问题")]
    coordinator.messages = messages

    events = []
    stream = coordinator.stream()
    try:
        async for event in stream:
            events.append(event)
    finally:
        await stream.aclose()

    assert len(requests_log) == 1, "Must never automatically retry after partial tokens yielded"
    assert all(c.is_closed for c in clients_log), "All HTTP clients must be closed"

    errors = [e for e in events if e.get("event") == "error"]
    dones = [e for e in events if e.get("event") == "done"]

    assert len(errors) == 1, f"Expected 1 error event for incomplete stream, got {events}"
    assert "未完整" in errors[0]["data"]["error"] or "中断" in errors[0]["data"]["error"] or "重试" in errors[0]["data"]["error"]
    assert len(dones) == 1, f"Expected 1 done event, got {events}"
    assert dones[0]["data"]["truncated"] is True
    assert "已经收到但未结束" in (dones[0]["data"]["chinese"] or "")

    # Affection must not be settled on truncated turn
    coordinator.affection_service.handle_turn_affection.assert_not_called()

    # Database must retain partial message
    async with get_db(database) as conn:
        saved = await crud.get_recent_messages(conn, "lines")
        assert saved[-1].role == "assistant"
        assert "已经收到但未结束" in saved[-1].content_chinese


@pytest.mark.asyncio
async def test_openai_stream_nonempty_length_truncation(coordinator_factory, monkeypatch):
    """
    Case C: HTTP 200 SSE yields partial content and ends with finish_reason='length' without [DONE].
    Must:
      - Prompt to adjust generation length / reasoning effort.
      - Final done has truncated=True.
      - Never award favorability (affection_calls == 0).
    """
    requests_log = []
    clients_log = []
    sse_body = (
        'data: {"choices":[{"delta":{"content":"{\\"chinese\\":\\"已经收到但未结束"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
    )

    adapter, client_factory = _create_mock_adapter([sse_body], requests_log, clients_log)
    monkeypatch.setattr("galgame2voice.adapters.llm.openai_adapter.create_async_client", client_factory)

    coordinator, _, database = coordinator_factory()
    coordinator.adapter = adapter
    coordinator.messages = [ChatMessage(role="user", content="问题")]

    events = []
    stream = coordinator.stream()
    try:
        async for event in stream:
            events.append(event)
    finally:
        await stream.aclose()

    assert len(requests_log) == 1
    assert all(c.is_closed for c in clients_log)

    errors = [e for e in events if e.get("event") == "error"]
    dones = [e for e in events if e.get("event") == "done"]

    assert len(errors) == 1, f"Expected 1 error/diagnostic for length truncation, got {events}"
    assert "长度" in errors[0]["data"]["error"]
    assert len(dones) == 1
    assert dones[0]["data"]["truncated"] is True
    coordinator.affection_service.handle_turn_affection.assert_not_called()


@pytest.mark.asyncio
async def test_openai_stream_stop_without_done_compatibility_control(coordinator_factory, monkeypatch):
    """
    Case B: Complete JSON + finish_reason='stop' without [DONE] is valid and must succeed.
    Must:
      - No error event.
      - Final done has truncated=False.
      - Settle favorability once.
    """
    requests_log = []
    clients_log = []
    sse_body = (
        'data: {"choices":[{"delta":{"content":"{\\"chinese\\":\\"完整回答\\",\\"japanese\\":\\"完全な回答\\"}"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    )

    adapter, client_factory = _create_mock_adapter([sse_body], requests_log, clients_log)
    monkeypatch.setattr("galgame2voice.adapters.llm.openai_adapter.create_async_client", client_factory)

    coordinator, _, _ = coordinator_factory()
    coordinator.adapter = adapter
    coordinator.messages = [ChatMessage(role="user", content="问题")]

    events = []
    stream = coordinator.stream()
    try:
        async for event in stream:
            events.append(event)
    finally:
        await stream.aclose()

    assert len(requests_log) == 1
    assert all(c.is_closed for c in clients_log)

    errors = [e for e in events if e.get("event") == "error"]
    dones = [e for e in events if e.get("event") == "done"]

    assert len(errors) == 0, f"Expected 0 errors for valid stop stream, got {errors}"
    assert len(dones) == 1
    assert dones[0]["data"]["truncated"] is False
    assert dones[0]["data"]["chinese"] == "完整回答"
    coordinator.affection_service.handle_turn_affection.assert_called_once()


@pytest.mark.asyncio
async def test_openai_stream_done_succeeds(coordinator_factory, monkeypatch):
    """
    Normal stream terminating with [DONE] succeeds.
    """
    requests_log = []
    clients_log = []
    sse_body = (
        'data: {"choices":[{"delta":{"content":"{\\"chinese\\":\\"正常完成\\",\\"japanese\\":\\"完了\\"}"}}]}\n\n'
        'data: [DONE]\n\n'
    )

    adapter, client_factory = _create_mock_adapter([sse_body], requests_log, clients_log)
    monkeypatch.setattr("galgame2voice.adapters.llm.openai_adapter.create_async_client", client_factory)

    coordinator, _, _ = coordinator_factory()
    coordinator.adapter = adapter
    coordinator.messages = [ChatMessage(role="user", content="问题")]

    events = [event async for event in coordinator.stream()]
    errors = [e for e in events if e.get("event") == "error"]
    dones = [e for e in events if e.get("event") == "done"]

    assert len(errors) == 0
    assert len(dones) == 1
    assert dones[0]["data"]["truncated"] is False
    coordinator.affection_service.handle_turn_affection.assert_called_once()


@pytest.mark.asyncio
async def test_openai_stream_empty_length_keeps_original_diagnosis(coordinator_factory, monkeypatch):
    """
    Empty content + finish_reason='length' retains original EmptyReplyError diagnosis.
    """
    requests_log = []
    clients_log = []
    sse_body = 'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'

    adapter, client_factory = _create_mock_adapter([sse_body], requests_log, clients_log)
    monkeypatch.setattr("galgame2voice.adapters.llm.openai_adapter.create_async_client", client_factory)

    coordinator, _, _ = coordinator_factory()
    coordinator.adapter = adapter
    coordinator.messages = [ChatMessage(role="user", content="问题")]

    events = [event async for event in coordinator.stream()]
    errors = [e for e in events if e.get("event") == "error"]
    dones = [e for e in events if e.get("event") == "done"]

    assert len(errors) == 1
    assert errors[0]["data"]["kind"] == "empty_reply"
    assert "生成长度用完" in errors[0]["data"]["error"]
    assert len(dones) == 0
    coordinator.affection_service.handle_turn_affection.assert_not_called()


@pytest.mark.asyncio
async def test_openai_stream_explicit_provider_error_terminal_without_done(coordinator_factory, monkeypatch):
    """
    Explicit provider error (500) before content raises error, emits error event and NO done event.
    """
    requests_log = []
    clients_log = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_log.append(request)
        return httpx.Response(500, text="Internal Server Error")

    def create_client(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        clients_log.append(client)
        return client

    monkeypatch.setattr("galgame2voice.adapters.llm.openai_adapter.create_async_client", create_client)

    adapter = OpenAICompatibleLLMAdapter(
        api_key="synthetic-key",
        base_url="https://synthetic.invalid/v1",
        allow_private=True,
    )
    coordinator, _, _ = coordinator_factory()
    coordinator.adapter = adapter
    coordinator.messages = [ChatMessage(role="user", content="问题")]

    events = [event async for event in coordinator.stream()]
    errors = [e for e in events if e.get("event") == "error"]
    dones = [e for e in events if e.get("event") == "done"]

    assert len(errors) == 1
    assert len(dones) == 0
    assert all(c.is_closed for c in clients_log)
    coordinator.affection_service.handle_turn_affection.assert_not_called()


@pytest.mark.asyncio
async def test_openai_stream_client_cancellation_exits(coordinator_factory, monkeypatch):
    """
    Cancellation while streaming exits with truncated=True.
    """
    requests_log = []
    clients_log = []
    cancel = asyncio.Event()

    sse_body = 'data: {"choices":[{"delta":{"content":"{\\"chinese\\":\\"句一。句二。"}}]}\n\n'
    adapter, client_factory = _create_mock_adapter([sse_body], requests_log, clients_log)
    monkeypatch.setattr("galgame2voice.adapters.llm.openai_adapter.create_async_client", client_factory)

    coordinator, _, _ = coordinator_factory(cancel_event=cancel)
    coordinator.adapter = adapter
    coordinator.messages = [ChatMessage(role="user", content="问题")]

    events = []
    stream = coordinator.stream()
    try:
        async for event in stream:
            events.append(event)
            if event.get("event") == "text":
                cancel.set()
    finally:
        await stream.aclose()

    dones = [e for e in events if e.get("event") == "done"]
    assert len(dones) == 1
    assert dones[0]["data"]["truncated"] is True
    coordinator.affection_service.handle_turn_affection.assert_not_called()
