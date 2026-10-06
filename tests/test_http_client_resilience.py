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

import httpx
import pytest

from galgame2voice.telegram_bot.proxy import get_telegram_request_kwargs
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


@pytest.mark.parametrize("http_proxy_configured", [True, False])
def test_plain_httpx_client_rejects_broken_proxy_environment(
    broken_proxy_environment, monkeypatch, http_proxy_configured
):
    """Documents the upstream failure the guard exists for.

    The bracketed NO_PROXY entry alone is enough to break construction, whether
    or not a proxy is actually configured.
    """
    if not http_proxy_configured:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.delenv(name, raising=False)

    with pytest.raises(httpx.InvalidURL):
        httpx.AsyncClient()


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


def test_proxy_environment_is_usable_false_under_broken_environment(broken_proxy_environment):
    """The diagnostic probe reports the environment, not a constant."""
    assert proxy_environment_is_usable() is False


def test_proxy_environment_is_usable_true_without_proxy_variables(clean_proxy_environment):
    assert proxy_environment_is_usable() is True


def test_telegram_request_kwargs_ignore_unusable_proxy_environment(broken_proxy_environment):
    """python-telegram-bot builds its own client, so it needs the same guard."""
    kwargs = get_telegram_request_kwargs(None)

    assert kwargs["httpx_kwargs"] == {"trust_env": False}


def test_telegram_request_kwargs_untouched_when_proxy_url_is_explicit(broken_proxy_environment):
    """An explicit proxy URL bypasses environment parsing, so nothing is overridden."""
    kwargs = get_telegram_request_kwargs(PROXY_URL)

    assert kwargs["proxy"] == PROXY_URL
    assert "httpx_kwargs" not in kwargs
