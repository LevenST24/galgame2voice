"""
Automated Provider Configuration & Key Protection Tests.
Covers:
1. Universal provider parameter submission across canonical providers and dispatch aliases.
2. Strict API key masking and zero credential leakage in API responses.
3. Non-destructive masked key retention on updates.
4. In-flight connectivity credential isolation (unsaved & stored keys).
5. Provider activation flow and synchronization with GET /api/config.
6. Settings schema persistence (stt_engine).
7. Private endpoint opt-in toggle (allow_private_llm_endpoints).
8. Provider preset seeds, legacy healing, and empty chat_model fallback.
"""

import json
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.adapters.registry import PROVIDER_PRESETS
from galgame2voice.database import crud
from galgame2voice.database.models import SettingsUpdate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.security import url_guard
from galgame2voice.services.chat_service import ChatService


# ============================================================================
# Fixtures and Mock Helpers
# ============================================================================

@pytest.fixture(autouse=True)
def mock_dns_resolution(monkeypatch):
    """Mocks DNS resolution for external hostnames to eliminate network latency and DNS timeouts."""
    orig_resolve_host = url_guard._resolve_host

    def fast_resolve_host(host: str, port: int):
        norm = host.lower().strip()
        # Let real resolver handle private/loopback IPs so SSRF tests remain 100% rigorous
        if (
            norm in ("127.0.0.1", "localhost", "169.254.169.254", "::1")
            or norm.startswith("10.")
            or norm.startswith("192.168.")
            or norm.startswith("172.16.")
            or norm.startswith("169.254.")
        ):
            return orig_resolve_host(host, port)
        return True, ""

    monkeypatch.setattr(url_guard, "_resolve_host", fast_resolve_host)


@pytest.fixture
async def app_client(isolate_test_database):
    """Provides an ASGI test client connected to an initialized, isolated SQLite test DB."""
    await init_db(isolate_test_database)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        yield client


def mock_external_http_responses(matchers: dict):
    """
    Creates mock get/post patches for httpx.AsyncClient that:
    1. Forwards any request targeting 'http://127.0.0.1:8080' or relative paths to the ASGI transport.
    2. Matches external requests against matchers (url substring -> Response or callable).
    3. Defaults to a 200 OK Response so unmocked external requests never hang or timeout.
    """
    orig_get = httpx.AsyncClient.get
    orig_post = httpx.AsyncClient.post

    def _is_internal(url_str: str) -> bool:
        return (not url_str.startswith("http://") and not url_str.startswith("https://")) or url_str.startswith("http://127.0.0.1:8080")

    async def mocked_get(self_client, url, *args, **kwargs):
        url_str = str(url)
        if _is_internal(url_str):
            return await orig_get(self_client, url, *args, **kwargs)
        for pattern, resp in matchers.items():
            if pattern in url_str:
                if callable(resp):
                    return resp("GET", url_str, kwargs)
                return resp
        # Default safe mock response for external endpoints
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "mock-model-1"}, {"id": "mock-model-2"}]},
            request=httpx.Request("GET", url_str),
        )

    async def mocked_post(self_client, url, *args, **kwargs):
        url_str = str(url)
        if _is_internal(url_str):
            return await orig_post(self_client, url, *args, **kwargs)
        for pattern, resp in matchers.items():
            if pattern in url_str:
                if callable(resp):
                    return resp("POST", url_str, kwargs)
                return resp
        # Default safe mock response for external chat endpoints
        return httpx.Response(
            200,
            json={
                "id": "mock-chat-123",
                "object": "chat.completion",
                "choices": [{"message": {"role": "assistant", "content": "mock response"}}],
            },
            request=httpx.Request("POST", url_str),
        )

    return patch.object(httpx.AsyncClient, "get", mocked_get), patch.object(httpx.AsyncClient, "post", mocked_post)


# ============================================================================
# 1. Universal Provider Parameter Submission
# ============================================================================

