"""
Regression tests: HTTP client construction must survive a malformed proxy environment.

httpx reads proxy configuration from the environment unless ``trust_env=False``,
and that parsing is not defensive. A single entry it cannot turn into a URL
pattern — the bracketed IPv6 loopback ``[::1]`` in ``NO_PROXY`` is a common one,
written by proxy tools and shell profiles — makes *every* ``httpx.AsyncClient()``
raise before a request is sent::

    httpx.InvalidURL: Invalid port: ':1]'

That used to take down provider connectivity tests, LLM/STT calls and Telegram
startup simultaneously. These tests pin both halves of the contract:

* the upstream behaviour that motivates the guard (so we notice if httpx ever
  fixes it and the workaround can be dropped), and
* our degradation path, which must disable only environment proxies and must
  leave a healthy environment untouched.
"""

import ssl

import httpx
import pytest

import urllib.request

from galgame2voice.adapters.base import ChatMessage
from galgame2voice.adapters.llm import anthropic_adapter, openai_adapter
from galgame2voice.utils.http_client import (
    SHARED_CLIENT_KEEPALIVE_EXPIRY,
    close_shared_async_clients,
    create_async_client,
    get_shared_async_client,
    proxy_environment_is_usable,
)

# The bracketed IPv6 literal is the entry that breaks httpx's pattern parsing.
BAD_NO_PROXY = "localhost,127.0.0.1,::1,[::1]"
PROXY_URL = "http://127.0.0.1:10808"


@pytest.fixture
def broken_proxy_environment(monkeypatch):
    """Reproduces a proxy environment httpx cannot parse."""
    monkeypatch.setenv("NO_PROXY", BAD_NO_PROXY)
    monkeypatch.setenv("no_proxy", BAD_NO_PROXY)
    monkeypatch.setenv("HTTP_PROXY", PROXY_URL)
    monkeypatch.setenv("HTTPS_PROXY", PROXY_URL)
    monkeypatch.setenv("ALL_PROXY", PROXY_URL)
    yield


@pytest.fixture
def clean_proxy_environment(monkeypatch):
    """Removes every proxy variable so the healthy path is exercised."""
    for name in ("NO_PROXY", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    yield


async def test_create_async_client_survives_broken_proxy_environment(broken_proxy_environment):
    """The guarded factory still returns a usable client under a broken env."""
    client = create_async_client(timeout=10.0)
    try:
        assert isinstance(client, httpx.AsyncClient)
        assert client.trust_env is False
        assert client.timeout.connect == 10.0
    finally:
        await client.aclose()


async def test_create_async_client_keeps_proxy_environment_when_healthy(clean_proxy_environment):
    """A healthy environment must not be silently opted out of."""
    client = create_async_client(timeout=5.0)
    try:
        assert client.trust_env is True
    finally:
        await client.aclose()


async def test_explicit_trust_env_false_is_respected(clean_proxy_environment):
    """An explicit trust_env=False passes through unchanged."""
    client = create_async_client(timeout=1.0, trust_env=False)
    try:
        assert client.trust_env is False
    finally:
        await client.aclose()


async def test_clients_share_one_ca_bundle_load(clean_proxy_environment, monkeypatch):
    """httpx loads the CA bundle for every client, once per transport (one more for each proxy variable).

    With the OpenSSL 3.0 of python.org's Python 3.13 one load blocks the event loop for about 0.2 s, and a
    client is built for every chat request, so the factory must load the bundle once and reuse it.
    """
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, PROXY_URL)
    loads = []
    real_load = ssl.SSLContext.load_verify_locations

    def counting_load(self, *args, **kwargs):
        loads.append(args or kwargs)
        return real_load(self, *args, **kwargs)

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", counting_load)
    clients = [create_async_client(timeout=5.0) for _ in range(3)]
    try:
        assert len(loads) <= 1
    finally:
        for client in clients:
            await client.aclose()


def test_proxy_environment_is_usable_false_under_broken_environment(broken_proxy_environment):
    """The diagnostic probe reports the environment, not a constant."""
    assert proxy_environment_is_usable() is False


def test_proxy_environment_is_usable_true_without_proxy_variables(clean_proxy_environment):
    assert proxy_environment_is_usable() is True


