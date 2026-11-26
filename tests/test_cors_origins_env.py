"""GALGAME2VOICE_CORS_ORIGINS accepts the comma-separated form people usually write."""

import httpx
import pytest

from galgame2voice.config import Settings, get_settings
from galgame2voice.main import create_app


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://192.168.1.5:8080,http://localhost:8080", ["http://192.168.1.5:8080", "http://localhost:8080"]),
        ("http://192.168.1.5:8080", ["http://192.168.1.5:8080"]),
        (" http://192.168.1.5:8080/ , ", ["http://192.168.1.5:8080"]),
        ("http://192.168.1.5:8080, / ", ["http://192.168.1.5:8080"]),
        ('["http://192.168.1.5:8080"]', ["http://192.168.1.5:8080"]),
        ('[" http://192.168.1.5:8080/ ", " / ", ""]', ["http://192.168.1.5:8080"]),
    ],
)
def test_cors_origins_from_env(monkeypatch, raw, expected):
    monkeypatch.setenv("GALGAME2VOICE_CORS_ORIGINS", raw)
    assert Settings(_env_file=None).cors_origins == expected


def test_default_cors_origins_unchanged(monkeypatch):
    monkeypatch.delenv("GALGAME2VOICE_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert Settings(_env_file=None).cors_origins == ["http://127.0.0.1:8080", "http://localhost:8080"]


@pytest.mark.asyncio
async def test_cors_allows_only_current_loopback_port_and_explicit_origins(monkeypatch):
    async def _acao(origin: str, port: int = 8080) -> str | None:
        get_settings.cache_clear()
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"http://127.0.0.1:{port}"
        ) as client:
            res = await client.get("/api/health", headers={"Origin": origin})
        return res.headers.get("access-control-allow-origin")

    monkeypatch.delenv("GALGAME2VOICE_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    try:
        assert await _acao("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
        assert await _acao("http://127.0.0.1:5555") is None
        assert await _acao("http://localhost:5555") is None

        monkeypatch.setenv("GALGAME2VOICE_PORT", "8081")
        assert await _acao("http://127.0.0.1:8081", 8081) == "http://127.0.0.1:8081"
        assert await _acao("http://[::1]:8081", 8081) == "http://[::1]:8081"
        assert await _acao("http://127.0.0.1:8080", 8081) is None

        monkeypatch.setenv("GALGAME2VOICE_CORS_ORIGINS", "http://192.168.1.5:8080")
        assert await _acao("http://192.168.1.5:8080", 8081) == "http://192.168.1.5:8080"
        assert await _acao("http://127.0.0.1:5555", 8081) is None
    finally:
        get_settings.cache_clear()
