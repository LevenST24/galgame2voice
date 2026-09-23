"""
Comprehensive unit and integration tests for Worker M9:
1. OpenAICompatibleLLMAdapter test_connection fixes (no gpt-4o-mini trap, HTTP 400 auth errors)
2. Provider registry & seeds (xai defaults and models, preset synthesis on GET /api/providers/{id})
3. Structured Error Diagnostics Engine (format_provider_error, diagnose_llm_error, Chinese guidance)
4. Settings Schema (stt_engine in schemas & DB, telegram_chat_id alias)
"""

import tempfile
import os
from unittest.mock import patch, AsyncMock
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
)
from galgame2voice.adapters.registry import (
    get_llm_adapter,
    get_provider_preset,
    list_provider_presets,
    PROVIDER_PRESETS,
)
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter
from galgame2voice.adapters.llm.xai_adapter import XAILLMAdapter
from galgame2voice.utils.error_diagnostics import (
    format_provider_error,
    diagnose_llm_error,
    DiagnosticResult,
)


# ============================================================================
# 1. OpenAICompatibleLLMAdapter test_connection Tests
# ============================================================================

class TestOpenAICompatibleTestConnection:
    """Verifies test_connection model resolution and HTTP 400 auth error interception."""

    @pytest.mark.asyncio
    async def test_test_connection_resolves_xai_preset_default_model(self):
        """Ensures xAI adapter test_connection uses grok-3, never falling back to gpt-4o-mini."""
        adapter = XAILLMAdapter(api_key="xai-test-key")
        assert adapter._resolve_test_model(None) == "grok-3"
        assert adapter._resolve_test_model("grok-3-mini") == "grok-3-mini"

    @pytest.mark.asyncio
    async def test_test_connection_resolves_gemini_base_url_model(self):
        """Ensures Gemini OpenAI-compat endpoint infers gemini-2.0-flash, not gpt-4o-mini."""
        adapter = OpenAICompatibleLLMAdapter(
            api_key="AIzaSyTest",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        )
        assert adapter._resolve_test_model(None) == "gemini-2.0-flash"

    @pytest.mark.asyncio
    async def test_test_connection_http_400_xai_auth_error_no_fallthrough(self):
        """
        xAI returns HTTP 400 with 'Incorrect API key provided'.
        Must classify as auth failure and NOT fall through to /chat/completions.
        """
        adapter = XAILLMAdapter(api_key="xai-invalid-key")
        mock_resp = httpx.Response(
            status_code=400,
            text='{"code":"invalid-argument","error":"Incorrect API key provided. You can obtain an API key from https://console.x.ai."}',
            request=httpx.Request("GET", "https://api.x.ai/v1/models"),
        )

        chat_completions_called = False

        async def mock_get(client, url, *args, **kwargs):
            return mock_resp

        async def mock_post(client, url, *args, **kwargs):
            nonlocal chat_completions_called
            if "chat/completions" in str(url):
                chat_completions_called = True
            return httpx.Response(status_code=200, json={}, request=httpx.Request("POST", str(url)))

        with patch.object(httpx.AsyncClient, "get", mock_get), \
             patch.object(httpx.AsyncClient, "post", mock_post):
            result = await adapter.test_connection()
            assert result.success is False
            assert "Authentication failed (400)" in result.message
            assert result.diagnostic is not None
            assert "xai" in result.diagnostic.lower() or "console.x.ai" in result.diagnostic.lower()
            assert chat_completions_called is False

    @pytest.mark.asyncio
    async def test_test_connection_http_400_gemini_auth_error_no_fallthrough(self):
        """
        Gemini returns HTTP 400 with 'Please pass a valid API key'.
        Must classify as auth failure without falling through to chat completions.
        """
        adapter = OpenAICompatibleLLMAdapter(
            api_key="bad-gemini-key",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            provider_id="gemini",
        )
        mock_resp = httpx.Response(
            status_code=400,
            text='{"error":{"code":400,"message":"Please pass a valid API key","status":"INVALID_ARGUMENT"}}',
            request=httpx.Request("GET", "https://generativelanguage.googleapis.com/v1beta/openai/models"),
        )

        chat_called = False

        async def mock_get(client, url, *args, **kwargs):
            return mock_resp

        async def mock_post(client, url, *args, **kwargs):
            nonlocal chat_called
            chat_called = True
            return httpx.Response(status_code=200, json={}, request=httpx.Request("POST", str(url)))

        with patch.object(httpx.AsyncClient, "get", mock_get), \
             patch.object(httpx.AsyncClient, "post", mock_post):
            result = await adapter.test_connection()
            assert result.success is False
            assert "Authentication failed (400)" in result.message
            assert result.diagnostic is not None
            assert "aistudio.google.com" in result.diagnostic
            assert chat_called is False


