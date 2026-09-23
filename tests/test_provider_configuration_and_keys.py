"""
Automated Provider Configuration & Key Protection Tests.
Covers:
1. Universal provider parameter submission across all canonical providers
   (gemini, openai, anthropic, deepseek, xai, siliconflow, custom and aliases).
2. Strict API key masking and zero credential leakage in responses and logs.
3. Non-destructive masked key retention on updates.
4. Dedicated coverage for xAI (Grok) (presets, custom endpoints, models, synthesized fallback).
5. In-flight connectivity testing with 100% mocked external HTTP calls.
6. Error diagnostics verification with structured Chinese guidance.
7. Provider activation flow and synchronization with GET /api/config.
8. Settings schema verification: stt_engine and telegram_chat_id persistence.
9. Security & SSRF boundaries.
"""

import json
import logging
import re
import time
from unittest.mock import patch
import aiosqlite
import httpx
import pytest
from httpx import AsyncClient, ASGITransport

from galgame2voice.main import create_app
from galgame2voice.database.session import get_db, init_db
from galgame2voice.database import crud
from galgame2voice.database.models import (
    SettingsBase,
    SettingsUpdate,
    SettingsResponse,
    ProviderCreate,
    ProviderUpdate,
)
from galgame2voice.adapters.registry import (
    get_provider_preset,
    list_provider_presets,
    get_llm_adapter,
    PROVIDER_PRESETS,
)
from galgame2voice.utils.logger import MaskingFilter
from galgame2voice.security import url_guard
from galgame2voice.utils.error_diagnostics import (
    format_provider_error,
    diagnose_llm_error,
)


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
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def mock_external_http_responses(matchers: dict):
    """
    Creates mock get/post patches for httpx.AsyncClient that:
    1. Forwards any request targeting 'http://test' or relative paths to the ASGI transport.
    2. Matches external requests against matchers (url substring -> Response or callable).
    3. Defaults to a 200 OK Response so unmocked external requests never hang or timeout.
    """
    orig_get = httpx.AsyncClient.get
    orig_post = httpx.AsyncClient.post

    def _is_internal(url_str: str) -> bool:
        return (not url_str.startswith("http://") and not url_str.startswith("https://")) or url_str.startswith("http://test")

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
    """Verifies parameter submission and SQLite persistence across all 8 providers."""

    @pytest.mark.asyncio
    async def test_update_and_persist_all_eight_canonical_providers(self, app_client, isolate_test_database):
        """Tests submitting parameters for all 8 canonical providers."""
        test_providers = [
            {
                "id": "gemini",
                "name": "Google Gemini Test",
                "api_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "api_key": "AIzaSyTestKeyGemini1234567890abcdef123",
                "chat_model": "gemini-2.5-pro",
                "stt_model": "",
            },
            {
                "id": "openai",
                "name": "OpenAI Test",
                "api_base_url": "https://api.openai.com/v1",
                "api_key": "sk-openai-live-secret-test-key-998877",
                "chat_model": "gpt-4o",
                "stt_model": "whisper-1",
            },
            {
                "id": "anthropic",
                "name": "Anthropic Claude Test",
                "api_base_url": "https://api.anthropic.com/v1",
                "api_key": "sk-ant-api03-claude-test-key-123456789",
                "chat_model": "claude-sonnet-4-20250514",
                "stt_model": "",
            },
            {
                "id": "deepseek",
                "name": "DeepSeek Test",
                "api_base_url": "https://api.deepseek.com",
                "api_key": "sk-deepseek-live-test-key-1122334455",
                "chat_model": "deepseek-reasoner",
                "stt_model": "",
            },
            {
                "id": "xai",
                "name": "xAI (Grok) Test",
                "api_base_url": "https://api.x.ai/v1",
                "api_key": "xai-test-secret-key-1234567890abcdef",
                "chat_model": "grok-3",
                "stt_model": "",
            },
            {
                "id": "siliconflow",
                "name": "SiliconFlow Test",
                "api_base_url": "https://api.siliconflow.cn/v1",
                "api_key": "sk-siliconflow-test-key-1234567890abc",
                "chat_model": "deepseek-ai/DeepSeek-V3",
                "stt_model": "FunAudioLLM/SenseVoiceSmall",
            },
            {
                "id": "custom",
                "name": "Custom Proxy Test",
                "api_base_url": "https://my-custom-proxy.internal.corp/v1",
                "api_key": "sk-custom-secret-key-998877665544",
                "chat_model": "qwen3-72b",
                "stt_model": "",
            },
        ]

        for p_data in test_providers:
            resp = await app_client.post("/api/providers", json=p_data)
            assert resp.status_code in (200, 201), f"Failed for {p_data['id']}: {resp.text}"
            data = resp.json()
            assert data.get("status") in ("success", "created")
            p_resp = data.get("provider")
            assert p_resp["id"] == p_data["id"]
            assert p_resp["chat_model"] == p_data["chat_model"]
            assert "****" in p_resp["api_key"]
            assert p_data["api_key"] not in json.dumps(data)

            # Direct verification of raw database persistence
            async with get_db(isolate_test_database) as conn:
                raw = await crud.get_provider_raw(conn, p_data["id"])
                assert raw is not None, f"Raw record not found in DB for {p_data['id']}"
                assert raw.api_key == p_data["api_key"], f"Plaintext key not stored accurately for {p_data['id']}"
                assert raw.chat_model == p_data["chat_model"]
                assert raw.api_base_url == p_data["api_base_url"]

    @pytest.mark.asyncio
    async def test_update_and_persist_dispatch_alias_provider_names(self, app_client, isolate_test_database):
        """Tests submitting parameters using the exact dispatch alias names."""
        dispatch_aliases = [
            {
                "id": "google_gemini",
                "name": "Google Gemini Alias",
                "api_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "api_key": "AIzaSyDispatchAliasGeminiKey123456",
                "chat_model": "gemini-2.5-flash",
            },
            {
                "id": "anthropic_claude",
                "name": "Anthropic Claude Alias",
                "api_base_url": "https://api.anthropic.com/v1",
                "api_key": "sk-ant-dispatch-alias-claude-key",
                "chat_model": "claude-haiku-4-20250414",
            },
            {
                "id": "__custom__",
                "name": "Custom Model Alias",
                "api_base_url": "https://llm-gateway.example.com/v1",
                "api_key": "sk-custom-alias-secret-key-12345",
                "chat_model": "deepseek-r1:latest",
            },
        ]

        for p_data in dispatch_aliases:
            resp = await app_client.post("/api/providers", json=p_data)
            assert resp.status_code in (200, 201)
            data = resp.json()
            assert data["provider"]["id"] == p_data["id"]
            assert "****" in data["provider"]["api_key"]

            async with get_db(isolate_test_database) as conn:
                raw = await crud.get_provider_raw(conn, p_data["id"])
                assert raw is not None
                assert raw.api_key == p_data["api_key"]
                assert raw.chat_model == p_data["chat_model"]

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
    """Verifies that API keys are strictly masked and never leaked in responses or logs."""

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
        """GET /api/config returns masked key for active_provider and telegram_bot_token."""
        raw_llm_key = "sk-live-llm-secret-key-11223344"
        raw_tg_token = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"

        # Save provider with raw key and activate it
        await app_client.post("/api/providers", json={
            "id": "openai",
            "api_key": raw_llm_key,
            "is_active": True,
        })
        # Save Telegram bot token
        await app_client.post("/api/config", json={
            "settings": {"telegram_bot_token": raw_tg_token},
        })

        # GET /api/config
        resp = await app_client.get("/api/config")
        assert resp.status_code == 200
        body = resp.text

        assert raw_llm_key not in body
        assert raw_tg_token not in body

        data = resp.json()
        assert data["active_provider"]["api_key"].startswith("sk-****")
        assert "****" in data["settings"]["telegram_bot_token"]

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

    def test_logger_masking_filter_protects_all_provider_keys(self):
        """Verifies MaskingFilter regex protection on raw strings, logs, and tracebacks."""
        samples = [
            ("sk-1234567890abcdef1234", "sk-****1234"),
            ("AIzaSyB3Abcdef123456789012345678901234", "AIz****1234"),
            ("api_key=my_secret_token_12345", "api_key=***REDACTED***"),
            ("Bearer my_super_secret_token_val", "Bearer ***REDACTED***"),
            ("https://api.telegram.org/bot123456:ABC-DEF/getMe", "https://api.telegram.org/bot***REDACTED***/getMe"),
        ]
        for raw, expected_snippet in samples:
            sanitized = MaskingFilter.sanitize(raw)
            assert raw not in sanitized or "****" in sanitized or "REDACTED" in sanitized


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
# 4. Dedicated Coverage for xAI (Grok)
# ============================================================================