class TestUniversalProviderParameterSubmission:
    """Verifies parameter submission and SQLite persistence across canonical providers and aliases."""

    @pytest.mark.asyncio
    async def test_update_and_persist_canonical_and_alias_providers(self, app_client, isolate_test_database):
        """Tests submitting parameters for canonical providers and dispatch aliases."""
        test_cases = [
            ("gemini", "Google Gemini Test", "https://generativelanguage.googleapis.com/v1beta/openai", "AIzaSyTestKeyGemini1234567890abcdef123", "gemini-2.5-pro", ""),
            ("openai", "OpenAI Test", "https://api.openai.com/v1", "sk-openai-live-secret-test-key-998877", "gpt-4o", "whisper-1"),
            ("anthropic", "Anthropic Claude Test", "https://api.anthropic.com/v1", "sk-ant-api03-claude-test-key-123456789", "claude-sonnet-4-20250514", ""),
            ("deepseek", "DeepSeek Test", "https://api.deepseek.com", "sk-deepseek-live-test-key-1122334455", "deepseek-reasoner", ""),
            ("xai", "xAI (Grok) Test", "https://api.x.ai/v1", "xai-test-secret-key-1234567890abcdef", "grok-3", ""),
            ("siliconflow", "SiliconFlow Test", "https://api.siliconflow.cn/v1", "sk-siliconflow-test-key-1234567890abc", "deepseek-ai/DeepSeek-V3", "FunAudioLLM/SenseVoiceSmall"),
            ("custom", "Custom Proxy Test", "https://my-custom-proxy.internal.corp/v1", "sk-custom-secret-key-998877665544", "qwen3-72b", ""),
            ("google_gemini", "Google Gemini Alias", "https://generativelanguage.googleapis.com/v1beta/openai", "AIzaSyDispatchAliasGeminiKey123456", "gemini-2.5-flash", None),
            ("anthropic_claude", "Anthropic Claude Alias", "https://api.anthropic.com/v1", "sk-ant-dispatch-alias-claude-key", "claude-haiku-4-20250414", None),
            ("__custom__", "Custom Model Alias", "https://llm-gateway.example.com/v1", "sk-custom-alias-secret-key-12345", "deepseek-r1:latest", None),
        ]

        for pid, name, base_url, api_key, chat_model, stt_model in test_cases:
            p_data = {
                "id": pid,
                "name": name,
                "api_base_url": base_url,
                "api_key": api_key,
                "chat_model": chat_model,
            }
            if stt_model is not None:
                p_data["stt_model"] = stt_model

            resp = await app_client.post("/api/providers", json=p_data)
            assert resp.status_code in (200, 201), f"Failed for {pid}: {resp.text}"
            data = resp.json()
            assert data.get("status") in ("success", "created")
            p_resp = data["provider"]
            assert p_resp["id"] == pid
            assert p_resp["chat_model"] == chat_model
            assert "****" in p_resp["api_key"]
            assert api_key not in json.dumps(data)

            async with get_db(isolate_test_database) as conn:
                raw = await crud.get_provider_raw(conn, pid)
                assert raw is not None, f"Raw record not found in DB for {pid}"
                assert raw.api_key == api_key, f"Plaintext key not stored accurately for {pid}"
                assert raw.chat_model == chat_model
                assert raw.api_base_url == base_url

    @pytest.mark.asyncio
    async def test_partial_update_preserves_other_fields(self, app_client, isolate_test_database):
        """Verifies that updating only the chat_model does not erase api_key or base_url."""
        # Setup initial provider
        await app_client.post("/api/providers", json={
            "id": "deepseek",
            "api_base_url": "https://api.deepseek.com",
            "api_key": "sk-initial-raw-secret-key-12345",
            "chat_model": "deepseek-chat",
            "stt_model": "initial-stt",
        })

        # Partial update: modify only chat_model
        resp = await app_client.post("/api/providers", json={
            "id": "deepseek",
            "chat_model": "deepseek-reasoner",
        })
        assert resp.status_code == 200

        async with get_db(isolate_test_database) as conn:
            raw = await crud.get_provider_raw(conn, "deepseek")
            assert raw.chat_model == "deepseek-reasoner"
            assert raw.api_key == "sk-initial-raw-secret-key-12345"
            assert raw.api_base_url == "https://api.deepseek.com"
            assert raw.stt_model == "initial-stt"

    @pytest.mark.asyncio
    async def test_custom_headers_submission_and_persistence(self, app_client, isolate_test_database):
        """Verifies that custom_headers are persisted and retrieved correctly."""
        headers_payload = {
            "X-Organization-Id": "org-998877",
            "X-Custom-Routing": "region-asia",
            "Authorization": "Bearer token-secret-9999",
        }
        resp = await app_client.post("/api/providers", json={
            "id": "xai",
            "custom_headers": headers_payload,
        })
        assert resp.status_code == 200

        # Verification via API: sensitive Authorization header is masked
        get_resp = await app_client.get("/api/providers/xai")
        assert get_resp.status_code == 200
        provider_data = get_resp.json()["provider"]
        assert provider_data["custom_headers"]["X-Organization-Id"] == "org-998877"
        assert "****" in provider_data["custom_headers"]["Authorization"]

        # Raw DB verification: raw secret header is preserved intact
        async with get_db(isolate_test_database) as conn:
            raw = await crud.get_provider_raw(conn, "xai")
            assert raw.custom_headers["Authorization"] == "Bearer token-secret-9999"

    @pytest.mark.asyncio
    async def test_invalid_provider_parameters_validation_errors(self, app_client):
        """Verifies HTTP 422 for invalid provider payloads (missing id, oversized id)."""
        # Missing ID
        resp = await app_client.post("/api/providers", json={"chat_model": "grok-3"})
        assert resp.status_code == 422

        # ID too long (> 64 chars)
        long_id = "a" * 65
        resp = await app_client.post("/api/providers", json={"id": long_id})
        assert resp.status_code == 422