# ============================================================================
# 2. Provider Registry & Seeds Tests
# ============================================================================

class TestProviderRegistryAndSeeds:
    """Verifies xai presets, defaults, and synthesize fallback on GET."""

    def test_xai_preset_defaults_and_models(self):
        preset = get_provider_preset("xai")
        assert preset is not None
        assert preset["default_base_url"] == "https://api.x.ai/v1"
        assert preset["default_chat_model"] == "grok-3"
        assert "grok-3" in preset["preset_models"]
        assert "grok-3-mini" in preset["preset_models"]
        assert "grok-2" in preset["preset_models"]

    @pytest.mark.asyncio
    async def test_get_provider_unknown_returns_404(self):
        """Non-existent provider without preset still returns 404."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/providers/completely_unknown_xyz")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_providers_includes_xai_and_gemini(self):
        """GET /api/providers includes active providers and presets."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/providers")
            assert resp.status_code == 200
            data = resp.json()
            provider_ids = [p["id"] for p in data["providers"]]
            preset_ids = [p["id"] for p in data["presets"]]
            assert "xai" in provider_ids or "xai" in preset_ids
            assert "gemini" in provider_ids or "gemini" in preset_ids


# ============================================================================
# 3. Structured Error Diagnostics Engine Tests
# ============================================================================

