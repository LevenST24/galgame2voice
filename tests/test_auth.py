"""
Security tests: console token authentication on API routes.
Re-enables authentication (the global conftest autouse fixture disables it)
and exercises the 401/200 paths, token sources, and docs gating.
"""

import os
import tempfile

import httpx
import pytest
from fastapi import FastAPI

from galgame2voice.main import create_app
from galgame2voice.database.session import init_db
from galgame2voice.security.auth import require_auth


TEST_TOKEN = "test_console_token"


@pytest.fixture
def auth_enabled(monkeypatch):
    """Removes the global auth kill-switch for the duration of a test."""
    monkeypatch.delenv("GALGAME2VOICE_AUTH_DISABLED", raising=False)
    yield


@pytest.fixture
async def app_client(auth_enabled, isolate_test_database):
    import sqlite3
    conn = sqlite3.connect(isolate_test_database)
    conn.execute("INSERT OR IGNORE INTO settings (id, console_token) VALUES (1, ?);", (TEST_TOKEN,))
    conn.commit()
    conn.close()

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestConsoleTokenAuth:
    @pytest.mark.asyncio
    async def test_missing_token_returns_401(self, app_client):
        resp = await app_client.get("/api/config")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_token_returns_401(self, app_client):
        resp = await app_client.get("/api/config", headers={"Authorization": "Bearer wrong-token"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_correct_bearer_token_returns_200(self, app_client):
        resp = await app_client.get("/api/config", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_x_console_token_header_accepted(self, app_client):
        resp = await app_client.get("/api/config", headers={"X-Console-Token": TEST_TOKEN})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_health_endpoint_stays_open(self, app_client):
        resp = await app_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_system_status_requires_auth(self, app_client):
        resp = await app_client.get("/api/system/status")
        assert resp.status_code == 401
        resp = await app_client.get("/api/system/status", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_chat_memory_affection_metrics_protected(self, app_client):
        for method, path, kwargs in [
            ("post", "/api/chat", {"json": {"prompt": "hi"}}),
            ("get", "/api/memory", {}),
            ("get", "/api/affection", {}),
            ("get", "/api/metrics/overview", {}),
            ("post", "/api/cache/clear", {}),
            ("get", "/api/chat/history", {}),
            ("delete", "/api/chat/history", {}),
            ("get", "/api/voice/profiles", {}),
        ]:
            resp = await app_client.request(method, path, **kwargs)
            assert resp.status_code == 401, f"{method.upper()} {path} should require auth"

    @pytest.mark.asyncio
    async def test_docs_disabled_by_default(self, app_client):
        assert (await app_client.get("/docs")).status_code in (404, 405)
        assert (await app_client.get("/redoc")).status_code in (404, 405)


class TestTokenSources:
    @pytest.mark.asyncio
    async def test_env_token_overrides_db(self, auth_enabled, isolate_test_database, monkeypatch):
        import sqlite3
        conn = sqlite3.connect(isolate_test_database)
        conn.execute("INSERT OR IGNORE INTO settings (id, console_token) VALUES (1, ?);", (TEST_TOKEN,))
        conn.commit()
        conn.close()

        monkeypatch.setenv("GALGAME2VOICE_CONSOLE_TOKEN", "env-secret-token")
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/config", headers={"Authorization": "Bearer env-secret-token"})
            assert resp.status_code == 200
            resp = await client.get("/api/config", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
            assert resp.status_code == 401


class TestTokenSeeding:
    @pytest.mark.asyncio
    async def test_empty_console_token_regenerated_on_init(self, monkeypatch):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="test_token_seed_")
        os.close(fd)
        try:
            await init_db(path)
            import aiosqlite
            async with aiosqlite.connect(path) as conn:
                cursor = await conn.execute("SELECT console_token FROM settings WHERE id = 1;")
                row = await cursor.fetchone()
                token_first = row[0]
                assert token_first, "init_db must seed a console token"

                # Simulate a legacy DB with a blank token, then re-run init
                await conn.execute("UPDATE settings SET console_token = '' WHERE id = 1;")
                await conn.commit()

            await init_db(path)
            async with aiosqlite.connect(path) as conn:
                cursor = await conn.execute("SELECT console_token FROM settings WHERE id = 1;")
                row = await cursor.fetchone()
                assert row[0], "blank console token must be regenerated"
        finally:
            if os.path.exists(path):
                os.remove(path)
