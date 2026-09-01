"""Tests for the SSRF url_guard module."""

import pytest

from galgame2voice.security.url_guard import validate_llm_base_url


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
        ok, reason = validate_llm_base_url("http://api.openai.com/v1")
        assert ok is False
        assert "https" in reason

    def test_official_host_https_accepted_with_allow_private(self):
        ok, _ = validate_llm_base_url("https://api.openai.com/v1", allow_private=True)
        assert ok is True
