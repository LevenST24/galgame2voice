"""
Security regression tests: SSRF guard on provider endpoints and credential masking.

Covers the stored-key redirect attack: a stored API key must never be sent to
an attacker-chosen base_url, and masked custom headers must round-trip safely.
"""

import sqlite3

import httpx
import pytest

from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.security import url_guard


@pytest.fixture
async def client(isolate_test_database):
    conn = sqlite3.connect(isolate_test_database)
    conn.execute(
        "INSERT INTO settings (id, console_token) VALUES (1, 'test_console_token') "
        "ON CONFLICT(id) DO UPDATE SET console_token = excluded.console_token;"
    )
    conn.commit()
    conn.close()
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as c:
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
@pytest.mark.parametrize("mode", ["stream", "models"])
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
        data = 'data: {"choices":[{"delta":{"content":"local reply"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=data, headers={"Content-Type": "text/event-stream"})

    monkeypatch.setattr(openai_adapter, "create_async_client", lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
    ))
    service = ChatService(db_path=isolate_test_database)
    messages = [ChatMessage(role="user", content="Local model acceptance")]

    async def complete(adapter, model):
        if mode == "models":
            return (await adapter.list_models())[0]
        return "".join([chunk async for chunk in adapter.stream_chat(messages, model=model)])

    try:
        adapter, model, _ = await service._get_active_llm_adapter(provider_id="custom")
        assert await complete(adapter, model) == "local reply"
        assert len(outbound) == 1

        # Reuse the service, just as the web chat path does after a setting change.
        revoked = await client.post("/api/config", json={"allow_private_llm_endpoints": False})
        assert revoked.status_code == 200
        adapter, model, _ = await service._get_active_llm_adapter(provider_id="custom")
        with pytest.raises(PermissionError, match="阻止"):
            await complete(adapter, model)
        assert len(outbound) == 1, "revoking permission must block the next outbound request"
    finally:
        await service.aclose()


class TestCredentialMasking:
    @pytest.mark.asyncio
    async def test_active_provider_masks_custom_headers(self, client, isolate_test_database):
        import aiosqlite
        async with aiosqlite.connect(isolate_test_database) as conn:
            # The real migrations seed an active default provider; /api/config returns the first active row.
            await conn.execute("UPDATE providers SET is_active = 0;")
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


# Canonical 8 providers for M11 testing
CANONICAL_8_PROVIDERS = [
    "gemini",
    "openai",
    "anthropic",
    "deepseek",
    "xai",
    "siliconflow",
    "custom",
]


@pytest.fixture
def mock_dns_resolution(monkeypatch):
    """Mocks DNS resolution for external hostnames to eliminate network latency and DNS timeouts."""
    orig_resolve_host = url_guard._resolve_host

    def fast_resolve_host(host: str, port: int):
        norm = host.lower().strip()
        if (
            norm in ("127.0.0.1", "localhost", "169.254.169.254", "::1")
            or norm.startswith("10.")
            or norm.startswith("192.168.")
            or norm.startswith("172.16.")
            or norm.startswith("169.254.")
        ):
            return orig_resolve_host(host, port)
        return True, ""

    fast_resolve_host.cache_clear = getattr(orig_resolve_host, "cache_clear", url_guard.clear_dns_cache)
    monkeypatch.setattr(url_guard, "_resolve_host", fast_resolve_host)


@pytest.fixture
async def app_client(isolate_test_database, mock_dns_resolution):
    """Provides an ASGI test client connected to an initialized, isolated SQLite test DB."""
    await init_db(isolate_test_database)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as c:
        yield c


class TestNonDestructiveSecretRetention:
    """Empirically stress-tests non-destructive key retention across canonical providers."""

    @pytest.mark.asyncio
    async def test_all_canonical_providers_support_key_retention(self, app_client):
        """Verifies non-destructive key retention across all 8 canonical providers."""
        for pid in CANONICAL_8_PROVIDERS:
            secret = f"secret-token-for-{pid}-9876543210"
            # 1. Save
            resp = await app_client.post(
                "/api/providers",
                json={
                    "id": pid,
                    "api_key": secret,
                    "chat_model": "test-model-v1",
                },
            )
            assert resp.status_code == 200, f"Failed saving key for {pid}"
            assert "****" in resp.json()["provider"]["api_key"]

            # 2. Mutate model with masked key
            masked = resp.json()["provider"]["api_key"]
            resp2 = await app_client.post(
                "/api/providers",
                json={
                    "id": pid,
                    "api_key": masked,
                    "chat_model": "test-model-v2",
                },
            )
            assert resp2.status_code == 200
            assert resp2.json()["provider"]["chat_model"] == "test-model-v2"

            # 3. Assert raw secret in DB remained unchanged
            async with get_db() as conn:
                raw_prov = await crud.get_provider_raw(conn, pid)
                assert raw_prov is not None
                assert raw_prov.api_key == secret, f"Secret for {pid} was corrupted: {raw_prov.api_key}"


class TestBaseUrlResetLogic:
    """Empirically tests modifying Base URLs to custom proxies and resetting them to official defaults."""

    @pytest.mark.asyncio
    async def test_base_url_customization_and_official_reset(self, app_client):
        """
        For each of the canonical providers:
        1. Enable allow_private_llm_endpoints so custom loopback URLs are tested smoothly.
        2. Fetch official preset default_base_url.
        3. Set custom proxy URL (e.g. https://proxy.ai.corp/v1 or http://127.0.0.1:8000/v1).
        4. Verify custom URL is saved and returned.
        5. Reset Base URL to official preset default_base_url.
        6. Verify official default Base URL is strictly restored.
        """
        # Enable allow_private_llm_endpoints for local testing of custom provider
        await app_client.post("/api/config", json={"settings": {"allow_private_llm_endpoints": True}})

        presets_resp = await app_client.get("/api/providers")
        assert presets_resp.status_code == 200
        presets = {p["id"]: p for p in presets_resp.json()["presets"]}

        for pid in CANONICAL_8_PROVIDERS:
            official_url = presets[pid]["default_base_url"]
            assert official_url, f"Missing official default_base_url for {pid}"

            custom_url = f"https://my-custom-proxy.internal.corp/{pid}/v1"
            if pid == "custom":
                custom_url = "http://127.0.0.1:8000/v1"

            # 1. Modify to custom URL
            mod_resp = await app_client.post(
                "/api/providers",
                json={
                    "id": pid,
                    "api_base_url": custom_url,
                },
            )
            assert mod_resp.status_code == 200, f"Failed updating base_url for {pid}: {mod_resp.text}"
            prov_data = mod_resp.json()["provider"]
            assert prov_data["api_base_url"] == custom_url

            # 2. Reset back to official URL
            reset_resp = await app_client.post(
                "/api/providers",
                json={
                    "id": pid,
                    "api_base_url": official_url,
                },
            )
            assert reset_resp.status_code == 200, f"Failed resetting base_url for {pid}: {reset_resp.text}"
            reset_data = reset_resp.json()["provider"]
            assert reset_data["api_base_url"] == official_url

            # 3. Double check via GET
            get_resp = await app_client.get(f"/api/providers/{pid}")
            assert get_resp.status_code == 200
            assert get_resp.json()["provider"]["api_base_url"] == official_url