# ============================================================================
# 2. Strict API Key Masking & Zero Leakage
# ============================================================================

class TestApiKeyMaskingAndZeroLeakage:
    """Verifies that API keys are strictly masked and never leaked in API responses."""

    @pytest.mark.asyncio
    async def test_api_key_masked_in_all_provider_get_endpoints(self, app_client, isolate_test_database):
        """GET /api/providers and GET /api/providers/{id} must return masked keys only."""
        raw_key = "xai-super-secret-production-key-998877"
        await app_client.post("/api/providers", json={
            "id": "xai",
            "api_key": raw_key,
        })

        # Test GET /api/providers/{id}
        resp_single = await app_client.get("/api/providers/xai")
        assert resp_single.status_code == 200
        body_single = resp_single.text
        assert raw_key not in body_single
        data_single = resp_single.json()["provider"]
        assert data_single["api_key"] == f"xai****{raw_key[-4:]}"

        # Test GET /api/providers
        resp_list = await app_client.get("/api/providers")
        assert resp_list.status_code == 200
        body_list = resp_list.text
        assert raw_key not in body_list
        providers = resp_list.json()["providers"]
        xai_p = next(p for p in providers if p["id"] == "xai")
        assert xai_p["api_key"] == f"xai****{raw_key[-4:]}"

    @pytest.mark.asyncio
    async def test_api_key_masked_in_config_endpoint(self, app_client):
        """GET /api/config returns masked key for active_provider."""
        raw_llm_key = "sk-live-llm-secret-key-11223344"

        # Save provider with raw key and activate it
        await app_client.post("/api/providers", json={
            "id": "openai",
            "api_key": raw_llm_key,
            "is_active": True,
        })

        # GET /api/config
        resp = await app_client.get("/api/config")
        assert resp.status_code == 200
        body = resp.text

        assert raw_llm_key not in body

        data = resp.json()
        assert data["active_provider"]["api_key"].startswith("sk-****")

    @pytest.mark.asyncio
    async def test_error_responses_contain_no_raw_api_key(self, app_client):
        """HTTP error responses on invalid requests must never echo the raw API key."""
        raw_secret = "sk-do-not-leak-this-secret-key-776655"
        # Cause SSRF error by probing private IP
        resp = await app_client.post("/api/providers/test", json={
            "id": "openai",
            "api_key": raw_secret,
            "base_url": "http://10.254.1.1/v1",
        })
        assert resp.status_code == 400
        assert raw_secret not in resp.text


