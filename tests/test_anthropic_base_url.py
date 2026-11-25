"""Tests that the Anthropic adapter reaches /v1/messages whichever base URL form is configured."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from galgame2voice.adapters.base import ChatMessage
from galgame2voice.adapters.llm.anthropic_adapter import AnthropicAdapter


async def _posted_url(base_url: str) -> str:
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    adapter = AnthropicAdapter(api_key="sk-ant-test", base_url=base_url)
    await adapter.chat(
        messages=[ChatMessage(role="user", content="Hi")],
        client_override=client,
        max_retries=0,
    )
    return client.post.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        # The form Anthropic-compatible services publish for the official SDK, without /v1.
        ("https://api.deepseek.com/anthropic", "https://api.deepseek.com/anthropic/v1/messages"),
        ("https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/", "https://api.anthropic.com/v1/messages"),
        # Forms that already worked stay the same.
        ("https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/v1/", "https://api.anthropic.com/v1/messages"),
        ("https://proxy.example.com/v1/messages", "https://proxy.example.com/v1/messages"),
    ],
)
async def test_chat_posts_to_v1_messages(base_url, expected):
    assert await _posted_url(base_url) == expected