class TestXaiGrokProviderCoverage:
    """Dedicated tests for xAI (Grok) provider configuration, endpoints, models and diagnostics."""

    def test_xai_preset_configuration_defaults(self):
        """Verifies xAI preset defaults in registry."""
        preset = get_provider_preset("xai")
        assert preset is not None
        assert preset["name"] == "xAI (Grok)"
        assert preset["default_base_url"] == "https://api.x.ai/v1"
        assert preset["default_chat_model"] == "grok-3"
        for m in ("grok-3", "grok-3-mini", "grok-2"):
            assert m in preset["preset_models"]

    @pytest.mark.asyncio
    async def test_xai_configuration_submission_and_persistence(self, app_client, isolate_test_database):
        """Tests full parameter submission for xAI."""
        payload = {
            "id": "xai",
            "name": "xAI Grok Production",
            "api_base_url": "https://api.x.ai/v1",
            "api_key": "xai-live-grok-api-key-1234567890",
            "chat_model": "grok-3-mini",
        }
        resp = await app_client.post("/api/providers", json=payload)
        assert resp.status_code == 200

        async with get_db(isolate_test_database) as conn:
            raw = await crud.get_provider_raw(conn, "xai")
            assert raw.name == "xAI Grok Production"
            assert raw.api_base_url == "https://api.x.ai/v1"
            assert raw.api_key == "xai-live-grok-api-key-1234567890"
            assert raw.chat_model == "grok-3-mini"

    @pytest.mark.asyncio
    async def test_xai_connectivity_test_success_mock(self, app_client):
        """Tests successful in-flight connectivity test for xAI with mocked HTTP response."""
        mock_models_resp = httpx.Response(
            200,
            json={"data": [{"id": "grok-3"}, {"id": "grok-3-mini"}, {"id": "grok-2"}]},
            request=httpx.Request("GET", "https://api.x.ai/v1/models"),
        )
        p_get, p_post = mock_external_http_responses({"api.x.ai/v1/models": mock_models_resp})

        with p_get, p_post:
            resp = await app_client.post("/api/providers/test", json={
                "id": "xai",
                "api_base_url": "https://api.x.ai/v1",
                "api_key": "xai-valid-test-key-12345",
                "chat_model": "grok-3",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["latency_ms"] is not None and data["latency_ms"] >= 0
            assert "grok-3" in data["models"]

    @pytest.mark.asyncio
    async def test_xai_400_and_401_auth_failure_error_diagnostics(self, app_client):
        """Verifies that xAI 400 and 401 errors return structured Chinese guidance pointing to console.x.ai."""
        # xAI returns 400 with 'Incorrect API key provided'
        mock_400 = httpx.Response(
            400,
            text='{"code":"invalid-argument","error":"Incorrect API key provided. You can obtain an API key from https://console.x.ai."}',
            request=httpx.Request("GET", "https://api.x.ai/v1/models"),
        )
        p_get, p_post = mock_external_http_responses({"api.x.ai": mock_400})

        with p_get, p_post:
            resp = await app_client.post("/api/providers/test", json={
                "id": "xai",
                "api_base_url": "https://api.x.ai/v1",
                "api_key": "xai-invalid-key-9999",
                "chat_model": "grok-3",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "console.x.ai" in data["diagnostic"]

        # xAI returns 401 Unauthorized
        mock_401 = httpx.Response(
            401,
            text='{"error": "Unauthorized: Invalid API key"}',
            request=httpx.Request("GET", "https://api.x.ai/v1/models"),
        )
        p_get2, p_post2 = mock_external_http_responses({"api.x.ai": mock_401})

        with p_get2, p_post2:
            resp = await app_client.post("/api/providers/test", json={
                "id": "xai",
                "api_key": "xai-unauthorized-key",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "console.x.ai" in data["diagnostic"]

    @pytest.mark.asyncio
    async def test_xai_429_quota_exceeded_error_diagnostics(self, app_client):
        """Verifies that xAI 429 quota/credit exhaustion returns Chinese guidance."""
        mock_429 = httpx.Response(
            429,
            text='{"error": "Rate limit exceeded: Not enough credits remaining."}',
            request=httpx.Request("GET", "https://api.x.ai/v1/models"),
        )
        p_get, p_post = mock_external_http_responses({"api.x.ai": mock_429})

        with p_get, p_post:
            resp = await app_client.post("/api/providers/test", json={
                "id": "xai",
                "api_key": "xai-no-credits-key",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "积分" in data["diagnostic"] or "Credits" in data["diagnostic"] or "console.x.ai" in data["diagnostic"]




# ============================================================================
# 6. In-Flight Connectivity Testing & Error Diagnostics
# ============================================================================

class TestInFlightConnectivityTestingAndDiagnostics:
    """Verifies that in-flight testing works without persisting, and returns structured Chinese diagnostics."""

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

    @pytest.mark.asyncio
    async def test_gemini_403_region_restriction_diagnostics(self, app_client):
        """Gemini 403 location error returns region guidance in Chinese."""
        mock_403 = httpx.Response(
            403,
            text='{"error": {"code": 403, "message": "User location is not supported for the API use.", "status": "FAILED_PRECONDITION"}}',
            request=httpx.Request("GET", "https://generativelanguage.googleapis.com/v1beta/openai/models"),
        )
        p_get, p_post = mock_external_http_responses({"generativelanguage.googleapis.com": mock_403})

        with p_get, p_post:
            resp = await app_client.post("/api/providers/test", json={
                "id": "gemini",
                "api_key": "AIzaSyValidFormatKey1234567890",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "地区访问受限" in data["error"]
            assert "代理" in data["diagnostic"]

    @pytest.mark.asyncio
    async def test_gemini_400_invalid_key_diagnostics(self, app_client):
        """Gemini 400 invalid key returns Google AI Studio guidance."""
        mock_400 = httpx.Response(
            400,
            text='{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}',
            request=httpx.Request("GET", "https://generativelanguage.googleapis.com/v1beta/openai/models"),
        )
        p_get, p_post = mock_external_http_responses({"generativelanguage.googleapis.com": mock_400})

        with p_get, p_post:
            resp = await app_client.post("/api/providers/test", json={
                "id": "gemini",
                "api_key": "AIzaSyInvalidKey123",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "aistudio.google.com" in data["diagnostic"]

    @pytest.mark.asyncio
    async def test_openai_429_quota_exceeded_diagnostics(self, app_client):
        """OpenAI 429 quota exhaustion returns billing recharge guidance."""
        mock_429 = httpx.Response(
            429,
            text='{"error": {"message": "You exceeded your current quota, please check your plan and billing details.", "type": "insufficient_quota"}}',
            request=httpx.Request("GET", "https://api.openai.com/v1/models"),
        )
        p_get, p_post = mock_external_http_responses({"api.openai.com": mock_429})

        with p_get, p_post:
            resp = await app_client.post("/api/providers/test", json={
                "id": "openai",
                "api_key": "sk-live-out-of-quota-key",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "额度已耗尽" in data["error"]
            assert "充值" in data["diagnostic"] or "billing" in data["diagnostic"]

    @pytest.mark.asyncio
    async def test_openai_401_invalid_key_diagnostics(self, app_client):
        """OpenAI 401 invalid key returns OpenAI console guidance."""
        mock_401 = httpx.Response(
            401,
            text='{"error": {"message": "Incorrect API key provided: sk-invalid***", "type": "invalid_request_error"}}',
            request=httpx.Request("GET", "https://api.openai.com/v1/models"),
        )
        p_get, p_post = mock_external_http_responses({"api.openai.com": mock_401})

        with p_get, p_post:
            resp = await app_client.post("/api/providers/test", json={
                "id": "openai",
                "api_key": "sk-invalid-test-key",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "身份验证失败" in data["error"]
            assert "platform.openai.com" in data["diagnostic"]

    @pytest.mark.asyncio
    async def test_universal_404_model_not_found_diagnostics(self, app_client):
        """404 Model Not Found returns friendly model name check guidance."""
        # Provider returns 404 with 'model not found'
        mock_404 = httpx.Response(
            404,
            text='{"error": {"message": "The model non-existent-model does not exist", "code": "model_not_found"}}',
            request=httpx.Request("GET", "https://api.openai.com/v1/models"),
        )
        p_get, p_post = mock_external_http_responses({"api.openai.com": mock_404})

        with p_get, p_post:
            resp = await app_client.post("/api/providers/test", json={
                "id": "openai",
                "api_key": "sk-valid-test-key-12345",
                "model": "non-existent-model",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "模型名称不存在" in data["error"]
            assert "官方推荐预设列表" in data["diagnostic"] or "模型" in data["diagnostic"]

    @pytest.mark.asyncio
    async def test_network_timeout_and_connection_refused_diagnostics(self, app_client):
        """Transport timeouts and refused connections return structured proxy guidance."""
        async def timeout_side_effect(self_client, url, *args, **kwargs):
            raise httpx.ConnectTimeout("Connection timed out after 10 seconds")

        with patch.object(httpx.AsyncClient, "get", timeout_side_effect):
            resp = await app_client.post("/api/providers/test", json={
                "id": "openai",
                "api_key": "sk-valid-key",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "超时" in data["error"]
            assert "代理" in data["diagnostic"]

        async def refused_side_effect(self_client, url, *args, **kwargs):
            raise httpx.ConnectError("[WinError 10061] No connection could be made because the target machine actively refused it")

        with patch.object(httpx.AsyncClient, "get", refused_side_effect):
            resp = await app_client.post("/api/providers/test", json={
                "id": "openai",
                "api_key": "sk-valid-key",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "被拒绝" in data["error"]
            assert "Base URL" in data["diagnostic"] or "端口" in data["diagnostic"]

    @pytest.mark.asyncio
    async def test_in_flight_mocked_execution_speed_under_three_seconds(self, app_client):
        """Confirms that mocked connectivity tests execute swiftly (< 3.0s) with zero network timeouts."""
        t0 = time.perf_counter()
        p_get, p_post = mock_external_http_responses({})

        with p_get, p_post:
            resp = await app_client.post("/api/providers/test", json={
                "id": "xai",
                "api_key": "xai-test-key",
            })
            assert resp.status_code == 200
            assert resp.json()["success"] is True

        elapsed = time.perf_counter() - t0
        assert elapsed < 3.0, f"Mocked connectivity test took {elapsed:.2f}s (expected < 3.0s)"


# ============================================================================
# 7. Provider Activation Flow
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
# 8. Settings Schema Verification (stt_engine & telegram_chat_id)
# ============================================================================

class TestSettingsSchemaPersistence:
    """Verifies schema integrity, default values, and round-trip persistence for settings."""

    def test_stt_engine_default_is_browser(self):
        """Verifies default stt_engine is 'browser' in all model schemas."""
        base = SettingsBase()
        assert base.stt_engine == "browser"
        resp = SettingsResponse()
        assert resp.stt_engine == "browser"

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

    def test_telegram_chat_id_and_admin_ids_bidirectional_sync(self):
        """Verifies Pydantic model validator synchronizes telegram_chat_id and telegram_admin_ids."""
        # telegram_chat_id provided -> populates telegram_admin_ids
        up1 = SettingsUpdate(telegram_chat_id="123456789")
        assert up1.telegram_admin_ids == "123456789"
        assert up1.telegram_chat_id == "123456789"

        # telegram_admin_ids provided -> populates telegram_chat_id
        up2 = SettingsUpdate(telegram_admin_ids="987654321")
        assert up2.telegram_chat_id == "987654321"
        assert up2.telegram_admin_ids == "987654321"

    @pytest.mark.asyncio
    async def test_telegram_chat_id_persistence_in_config_endpoint(self, app_client, isolate_test_database):
        """Tests that telegram_chat_id persists and maps cleanly in REST calls."""
        # Submit telegram_chat_id
        resp = await app_client.post("/api/config", json={
            "settings": {"telegram_chat_id": "111222333,444555666"},
        })
        assert resp.status_code == 200

        # Read back from GET /api/config
        cfg = await app_client.get("/api/config")
        assert cfg.status_code == 200
        settings = cfg.json()["settings"]
        assert settings["telegram_chat_id"] == "111222333,444555666"
        assert settings["telegram_admin_ids"] == "111222333,444555666"

        # Submit telegram_admin_ids
        resp2 = await app_client.post("/api/config", json={
            "settings": {"telegram_admin_ids": "777888999"},
        })
        assert resp2.status_code == 200

        cfg2 = await app_client.get("/api/config")
        settings2 = cfg2.json()["settings"]
        assert settings2["telegram_chat_id"] == "777888999"
        assert settings2["telegram_admin_ids"] == "777888999"


# ============================================================================
# 9. Provider Security & SSRF Boundaries
# ============================================================================

class TestProviderSecurityAndSsrfBoundaries:
    """Verifies that SSRF attacks, private networks, and credential theft attempts are blocked."""

    @pytest.mark.asyncio
    async def test_stored_key_cannot_be_tested_with_custom_base_url(self, app_client):
        """Attacker cannot test stored API credentials against an arbitrary custom base_url."""
        # Store key with official URL
        await app_client.post("/api/providers", json={
            "id": "openai",
            "api_key": "sk-real-openai-stored-key-9999",
            "api_base_url": "https://api.openai.com/v1",
        })

        # Attempt to probe custom URL without supplying the key
        resp = await app_client.post("/api/providers/test", json={
            "id": "openai",
            "base_url": "https://attacker.evil-proxy.org/v1",
        })
        assert resp.status_code == 400
        assert "Cannot test custom base_url with stored API key" in resp.text

    @pytest.mark.asyncio
    async def test_private_and_loopback_ips_blocked_by_default(self, app_client):
        """Private IP ranges and AWS metadata endpoints are blocked by default."""
        probes = [
            "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.1/v1",
            "http://192.168.1.1/v1",
            "http://172.16.0.1/v1",
            "http://127.0.0.1:11434/v1",
            "http://localhost:8080/v1",
        ]
        for url in probes:
            resp = await app_client.post("/api/providers/test", json={
                "id": "custom",
                "api_key": "sk-test-key",
                "base_url": url,
            })
            assert resp.status_code == 400, f"Expected 400 for {url}, got {resp.status_code}"

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

    @pytest.mark.asyncio
    async def test_invalid_url_schemes_blocked(self, app_client):
        """Non-HTTP schemes (file://, gopher://, ftp://) are rejected."""
        for scheme_url in ("file:///etc/passwd", "gopher://127.0.0.1:70", "ftp://internal.server/"):
            resp = await app_client.post("/api/providers/test", json={
                "id": "custom",
                "api_key": "sk-key",
                "base_url": scheme_url,
            })
            assert resp.status_code == 400