# ============================================================================
# 3. Non-Destructive Masked Key Retention
# ============================================================================

class TestMaskedKeyRetentionAndNonDestructiveRoundtrip:
    """Verifies that submitting masked keys or omitting keys preserves stored secrets."""

    @pytest.mark.asyncio
    async def test_submitting_masked_key_retains_stored_secret(self, app_client, isolate_test_database):
        """Submitting 'xai****8877' must preserve the underlying raw secret in SQLite."""
        raw_secret = "xai-original-secret-key-alpha-998877"
        await app_client.post("/api/providers", json={
            "id": "xai",
            "api_key": raw_secret,
            "chat_model": "grok-3",
        })

        # Simulate UI roundtrip: submit masked key with an updated model
        resp = await app_client.post("/api/providers", json={
            "id": "xai",
            "api_key": "xai****8877",
            "chat_model": "grok-3-mini",
        })
        assert resp.status_code == 200

        async with get_db(isolate_test_database) as conn:
            raw = await crud.get_provider_raw(conn, "xai")
            assert raw.api_key == raw_secret
            assert raw.chat_model == "grok-3-mini"

    @pytest.mark.asyncio
    async def test_submitting_empty_or_whitespace_key_retains_stored_secret(self, app_client, isolate_test_database):
        """Submitting empty string or whitespace does not overwrite the stored secret."""
        raw_secret = "sk-original-moonshot-secret-key-12345"
        await app_client.post("/api/providers", json={
            "id": "moonshot",
            "api_key": raw_secret,
        })

        # Submit empty key
        resp = await app_client.post("/api/providers", json={
            "id": "moonshot",
            "api_key": "   ",
            "chat_model": "moonshot-v1-32k",
        })
        assert resp.status_code == 200

        async with get_db(isolate_test_database) as conn:
            raw = await crud.get_provider_raw(conn, "moonshot")
            assert raw.api_key == raw_secret
            assert raw.chat_model == "moonshot-v1-32k"

    @pytest.mark.asyncio
    async def test_submitting_new_raw_key_overwrites_secret(self, app_client, isolate_test_database):
        """Submitting a fresh unmasked key successfully overwrites the stored key."""
        old_secret = "sk-old-secret-key-111111"
        new_secret = "sk-new-fresh-secret-key-222222"

        await app_client.post("/api/providers", json={"id": "deepseek", "api_key": old_secret})
        await app_client.post("/api/providers", json={"id": "deepseek", "api_key": new_secret})

        async with get_db(isolate_test_database) as conn:
            raw = await crud.get_provider_raw(conn, "deepseek")
            assert raw.api_key == new_secret

    @pytest.mark.asyncio
    async def test_custom_headers_masked_retention(self, app_client, isolate_test_database):
        """Masked values in custom_headers (e.g. 'Bearer ****') do not overwrite stored secrets."""
        await app_client.post("/api/providers", json={
            "id": "custom",
            "custom_headers": {"Authorization": "Bearer real-secret-token-xyz"},
        })

        # Submit back masked header
        await app_client.post("/api/providers", json={
            "id": "custom",
            "custom_headers": {"Authorization": "Bearer ****-xyz", "X-App": "gal2voice"},
        })

        async with get_db(isolate_test_database) as conn:
            raw = await crud.get_provider_raw(conn, "custom")
            assert raw.custom_headers["Authorization"] == "Bearer real-secret-token-xyz"
            assert raw.custom_headers["X-App"] == "gal2voice"


# ============================================================================
# 4. In-Flight Connectivity Testing (Unsaved & Stored Credentials)
# ============================================================================

