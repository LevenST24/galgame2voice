"""HTTP 429 that means "out of balance" is reported at once and as a quota problem."""

import httpx
import pytest

from galgame2voice.adapters.base import ChatMessage
from galgame2voice.adapters.llm import openai_adapter
from galgame2voice.security import url_guard
from galgame2voice.utils.error_diagnostics import chat_error_payload

GLM_NO_BALANCE = '{"error":{"code":"1113","message":"余额不足或无可用资源包,请充值。"}}'
OPENAI_NO_QUOTA = (
    '{"error":{"message":"You exceeded your current quota, please check your plan and billing details.",'
    '"type":"insufficient_quota","code":"insufficient_quota"}}'
)
RATE_LIMITED = '{"error":{"message":"Rate limit reached for requests","type":"requests","code":"rate_limit_exceeded"}}'


async def _requests_until_error(monkeypatch, body):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(429, text=body, request=request)

    async def allow(*args, **kwargs):
        return None

    async def no_wait(*args, **kwargs):
        return None

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(url_guard, "assert_llm_url_safe", allow)
    monkeypatch.setattr(openai_adapter, "get_shared_async_client", lambda factory=None: client)
    monkeypatch.setattr(openai_adapter.asyncio, "sleep", no_wait)
    adapter = openai_adapter.OpenAICompatibleLLMAdapter(api_key="sk-test", base_url="https://api.example.com/v1")
    try:
        with pytest.raises(RuntimeError) as raised:
            async for _ in adapter.stream_chat([ChatMessage(role="user", content="你好")], model="m"):
                pass
    finally:
        await client.aclose()
    return len(calls), str(raised.value)


@pytest.mark.parametrize("body", [GLM_NO_BALANCE, OPENAI_NO_QUOTA])
async def test_out_of_balance_429_is_not_retried(monkeypatch, body):
    calls, error = await _requests_until_error(monkeypatch, body)
    # Retrying cannot add money to the account; it only delayed the message by ~8 s.
    assert calls == 1
    assert "429" in error


async def test_a_real_rate_limit_is_still_retried(monkeypatch):
    calls, _ = await _requests_until_error(monkeypatch, RATE_LIMITED)
    assert calls == 4


@pytest.mark.parametrize("provider_id", ["glm", "custom"])
def test_glm_no_balance_429_says_top_up_not_slow_down(provider_id):
    payload = chat_error_payload(
        RuntimeError(f"Rate limit exceeded (429): {GLM_NO_BALANCE}"),
        provider_id=provider_id,
        api_key_configured=True,
    )
    assert payload["diagnostic_code"] == "QUOTA_EXCEEDED"
    assert payload["retryable"] is False
