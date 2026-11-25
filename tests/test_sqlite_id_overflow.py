"""Ids too big for SQLite give a plain "not found" instead of a 500."""

import httpx
import pytest

from galgame2voice.main import create_app

HUGE = 10**24


@pytest.fixture
async def client(isolate_test_database, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_RATE_LIMIT_DISABLED", "1")
    app = create_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as c:
        yield app, c


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", f"/api/voice/profiles/{HUGE}", None),
        ("POST", "/api/voice/switch", {"profile_id": HUGE}),
        ("POST", "/api/voice/switch", {"id": HUGE}),
        (
            "POST",
            "/api/voice/regenerate-line",
            {"session_id": "s1", "line_index": 0, "text": "こんにちは", "voice_profile_id": HUGE},
        ),
        ("DELETE", f"/api/characters/1/memories/{HUGE}", None),
        ("GET", f"/api/characters/{HUGE}/portrait", None),
    ],
)
async def test_huge_id_is_not_found(client, method, path, body):
    _, c = client
    resp = await c.request(method, path, json=body)
    assert resp.status_code == 404, resp.text
    assert "编号" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_other_overflow_errors_still_500(client):
    app, c = client

    @app.get("/api/_test_overflow")
    async def _boom():
        raise OverflowError("math range error")

    resp = await c.get("/api/_test_overflow")
    assert resp.status_code == 500
    assert resp.json()["error_type"] == "OverflowError"
