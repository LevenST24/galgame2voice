"""Retries must recheck the address without contacting any external service."""

import email.utils
import socket
import time
from typing import List
from unittest.mock import patch

import httpx
import pytest

from galgame2voice.adapters.base import (
    ChatMessage,
    TRANSIENT_STATUS_CODES,
    calculate_backoff_delay,
    parse_retry_after,
)
from galgame2voice.adapters.llm import anthropic_adapter, openai_adapter
from galgame2voice.adapters.llm.anthropic_adapter import AnthropicAdapter
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter
from galgame2voice.security import url_guard


# Chat turns stream; only the Anthropic connection test still makes a non-streaming call.
@pytest.mark.parametrize(('kind', 'stream'), [('openai', True), ('anthropic', False), ('anthropic', True)])
@pytest.mark.parametrize('failure', ['status', 'connection'])
@pytest.mark.parametrize('mode', ['blocked', 'allow_private', 'public'])
async def test_retry_rechecks_dns_before_sending(monkeypatch, kind, stream, failure, mode):
    module = openai_adapter if kind == 'openai' else anthropic_adapter
    adapter_cls = module.OpenAICompatibleLLMAdapter if kind == 'openai' else module.AnthropicAdapter
    allow_private = mode == 'allow_private'
    addresses = ['1.1.1.1', '127.0.0.1' if mode == 'blocked' else '1.1.1.1']
    resolutions = []
    calls = []
    clients = []
    url_guard.clear_dns_cache()

    def resolve(host, port, **kwargs):
        assert host == 'retry-guard.invalidhost'
        address = addresses[min(len(resolutions), len(addresses) - 1)]
        resolutions.append(address)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, port))]

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            if failure == 'connection':
                raise httpx.ConnectError('synthetic failure', request=request)
            return httpx.Response(503, text='synthetic temporary error', request=request)
        if stream:
            data = ('data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                    if kind == 'openai' else
                    'data: {"type":"content_block_delta","delta":{"text":"ok"}}\n\n')
            return httpx.Response(200, text=data + 'data: [DONE]\n\n', request=request)
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': 'ok'}]}, request=request)

    def create_client(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
        clients.append(client)
        return client

    monkeypatch.setattr(url_guard.socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(module, 'create_async_client', create_client)
    adapter = adapter_cls(api_key='synthetic-key', base_url='https://retry-guard.invalidhost/v1',
                          allow_private=allow_private)
    messages = [ChatMessage(role='user', content='synthetic input')]

    async def run():
        if stream:
            return ''.join([part async for part in adapter.stream_chat(messages, model='fake', max_retries=1, base_delay=0)])
        return (await adapter.chat(messages, model='fake', max_retries=1, base_delay=0)).content

    try:
        if mode == 'blocked':
            with pytest.raises(PermissionError, match='SSRF'):
                await run()
            assert resolutions == addresses
            assert len(calls) == 1, 'a retry must not be sent after DNS resolves to loopback'
        else:
            assert await run() == 'ok'
            assert len(calls) == 2
            assert resolutions == ([] if allow_private else addresses)
        assert all(client.is_closed for client in clients)
    finally:
        url_guard.clear_dns_cache()


_ORIG_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


class MockTransportWithFailures(httpx.AsyncBaseTransport):
    """
    Simulates a sequence of HTTP error responses followed by a successful SSE stream or JSON response.
    """

    def __init__(self, failure_statuses: List[int], success_content: str, is_stream: bool = True, retry_after: str = "0.01"):
        self.failure_statuses = list(failure_statuses)
        self.success_content = success_content
        self.is_stream = is_stream
        self.retry_after = retry_after
        self.attempt_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.attempt_count += 1
        if self.failure_statuses:
            status = self.failure_statuses.pop(0)
            headers = {"retry-after": self.retry_after} if status in (429, 503) else {}
            if status == 429:
                body = b'{"error": {"message": "Rate limit exceeded (429)", "type": "rate_limit"}}'
            elif status == 503:
                body = b'{"error": {"message": "Service unavailable (503)", "type": "service_unavailable"}}'
            elif status in (401, 403):
                body = b'{"error": {"message": "Invalid authentication credentials (401/403)", "type": "auth_error"}}'
            else:
                body = f'{{"error": "HTTP {status}"}}'.encode("utf-8")
            return httpx.Response(status_code=status, headers=headers, content=body, request=request)

        # Success response
        if self.is_stream:
            headers = {"content-type": "text/event-stream"}
            return httpx.Response(
                status_code=200,
                headers=headers,
                content=self.success_content.encode("utf-8"),
                request=request,
            )
        else:
            headers = {"content-type": "application/json"}
            return httpx.Response(
                status_code=200,
                headers=headers,
                content=self.success_content.encode("utf-8"),
                request=request,
            )


def _openai_adapter(
    *,
    api_key: str = "sk-test-valid-key-12345",
    max_retries: int = 3,
    base_delay: float = 0.01,
) -> OpenAICompatibleLLMAdapter:
    return OpenAICompatibleLLMAdapter(
        api_key=api_key,
        base_url="https://api.openai.com/v1",
        max_retries=max_retries,
        base_delay=base_delay,
    )


def _patched_transport(
    failure_statuses: List[int],
    success_content: str = "",
    retry_after: str = "0.01",
):
    transport = MockTransportWithFailures(
        failure_statuses,
        success_content=success_content,
        is_stream=True,
        retry_after=retry_after,
    )

    def _client_factory(*args, **kwargs):
        clean_kwargs = {k: v for k, v in kwargs.items() if k != "transport"}
        return _ORIG_HTTPX_ASYNC_CLIENT(transport=transport, **clean_kwargs)

    return transport, patch("httpx.AsyncClient", _client_factory)


async def _collect_stream(adapter, prompt: str = "hello", **stream_kwargs):
    messages = [ChatMessage(role="user", content=prompt)]
    return [token async for token in adapter.stream_chat(messages, **stream_kwargs)]


@pytest.mark.asyncio
class TestLlmRetryEdgeCases:
    """Rigorous challenge tests for LLM streaming retry resilience."""

    async def test_openai_stream_chat_recovers_from_429_before_yielding(self):
        """Simulates consecutive HTTP 429 rate limits, verifies backoff recovery before yielding SSE tokens."""
        sse_body = (
            'data: {"choices":[{"delta":{"content":"\\u4f60\\u597d"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"\\uff0c\\u590f\\u76ee"}}]}\n\n'
            'data: [DONE]\n\n'
        )
        transport, client_patch = _patched_transport([429, 429], sse_body)
        adapter = _openai_adapter(max_retries=3)
        with client_patch:
            tokens = await _collect_stream(adapter, model="gpt-4o", base_delay=0.01)

            assert transport.attempt_count == 3  # 2 failed attempts (429) + 1 successful attempt
            assert tokens == ["你好", "，夏目"]


    async def test_openai_stream_chat_fails_fast_on_401_403(self):
        """Verifies that HTTP 401/403 fails fast on attempt 1 without entering retry loops."""
        transport, client_patch = _patched_transport([401, 401, 401])
        adapter = _openai_adapter(api_key="sk-invalid-key-9999", max_retries=5)
        with client_patch:
            with pytest.raises(ValueError, match="Authentication error"):
                await _collect_stream(adapter, model="gpt-4o")

            # Crucial check: only 1 attempt made, failed immediately!
            assert transport.attempt_count == 1

    async def test_anthropic_stream_chat_recovers_and_fails_fast(self):
        """Verifies Anthropic adapter stream_chat behaves identically: 429 recovery & 401 fail fast."""
        sse_body = (
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Claude"}}\n\n'
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " Response"}}\n\n'
            'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}\n\n'
            'data: {"type": "message_stop"}\n\n'
        )
        # 1. Test 429 recovery
        transport_429, patch_429 = _patched_transport([429], sse_body)
        adapter = AnthropicAdapter(
            api_key="sk-ant-test-key-12345",
            max_retries=2,
            base_delay=0.01,
        )
        with patch_429:
            tokens = await _collect_stream(adapter, prompt="hi", base_delay=0.01)
            assert transport_429.attempt_count == 2
            assert tokens == ["Claude", " Response"]

        # 2. Test 401 fail fast
        transport_401, patch_401 = _patched_transport([401])
        with patch_401:
            with pytest.raises(ValueError, match="auth failed|authentication failed"):
                await _collect_stream(adapter, prompt="hi")
            assert transport_401.attempt_count == 1

    async def test_stream_chat_early_read_timeout_retries_successfully(self):
        adapter = _openai_adapter(api_key="sk-test")
        attempt_count = 0

        class MockStreamResponse:
            status_code = 200
            headers = {}

            async def aiter_lines(self):
                nonlocal attempt_count
                attempt_count += 1
                if attempt_count == 1:
                    raise httpx.ReadTimeout("Server timed out before first byte")
                yield 'data: {"choices": [{"delta": {"content": "RecoveredStream"}}]}'
                yield 'data: [DONE]'

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch("httpx.AsyncClient.stream", return_value=MockStreamResponse()):
            tokens = await _collect_stream(adapter, model="gpt-4o", max_retries=2, base_delay=0.01)

            assert "".join(tokens) == "RecoveredStream"
            assert attempt_count == 2


