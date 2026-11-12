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
    chat_error_payload,
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
        """Ensures Gemini OpenAI-compat endpoint infers gemini-2.5-flash, not gpt-4o-mini."""
        adapter = OpenAICompatibleLLMAdapter(
            api_key="AIzaSyTest",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        )
        assert adapter._resolve_test_model(None) == PROVIDER_PRESETS["gemini"]["default_chat_model"]

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
        assert "额度已耗尽" in diag429["error"]
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
        assert any(term in diag_refused.guidance for term in ("端口", "Base URL", "反向代理"))

    def test_http_402_insufficient_balance(self):
        """DeepSeek / Moonshot / SiliconFlow 402 Insufficient Balance must be diagnosed clearly without blaming API Key / Base URL."""
        raw_error = 'RuntimeError: Streaming endpoint returned HTTP 402: {"error": {"message": "Insufficient Balance"}}'
        diag = diagnose_llm_error(raw_error, provider_id="deepseek")
        assert diag.error_code == "QUOTA_EXCEEDED"
        assert "余额" in diag.message or "余额" in diag.guidance
        assert "Base URL" not in diag.guidance
        assert "API Key" not in diag.guidance

        diag_moonshot = format_provider_error("moonshot", 402, '{"error": {"message": "Insufficient Balance"}}')
        assert diag_moonshot["error_code"] == "QUOTA_EXCEEDED"
        assert "余额" in diag_moonshot["error"] or "余额" in diag_moonshot["diagnostic"]
        assert "Base URL" not in diag_moonshot["diagnostic"]

        payload = chat_error_payload(RuntimeError('API returned status 402: {"message": "余额不足"}'), provider_id="siliconflow")
        assert payload["error_code"] == "QUOTA_EXCEEDED"
        assert "余额" in payload["error"]

        # Real httpx.HTTPStatusError with 402 response status code
        req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        resp = httpx.Response(402, request=req)
        exc = httpx.HTTPStatusError("Client error '402' for url 'https://api.deepseek.com/chat/completions'", request=req, response=resp)
        diag_exc = diagnose_llm_error(exc, provider_id="deepseek")
        assert diag_exc.error_code == "QUOTA_EXCEEDED"
        assert diag_exc.status_code == 402
        assert "余额" in diag_exc.message or "余额" in diag_exc.guidance

    def test_http_400_context_length_exceeded(self):
        """HTTP 400 Context Length Exceeded must diagnose as CONTEXT_LENGTH_EXCEEDED, not auth failed or provider error."""
        raw_error = "RuntimeError: API returned status 400: This model's maximum context length is 32768 tokens. However, your messages resulted in 35000 tokens."
        diag = diagnose_llm_error(raw_error, provider_id="openai")
        assert diag.error_code == "CONTEXT_LENGTH_EXCEEDED"
        assert "上下文" in diag.guidance

        diag_xai = format_provider_error("xai", 400, "This model's maximum context length is 32768 tokens")
        assert diag_xai["error_code"] == "CONTEXT_LENGTH_EXCEEDED"
        assert diag_xai["error_code"] != "XAI_AUTH_FAILED"
        assert "上下文" in diag_xai["diagnostic"]

        diag_gemini = format_provider_error("gemini", 400, "INVALID_ARGUMENT: This model's maximum context length is 32768 tokens")
        assert diag_gemini["error_code"] == "CONTEXT_LENGTH_EXCEEDED"
        assert diag_gemini["error_code"] != "GEMINI_KEY_INVALID"
        assert "上下文" in diag_gemini["diagnostic"]

    def test_xai_and_gemini_generic_400_not_misdiagnosed_as_key_invalid(self):
        """Generic 400 (e.g. parameter error) must not be reported as auth/key failure, while real key errors still are."""
        diag_xai_param = format_provider_error("xai", 400, '{"error": {"message": "temperature must be between 0 and 2"}}')
        assert diag_xai_param["error_code"] != "XAI_AUTH_FAILED"
        assert "身份验证失败" not in diag_xai_param["error"]

        diag_gemini_param = format_provider_error("gemini", 400, '{"error": {"message": "INVALID_ARGUMENT: temperature must be between 0 and 2"}}')
        assert diag_gemini_param["error_code"] != "GEMINI_KEY_INVALID"
        assert "密钥验证失败" not in diag_gemini_param["diagnostic"]

        diag_xai_auth = format_provider_error("xai", 400, '{"code":"invalid-argument","error":"Incorrect API key provided."}')
        assert diag_xai_auth["error_code"] == "XAI_AUTH_FAILED"

        diag_gemini_key = format_provider_error("gemini", 400, 'API key not valid. Please pass a valid API key.')
        assert diag_gemini_key["error_code"] == "GEMINI_KEY_INVALID"

    def test_cloudflare_524_and_nginx_502_html_sanitized_and_diagnosed(self):
        """Cloudflare 524 and Nginx 502 HTML error pages must not leak HTML tags and must not blame API Key."""
        cf_524_html = (
            "<!DOCTYPE html>\n<html>\n<head><title>524 A timeout occurred</title></head>\n"
            "<body>\n<h1>Error 524</h1>\n<p>A timeout occurred</p>\n</body>\n</html>"
        )
        diag_524 = diagnose_llm_error(cf_524_html, provider_id="deepseek")
        assert diag_524.error_code == "NETWORK_TIMEOUT"
        assert diag_524.status_code == 524
        assert "超时" in diag_524.message
        assert "<html>" not in diag_524.message and "<title>" not in diag_524.message
        assert "<!DOCTYPE" not in diag_524.raw_error and "<html>" not in diag_524.raw_error
        assert "API Key" not in diag_524.guidance

        nginx_502_html = (
            "<!DOCTYPE html>\n<html>\n<head><title>502 Bad Gateway</title></head>\n"
            "<body>\n<center><h1>502 Bad Gateway</h1></center>\n<hr><center>nginx/1.24.0</center>\n</body>\n</html>"
        )
        diag_502 = diagnose_llm_error(nginx_502_html, status_code=502, provider_id="openai")
        assert diag_502.error_code == "BAD_GATEWAY"
        assert diag_502.status_code == 502
        assert "网关" in diag_502.message
        assert "<html>" not in diag_502.message and "<title>" not in diag_502.message
        assert "<!DOCTYPE" not in diag_502.raw_error and "<html>" not in diag_502.raw_error
        assert "API Key" not in diag_502.guidance

        stream_err = RuntimeError(f"API returned status 502: {nginx_502_html}")
        payload = chat_error_payload(stream_err, provider_id="custom")
        assert payload["error_code"] == "BAD_GATEWAY"
        assert payload["status_code"] == 502
        assert "<html>" not in payload["error"] and "<title>" not in payload["error"]

    def test_http_402_and_429_with_api_key_substring_not_auth_failure(self):
        """OpenAI/xAI 402 or 429 mentioning 'API key' must not be misdiagnosed as auth failure."""
        diag_openai_402 = format_provider_error("openai", 402, "API key has insufficient balance")
        assert diag_openai_402["error_code"] == "QUOTA_EXCEEDED"
        assert diag_openai_402["error_code"] != "OPENAI_AUTH_FAILED"

        diag_xai_402 = format_provider_error("xai", 402, "API key has insufficient balance")
        assert diag_xai_402["error_code"] == "QUOTA_EXCEEDED"
        assert diag_xai_402["error_code"] != "XAI_AUTH_FAILED"

        diag_xai_429 = format_provider_error("xai", 429, "Rate limit exceeded for API key")
        assert diag_xai_429["error_code"] == "XAI_QUOTA_EXCEEDED"
        assert diag_xai_429["error_code"] != "XAI_AUTH_FAILED"

    def test_gemini_and_xai_field_key_or_author_not_misdiagnosed_as_auth_failure(self):
        """Bare words like 'key' in JSON/schema error or 'author' must not trigger auth failure."""
        diag_gemini = format_provider_error("gemini", 400, 'Invalid JSON: missing key "contents"')
        assert diag_gemini["error_code"] != "GEMINI_KEY_INVALID"
        assert "密钥验证失败" not in diag_gemini["diagnostic"]

        diag_xai_key = format_provider_error("xai", 400, 'Unknown key "temperature"')
        assert diag_xai_key["error_code"] != "XAI_AUTH_FAILED"
        assert "身份验证失败" not in diag_xai_key["error"]

        diag_xai_author = format_provider_error("xai", 400, 'Invalid author parameter')
        assert diag_xai_author["error_code"] != "XAI_AUTH_FAILED"

    def test_html_sanitizer_edge_cases(self):
        """_clean_html_text correctly handles empty title, scripts with fake title, and HTML entities."""
        from galgame2voice.utils.error_diagnostics import _clean_html_text

        empty_title_html = "<html><head><title></title></head><body><h1>502 Bad Gateway</h1></body></html>"
        cleaned_empty = _clean_html_text(empty_title_html)
        assert cleaned_empty == "502 Bad Gateway"

        script_title_html = '<html><head><script>var x = "<title>fake</title>";</script><title>Real 524 Timeout</title></head><body>body</body></html>'
        cleaned_script = _clean_html_text(script_title_html)
        assert cleaned_script == "Real 524 Timeout"

        entity_html = "<title>504 Gateway Time-out &mdash; &quot;Server Error&quot;</title>"
        cleaned_entity = _clean_html_text(entity_html)
        assert cleaned_entity == '504 Gateway Time-out — "Server Error"'

        # Non-HTML text with angle bracket placeholders must not be stripped
        non_html = "Invalid value for <model_name>: custom"
        assert _clean_html_text(non_html) == non_html
        traceback_str = "File <string>, line 1, in <module>"
        assert _clean_html_text(traceback_str) == traceback_str

    def test_extract_status_code_prioritizes_explicit_status_over_token_or_ms_count(self):
        """High-confidence status 502 must not be hijacked by '400 ms' in the error message."""
        from galgame2voice.utils.error_diagnostics import _extract_status_code

        code = _extract_status_code(None, "RuntimeError: API returned status 502: request took 400 ms")
        assert code == 502

        assert _extract_status_code(None, "httpx.HTTPStatusError: Client error '402' for url 'https://api.deepseek.com'") == 402
        assert _extract_status_code(None, "status_code=400 message: bad request") == 400
        assert _extract_status_code(None, "Server error: [524] Gateway timeout") == 524

    def test_context_window_and_chinese_variants(self):
        """Context window exceeded variants and Chinese phrases must diagnose as CONTEXT_LENGTH_EXCEEDED."""
        diag_window = format_provider_error("custom", 400, "The model's context window is 4096 tokens, but your request has 5000 tokens")
        assert diag_window["error_code"] == "CONTEXT_LENGTH_EXCEEDED"

        diag_zh = format_provider_error("deepseek", 400, "超出模型上下文限制")
        assert diag_zh["error_code"] == "CONTEXT_LENGTH_EXCEEDED"

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
    """Verifies stt_engine schema & persistence."""

    def test_settings_models_stt_engine_defaults(self):
        base = SettingsBase()
        assert base.stt_engine == "browser"
        resp = SettingsResponse()
        assert resp.stt_engine == "browser"

    @pytest.mark.asyncio
    async def test_stt_engine_persistence(self):
        """Tests that stt_engine persists accurately in SQLite settings."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            await init_db(db_path)
            async with get_db(db_path) as conn:
                updates = SettingsUpdate(
                    stt_engine="sensevoice",
                )
                updated = await crud.update_settings(conn, updates)
                assert updated.stt_engine == "sensevoice"

                # Read back from DB
                settings = await crud.get_settings(conn)
                assert settings.stt_engine == "sensevoice"
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)
