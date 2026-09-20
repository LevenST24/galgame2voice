"""
Tests for the DNS-rebinding mitigation: the /api Host-header allowlist.

The automated suite disables this middleware by default (see tests/conftest.py)
because it drives the ASGI app in-process with ``Host: test``. These tests
explicitly opt back in and prove the protection both blocks forged Host headers
and lets legitimate loopback access through.
"""

import pytest
from fastapi.testclient import TestClient

from galgame2voice.config import get_settings
from galgame2voice.main import create_app


@pytest.fixture
def guarded_client_factory(monkeypatch):
    """Builds clients with the Host allowlist ENABLED, restoring settings afterwards."""
    monkeypatch.setenv("GALGAME2VOICE_HOST_HEADER_VALIDATION_DISABLED", "0")
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "1")
    monkeypatch.setenv("GALGAME2VOICE_HOST", "127.0.0.1")
    get_settings.cache_clear()

    def _factory(base_url: str) -> TestClient:
        # The Host header is derived from base_url, which is how we forge it.
        return TestClient(create_app(), base_url=base_url)

    yield _factory

    get_settings.cache_clear()


def test_forged_host_header_is_rejected(guarded_client_factory):
    """A rebinding page sends its own domain as Host and must be refused."""
    for forged in ("http://attacker.example", "http://evil.local:8080", "http://127.0.0.1.evil.com"):
        resp = guarded_client_factory(forged).get("/api/health")
        assert resp.status_code == 403, f"Host {forged} was not rejected: {resp.status_code}"
        assert "Host" in resp.json().get("detail", "")


def test_loopback_hosts_are_allowed(guarded_client_factory):
    """Legitimate local console access keeps working (never 403)."""
    # NOTE: the IPv6 loopback form is covered by test_extract_hostname_* below --
    # Starlette's TestClient itself cannot handle an IPv6 base_url (it does a naive
    # netloc.split(":")), so it cannot be exercised end-to-end through TestClient.
    for allowed in ("http://127.0.0.1:8080", "http://localhost:8080"):
        resp = guarded_client_factory(allowed).get("/api/health")
        assert resp.status_code != 403, f"legitimate Host {allowed} was rejected"


def test_extract_hostname_handles_ipv6_and_ports():
    """Host parsing must strip ports for both IPv4/DNS and bracketed IPv6 literals."""
    from galgame2voice.main import HostValidationMiddleware

    cases = {
        "127.0.0.1:8080": "127.0.0.1",
        "127.0.0.1": "127.0.0.1",
        "localhost:8080": "localhost",
        "LOCALHOST": "localhost",
        "[::1]:8080": "::1",
        "[::1]": "::1",
        "  127.0.0.1:8080  ": "127.0.0.1",
        "": "",
        "attacker.example": "attacker.example",
        "evil.local:8080": "evil.local",
    }
    for raw, expected in cases.items():
        assert HostValidationMiddleware.extract_hostname(raw) == expected, raw

    # A bracketed IPv6 literal must be recognised as loopback by the allowlist.
    assert HostValidationMiddleware.extract_hostname("[::1]:8080") in (
        HostValidationMiddleware.ALLOWED_LOOPBACK_HOSTS
    )


def test_non_api_paths_are_not_host_guarded(guarded_client_factory):
    """Static/UI paths stay reachable so the console can render its own error page."""
    resp = guarded_client_factory("http://attacker.example").get("/")
    assert resp.status_code != 403


def test_allowlist_is_disabled_when_flag_is_set(monkeypatch):
    """The suite-wide opt-out must actually disable the middleware."""
    monkeypatch.setenv("GALGAME2VOICE_HOST_HEADER_VALIDATION_DISABLED", "1")
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "1")
    get_settings.cache_clear()
    try:
        client = TestClient(create_app(), base_url="http://attacker.example")
        assert client.get("/api/health").status_code != 403
    finally:
        get_settings.cache_clear()