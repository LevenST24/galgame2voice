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

from galgame2voice.utils.http_client import (
    create_async_client,
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
