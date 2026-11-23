"""Tests for the SSRF url_guard module."""

import socket
import time
from urllib.parse import urlparse

import pytest

from galgame2voice.adapters.registry import PROVIDER_PRESETS
from galgame2voice.security.url_guard import clear_dns_cache, validate_llm_base_url


class TestSchemeValidation:
    def test_empty_url_rejected(self):
        assert validate_llm_base_url("")[0] is False
        assert validate_llm_base_url(None)[0] is False
        assert validate_llm_base_url("   ")[0] is False

    def test_non_http_scheme_rejected(self):
        assert validate_llm_base_url("ftp://example.com")[0] is False
        assert validate_llm_base_url("file:///etc/passwd")[0] is False
        assert validate_llm_base_url("gopher://example.com")[0] is False

    def test_credentials_in_url_rejected(self):
        assert validate_llm_base_url("https://user:pass@api.example.com/v1")[0] is False

    def test_missing_host_rejected(self):
        assert validate_llm_base_url("https:///v1")[0] is False


class TestPrivateAddressBlocking:
    def test_loopback_rejected(self):
        ok, _ = validate_llm_base_url("http://127.0.0.1:11434/v1")
        assert ok is False

    def test_private_ranges_rejected(self):
        for url in [
            "http://10.0.0.5/v1",
            "http://192.168.1.10/v1",
            "http://172.16.0.1/v1",
        ]:
            ok, _ = validate_llm_base_url(url)
            assert ok is False, url

    def test_link_local_metadata_rejected(self):
        assert validate_llm_base_url("http://169.254.169.254/latest/meta-data")[0] is False

    def test_ipv6_loopback_rejected(self):
        assert validate_llm_base_url("http://[::1]:8080/v1")[0] is False

    def test_localhost_hostname_rejected(self):
        assert validate_llm_base_url("http://localhost:11434/v1")[0] is False

    def test_allow_private_permits_loopback(self):
        ok, _ = validate_llm_base_url("http://127.0.0.1:11434/v1", allow_private=True)
        assert ok is True

    def test_allow_private_permits_metadata(self):
        ok, _ = validate_llm_base_url("http://169.254.169.254/", allow_private=True)
        assert ok is True


class TestOfficialHosts:
    def test_official_host_http_rejected(self):
        for preset_id, preset in PROVIDER_PRESETS.items():
            if preset_id == "custom":
                continue
            host = urlparse(preset["default_base_url"]).hostname
            ok, reason = validate_llm_base_url(f"http://{host}/v1")
            assert ok is False and "https" in reason, preset_id

    def test_official_host_https_accepted_with_allow_private(self):
        ok, _ = validate_llm_base_url("https://api.openai.com/v1", allow_private=True)
        assert ok is True


def test_url_guard_malformed_urls_and_cgnat_blocked():
    """validate_llm_base_url rejects malformed IPv6, invalid ports, CGNAT, and mapped IPv6 without raising ValueError."""
    for malformed in (
        "http://[::1",
        "http://example.com:99999/v1",
        "http://example.com:notaport/v1",
        "http://100.64.0.1:8000/v1",
        "http://100.127.255.254:8000/v1",
        "http://[::ffff:127.0.0.1]:8000/v1",
        "http://[::ffff:100.64.0.1]:8000/v1",
        "http://[64:ff9b::7f00:1]:8000/v1",
        "http://[64:ff9b::a00:1]:8000/v1",
        "http://[fe80::1%25eth0]:8000/v1",
    ):
        ok, reason = validate_llm_base_url(malformed, allow_private=False)
        assert ok is False, f"Expected {malformed} to be rejected, but got ok=True"
        assert reason

    # Even with allow_private=True, syntactically invalid ports/URLs must be rejected
    for bad_syntax in ("http://[::1", "http://127.0.0.1:99999", "http://localhost:abc"):
        ok, _ = validate_llm_base_url(bad_syntax, allow_private=True)
        assert ok is False


def test_dns_rebind_ssrf_cache_vulnerability(monkeypatch):
    """
    Simulates DNS rebinding attack where domain originally resolves to public IP
    and is later updated to private/loopback IP (127.0.0.1).
    A secure SSRF guard must detect the updated IP via TTL expiration and clear_dns_cache.
    """
    clear_dns_cache()
    current_time = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: current_time[0])

    # Step 1: Initial DNS resolves to public IP 93.184.216.34 (example.com)
    mock_addrs = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: mock_addrs)
    ok, _ = validate_llm_base_url("https://rebind-test.com/v1")
    assert ok is True

    # Step 2: Attacker changes DNS record to point to local loopback 127.0.0.1
    mock_addrs = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
    ]
    # Advance time beyond 60s TTL
    current_time[0] += 65.0

    # With TTL expiration, the stale public IP cache has expired, so 127.0.0.1 is blocked!
    ok_after_ttl, reason = validate_llm_base_url("https://rebind-test.com/v1")
    assert ok_after_ttl is False
    assert "环回/私网" in reason

    # Step 3: Explicit clear_dns_cache immediately purges cache regardless of time
    clear_dns_cache()
    current_time[0] = 1000.0
    mock_addrs = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    assert validate_llm_base_url("https://rebind-test.com/v1")[0] is True
    mock_addrs = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    clear_dns_cache()
    assert validate_llm_base_url("https://rebind-test.com/v1")[0] is False


def test_proxy_tun_fake_ip_allowed_and_error_names_real_checkbox(monkeypatch):
    """Clash/Mihomo/sing-box TUN Fake-IP ranges pass while private/loopback/metadata stay blocked."""
    # Hosts must not be official or example.* ones: the autouse conftest fixture lets those skip DNS checks.
    clear_dns_cache()

    def make_resolver(ip: str, family: int):
        return lambda *args, **kwargs: [(family, socket.SOCK_STREAM, 6, "", (ip, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", make_resolver("198.18.0.7", socket.AF_INET))
    ok, reason = validate_llm_base_url("https://relay.fakeip-check.net/v1")
    assert ok is True, reason

    clear_dns_cache()
    monkeypatch.setattr(socket, "getaddrinfo", make_resolver("fc00::1", socket.AF_INET6))
    ok, reason = validate_llm_base_url("https://relay6.fakeip-check.net/v1")
    assert ok is True, reason

    for blocked_ip, family in (
        ("10.0.0.1", socket.AF_INET),
        ("127.0.0.1", socket.AF_INET),
        ("169.254.169.254", socket.AF_INET),
        ("fd00::1", socket.AF_INET6),
    ):
        clear_dns_cache()
        monkeypatch.setattr(socket, "getaddrinfo", make_resolver(blocked_ip, family))
        ok, reason = validate_llm_base_url("https://relay.lan-check.net/v1")
        assert ok is False, f"{blocked_ip} must stay blocked"
        assert "「允许连接本机或局域网的模型服务」" in reason
        assert "允许私网 LLM 端点" not in reason