class TestInFlightConnectivityTestingAndDiagnostics:
    """Verifies that in-flight testing works with unsaved or stored keys without mutating database state."""

    @pytest.mark.asyncio
    async def test_in_flight_test_with_explicit_credentials_does_not_persist(self, app_client, isolate_test_database):
        """Testing freshly entered credentials must not save or overwrite database state."""
        mock_resp = httpx.Response(
            200,
            json={"data": [{"id": "gpt-4o"}]},
            request=httpx.Request("GET", "https://api.openai.com/v1/models"),
        )
        p_get, p_post = mock_external_http_responses({"api.openai.com": mock_resp})

        with p_get, p_post:
            resp = await app_client.post("/api/providers/test", json={
                "id": "openai",
                "api_key": "sk-in-flight-unsaved-test-key-9999",
                "chat_model": "gpt-4o",
            })
            assert resp.status_code == 200
            assert resp.json()["success"] is True
            assert "gpt-4o" in resp.json()["models"]

        # Database must still have original state
        async with get_db(isolate_test_database) as conn:
            raw = await crud.get_provider_raw(conn, "openai")
            assert raw.api_key != "sk-in-flight-unsaved-test-key-9999"

    @pytest.mark.asyncio
    async def test_in_flight_test_with_stored_key_when_masked_or_omitted(self, app_client):
        """When api_key is omitted or masked in test request, it uses the stored key."""
        stored_key = "sk-live-stored-openai-key-554433"
        await app_client.post("/api/providers", json={
            "id": "openai",
            "api_key": stored_key,
            "api_base_url": "https://api.openai.com/v1",
        })

        intercepted_key = None

        def check_auth(method, url, kwargs):
            nonlocal intercepted_key
            headers = kwargs.get("headers", {})
            intercepted_key = headers.get("Authorization")
            return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]}, request=httpx.Request(method, url))

        p_get, p_post = mock_external_http_responses({"api.openai.com": check_auth})

        with p_get, p_post:
            resp = await app_client.post("/api/providers/test", json={
                "id": "openai",
                "api_key": "sk-****4433",
            })
            assert resp.status_code == 200
            assert resp.json()["success"] is True
            assert intercepted_key == f"Bearer {stored_key}"


# ============================================================================
# 5. Provider Activation Flow
# ============================================================================

class TestProviderActivationFlow:
    """Verifies activating providers via REST endpoints and synchronization with global config."""

    @pytest.mark.asyncio
    async def test_activate_provider_sets_is_active_flag_exclusively(self, app_client, isolate_test_database):
        """Activating a provider sets is_active = 1 for that provider and 0 for all others."""
        resp = await app_client.post("/api/providers/deepseek/activate")
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

        async with get_db(isolate_test_database) as conn:
            cursor = await conn.execute("SELECT id, is_active FROM providers WHERE is_active = 1;")
            active_rows = await cursor.fetchall()
            assert len(active_rows) == 1
            assert active_rows[0][0] == "deepseek"

        # Switch to xAI
        resp_xai = await app_client.post("/api/providers/xai/activate")
        assert resp_xai.status_code == 200

        async with get_db(isolate_test_database) as conn:
            cursor = await conn.execute("SELECT id, is_active FROM providers WHERE is_active = 1;")
            active_rows = await cursor.fetchall()
            assert len(active_rows) == 1
            assert active_rows[0][0] == "xai"

    @pytest.mark.asyncio
    async def test_activate_provider_updates_config_active_provider(self, app_client):
        """POST /api/providers/{id}/activate immediately reflects in GET /api/config."""
        await app_client.post("/api/providers/xai/activate")

        resp = await app_client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_provider"]["id"] == "xai"
        assert data["settings"]["active_provider_id"] == "xai"

    @pytest.mark.asyncio
    async def test_activate_synthesized_preset_creates_db_record(self, app_client, isolate_test_database):
        """Activating a synthesized preset (e.g. moonshot before save) creates DB record and activates it."""
        async with get_db(isolate_test_database) as conn:
            await conn.execute("DELETE FROM providers WHERE id = 'moonshot';")
            await conn.commit()

        resp = await app_client.post("/api/providers/moonshot/activate")
        assert resp.status_code == 200

        async with get_db(isolate_test_database) as conn:
            raw = await crud.get_provider_raw(conn, "moonshot")
            assert raw is not None
            assert raw.is_active is True

        cfg_resp = await app_client.get("/api/config")
        assert cfg_resp.json()["active_provider"]["id"] == "moonshot"

    @pytest.mark.asyncio
    async def test_config_post_active_provider_id_syncs_providers_table(self, app_client, isolate_test_database):
        """Updating active_provider_id via POST /api/config synchronizes is_active in providers table."""
        resp = await app_client.post("/api/config", json={
            "settings": {"active_provider_id": "gemini"},
        })
        assert resp.status_code == 200

        async with get_db(isolate_test_database) as conn:
            cursor = await conn.execute("SELECT id FROM providers WHERE is_active = 1;")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "gemini"

    @pytest.mark.asyncio
    async def test_activate_unknown_provider_returns_404(self, app_client):
        """Attempting to activate an unknown provider with no preset returns 404."""
        resp = await app_client.post("/api/providers/completely_fictional_provider_123/activate")
        assert resp.status_code == 404


