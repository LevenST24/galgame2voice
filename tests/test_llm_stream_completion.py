import asyncio
import httpx
import pytest

from galgame2voice.adapters.base import ChatMessage
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter
from galgame2voice.database import crud
from galgame2voice.database.session import get_db

pytest_plugins = ["tests.test_stream_reply_lines"]

_OPENAI_CLIENT_PATCH = "galgame2voice.adapters.llm.openai_adapter.create_async_client"


async def drive_stream_completion(
    coordinator_factory,
    monkeypatch,
    *,
    adapter_cls,
    client_patch: str,
    sse_body: str | None = None,
    response_factory=None,
    cancel_on_text: bool = False,
):
    """Install one mocked adapter, read ``coordinator.stream()`` until ``aclose``, and check the client.

    ``sse_body`` is one HTTP 200 ``text/event-stream`` payload and must be the only upstream call.
    ``response_factory`` replaces that payload for a non-SSE transport such as HTTP 500. A 500 is
    retried by the adapter, so that path checks only that every client was closed.
    ``cancel_on_text`` sets the coordinator cancel event when the first ``text`` event arrives.
    """
    if (sse_body is None) == (response_factory is None):
        raise ValueError("pass exactly one of sse_body or response_factory")

    requests_log = []
    clients_log = []
    if response_factory is None:
        responses_sse = [sse_body]
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
    else:
        def handler(request: httpx.Request) -> httpx.Response:
            requests_log.append(request)
            return response_factory(request)

    def create_client(**kwargs):
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, trust_env=False)
        clients_log.append(client)
        return client

    monkeypatch.setattr(client_patch, create_client)
    factory_kwargs = {}
    cancel = None
    if cancel_on_text:
        cancel = asyncio.Event()
        factory_kwargs["cancel_event"] = cancel
    coordinator, _, database = coordinator_factory(**factory_kwargs)
    coordinator.adapter = adapter_cls(
        api_key="synthetic-key",
        base_url="https://synthetic.invalid/v1",
        allow_private=True,
    )
    coordinator.messages = [ChatMessage(role="user", content="问题")]

    events = []
    stream = coordinator.stream()
    try:
        async for event in stream:
            events.append(event)
            if cancel is not None and event.get("event") == "text":
                cancel.set()
    finally:
        await stream.aclose()

    # A non-SSE transport such as HTTP 500 is retried, so only SSE drives are one request.
    if sse_body is not None:
        assert len(requests_log) == 1, "Must never automatically retry after partial tokens yielded"
    assert all(client.is_closed for client in clients_log), "All HTTP clients must be closed"
    return coordinator, database, events


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
    sse_body = 'data: {"choices":[{"delta":{"content":"{\\"chinese\\":\\"已经收到但未结束"}}]}\n\n'
    coordinator, database, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=OpenAICompatibleLLMAdapter, client_patch=_OPENAI_CLIENT_PATCH,
        sse_body=sse_body,
    )

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
    sse_body = (
        'data: {"choices":[{"delta":{"content":"{\\"chinese\\":\\"已经收到但未结束"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
    )
    coordinator, _, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=OpenAICompatibleLLMAdapter, client_patch=_OPENAI_CLIENT_PATCH,
        sse_body=sse_body,
    )

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
    sse_body = (
        'data: {"choices":[{"delta":{"content":"{\\"chinese\\":\\"完整回答\\",\\"japanese\\":\\"完全な回答\\"}"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    )
    coordinator, _, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=OpenAICompatibleLLMAdapter, client_patch=_OPENAI_CLIENT_PATCH,
        sse_body=sse_body,
    )

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
    sse_body = (
        'data: {"choices":[{"delta":{"content":"{\\"chinese\\":\\"正常完成\\",\\"japanese\\":\\"完了\\"}"}}]}\n\n'
        'data: [DONE]\n\n'
    )
    coordinator, _, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=OpenAICompatibleLLMAdapter, client_patch=_OPENAI_CLIENT_PATCH,
        sse_body=sse_body,
    )
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
    sse_body = 'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
    coordinator, _, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=OpenAICompatibleLLMAdapter, client_patch=_OPENAI_CLIENT_PATCH,
        sse_body=sse_body,
    )
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
    coordinator, _, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=OpenAICompatibleLLMAdapter, client_patch=_OPENAI_CLIENT_PATCH,
        response_factory=lambda _request: httpx.Response(500, text="Internal Server Error"),
    )
    errors = [e for e in events if e.get("event") == "error"]
    dones = [e for e in events if e.get("event") == "done"]

    assert len(errors) == 1
    assert len(dones) == 0
    coordinator.affection_service.handle_turn_affection.assert_not_called()


@pytest.mark.asyncio
async def test_openai_stream_client_cancellation_exits(coordinator_factory, monkeypatch):
    """
    Cancellation while streaming exits with truncated=True.
    """
    sse_body = 'data: {"choices":[{"delta":{"content":"{\\"chinese\\":\\"句一。句二。"}}]}\n\n'
    coordinator, _, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=OpenAICompatibleLLMAdapter, client_patch=_OPENAI_CLIENT_PATCH,
        sse_body=sse_body, cancel_on_text=True,
    )

    dones = [e for e in events if e.get("event") == "done"]
    assert len(dones) == 1
    assert dones[0]["data"]["truncated"] is True
    coordinator.affection_service.handle_turn_affection.assert_not_called()
