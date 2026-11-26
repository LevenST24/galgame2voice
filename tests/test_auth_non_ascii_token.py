"""A mistyped token with a non-ASCII character is rejected with 401, never a 500."""

import sqlite3

import httpx
import pytest

from galgame2voice.main import create_app
from galgame2voice.security.media_urls import verify_media_url_signature

DB_TOKEN = "db_console_token"
# A browser can send Latin-1 characters in a header; a full-width token cannot be sent at all.
BAD_TOKEN_HEADER = "Bearer café".encode("latin-1")


async def _get_config(app, headers):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/api/config", headers=headers)


@pytest.mark.asyncio
async def test_env_token_rejects_non_ascii_with_401(monkeypatch, isolate_test_database):
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "0")
    monkeypatch.setenv("GALGAME2VOICE_CONSOLE_TOKEN", "env_token")
    resp = await _get_config(create_app(), {"Authorization": BAD_TOKEN_HEADER})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stored_token_rejects_non_ascii_with_401(monkeypatch, isolate_test_database):
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "0")
    monkeypatch.delenv("GALGAME2VOICE_CONSOLE_TOKEN", raising=False)
    conn = sqlite3.connect(isolate_test_database)
    conn.execute(
        "INSERT INTO settings (id, console_token) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET console_token = excluded.console_token;",
        (DB_TOKEN,),
    )
    conn.commit()
    conn.close()
    app = create_app()
    resp = await _get_config(app, {"Authorization": BAD_TOKEN_HEADER})
    assert resp.status_code == 401
    resp = await _get_config(app, {"Authorization": f"Bearer {DB_TOKEN}"})
    assert resp.status_code == 200


def test_media_signature_with_non_ascii_is_invalid_not_an_error():
    assert verify_media_url_signature("portraits/a.png", 4_000_000_000, None, "é" * 64, now=0, key=b"k") is False