# ============================================================================
# 6. Settings Schema Verification (stt_engine)
# ============================================================================

class TestSettingsSchemaPersistence:
    """Verifies round-trip persistence for settings."""

    @pytest.mark.asyncio
    async def test_stt_engine_persistence_sensevoice_and_whisper(self, app_client, isolate_test_database):
        """Tests that stt_engine persists when updated to 'sensevoice' or 'whisper'."""
        # Initial check
        resp = await app_client.get("/api/config")
        assert resp.json()["settings"]["stt_engine"] == "browser"

        # Update to sensevoice
        up1 = await app_client.post("/api/config", json={"stt_engine": "sensevoice"})
        assert up1.status_code == 200

        resp1 = await app_client.get("/api/config")
        assert resp1.json()["settings"]["stt_engine"] == "sensevoice"

        # Update to whisper via nested settings dict
        up2 = await app_client.post("/api/config", json={"settings": {"stt_engine": "whisper"}})
        assert up2.status_code == 200

        resp2 = await app_client.get("/api/config")
        assert resp2.json()["settings"]["stt_engine"] == "whisper"

        # Verify raw SQLite storage
        async with get_db(isolate_test_database) as conn:
            settings = await crud.get_settings(conn)
            assert settings.stt_engine == "whisper"


# ============================================================================
# 7. Provider Security & SSRF Boundaries
# ============================================================================

class TestProviderSecurityAndSsrfBoundaries:
    """Verifies that allow_private_llm_endpoints permits local loopback when explicitly enabled."""

    @pytest.mark.asyncio
    async def test_allow_private_endpoints_toggle_permits_loopback(self, app_client):
        """When allow_private_llm_endpoints is True, local loopback (Ollama/vLLM) is permitted."""
        await app_client.post("/api/config", json={
            "settings": {"allow_private_llm_endpoints": True},
        })

        mock_resp = httpx.Response(
            200,
            json={"data": [{"id": "deepseek-r1:latest"}]},
            request=httpx.Request("GET", "http://127.0.0.1:11434/v1/models"),
        )
        p_get, p_post = mock_external_http_responses({"127.0.0.1:11434": mock_resp})

        with p_get, p_post:
            resp = await app_client.post("/api/providers/test", json={
                "id": "custom",
                "api_key": "sk-local-ollama-key",
                "base_url": "http://127.0.0.1:11434/v1",
            })
            assert resp.status_code == 200
            assert resp.json()["success"] is True


