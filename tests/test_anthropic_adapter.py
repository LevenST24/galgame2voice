"""
Tests for AnthropicAdapter payload invariance, reasoning_effort mapping, and empty API key fast-fail.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from galgame2voice.adapters.base import ChatMessage, MissingApiKeyError
from galgame2voice.adapters.llm.anthropic_adapter import AnthropicAdapter
from galgame2voice.utils.error_diagnostics import chat_error_payload


class TestAnthropicPayloadInvariance:
    """Verifies Anthropic payload conforms strictly to Anthropic /v1/messages specifications."""

    def test_consecutive_user_messages_are_merged(self):
        """Consecutive user messages must be merged with double newlines."""
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        messages = [
            ChatMessage(role="user", content="First question"),
            ChatMessage(role="user", content="Second question immediately after"),
        ]

        payload = adapter._prepare_anthropic_payload(messages)
        msgs = payload["messages"]

        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "First question\n\nSecond question immediately after"

    def test_consecutive_assistant_messages_are_merged(self):
        """Consecutive assistant messages must be merged with double newlines."""
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Part 1"),
            ChatMessage(role="assistant", content="Part 2"),
        ]

        payload = adapter._prepare_anthropic_payload(messages)
        msgs = payload["messages"]

        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "Part 1\n\nPart 2"

    def test_empty_content_is_sanitized_to_non_empty(self):
        """Empty or whitespace-only messages must be replaced with non-empty string."""
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        messages = [
            ChatMessage(role="user", content="   "),
        ]

        payload = adapter._prepare_anthropic_payload(messages)
        msgs = payload["messages"]

        assert len(msgs) == 1
        assert msgs[0]["content"] == "..."

    def test_leading_assistant_message_prepends_user_greeting(self):
        """If messages start with assistant, a leading user message must be prepended."""
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        messages = [
            ChatMessage(role="assistant", content="Welcome!"),
        ]

        payload = adapter._prepare_anthropic_payload(messages)
        msgs = payload["messages"]

        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Hello"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "Welcome!"

    def test_system_prompt_extracted_to_top_level(self):
        """System prompt must be removed from messages and placed into top-level 'system'."""
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        messages = [
            ChatMessage(role="system", content="You are a helpful galgame narrator."),
            ChatMessage(role="user", content="Hello"),
        ]

        payload = adapter._prepare_anthropic_payload(messages)

        assert payload["system"] == "You are a helpful galgame narrator."
        assert len(payload["messages"]) == 1
        assert payload["messages"][0]["role"] == "user"
        assert payload["messages"][0]["content"] == "Hello"


    @pytest.mark.asyncio
    async def test_chat_omits_top_p_and_penalties_in_request_payload(self):
        """chat() request payload must omit temperature, top_p, frequency_penalty, presence_penalty."""
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        messages = [ChatMessage(role="user", content="Hello")]

        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"content": [{"type": "text", "text": "Hi"}]}'
        mock_resp.json.return_value = {"content": [{"type": "text", "text": "Hi"}]}
        mock_client.post.return_value = mock_resp

        res = await adapter.chat(
            messages,
            temperature=0.7,
            top_p=1.0,
            frequency_penalty=0.0,
            presence_penalty=0.0,
            client_override=mock_client,
        )

        assert res.content == "Hi"
        assert mock_client.post.called
        sent_payload = mock_client.post.call_args.kwargs["json"]
        assert "temperature" not in sent_payload
        assert "top_p" not in sent_payload
        assert "frequency_penalty" not in sent_payload
        assert "presence_penalty" not in sent_payload

    @pytest.mark.asyncio
    async def test_stream_chat_omits_top_p_and_penalties_in_request_payload(self):
        """stream_chat() request payload must omit temperature, top_p, frequency_penalty, presence_penalty."""
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        messages = [ChatMessage(role="user", content="Hello")]

        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = (
            'event: content_block_delta\n'
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}\n\n'
            'event: message_delta\n'
            'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}\n\n'
            'event: message_stop\n'
            'data: {"type": "message_stop"}\n\n'
        )
        mock_client.post.return_value = mock_resp

        chunks = [
            chunk
            async for chunk in adapter.stream_chat(
                messages,
                temperature=0.7,
                top_p=1.0,
                frequency_penalty=0.0,
                presence_penalty=0.0,
                client_override=mock_client,
            )
        ]

        assert chunks == ["Hi"]
        assert mock_client.post.called
        sent_payload = mock_client.post.call_args.kwargs["json"]
        assert "temperature" not in sent_payload
        assert "top_p" not in sent_payload
        assert "frequency_penalty" not in sent_payload
        assert "presence_penalty" not in sent_payload

    @pytest.mark.asyncio
    async def test_allows_top_p_when_temperature_not_specified(self):
        """When temperature is None or omitted and top_p is provided, payload omits both temperature and top_p."""
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        messages = [ChatMessage(role="user", content="Hello")]

        payload = adapter._prepare_anthropic_payload(messages, temperature=None, top_p=0.9)
        assert "temperature" not in payload
        assert "top_p" not in payload

        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"content": [{"type": "text", "text": "Hi"}]}'
        mock_resp.json.return_value = {"content": [{"type": "text", "text": "Hi"}]}
        mock_client.post.return_value = mock_resp

        await adapter.chat(messages, top_p=0.85, client_override=mock_client)
        sent_payload = mock_client.post.call_args.kwargs["json"]
        assert "temperature" not in sent_payload
        assert "top_p" not in sent_payload

    @pytest.mark.asyncio
    async def test_claude_5_5_payload_omits_sampling_and_maps_reasoning_effort(self):
        adapter = AnthropicAdapter(api_key="sk-ant-test", reasoning_effort="high")
        msgs = [ChatMessage(role="user", content="Hello")]
        sonnet = adapter._prepare_anthropic_payload(
            msgs, model="claude-sonnet-5-5", temperature=0.7, top_p=1.0, top_k=5,
            frequency_penalty=0, presence_penalty=0, reasoning_effort="none",
        )
        for key in ("temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty", "reasoning_effort"):
            assert key not in sonnet
        assert sonnet["thinking"] == {"type": "between_tools"}
        assert sonnet["output_config"] == {"effort": "low"}

        haiku = adapter._prepare_anthropic_payload(msgs, model="claude-haiku-5-5", reasoning_effort="none")
        assert haiku["thinking"] == {"type": "disabled"}
        assert haiku["output_config"] == {"effort": "low"}

        opus = adapter._prepare_anthropic_payload(msgs, model="claude-opus-5-5", reasoning_effort="none")
        assert "thinking" not in opus
        assert opus["output_config"] == {"effort": "low"}

        medium = adapter._prepare_anthropic_payload(msgs, model="claude-sonnet-5-5", reasoning_effort="medium")
        assert medium["output_config"] == {"effort": "medium"}
        assert "thinking" not in medium

        unset = adapter._prepare_anthropic_payload(msgs, model="claude-sonnet-5-5", reasoning_effort=None)
        assert "output_config" not in unset
        assert "thinking" not in unset

        mock_resp = MagicMock(status_code=200, text="{}")
        mock_resp.json.return_value = {"content": [{"type": "thinking", "thinking": "."}], "stop_reason": "max_tokens"}
        adapter.client_override = AsyncMock(post=AsyncMock(return_value=mock_resp))
        probe = await adapter.test_connection(model="claude-opus-5-5")
        sent = adapter.client_override.post.call_args.kwargs["json"]
        assert probe.success is True
        assert sent["max_tokens"] == 64
        assert sent["output_config"] == {"effort": "low"}
        assert probe.models.count("claude-opus-5-5") == 1

        # A model outside the built-in sample that just answered is listed, so the settings page
        # does not report 「模型列表里没有」 for a working model name.
        other = await adapter.test_connection(model="claude-opus-4-1")
        assert other.success is True
        assert "claude-opus-4-1" in other.models


class TestAnthropicEmptyApiKeyFastFail:
    """Verifies AnthropicAdapter fails fast without making network requests when api_key is empty."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("empty_key", ["", "   ", None])
    async def test_chat_raises_missing_api_key_error_without_network_request(self, empty_key, monkeypatch):
        """Calling chat() with an empty API key immediately raises MissingApiKeyError before any network/DNS call."""
        network_called = False

        def _fail_network(*args, **kwargs):
            nonlocal network_called
            network_called = True
            raise AssertionError("Network call attempted with missing API key")

        monkeypatch.setattr("galgame2voice.adapters.llm.anthropic_adapter.create_async_client", _fail_network)
        monkeypatch.setattr("galgame2voice.security.url_guard.assert_llm_url_safe", _fail_network)

        adapter = AnthropicAdapter(api_key=empty_key)
        with pytest.raises(MissingApiKeyError) as exc_info:
            await adapter.chat(messages=[ChatMessage(role="user", content="Hi")], model="claude-3-5-sonnet-20241022")

        assert "api key is not configured" in str(exc_info.value).lower()
        assert not network_called

        # Verify chat_error_payload maps this to API_KEY_MISSING both with false flag and default None flag
        is_configured = bool(str(empty_key or "").strip())
        payload = chat_error_payload(exc_info.value, provider_id="anthropic", api_key_configured=is_configured)
        assert payload["error_code"] == "API_KEY_MISSING"
        assert "Anthropic Claude" in payload["error"]
        assert "在上面的 API 密钥里填入后再测试" in payload["error"]

        payload_default = chat_error_payload(exc_info.value, provider_id="anthropic")
        assert payload_default["error_code"] == "API_KEY_MISSING"
        assert "Anthropic Claude" in payload_default["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("empty_key", ["", "   ", None])
    async def test_stream_chat_raises_missing_api_key_error_without_network_request(self, empty_key, monkeypatch):
        """Calling stream_chat() with an empty API key immediately raises MissingApiKeyError on iteration."""
        network_called = False

        def _fail_network(*args, **kwargs):
            nonlocal network_called
            network_called = True
            raise AssertionError("Network call attempted with missing API key")

        monkeypatch.setattr("galgame2voice.adapters.llm.anthropic_adapter.create_async_client", _fail_network)
        monkeypatch.setattr("galgame2voice.security.url_guard.assert_llm_url_safe", _fail_network)

        adapter = AnthropicAdapter(api_key=empty_key)
        stream_gen = adapter.stream_chat(
            messages=[ChatMessage(role="user", content="Hi")],
            model="claude-3-5-sonnet-20241022",
        )

        with pytest.raises(MissingApiKeyError) as exc_info:
            async for _ in stream_gen:
                pass

        assert "api key is not configured" in str(exc_info.value).lower()
        assert not network_called

        # Verify chat_error_payload maps this to API_KEY_MISSING
        is_configured = bool(str(empty_key or "").strip())
        payload = chat_error_payload(exc_info.value, provider_id="anthropic", api_key_configured=is_configured)
        assert payload["error_code"] == "API_KEY_MISSING"
        assert "Anthropic Claude" in payload["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("empty_key", ["", "   ", None])
    async def test_test_connection_fails_immediately_without_network_request(self, empty_key, monkeypatch):
        """Calling test_connection() with an empty API key returns failure immediately without network/DNS call."""
        network_called = False

        def _fail_network(*args, **kwargs):
            nonlocal network_called
            network_called = True
            raise AssertionError("Network call attempted with missing API key")

        monkeypatch.setattr("galgame2voice.adapters.llm.anthropic_adapter.create_async_client", _fail_network)
        monkeypatch.setattr("galgame2voice.security.url_guard.assert_llm_url_safe", _fail_network)

        adapter = AnthropicAdapter(api_key=empty_key)
        res = await adapter.test_connection(model="claude-3-5-sonnet-20241022")

        assert res.success is False
        assert res.message == "Authentication failed: Invalid API key"
        assert res.latency_ms is None
        assert not network_called
