import asyncio
import httpx
import pytest

from galgame2voice.adapters.base import ChatMessage, extract_stream_token, parse_sse_lines
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
    responses_log = []
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
            resp = httpx.Response(200, text=content, headers={"Content-Type": "text/event-stream"})
            responses_log.append(resp)
            return resp
    else:
        def handler(request: httpx.Request) -> httpx.Response:
            requests_log.append(request)
            resp = response_factory(request)
            responses_log.append(resp)
            return resp

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

    try:
        # A non-SSE transport such as HTTP 500 is retried, so only SSE drives are one request.
        if sse_body is not None:
            assert len(requests_log) == 1, "Must never automatically retry after partial tokens yielded"
        assert all(resp.is_closed for resp in responses_log), "All HTTP stream responses must be closed"
    finally:
        for client in clients_log:
            if not client.is_closed:
                await client.aclose()
    return coordinator, database, events


async def _run_openai_stream(
    coordinator_factory,
    monkeypatch,
    *,
    sse_body: str | None = None,
    response_factory=None,
    cancel_on_text: bool = False,
):
    coordinator, database, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=OpenAICompatibleLLMAdapter, client_patch=_OPENAI_CLIENT_PATCH,
        sse_body=sse_body, response_factory=response_factory, cancel_on_text=cancel_on_text,
    )
    errors = [e for e in events if e.get("event") == "error"]
    dones = [e for e in events if e.get("event") == "done"]
    return coordinator, database, events, errors, dones


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
    coordinator, database, events, errors, dones = await _run_openai_stream(
        coordinator_factory, monkeypatch, sse_body=sse_body,
    )

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
    coordinator, _, events, errors, dones = await _run_openai_stream(
        coordinator_factory, monkeypatch, sse_body=sse_body,
    )

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
    coordinator, _, _, errors, dones = await _run_openai_stream(
        coordinator_factory, monkeypatch, sse_body=sse_body,
    )

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
    coordinator, _, _, errors, dones = await _run_openai_stream(
        coordinator_factory, monkeypatch, sse_body=sse_body,
    )

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
    coordinator, _, _, errors, dones = await _run_openai_stream(
        coordinator_factory, monkeypatch, sse_body=sse_body,
    )

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
    coordinator, _, _, errors, dones = await _run_openai_stream(
        coordinator_factory, monkeypatch,
        response_factory=lambda _request: httpx.Response(500, text="Internal Server Error"),
    )

    assert len(errors) == 1
    assert len(dones) == 0
    coordinator.affection_service.handle_turn_affection.assert_not_called()


@pytest.mark.asyncio
async def test_openai_stream_client_cancellation_exits(coordinator_factory, monkeypatch):
    """
    Cancellation while streaming exits with truncated=True.
    """
    sse_body = 'data: {"choices":[{"delta":{"content":"{\\"chinese\\":\\"句一。句二。"}}]}\n\n'
    coordinator, _, _, _, dones = await _run_openai_stream(
        coordinator_factory, monkeypatch, sse_body=sse_body, cancel_on_text=True,
    )

    assert len(dones) == 1
    assert dones[0]["data"]["truncated"] is True
    coordinator.affection_service.handle_turn_affection.assert_not_called()


class TestStreamingSSEParser:
    """Tests for SSE line parsing, fragmented payloads, malformed chunks, and error events."""

    def test_extract_stream_token_openai(self):
        chunk = {"choices": [{"delta": {"content": "Hello"}}]}
        assert extract_stream_token(chunk) == "Hello"

        chunk_text = {"choices": [{"text": "World"}]}
        assert extract_stream_token(chunk_text) == "World"

        chunk_empty = {"choices": [{"delta": {}}]}
        assert extract_stream_token(chunk_empty) is None

    def test_extract_stream_token_anthropic(self):
        chunk = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "AnthropicToken"}}
        assert extract_stream_token(chunk) == "AnthropicToken"

        chunk_start = {"type": "message_start"}
        assert extract_stream_token(chunk_start) is None

    def test_extract_stream_token_error_payload(self):
        chunk_err = {"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}}
        with pytest.raises(RuntimeError, match="Rate limit exceeded"):
            extract_stream_token(chunk_err)

    def test_extract_stream_token_null_or_false_error(self):
        chunk_null = {"error": None, "choices": [{"delta": {"content": "ValidContent"}}]}
        assert extract_stream_token(chunk_null) == "ValidContent"

        chunk_false = {"error": False, "choices": [{"delta": {"content": "StillValid"}}]}
        assert extract_stream_token(chunk_false) == "StillValid"

    @staticmethod
    async def _collect_sse_tokens(lines):
        async def line_gen():
            for line in lines:
                yield line

        return [t async for t in parse_sse_lines(line_gen())]

    @pytest.mark.asyncio
    async def test_parse_sse_lines_fragmented_and_comments(self):
        lines = [
            ": ping keepalive",
            'data: {"choices": [{"delta": ',
            'data: {"content": "Fragmented"}}]}',
            "",
            ': another keepalive',
            'data: {"choices": [{"delta": {"content": " Resolved"}}]}',
            'data: [DONE]',
        ]

        tokens = await self._collect_sse_tokens(lines)

        assert "".join(tokens) == "Fragmented Resolved"

    @pytest.mark.asyncio
    async def test_parse_sse_lines_malformed_chunk_tolerance(self):
        lines = [
            'data: {"choices": [{"delta": {"content": "Start "}}]}',
            'data: {corrupted json {[[[',
            'data: {"choices": [{"delta": {"content": "End"}}]}',
            'data: [DONE]',
        ]

        tokens = await self._collect_sse_tokens(lines)

        assert "".join(tokens) == "Start End"

    @pytest.mark.asyncio
    async def test_parse_sse_lines_multi_fragment_triplet(self):
        lines = [
            'data: {"choices": [',
            'data: {"delta": ',
            'data: {"content": "MultiFragmentTriplet"}}]}',
            "",
            'data: [DONE]',
        ]

        tokens = await self._collect_sse_tokens(lines)
        assert "".join(tokens) == "MultiFragmentTriplet"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_stream_chat_webpage_response_points_at_base_url(coordinator_factory, monkeypatch, kind):
    """接口地址填成网页时，发消息提示去改「接口地址」，不再说「无需修改配置，可直接重新发送」。"""
    from galgame2voice.adapters.llm.anthropic_adapter import AnthropicAdapter
    from galgame2voice.utils.error_diagnostics import WEBPAGE_ENDPOINT_ACTION_ZH

    def page(request):
        return httpx.Response(
            200,
            text="<!doctype html><html><body>Console</body></html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
            request=request,
        )

    _, _, events = await drive_stream_completion(
        coordinator_factory,
        monkeypatch,
        adapter_cls=OpenAICompatibleLLMAdapter if kind == "openai" else AnthropicAdapter,
        client_patch=_OPENAI_CLIENT_PATCH if kind == "openai" else "galgame2voice.adapters.llm.anthropic_adapter.create_async_client",
        response_factory=page,
    )
    errors = [e["data"] for e in events if e.get("event") == "error"]
    assert [(e["error_code"], e["action_zh"]) for e in errors] == [("INVALID_BASE_URL", WEBPAGE_ENDPOINT_ACTION_ZH)]
    assert "无需修改配置" not in errors[0]["error"]
