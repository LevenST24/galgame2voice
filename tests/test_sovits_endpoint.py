"""
Unit tests for the unified GPT-SoVITS endpoint resolver
(galgame2voice.services.sovits_endpoint).

Covers URL parsing/validation, the is_local property, and the
env > db > dotenv > default priority chain of resolve_sovits_url.
"""

import pytest

from galgame2voice.services.sovits_endpoint import (
    DEFAULT_SOVITS_BASE_URL,
    SovitsEndpoint,
    get_explicit_process_env_url,
    parse_sovits_endpoint,
    resolve_sovits_url,
)

ENV_KEYS = ("GALGAME2VOICE_GPT_SOVITS_BASE_URL", "GPT_SOVITS_BASE_URL")


def _clear_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class TestParseSovitsEndpoint:
    def test_basic_http(self):
        ep = parse_sovits_endpoint("http://127.0.0.1:9880", source="db")
        assert ep == SovitsEndpoint(
            base_url="http://127.0.0.1:9880",
            host="127.0.0.1",
            port=9880,
            scheme="http",
            source="db",
        )

    def test_default_port_http(self):
        ep = parse_sovits_endpoint("http://192.168.1.20", source="env")
        assert ep.port == 9880
        assert ep.base_url == "http://192.168.1.20:9880"

    def test_default_port_https(self):
        ep = parse_sovits_endpoint("https://sovits.example.com", source="env")
        assert ep.port == 443
        assert ep.base_url == "https://sovits.example.com:443"

    def test_explicit_port_kept_and_trailing_slash_stripped(self):
        ep = parse_sovits_endpoint("http://127.0.0.1:9999/", source="db")
        assert ep.port == 9999
        assert ep.base_url == "http://127.0.0.1:9999"

    def test_ipv6_loopback(self):
        ep = parse_sovits_endpoint("http://[::1]:9880", source="db")
        assert ep.host == "::1"
        assert ep.base_url == "http://[::1]:9880"
        assert ep.is_local is True

    def test_bad_scheme_rejected(self):
        with pytest.raises(ValueError):
            parse_sovits_endpoint("ftp://127.0.0.1:9880", source="db")

    def test_missing_host_rejected(self):
        with pytest.raises(ValueError):
            parse_sovits_endpoint("http://", source="db")

    def test_credentials_rejected(self):
        with pytest.raises(ValueError):
            parse_sovits_endpoint("http://user:pass@127.0.0.1:9880", source="db")

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            parse_sovits_endpoint("   ", source="db")


class TestIsLocal:
    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "LOCALHOST", "127.0.0.2"])
    def test_loopback_is_local(self, host):
        assert parse_sovits_endpoint(f"http://{host}:9880", source="db").is_local is True

    @pytest.mark.parametrize(
        "host", ["192.168.1.50", "10.0.0.20", "sovits.example.com"]
    )
    def test_non_loopback_not_local(self, host):
        assert parse_sovits_endpoint(f"http://{host}:9880", source="db").is_local is False


class TestGetExplicitProcessEnvUrl:
    def test_first_non_empty_wins(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("GALGAME2VOICE_GPT_SOVITS_BASE_URL", "http://10.0.0.1:1111")
        monkeypatch.setenv("GPT_SOVITS_BASE_URL", "http://10.0.0.2:2222")
        assert get_explicit_process_env_url() == "http://10.0.0.1:1111"

    def test_empty_value_skipped(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("GALGAME2VOICE_GPT_SOVITS_BASE_URL", "   ")
        monkeypatch.setenv("GPT_SOVITS_BASE_URL", "http://10.0.0.2:2222")
        assert get_explicit_process_env_url() == "http://10.0.0.2:2222"

    def test_none_when_unset(self, monkeypatch):
        _clear_env(monkeypatch)
        assert get_explicit_process_env_url() is None


class TestResolvePrecedence:
    def test_env_beats_db_and_dotenv(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("GPT_SOVITS_BASE_URL", "http://10.1.2.3:7777")
        ep = resolve_sovits_url(
            db_url="http://127.0.0.1:9880", settings_url="http://127.0.0.1:9999"
        )
        assert ep.base_url == "http://10.1.2.3:7777"
        assert ep.source == "env"

    def test_namespaced_env_key_wins(self, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("GALGAME2VOICE_GPT_SOVITS_BASE_URL", "http://10.9.9.9:1234")
        ep = resolve_sovits_url(db_url="http://127.0.0.1:9880", settings_url=None)
        assert ep.source == "env"
        assert ep.host == "10.9.9.9"
        assert ep.port == 1234

    def test_db_beats_dotenv(self, monkeypatch):
        _clear_env(monkeypatch)
        ep = resolve_sovits_url(
            db_url="http://192.168.1.20:9999", settings_url="http://127.0.0.1:9880"
        )
        assert ep.base_url == "http://192.168.1.20:9999"
        assert ep.source == "db"

    def test_dotenv_used_when_no_db(self, monkeypatch):
        _clear_env(monkeypatch)
        ep = resolve_sovits_url(db_url=None, settings_url="http://192.168.1.30:9999")
        assert ep.base_url == "http://192.168.1.30:9999"
        assert ep.source == "dotenv"

    def test_empty_db_url_ignored(self, monkeypatch):
        _clear_env(monkeypatch)
        ep = resolve_sovits_url(db_url="   ", settings_url="http://192.168.1.30:9999")
        assert ep.source == "dotenv"

    def test_default_settings_url_reports_default_source(self, monkeypatch):
        _clear_env(monkeypatch)
        ep = resolve_sovits_url(db_url=None, settings_url="http://127.0.0.1:9880")
        assert ep.base_url == DEFAULT_SOVITS_BASE_URL
        assert ep.source == "default"

    def test_nothing_configured_falls_back_to_default(self, monkeypatch):
        _clear_env(monkeypatch)
        ep = resolve_sovits_url(db_url=None, settings_url=None)
        assert ep.base_url == "http://127.0.0.1:9880"
        assert ep.source == "default"
        assert ep.is_local is True

    def test_custom_dotenv_beats_seeded_default_db(self, monkeypatch):
        _clear_env(monkeypatch)
        # Database was initialized with default seed http://127.0.0.1:9880,
        # but user configured custom .env with port 9999. .env must win!
        ep = resolve_sovits_url(
            db_url="http://127.0.0.1:9880", settings_url="http://127.0.0.1:9999"
        )
        assert ep.base_url == "http://127.0.0.1:9999"
        assert ep.source == "dotenv"

    def test_custom_db_beats_custom_dotenv(self, monkeypatch):
        _clear_env(monkeypatch)
        ep = resolve_sovits_url(
            db_url="http://192.168.1.100:9880", settings_url="http://127.0.0.1:9999"
        )
        assert ep.base_url == "http://192.168.1.100:9880"
        assert ep.source == "db"


class TestManagedVsClientOnly:
    def test_http_loopback_is_managed(self):
        ep = parse_sovits_endpoint("http://127.0.0.1:9880", source="default")
        assert ep.is_loopback is True
        assert ep.is_local is True

    def test_https_loopback_is_client_only(self):
        ep = parse_sovits_endpoint("https://127.0.0.1:9880", source="db")
        assert ep.is_loopback is True
        assert ep.is_local is False

    def test_subpath_loopback_is_client_only(self):
        ep = parse_sovits_endpoint("http://127.0.0.1:9880/subpath", source="db")
        assert ep.is_loopback is True
        assert ep.is_local is False