async def test_shared_async_client_configures_long_keepalive_without_client_timeout(clean_proxy_environment):
    """Shared LLM client uses keepalive_expiry >= 300s, shared SSL, and no client-level timeout."""
    captured_kwargs = []
    real_create = create_async_client

    def spy_factory(**kwargs):
        captured_kwargs.append(kwargs)
        return real_create(**kwargs)

    try:
        c1 = get_shared_async_client(factory=spy_factory)
        c2 = get_shared_async_client(factory=spy_factory)
        assert c1 is c2
        assert not c1.is_closed
        assert len(captured_kwargs) == 1
        assert "timeout" not in captured_kwargs[0]
        limits = captured_kwargs[0]["limits"]
        assert limits.keepalive_expiry >= 300
        assert SHARED_CLIENT_KEEPALIVE_EXPIRY >= 300
    finally:
        await close_shared_async_clients()
    assert c1.is_closed


async def test_shared_async_client_recreates_when_proxy_settings_change(clean_proxy_environment, monkeypatch):
    """Toggling ambient/system proxy settings invalidates the cached client on the next call."""
    proxies = {}
    monkeypatch.setattr(urllib.request, "getproxies", lambda: dict(proxies))
    try:
        c1 = get_shared_async_client()
        assert get_shared_async_client() is c1
        proxies["https"] = "http://127.0.0.1:7890"
        c2 = get_shared_async_client()
        assert c2 is not c1
        assert get_shared_async_client() is c2
        proxies.clear()
        c3 = get_shared_async_client()
        assert c3 is not c2
    finally:
        await close_shared_async_clients()
    assert c1.is_closed and c2.is_closed and c3.is_closed


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_consecutive_stream_chat_reuses_one_open_shared_client(monkeypatch, kind):
    """Two back-to-back stream_chat turns reuse one open client while closing every response."""
    module = openai_adapter if kind == "openai" else anthropic_adapter
    adapter_cls = module.OpenAICompatibleLLMAdapter if kind == "openai" else module.AnthropicAdapter
    clients = []
    responses = []
    captured_limits = []
    request_timeouts = []

    def handle(request):
        request_timeouts.append(request.extensions.get("timeout"))
        if kind == "openai":
            body = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
        else:
            body = (
                'event: content_block_delta\n'
                'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n'
                'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            )
        resp = httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"}, request=request)
        responses.append(resp)
        return resp

    def fake_create(**kwargs):
        captured_limits.append(kwargs.get("limits"))
        assert "timeout" not in kwargs
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
        clients.append(client)
        return client

    monkeypatch.setattr(module, "create_async_client", fake_create)
    adapter = adapter_cls(
        api_key="synthetic-key",
        base_url="https://synthetic.invalid/v1",
        allow_private=True,
    )
    msg = [ChatMessage(role="user", content="hello")]
    try:
        out1 = "".join([chunk async for chunk in adapter.stream_chat(msg, model="m", timeout_s=42.0)])
        assert out1 == "ok"
        assert len(clients) == 1
        assert not clients[0].is_closed

        out2 = "".join([chunk async for chunk in adapter.stream_chat(msg, model="m", timeout_s=42.0)])
        assert out2 == "ok"
        assert len(clients) == 1
        assert not clients[0].is_closed
        assert captured_limits[0].keepalive_expiry >= 300
        assert len(responses) == 2
        assert all(resp.is_closed for resp in responses)
        # The shared client has no client-level timeout, so each request must carry the caller's.
        assert request_timeouts == [httpx.Timeout(42.0).as_dict()] * 2
    finally:
        await close_shared_async_clients()
    assert clients[0].is_closed

async def test_retired_shared_clients_drain_inflight_then_close_and_drop(clean_proxy_environment, monkeypatch):
    """Retired shared clients wait for in-flight streams, then close and leave the retired list."""
    import asyncio
    from galgame2voice.utils import http_client
    release = asyncio.Event()
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nhi")
        await writer.drain()
        await release.wait()
        writer.write(b"!!")
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    proxies: dict[str, str] = {}
    monkeypatch.setattr(urllib.request, "getproxies", lambda: dict(proxies))
    try:
        c1 = get_shared_async_client()
        retired = [c1]
        async with c1.stream("GET", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/") as resp:
            for i in range(10):
                proxies["https"] = f"http://127.0.0.1:{7800 + i}"
                retired.append(get_shared_async_client())
            proxies.clear()
            active = get_shared_async_client()
            await asyncio.sleep(0.05)
            assert not c1.is_closed
            release.set()
            assert await resp.aread() == b"hi!!"
        for _ in range(40):
            await asyncio.sleep(0.02)
            if all(c.is_closed for c in retired) and not http_client._retired_clients_by_loop.get(asyncio.get_running_loop()):
                break
        assert all(c.is_closed for c in retired) and not active.is_closed
        assert not http_client._retired_clients_by_loop.get(asyncio.get_running_loop())
    finally:
        server.close()
        await server.wait_closed()
        await close_shared_async_clients()