class TestProviderPresetSeedsAndFallback:
    """Regression tests for provider preset seeds, legacy healing, and empty chat_model fallback."""

    FICTIONAL_MODELS = {
        "gemini-3.7-flash",
        "gpt-5.6-sol",
        "deepseek-v4-pro",
        "claude-5-sonnet-latest",
        "glm-5.3",
        "qwen3.8-max",
        "deepseek-v4:latest",
    }

    @pytest.mark.asyncio
    async def test_initial_db_seeds_all_10_presets_without_fictional_models(self, tmp_path):
        """Fresh database must seed all 10 presets with real default models, no fictional models."""
        db_path = tmp_path / "fresh.db"
        await init_db(db_path)

        async with get_db(db_path) as conn:
            providers = await crud.list_providers(conn, mask=False)
            provider_map = {p.id: p for p in providers}

            # 1. All 10 presets must exist
            for preset_id in PROVIDER_PRESETS:
                assert preset_id in provider_map, f"Preset '{preset_id}' missing from freshly seeded providers"

            # 2. None of the providers may use fictional model names
            for p_id, p in provider_map.items():
                assert p.chat_model not in self.FICTIONAL_MODELS, (
                    f"Provider '{p_id}' has fictional chat_model '{p.chat_model}'"
                )

            # 3. Built-in presets must match PROVIDER_PRESETS default_chat_model
            for preset_id, preset in PROVIDER_PRESETS.items():
                expected_model = preset["default_chat_model"]
                assert provider_map[preset_id].chat_model == expected_model, (
                    f"Provider '{preset_id}' chat_model is '{provider_map[preset_id].chat_model}', "
                    f"expected '{expected_model}'"
                )

    @pytest.mark.asyncio
    async def test_legacy_db_seed_healing_and_user_customization_preserved(self, tmp_path):
        """Old DB missing siliconflow/moonshot is seeded; saved chat models stay untouched across restarts."""
        db_path = tmp_path / "legacy.db"
        await init_db(db_path)

        saved_models = {
            "gemini": "gemini-3.7-flash",
            "openai": "gpt-5.6-sol",
            "deepseek": "deepseek-v4-pro",
            "anthropic": "claude-5-sonnet-latest",
            "glm": "glm-5.3",
            "qwen": "qwen3.8-max",
            "custom": "deepseek-v4:latest",
        }
        async with get_db(db_path) as conn:
            await conn.execute("DELETE FROM providers WHERE id IN ('siliconflow', 'moonshot');")
            for pid, model in saved_models.items():
                await conn.execute("UPDATE providers SET chat_model = ? WHERE id = ?;", (model, pid))
            await conn.commit()

            from galgame2voice.database.migrations import init_schema_and_seeds
            for _ in range(2):
                await init_schema_and_seeds(conn)

            provs = {p.id: p for p in await crud.list_providers(conn, mask=False)}
            for pid in ("siliconflow", "moonshot"):
                assert provs[pid].is_active == 0
                assert provs[pid].chat_model == PROVIDER_PRESETS[pid]["default_chat_model"]
            for pid, model in saved_models.items():
                assert provs[pid].chat_model == model

    @pytest.mark.asyncio
    async def test_update_settings_active_provider_unseeded_preset(self, tmp_path):
        """Updating active_provider_id to an unseeded preset creates and activates it safely."""
        db_path = tmp_path / "settings_prov.db"
        await init_db(db_path)

        async with get_db(db_path) as conn:
            # Force remove siliconflow to simulate unseeded state
            await conn.execute("DELETE FROM providers WHERE id = 'siliconflow';")
            await conn.commit()

            # Now update settings to activate siliconflow (testing case-insensitivity as well)
            updated_settings = await crud.update_settings(conn, SettingsUpdate(active_provider_id="SiliconFlow"))

            # Must not be None!
            active_raw = await crud.get_active_provider_raw(conn)
            assert active_raw is not None
            assert active_raw.id == "siliconflow"
            assert active_raw.is_active == 1
            assert updated_settings.active_provider_id == "siliconflow"

    @pytest.mark.asyncio
    async def test_retired_anthropic_models_migrate_to_current_claude(self, tmp_path):
        """Retired Anthropic models migrate to Claude 5.5 with reasoning_effort='none'."""
        from galgame2voice.database.models import ProviderUpdate

        cases = [
            ("claude-sonnet-4-20250514", "claude-sonnet-5-5"),
            ("claude-haiku-4-20250414", "claude-haiku-5-5"),
            ("claude-opus-4-20250514", "claude-opus-5-5"),
        ]
        for idx, (old_model, expected_model) in enumerate(cases):
            db_path = tmp_path / f"ant_migrate_{idx}.db"
            await init_db(db_path)
            async with get_db(db_path) as conn:
                upd = ProviderUpdate(api_key="sk-ant-saved-1234", chat_model=old_model, reasoning_effort=None)
                await crud.update_provider(conn, "anthropic", upd)
            await init_db(db_path)
            async with get_db(db_path) as conn:
                raw = await crud.get_provider_raw(conn, "anthropic")
                assert raw.api_key == "sk-ant-saved-1234"
                assert (raw.chat_model, raw.reasoning_effort) == (expected_model, "none")

    @pytest.mark.asyncio
    async def test_retired_deepseek_models_migrate_preserving_keys_and_custom_models(self, tmp_path):
        """Retired deepseek models migrate to deepseek-flash while preserving keys and custom names."""
        from galgame2voice.database.models import ProviderUpdate

        cases = [
            ("deepseek-chat", None, "deepseek-flash", "none"),
            ("deepseek-reasoner", "low", "deepseek-flash", "low"),
            ("my-proxy-model", "high", "my-proxy-model", "high"),
        ]
        sql = "SELECT api_key FROM providers WHERE id = 'deepseek';"
        for idx, (old_model, old_effort, expected_model, expected_effort) in enumerate(cases):
            db_path = tmp_path / f"migrate_{idx}.db"
            await init_db(db_path)
            async with get_db(db_path) as conn:
                upd = ProviderUpdate(api_key="sk-ds-saved-9876", chat_model=old_model, reasoning_effort=old_effort)
                await crud.update_provider(conn, "deepseek", upd)
                row_before = await (await conn.execute(sql)).fetchone()
            await init_db(db_path)
            async with get_db(db_path) as conn:
                row_after = await (await conn.execute(sql)).fetchone()
                raw = await crud.get_provider_raw(conn, "deepseek")
                assert row_after[0] == row_before[0]
                assert raw.api_key == "sk-ds-saved-9876" and raw.is_active is True
                assert (raw.chat_model, raw.reasoning_effort) == (expected_model, expected_effort)

    @pytest.mark.asyncio
    async def test_empty_chat_model_falls_back_to_preset_default_not_gpt4omini(self, tmp_path):
        """When provider chat_model is empty/blank in DB, ChatService uses preset default_chat_model, not gpt-4o-mini."""
        db_path = tmp_path / "empty_model.db"
        await init_db(db_path)
        service = ChatService(db_path=db_path)

        for pid, blank, expected in [
            ("deepseek", "", "deepseek-flash"),
            ("xai", "   ", "grok-3"),
            ("moonshot", "", "moonshot-v1-32k"),
        ]:
            async with get_db(db_path) as conn:
                await conn.execute("UPDATE providers SET is_active = 0;")
                await conn.execute("UPDATE providers SET is_active = 1, chat_model = ? WHERE id = ?;", (blank, pid))
                await conn.execute("UPDATE settings SET active_provider_id = ? WHERE id = 1;", (pid,))
                await conn.commit()

            adapter, model_name, provider_id = await service._get_active_llm_adapter()
            assert provider_id == pid
            assert model_name == expected
            assert model_name != "gpt-4o-mini"

        # Test custom non-preset provider with empty chat_model gracefully falls back to gpt-4o-mini
        async with get_db(db_path) as conn:
            await conn.execute(
                "INSERT INTO providers (id, name, api_base_url, api_key, chat_model, is_active) "
                "VALUES ('custom_unknown', 'My Unknown Provider', 'https://example.com/v1', 'key', '', 1);"
            )
            await conn.execute("UPDATE providers SET is_active = 0 WHERE id != 'custom_unknown';")
            await conn.execute("UPDATE settings SET active_provider_id = 'custom_unknown' WHERE id = 1;")
            await conn.commit()

        adapter, model_name, provider_id = await service._get_active_llm_adapter()
        assert provider_id == "custom_unknown"
        assert model_name == "gpt-4o-mini"
