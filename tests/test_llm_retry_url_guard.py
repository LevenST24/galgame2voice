"""Retries must recheck the address without contacting any external service."""

import socket

import httpx
import pytest

from galgame2voice.adapters.base import ChatMessage
from galgame2voice.adapters.llm import anthropic_adapter, openai_adapter
from galgame2voice.security import url_guard


@pytest.mark.parametrize('kind', ['openai', 'anthropic'])
@pytest.mark.parametrize('stream', [False, True])
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
        payload = ({'choices': [{'message': {'content': 'ok'}}]} if kind == 'openai'
                   else {'content': [{'type': 'text', 'text': 'ok'}]})
        return httpx.Response(200, json=payload, request=request)

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
