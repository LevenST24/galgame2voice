"""Retries must recheck the address without contacting any external service."""

import socket
from typing import List
from unittest.mock import patch

import httpx
import pytest

from galgame2voice.adapters.base import ChatMessage
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
        transport = MockTransportWithFailures([429, 429], success_content=sse_body, is_stream=True, retry_after="0.01")
        adapter = OpenAICompatibleLLMAdapter(
            api_key="sk-test-valid-key-12345",
            base_url="https://api.openai.com/v1",
            max_retries=3,
            base_delay=0.01,
        )

        def _client_factory(*args, **kwargs):
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "transport"}
            return _ORIG_HTTPX_ASYNC_CLIENT(transport=transport, **clean_kwargs)

        with patch("httpx.AsyncClient", _client_factory):
            messages = [ChatMessage(role="user", content="hello")]
            tokens = []
            async for token in adapter.stream_chat(messages, model="gpt-4o", base_delay=0.01):
                tokens.append(token)

            assert transport.attempt_count == 3  # 2 failed attempts (429) + 1 successful attempt
            assert tokens == ["你好", "，夏目"]

    async def test_openai_stream_chat_recovers_from_503_service_unavailable(self):
        """Simulates HTTP 503 service unavailable, verifies retry and stream recovery."""
        sse_body = (
            'data: {"choices":[{"delta":{"content":"Resilient"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":" Stream"}}]}\n\n'
            'data: [DONE]\n\n'
        )
        transport = MockTransportWithFailures([503], success_content=sse_body, is_stream=True, retry_after="0.01")
        adapter = OpenAICompatibleLLMAdapter(
            api_key="sk-test-valid-key-12345",
            base_url="https://api.openai.com/v1",
            max_retries=2,
            base_delay=0.01,
        )

        def _client_factory(*args, **kwargs):
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "transport"}
            return _ORIG_HTTPX_ASYNC_CLIENT(transport=transport, **clean_kwargs)

        with patch("httpx.AsyncClient", _client_factory):
            messages = [ChatMessage(role="user", content="hello")]
            tokens = []
            async for token in adapter.stream_chat(messages, model="gpt-4o", base_delay=0.01):
                tokens.append(token)

            assert transport.attempt_count == 2
            assert tokens == ["Resilient", " Stream"]

    async def test_openai_stream_chat_fails_fast_on_401_403(self):
        """Verifies that HTTP 401/403 fails fast on attempt 1 without entering retry loops."""
        transport = MockTransportWithFailures([401, 401, 401], success_content="", is_stream=True)
        adapter = OpenAICompatibleLLMAdapter(
            api_key="sk-invalid-key-9999",
            base_url="https://api.openai.com/v1",
            max_retries=5,
            base_delay=0.01,
        )

        def _client_factory(*args, **kwargs):
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "transport"}
            return _ORIG_HTTPX_ASYNC_CLIENT(transport=transport, **clean_kwargs)

        with patch("httpx.AsyncClient", _client_factory):
            messages = [ChatMessage(role="user", content="hello")]
            with pytest.raises(ValueError, match="Authentication error"):
                async for _ in adapter.stream_chat(messages, model="gpt-4o"):
                    pass

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
        transport_429 = MockTransportWithFailures([429], success_content=sse_body, is_stream=True, retry_after="0.01")
        adapter = AnthropicAdapter(
            api_key="sk-ant-test-key-12345",
            max_retries=2,
            base_delay=0.01,
        )

        def _client_factory_429(*args, **kwargs):
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "transport"}
            return _ORIG_HTTPX_ASYNC_CLIENT(transport=transport_429, **clean_kwargs)

        with patch("httpx.AsyncClient", _client_factory_429):
            tokens = []
            async for t in adapter.stream_chat([ChatMessage(role="user", content="hi")], base_delay=0.01):
                tokens.append(t)
            assert transport_429.attempt_count == 2
            assert tokens == ["Claude", " Response"]

        # 2. Test 401 fail fast
        transport_401 = MockTransportWithFailures([401], success_content="", is_stream=True)
        def _client_factory_401(*args, **kwargs):
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "transport"}
            return _ORIG_HTTPX_ASYNC_CLIENT(transport=transport_401, **clean_kwargs)

        with patch("httpx.AsyncClient", _client_factory_401):
            with pytest.raises(ValueError, match="auth failed|authentication failed"):
                async for _ in adapter.stream_chat([ChatMessage(role="user", content="hi")]):
                    pass
            assert transport_401.attempt_count == 1
