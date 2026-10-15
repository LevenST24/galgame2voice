"""
Security regression tests: SSRF guard on provider endpoints and credential masking.

Covers the stored-key redirect attack: a stored API key must never be sent to
an attacker-chosen base_url, and masked custom headers must round-trip safely.
"""

import sqlite3

import httpx
import pytest

from galgame2voice.main import create_app


@pytest.fixture
async def client(isolate_test_database):
    conn = sqlite3.connect(isolate_test_database)
    conn.execute("INSERT OR IGNORE INTO settings (id, console_token) VALUES (1, 'test_console_token');")
    conn.commit()
    conn.close()
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_provider(isolate_test_database):
    import aiosqlite
    async with aiosqlite.connect(isolate_test_database) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO providers (id, name, api_base_url, api_key, chat_model, is_active, custom_headers) "
            "VALUES ('deepseek', 'DeepSeek', 'https://api.deepseek.com', 'sk-live-stored-secret-key-9988', 'deepseek-chat', 1, '{}');"
        )
        await conn.commit()


class TestSsrfGuard:
    @pytest.mark.asyncio
    async def test_stored_key_cannot_probe_metadata(self, client, isolate_test_database):
        await _seed_provider(isolate_test_database)
        # No explicit api_key -> stored key would be used; base_url must be rejected
        resp = await client.post("/api/providers/test", json={
            "id": "deepseek",
            "base_url": "http://169.254.169.254/latest/meta-data/",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_explicit_key_cannot_probe_private_ranges(self, client):
        resp = await client.post("/api/providers/test", json={
            "id": "openai",
            "api_key": "sk-my-own-real-key-123456",
            "base_url": "http://10.1.2.3/v1",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_loopback_rejected_by_default(self, client):
        resp = await client.post("/api/providers/test", json={
            "id": "custom",
            "api_key": "dummy",
            "base_url": "http://127.0.0.1:8080/v1",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_allow_private_permits_loopback(self, client):
        await client.post("/api/config", json={"allow_private_llm_endpoints": True})
        resp = await client.post("/api/providers/test", json={
            "id": "custom",
            "api_key": "dummy",
            "base_url": "http://127.0.0.1:59998/v1",
        })
        # Guard passes; the request itself fails to connect (dead port) -> 200 + success=False
        assert resp.status_code == 200
        assert resp.json()["success"] is False

    @pytest.mark.asyncio
    async def test_provider_upsert_rejects_private_url(self, client):
        resp = await client.post("/api/providers", json={
            "id": "evil_provider",
            "name": "Evil",
            "api_base_url": "http://192.168.0.100/v1",
            "api_key": "sk-key-1234567890",
            "chat_model": "m",
        })
        assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "stream", "models"])
async def test_saved_private_permission_reaches_real_chat_adapter_and_can_be_revoked(
    client, isolate_test_database, monkeypatch, mode,
):
    from galgame2voice.adapters.base import ChatMessage
    from galgame2voice.adapters.llm import openai_adapter
    from galgame2voice.services.chat_service import ChatService

    assert (await client.post("/api/config", json={"allow_private_llm_endpoints": True})).status_code == 200
    configured = await client.post("/api/providers", json={
        "id": "custom", "name": "Local acceptance model", "api_base_url": "http://127.0.0.1:11434/v1",
        "chat_model": "local-fixture", "is_active": True,
    })
    assert configured.status_code == 200
    outbound = []

    def upstream(request):
        outbound.append(request)
        if mode == "models":
            return httpx.Response(200, json={"data": [{"id": "local reply"}]})
        if mode == "stream":
            data = 'data: {"choices":[{"delta":{"content":"local reply"}}]}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, text=data, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "local reply"}}]})

    monkeypatch.setattr(openai_adapter, "create_async_client", lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
    ))
    service = ChatService(db_path=isolate_test_database)
    messages = [ChatMessage(role="user", content="Local model acceptance")]

    async def complete(adapter, model):
        if mode == "models":
            return (await adapter.list_models())[0]
        if mode == "stream":
            return "".join([chunk async for chunk in adapter.stream_chat(messages, model=model)])
        return (await adapter.chat(messages, model=model)).content

    try:
        adapter, model, _ = await service.get_active_llm_adapter(provider_id="custom")
        assert await complete(adapter, model) == "local reply"
        assert len(outbound) == 1

        # Reuse the service, just as the web/Telegram chat paths do after a setting change.
        revoked = await client.post("/api/config", json={"allow_private_llm_endpoints": False})
        assert revoked.status_code == 200
        adapter, model, _ = await service.get_active_llm_adapter(provider_id="custom")
        with pytest.raises(PermissionError, match="阻止"):
            await complete(adapter, model)
        assert len(outbound) == 1, "revoking permission must block the next outbound request"
        listing = await client.get("/api/providers/custom/models")
        assert listing.status_code == 400
        assert len(outbound) == 1
    finally:
        await service.aclose()


class TestCredentialMasking:
    @pytest.mark.asyncio
    async def test_active_provider_masks_custom_headers(self, client, isolate_test_database):
        import aiosqlite
        async with aiosqlite.connect(isolate_test_database) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO providers (id, name, api_base_url, api_key, chat_model, is_active, custom_headers) "
                "VALUES ('p1', 'P1', 'https://api.deepseek.com', 'sk-live-secret-998877', 'm', 1, "
                "'{\"X-Auth-Token\": \"super-secret-value-123456\", \"X-Trace-Id\": \"trace-abc\"}');"
            )
            await conn.commit()

        resp = await client.get("/api/config")
        assert resp.status_code == 200
        active = resp.json()["active_provider"]
        assert active["api_key"].startswith("sk-****")
        assert active["custom_headers"]["X-Auth-Token"].endswith("****") or "****" in active["custom_headers"]["X-Auth-Token"]
        assert "super-secret-value" not in resp.text
        assert active["custom_headers"]["X-Trace-Id"] == "trace-abc"

    @pytest.mark.asyncio
    async def test_masked_header_roundtrip_preserves_secret(self, client, isolate_test_database):
        import aiosqlite
        async with aiosqlite.connect(isolate_test_database) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO providers (id, name, api_base_url, api_key, chat_model, is_active, custom_headers) "
                "VALUES ('p2', 'P2', 'https://api.deepseek.com', '', 'm', 0, "
                "'{\"X-Auth-Token\": \"real-secret-value-777\", \"X-Note\": \"hello\"}');"
            )
            await conn.commit()

        # UI echoes back the masked header plus edits a plain one
        resp = await client.post("/api/providers", json={
            "id": "p2",
            "custom_headers": {
                "X-Auth-Token": "real****-777",
                "X-Note": "changed",
            },
        })
        assert resp.status_code == 200

        async with aiosqlite.connect(isolate_test_database) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT custom_headers FROM providers WHERE id = 'p2';")
            row = await cursor.fetchone()
        import json as _json
        stored = _json.loads(row["custom_headers"])
        assert stored["X-Auth-Token"] == "real-secret-value-777"
        assert stored["X-Note"] == "changed"
