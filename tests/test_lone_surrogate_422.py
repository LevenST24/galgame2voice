"""A lone UTF-16 surrogate in a JSON string gives 422, not a 500."""

import httpx
import pytest

from galgame2voice.main import create_app

LONE = '"\\ud800"'


@pytest.fixture
async def client(isolate_test_database, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_RATE_LIMIT_DISABLED", "1")
    app = create_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as c:
        yield app, c


async def _post(c, path, raw):
    return await c.post(path, content=raw.encode(), headers={"content-type": "application/json"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "raw"),
    [
        # Invalid request: the validation error echoes the bad string back.
        ("/api/voice/profiles", '{"name": ' + LONE + ', "is_default": "x"}'),
        ("/api/voice/switch", '{"profile_name": ' + LONE + ', "force": "x"}'),
        ("/api/characters/redeem/list", '{"code": ' + LONE + '}'),
        # Valid request: the string reaches the database.
        (
            "/api/voice/profiles",
            '{"name": "a' + LONE[1:-1] + 'b", "gpt_weights_path": "a.ckpt", "sovits_weights_path": "b.pth"}',
        ),
    ],
)
async def test_lone_surrogate_is_422(client, path, raw):
    _, c = client
    resp = await _post(c, path, raw)
    assert resp.status_code == 422, resp.text
    assert "无效字符" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_paired_surrogates_still_work(client):
    _, c = client
    raw = '{"name": "\\ud83d\\ude00 voice", "gpt_weights_path": "a.ckpt", "sovits_weights_path": "b.pth"}'
    resp = await _post(c, "/api/voice/profiles", raw)
    assert resp.status_code < 300, resp.text
    assert resp.json()["name"] == "\U0001f600 voice"


@pytest.mark.asyncio
async def test_other_unicode_errors_still_500(client):
    app, c = client

    @app.get("/api/_test_ascii")
    async def _boom():
        "é".encode("ascii")

    resp = await c.get("/api/_test_ascii")
    assert resp.status_code == 500