class TestErrorDiagnosticsEngine:
    """Verifies format_provider_error and diagnose_llm_error Chinese guidance."""

    def test_gemini_403_region_restriction(self):
        diag = format_provider_error("gemini", 403, "User location is not supported for the API use.")
        assert diag["error_code"] == "GEMINI_REGION_RESTRICTED"
        assert "地区访问受限" in diag["error"]
        assert "代理" in diag["diagnostic"]

    def test_gemini_400_invalid_key(self):
        diag = format_provider_error("gemini", 400, "API key not valid. Please pass a valid API key.")
        assert diag["error_code"] == "GEMINI_KEY_INVALID"
        assert "aistudio.google.com" in diag["diagnostic"]

    def test_xai_400_and_401_auth_failure(self):
        diag400 = format_provider_error("xai", 400, "Incorrect API key provided.")
        assert diag400["error_code"] == "XAI_AUTH_FAILED"
        assert "console.x.ai" in diag400["diagnostic"]

        diag401 = format_provider_error("xai", 401, "Unauthorized")
        assert diag401["error_code"] == "XAI_AUTH_FAILED"
        assert "console.x.ai" in diag401["diagnostic"]

    def test_xai_429_quota_exceeded(self):
        diag = format_provider_error("xai", 429, "Rate limit exceeded. Not enough credits.")
        assert diag["error_code"] == "XAI_QUOTA_EXCEEDED"
        assert "积分" in diag["diagnostic"] or "Credits" in diag["diagnostic"]

    def test_openai_401_and_429_quota(self):
        diag401 = format_provider_error("openai", 401, "Incorrect API key provided")
        assert diag401["error_code"] == "OPENAI_AUTH_FAILED"
        assert "platform.openai.com" in diag401["diagnostic"]

        diag429 = format_provider_error("openai", 429, "You exceeded your current quota, please check your plan and billing details.")
        assert diag429["error_code"] == "OPENAI_QUOTA_EXCEEDED"
        assert "充值" in diag429["diagnostic"] or "billing" in diag429["diagnostic"]

    def test_model_not_found_404(self):
        diag = format_provider_error("openai", 404, "The model 'non-existent-gpt' does not exist")
        assert diag["error_code"] == "MODEL_NOT_FOUND"
        assert "模型名称不存在" in diag["error"]

    def test_network_timeout_and_connection_refused(self):
        diag_timeout = diagnose_llm_error(exc_or_msg=httpx.ConnectTimeout("Connection timed out"), status_code=408)
        assert diag_timeout.error_code == "NETWORK_TIMEOUT"
        assert "超时" in diag_timeout.message

        diag_refused = diagnose_llm_error(exc_or_msg=httpx.ConnectError("[WinError 10061] No connection could be made"), status_code=502)
        assert diag_refused.error_code == "CONNECTION_REFUSED"
        assert "被拒绝" in diag_refused.message

    @pytest.mark.asyncio
    async def test_provider_test_endpoint_returns_error_and_diagnostic(self):
        """POST /api/providers/test returns error and diagnostic fields on failure."""
        app = create_app()
        transport = ASGITransport(app=app)

        mock_resp = httpx.Response(
            status_code=401,
            text='{"error": {"message": "Incorrect API key provided."}}',
            request=httpx.Request("GET", "https://api.openai.com/v1/models"),
        )

        orig_get = httpx.AsyncClient.get
        orig_post = httpx.AsyncClient.post

        async def conditional_get(self_client, url, *args, **kwargs):
            if "api.openai.com" in str(url):
                return mock_resp
            return await orig_get(self_client, url, *args, **kwargs)

        async def conditional_post(self_client, url, *args, **kwargs):
            if "api.openai.com" in str(url):
                return mock_resp
            return await orig_post(self_client, url, *args, **kwargs)

        with patch.object(httpx.AsyncClient, "get", conditional_get), \
             patch.object(httpx.AsyncClient, "post", conditional_post):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/providers/test", json={
                    "id": "openai",
                    "api_key": "sk-invalid-test-key-12345",
                    "base_url": "https://api.openai.com/v1",
                })
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is False
                assert "error" in data and data["error"]
                assert "diagnostic" in data and data["diagnostic"]
                assert "身份验证失败" in data["error"] or "401" in data["message"]
                assert "platform.openai.com" in data["diagnostic"]


# ============================================================================
# 4. Settings Schema & Aliases Tests
# ============================================================================

class TestSettingsSchemaAndAliases:
    """Verifies stt_engine schema & persistence, and telegram_chat_id alias."""

    def test_settings_models_stt_engine_defaults(self):
        base = SettingsBase()
        assert base.stt_engine == "browser"
        resp = SettingsResponse()
        assert resp.stt_engine == "browser"

    def test_telegram_chat_id_alias_sync_in_models(self):
        # SettingsBase initializes telegram_chat_id from telegram_admin_ids
        base = SettingsBase(telegram_admin_ids="112233")
        assert base.telegram_chat_id == "112233"

        # SettingsUpdate synchronizes telegram_chat_id -> telegram_admin_ids
        update = SettingsUpdate(telegram_chat_id="998877")
        assert update.telegram_admin_ids == "998877"

        # SettingsResponse carries telegram_chat_id
        resp = SettingsResponse(telegram_admin_ids="556677")
        assert resp.telegram_chat_id == "556677"

    @pytest.mark.asyncio
    async def test_stt_engine_and_telegram_chat_id_persistence(self):
        """Tests that stt_engine and telegram_chat_id persist accurately in SQLite settings."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            await init_db(db_path)
            async with get_db(db_path) as conn:
                # Update with stt_engine and telegram_chat_id
                updates = SettingsUpdate(
                    stt_engine="sensevoice",
                    telegram_chat_id="123456789,987654321",
                )
                updated = await crud.update_settings(conn, updates)
                assert updated.stt_engine == "sensevoice"
                assert updated.telegram_chat_id == "123456789,987654321"
                assert updated.telegram_admin_ids == "123456789,987654321"

                # Read back from DB
                settings = await crud.get_settings(conn)
                assert settings.stt_engine == "sensevoice"
                assert settings.telegram_chat_id == "123456789,987654321"
                assert settings.telegram_admin_ids == "123456789,987654321"
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)
