import json
from unittest.mock import AsyncMock, MagicMock
import pytest

from galgame2voice.adapters.base import ChatMessage
from galgame2voice.adapters.llm.anthropic_adapter import AnthropicAdapter
from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from tests.test_llm_stream_completion import drive_stream_completion

pytest_plugins = ["tests.test_stream_reply_lines"]

_ANTHROPIC_CLIENT_PATCH = "galgame2voice.adapters.llm.anthropic_adapter.create_async_client"


async def _run_anthropic_stream(
    coordinator_factory,
    monkeypatch,
    *,
    text: str | None = None,
    stop_reason: str | None = None,
    message_stop: bool = False,
    cancel_on_text: bool = False,
):
    parts = []
    if text is not None:
        parts.append(
            "event: content_block_delta\n"
            f'data: {{"type":"content_block_delta","delta":{{"type":"text_delta","text":{json.dumps(text, ensure_ascii=False)}}}}}\n\n'
        )
    if stop_reason is not None:
        parts.append(
            "event: message_delta\n"
            f'data: {{"type":"message_delta","delta":{{"stop_reason":"{stop_reason}"}}}}\n\n'
        )
    if message_stop:
        parts.append('event: message_stop\ndata: {"type":"message_stop"}\n\n')
    coordinator, database, events = await drive_stream_completion(
        coordinator_factory, monkeypatch,
        adapter_cls=AnthropicAdapter, client_patch=_ANTHROPIC_CLIENT_PATCH,
        sse_body="".join(parts), cancel_on_text=cancel_on_text,
    )
    errors = [e for e in events if e.get("event") == "error"]
    dones = [e for e in events if e.get("event") == "done"]
    return coordinator, database, events, errors, dones


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
    coordinator, database, events, errors, dones = await _run_anthropic_stream(
        coordinator_factory, monkeypatch, text='{"chinese":"已经收到但未结束',
    )

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
    coordinator, database, events, errors, dones = await _run_anthropic_stream(
        coordinator_factory, monkeypatch,
        text='{"chinese":"已经收到但未结束', stop_reason="max_tokens", message_stop=True,
    )

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
    coordinator, _, _, errors, dones = await _run_anthropic_stream(
        coordinator_factory, monkeypatch, stop_reason="max_tokens", message_stop=True,
    )

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
    coordinator, _, _, errors, dones = await _run_anthropic_stream(
        coordinator_factory, monkeypatch,
        text='{"chinese":"完整回答","japanese":"完全な回答"}',
        stop_reason="end_turn", message_stop=True,
    )

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
    coordinator, _, _, errors, dones = await _run_anthropic_stream(
        coordinator_factory, monkeypatch,
        text='{"chinese":"完整回答","japanese":"完全な回答"}',
        stop_reason="end_turn",
    )

    assert len(errors) == 0
    assert len(dones) == 1
    assert dones[0]["data"]["truncated"] is False
    coordinator.affection_service.handle_turn_affection.assert_called_once()


@pytest.mark.asyncio
async def test_anthropic_stream_stop_sequence_succeeds(coordinator_factory, monkeypatch):
    """
    Case B3: stop_reason='stop_sequence' is a normal completion and succeeds.
    """
    coordinator, _, _, errors, dones = await _run_anthropic_stream(
        coordinator_factory, monkeypatch,
        text='{"chinese":"完整回答","japanese":"完全な回答"}',
        stop_reason="stop_sequence", message_stop=True,
    )

    assert len(errors) == 0
    assert len(dones) == 1
    assert dones[0]["data"]["truncated"] is False
    coordinator.affection_service.handle_turn_affection.assert_called_once()


@pytest.mark.asyncio
async def test_anthropic_stream_client_cancellation_exits(coordinator_factory, monkeypatch):
    """
    Client cancellation while streaming exits with truncated=True.
    """
    coordinator, _, _, _, dones = await _run_anthropic_stream(
        coordinator_factory, monkeypatch,
        text='{"chinese":"句一。句二。', cancel_on_text=True,
    )

    assert len(dones) == 1
    assert dones[0]["data"]["truncated"] is True
    coordinator.affection_service.handle_turn_affection.assert_not_called()


class TestAnthropicStreamErrorHandling:
    """Verifies that SSE error chunks raise RuntimeError in both client_override and live modes."""

    @pytest.mark.asyncio
    async def test_client_override_stream_error_chunk_raises(self):
        adapter = AnthropicAdapter(api_key="test-key")

        mock_client = AsyncMock()
        error_sse = (
            'data: {"type": "content_block_delta", "delta": {"text": "Start"}}\n\n'
            'data: {"type": "error", "error": {"type": "rate_limit_error", "message": "Rate limited"}}\n\n'
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = error_sse
        mock_client.post.return_value = mock_resp

        gen = adapter.stream_chat(
            messages=[ChatMessage(role="user", content="Hello")],
            client_override=mock_client,
        )

        first_token = await anext(gen)
        assert first_token == "Start"

        with pytest.raises(RuntimeError, match="Anthropic stream error: .*rate_limit_error"):
            await anext(gen)