class TestLLMRetryPolicies:
    """Tests for standardized exponential backoff, jitter, and Retry-After parsing."""

    def test_transient_status_codes(self):
        expected_codes = {408, 429, 500, 502, 503, 504}
        for code in expected_codes:
            assert code in TRANSIENT_STATUS_CODES

    def test_parse_retry_after_numeric(self):
        assert parse_retry_after({"retry-after": "5"}) == 5.0
        assert parse_retry_after({"Retry-After": "2.5"}) == 2.5
        assert parse_retry_after(None) is None
        assert parse_retry_after({}) is None
        assert parse_retry_after({"retry-after": "invalid_val"}) is None

    def test_parse_retry_after_http_date(self):
        future_time = time.time() + 120
        date_str = email.utils.formatdate(future_time, usegmt=True)
        val = parse_retry_after({"retry-after": date_str})
        assert val is not None
        assert 110.0 < val < 130.0

    def test_calculate_backoff_delay_exponential_and_jitter(self):
        for attempt in range(5):
            delay = calculate_backoff_delay(attempt, base_delay=0.1, jitter_min=0.05, jitter_max=0.15)
            min_expected = 0.1 * (2 ** attempt) + 0.05
            max_expected = 0.1 * (2 ** attempt) + 0.15
            assert min_expected <= delay <= max_expected

    def test_calculate_backoff_delay_with_retry_after(self):
        delay = calculate_backoff_delay(0, base_delay=1.0, retry_after=15.0, jitter_min=0.1, jitter_max=0.4)
        assert 15.1 <= delay <= 15.4

    def test_calculate_backoff_delay_max_ceiling(self):
        delay = calculate_backoff_delay(20, base_delay=10.0, retry_after=120.0, max_delay=60.0)
        assert delay <= 60.0
