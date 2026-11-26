"""Cross-site mutating /api/ requests from web pages are rejected with 403."""

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.main import create_app

DENIED_DETAIL = "已拒绝来自其他网页的请求。"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
    ],
)
async def test_cross_site_write_rejected_without_running_route(headers):
    sentinel = get_settings().audio_dir / "cache" / "line.wav"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b"RIFF")
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8080") as client:
        resp = await client.post("/api/cache/clear", headers=headers)
    assert resp.status_code == 403 and resp.json()["detail"] == DENIED_DETAIL
    assert sentinel.is_file()


@pytest.mark.asyncio
async def test_same_origin_cors_none_and_get_requests_allowed(monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_CORS_ORIGINS", "https://trusted.example")
    get_settings.cache_clear()
    sentinel = get_settings().audio_dir / "cache" / "line.wav"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8080") as client:
        assert (await client.get("/api/health", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 200
        for headers in (
            {"Origin": "https://127.0.0.1:8080"},
            {"Origin": "https://trusted.example"},
            {"Sec-Fetch-Site": "same-origin", "Origin": "https://proxy.example"},
            {"Sec-Fetch-Site": "none"},
            {},
        ):
            sentinel.write_bytes(b"RIFF")
            resp = await client.post("/api/cache/clear", headers=headers)
            assert resp.status_code == 200 and not sentinel.exists()
