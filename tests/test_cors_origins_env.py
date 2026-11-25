"""GALGAME2VOICE_CORS_ORIGINS accepts the comma-separated form people usually write."""

import pytest

from galgame2voice.config import Settings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://192.168.1.5:8080,http://localhost:8080", ["http://192.168.1.5:8080", "http://localhost:8080"]),
        ("http://192.168.1.5:8080", ["http://192.168.1.5:8080"]),
        (" http://192.168.1.5:8080/ , ", ["http://192.168.1.5:8080"]),
        ('["http://192.168.1.5:8080"]', ["http://192.168.1.5:8080"]),
    ],
)
def test_cors_origins_from_env(monkeypatch, raw, expected):
    monkeypatch.setenv("GALGAME2VOICE_CORS_ORIGINS", raw)
    assert Settings().cors_origins == expected


def test_default_cors_origins_unchanged(monkeypatch):
    monkeypatch.delenv("GALGAME2VOICE_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert Settings().cors_origins == ["http://127.0.0.1:8080", "http://localhost:8080"]
