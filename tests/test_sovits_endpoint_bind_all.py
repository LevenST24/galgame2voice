"""A GPT-SoVITS URL with the listen-on-all address means this computer."""

import pytest

from galgame2voice.services.sovits_endpoint import parse_sovits_endpoint, resolve_sovits_url


@pytest.mark.parametrize(
    ("url", "base_url"),
    [
        ("http://0.0.0.0:9880", "http://127.0.0.1:9880"),
        ("http://0.0.0.0:9880/", "http://127.0.0.1:9880"),
        ("http://0.0.0.0", "http://127.0.0.1:9880"),
        ("http://[::]:9880", "http://[::1]:9880"),
    ],
)
def test_bind_all_address_connects_over_loopback(url, base_url):
    endpoint = parse_sovits_endpoint(url, source="db")
    assert endpoint.base_url == base_url
    assert endpoint.is_local is True


def test_saved_bind_all_url_is_a_local_engine(monkeypatch):
    monkeypatch.delenv("GALGAME2VOICE_GPT_SOVITS_BASE_URL", raising=False)
    monkeypatch.delenv("GPT_SOVITS_BASE_URL", raising=False)
    endpoint = resolve_sovits_url(db_url="http://0.0.0.0:9881", settings_url=None)
    assert (endpoint.host, endpoint.port, endpoint.source, endpoint.is_local) == ("127.0.0.1", 9881, "db", True)


def test_other_hosts_are_unchanged():
    assert parse_sovits_endpoint("http://192.168.1.20:9880", source="db").is_local is False
    assert parse_sovits_endpoint("http://127.0.0.1:9880", source="db").base_url == "http://127.0.0.1:9880"
