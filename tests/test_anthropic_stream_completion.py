import pytest

from galgame2voice.adapters.llm.anthropic_adapter import AnthropicAdapter
from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from tests.test_llm_stream_completion import drive_stream_completion

pytest_plugins = ["tests.test_stream_reply_lines"]

_ANTHROPIC_CLIENT_PATCH = "galgame2voice.adapters.llm.anthropic_adapter.create_async_client"


@pytest.mark.asyncio
async def test_anthropic_stream_early_eof_without_terminal_signals(coordinator_factory, monkeypatch):
    """
    Case A: Native Anthropic SSE yields partial content and cleanly ends without stop_reason or message_stop.
    Must:
      - Preserve partial content.
      - Make exactly 1 upstream request (no auto-retry).
      - Close httpx client.
      - Emit Chinese incomplete/retry guidance error and final done with truncated=True.
      - Never award favorability (affection_calls == 0).
    """
    sse_body = (
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"{\\"chinese\\":\\"已经收到但未结束"}}\n\n'
    )
    coordinator, database, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=AnthropicAdapter, client_patch=_ANTHROPIC_CLIENT_PATCH,
        sse_body=sse_body,
    )

    errors = [e for e in events if e.get("event") == "error"]
    dones = [e for e in events if e.get("event") == "done"]

    assert len(errors) == 1, f"Expected 1 error event for incomplete stream, got {events}"
    assert "未完整" in errors[0]["data"]["error"] or "中断" in errors[0]["data"]["error"] or "重试" in errors[0]["data"]["error"]
    assert len(dones) == 1, f"Expected 1 done event, got {events}"
    assert dones[0]["data"]["truncated"] is True
    assert "已经收到但未结束" in (dones[0]["data"]["chinese"] or "")

    coordinator.affection_service.handle_turn_affection.assert_not_called()

    async with get_db(database) as conn:
        saved = await crud.get_recent_messages(conn, "lines")
        assert saved[-1].role == "assistant"
        assert "已经收到但未结束" in saved[-1].content_chinese
        assert saved[-1].reply_to_message_id == 1


@pytest.mark.asyncio
async def test_anthropic_stream_nonempty_max_tokens_truncation(coordinator_factory, monkeypatch):
    """
    Case C: Native Anthropic SSE yields partial content and ends with stop_reason='max_tokens'.
    Must:
      - Prompt to adjust generation length / reasoning effort.
      - Final done has truncated=True.
      - Never award favorability (affection_calls == 0).
    """
    sse_body = (
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"{\\"chinese\\":\\"已经收到但未结束"}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )
    coordinator, database, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=AnthropicAdapter, client_patch=_ANTHROPIC_CLIENT_PATCH,
        sse_body=sse_body,
    )

    errors = [e for e in events if e.get("event") == "error"]
    dones = [e for e in events if e.get("event") == "done"]

    assert len(errors) == 1, f"Expected 1 error/diagnostic for length truncation, got {events}"
    assert "长度" in errors[0]["data"]["error"]
    assert len(dones) == 1
    assert dones[0]["data"]["truncated"] is True
    coordinator.affection_service.handle_turn_affection.assert_not_called()

    async with get_db(database) as conn:
        saved = await crud.get_recent_messages(conn, "lines")
        assert saved[-1].role == "assistant"
        assert "已经收到但未结束" in saved[-1].content_chinese
        assert saved[-1].reply_to_message_id == 1


@pytest.mark.asyncio
async def test_anthropic_stream_empty_max_tokens_length_diagnosis(coordinator_factory, monkeypatch):
    """
    Case D: Empty content + stop_reason='max_tokens' must produce EmptyReplyError with length diagnosis.
    """
    sse_body = (
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )
    coordinator, _, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=AnthropicAdapter, client_patch=_ANTHROPIC_CLIENT_PATCH,
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
async def test_anthropic_stream_end_turn_message_stop_succeeds(coordinator_factory, monkeypatch):
    """
    Case B: Native Anthropic message_delta.stop_reason='end_turn' + message_stop (no OpenAI DONE) succeeds.
    """
    sse_body = (
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"{\\"chinese\\":\\"完整回答\\",\\"japanese\\":\\"完全な回答\\"}"}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )
    coordinator, _, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=AnthropicAdapter, client_patch=_ANTHROPIC_CLIENT_PATCH,
        sse_body=sse_body,
    )

    errors = [e for e in events if e.get("event") == "error"]
    dones = [e for e in events if e.get("event") == "done"]

    assert len(errors) == 0
    assert len(dones) == 1
    assert dones[0]["data"]["truncated"] is False
    assert dones[0]["data"]["chinese"] == "完整回答"
    coordinator.affection_service.handle_turn_affection.assert_called_once()


@pytest.mark.asyncio
async def test_anthropic_stream_end_turn_without_message_stop_succeeds(coordinator_factory, monkeypatch):
    """
    Case B2: Explicit end_turn without message_stop is also valid and succeeds.
    """
    sse_body = (
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"{\\"chinese\\":\\"完整回答\\",\\"japanese\\":\\"完全な回答\\"}"}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
    )
    coordinator, _, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=AnthropicAdapter, client_patch=_ANTHROPIC_CLIENT_PATCH,
        sse_body=sse_body,
    )
    errors = [e for e in events if e.get("event") == "error"]
    dones = [e for e in events if e.get("event") == "done"]

    assert len(errors) == 0
    assert len(dones) == 1
    assert dones[0]["data"]["truncated"] is False
    coordinator.affection_service.handle_turn_affection.assert_called_once()


@pytest.mark.asyncio
async def test_anthropic_stream_stop_sequence_succeeds(coordinator_factory, monkeypatch):
    """
    Case B3: stop_reason='stop_sequence' is a normal completion and succeeds.
    """
    sse_body = (
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"{\\"chinese\\":\\"完整回答\\",\\"japanese\\":\\"完全な回答\\"}"}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"stop_sequence"}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )
    coordinator, _, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=AnthropicAdapter, client_patch=_ANTHROPIC_CLIENT_PATCH,
        sse_body=sse_body,
    )
    errors = [e for e in events if e.get("event") == "error"]
    dones = [e for e in events if e.get("event") == "done"]

    assert len(errors) == 0
    assert len(dones) == 1
    assert dones[0]["data"]["truncated"] is False
    coordinator.affection_service.handle_turn_affection.assert_called_once()


@pytest.mark.asyncio
async def test_anthropic_stream_client_cancellation_exits(coordinator_factory, monkeypatch):
    """
    Client cancellation while streaming exits with truncated=True.
    """
    sse_body = (
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"{\\"chinese\\":\\"句一。句二。"}}\n\n'
    )
    coordinator, _, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=AnthropicAdapter, client_patch=_ANTHROPIC_CLIENT_PATCH,
        sse_body=sse_body, cancel_on_text=True,
    )

    dones = [e for e in events if e.get("event") == "done"]
    assert len(dones) == 1
    assert dones[0]["data"]["truncated"] is True
    coordinator.affection_service.handle_turn_affection.assert_not_called()
