"""
Empirical Challenger M11-2 Stress Test Suite.
Verifies connectivity testing, error diagnostics, and settings schema persistence.
Author: Challenger M11-2 (Adversarial Critic and Domain Specialist)
"""

import asyncio
import os
import time
from unittest.mock import patch
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
from galgame2voice.security import url_guard
from galgame2voice.utils.error_diagnostics import (
    format_provider_error,
    diagnose_llm_error,
)


@pytest.fixture(autouse=True)
def fast_dns_resolution(monkeypatch):
    """Fast DNS resolver mock so external mock hosts resolve instantly without network lookups."""
    orig_resolve = url_guard._resolve_host

    def fast_resolver(host: str, port: int):
        norm = host.lower().strip()
        if (
            norm in ("127.0.0.1", "localhost", "169.254.169.254", "::1")
            or norm.startswith("10.")
            or norm.startswith("192.168.")
            or norm.startswith("172.16.")
            or norm.startswith("169.254.")
        ):
            return orig_resolve(host, port)
        return True, ""

    monkeypatch.setattr(url_guard, "_resolve_host", fast_resolver)


@pytest.fixture
async def app_client(isolate_test_database):
    """ASGI async client with isolated test database."""
    await init_db(isolate_test_database)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def make_http_mocks(matchers: dict):
    """Patches httpx.AsyncClient get/post with URL-pattern matchers."""
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
        return httpx.Response(
            200,
            json={"data": [{"id": "default-mock-model"}]},
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
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "pong"}}]},
            request=httpx.Request("POST", url_str),
        )

    return patch.object(httpx.AsyncClient, "get", mocked_get), patch.object(httpx.AsyncClient, "post", mocked_post)


# ============================================================================
# Category 1: In-Flight Connectivity Testing & Error Diagnostics
# ============================================================================

class TestInFlightConnectivityTesting:
    """Empirically tests POST /api/providers/test across diverse providers and failure modes."""

    @pytest.mark.asyncio
    async def test_valid_parameters_mock_success_and_latency(self, app_client):
        """Valid parameters mock -> returns success=True, latency_ms is a positive number, and models discovered."""
        mock_models = httpx.Response(
            200,
            json={"data": [{"id": "grok-3"}, {"id": "grok-3-mini"}]},
            request=httpx.Request("GET", "https://api.x.ai/v1/models"),
        )
        p_get, p_post = make_http_mocks({"api.x.ai": mock_models})

        with p_get, p_post:
            t0 = time.perf_counter()
            resp = await app_client.post("/api/providers/test", json={
                "id": "xai",
                "api_key": "xai-valid-emp-test-key-123",
                "chat_model": "grok-3",
            })
            elapsed = time.perf_counter() - t0

            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert isinstance(data["latency_ms"], (int, float))
            assert data["latency_ms"] >= 0
            assert "grok-3" in data.get("models", [])
            assert elapsed < 3.0, f"Execution took {elapsed}s, expected < 3.0s"

    @pytest.mark.asyncio
    async def test_invalid_xai_key_http_400_diagnostics(self, app_client):
        """xAI HTTP 400 auth error -> structured Chinese diagnostic pointing to console.x.ai."""
        mock_400 = httpx.Response(
            400,
            text='{"code":"invalid-argument","error":"Incorrect API key provided. You can obtain an API key from https://console.x.ai."}',
            request=httpx.Request("GET", "https://api.x.ai/v1/models"),
        )
        p_get, p_post = make_http_mocks({"api.x.ai": mock_400})

        with p_get, p_post:
            t0 = time.perf_counter()
            resp = await app_client.post("/api/providers/test", json={
                "id": "xai",
                "api_key": "xai-bad-key-400",
            })
            elapsed = time.perf_counter() - t0

            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert data.get("diagnostic"), "diagnostic field must not be empty"
            assert "console.x.ai" in data["diagnostic"]
            assert any(c in data.get("error", "") or c in data["diagnostic"] for c in ("xAI", "Grok", "验证", "密钥"))
            assert elapsed < 3.0

    @pytest.mark.asyncio
    async def test_invalid_xai_key_http_401_diagnostics(self, app_client):
        """xAI HTTP 401 unauthorized -> structured Chinese diagnostic pointing to console.x.ai."""
        mock_401 = httpx.Response(
            401,
            text='{"error":"Unauthorized: Invalid API key"}',
            request=httpx.Request("GET", "https://api.x.ai/v1/models"),
        )
        p_get, p_post = make_http_mocks({"api.x.ai": mock_401})

        with p_get, p_post:
            t0 = time.perf_counter()
            resp = await app_client.post("/api/providers/test", json={
                "id": "xai",
                "api_key": "xai-bad-key-401",
            })
            elapsed = time.perf_counter() - t0

            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert data.get("diagnostic"), "diagnostic field must not be empty"
            assert "console.x.ai" in data["diagnostic"]
            assert elapsed < 3.0

    @pytest.mark.asyncio
    async def test_invalid_groq_key_http_401_diagnostics(self, app_client):
        """Groq HTTP 401 unauthorized -> structured Chinese diagnostic pointing to console.groq.com."""
        mock_401 = httpx.Response(
            401,
            text='{"error": {"message": "Invalid API Key provided", "type": "invalid_request_error"}}',
            request=httpx.Request("GET", "https://api.groq.com/openai/v1/models"),
        )
        p_get, p_post = make_http_mocks({"api.groq.com": mock_401})

        with p_get, p_post:
            t0 = time.perf_counter()
            resp = await app_client.post("/api/providers/test", json={
                "id": "groq",
                "api_key": "gsk_invalid_emp_key",
            })
            elapsed = time.perf_counter() - t0

            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert data.get("diagnostic"), "diagnostic field must not be empty"
            assert "console.groq.com" in data["diagnostic"]
            assert elapsed < 3.0

    @pytest.mark.asyncio
    async def test_gemini_403_region_restriction_diagnostics(self, app_client):
        """Gemini HTTP 403 region restriction -> clear Chinese diagnostic with proxy/region advice."""
        mock_403 = httpx.Response(
            403,
            text='{"error": {"code": 403, "message": "User location is not supported for the API use.", "status": "FAILED_PRECONDITION"}}',
            request=httpx.Request("GET", "https://generativelanguage.googleapis.com/v1beta/openai/models"),
        )
        p_get, p_post = make_http_mocks({"generativelanguage.googleapis.com": mock_403})

        with p_get, p_post:
            t0 = time.perf_counter()
            resp = await app_client.post("/api/providers/test", json={
                "id": "gemini",
                "api_key": "AIzaSyValidFormatKeyForRegionTest",
            })
            elapsed = time.perf_counter() - t0

            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "地区访问受限" in data.get("error", "")
            assert "代理" in data.get("diagnostic", "")
            assert elapsed < 3.0

    @pytest.mark.asyncio
    async def test_gemini_400_bad_key_diagnostics(self, app_client):
        """Gemini HTTP 400 bad key -> clear Chinese diagnostic pointing to aistudio.google.com."""
        mock_400 = httpx.Response(
            400,
            text='{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}',
            request=httpx.Request("GET", "https://generativelanguage.googleapis.com/v1beta/openai/models"),
        )
        p_get, p_post = make_http_mocks({"generativelanguage.googleapis.com": mock_400})

        with p_get, p_post:
            t0 = time.perf_counter()
            resp = await app_client.post("/api/providers/test", json={
                "id": "gemini",
                "api_key": "AIzaSyBadKeyFormat",
            })
            elapsed = time.perf_counter() - t0

            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "aistudio.google.com" in data.get("diagnostic", "")
            assert elapsed < 3.0

    @pytest.mark.asyncio
    async def test_openai_429_quota_exhausted_diagnostics(self, app_client):
        """OpenAI HTTP 429 quota exhausted -> clear Chinese diagnostic with billing/recharge advice."""
        mock_429 = httpx.Response(
            429,
            text='{"error": {"message": "You exceeded your current quota, please check your plan and billing details.", "type": "insufficient_quota"}}',
            request=httpx.Request("GET", "https://api.openai.com/v1/models"),
        )
        p_get, p_post = make_http_mocks({"api.openai.com": mock_429})

        with p_get, p_post:
            t0 = time.perf_counter()
            resp = await app_client.post("/api/providers/test", json={
                "id": "openai",
                "api_key": "sk-quota-exhausted-key",
            })
            elapsed = time.perf_counter() - t0

            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "额度已耗尽" in data.get("error", "")
            assert any(term in data.get("diagnostic", "") for term in ("充值", "billing", "开发者平台"))
            assert elapsed < 3.0

    @pytest.mark.asyncio
    async def test_openai_401_invalid_key_diagnostics(self, app_client):
        """OpenAI HTTP 401 unauthorized -> clear Chinese diagnostic pointing to platform.openai.com."""
        mock_401 = httpx.Response(
            401,
            text='{"error": {"message": "Incorrect API key provided: sk-invalid***", "type": "invalid_request_error"}}',
            request=httpx.Request("GET", "https://api.openai.com/v1/models"),
        )
        p_get, p_post = make_http_mocks({"api.openai.com": mock_401})

        with p_get, p_post:
            t0 = time.perf_counter()
            resp = await app_client.post("/api/providers/test", json={
                "id": "openai",
                "api_key": "sk-bad-openai-key-999",
            })
            elapsed = time.perf_counter() - t0

            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "身份验证失败" in data.get("error", "")
            assert "platform.openai.com" in data.get("diagnostic", "")
            assert elapsed < 3.0

    @pytest.mark.asyncio
    async def test_nonexistent_model_404_diagnostics(self, app_client):
        """Nonexistent model HTTP 404 -> model check diagnostic with Chinese guidance."""
        mock_404 = httpx.Response(
            404,
            text='{"error": {"message": "The model fictional-grok-99 does not exist", "code": "model_not_found"}}',
            request=httpx.Request("GET", "https://api.x.ai/v1/models"),
        )
        p_get, p_post = make_http_mocks({"api.x.ai": mock_404})

        with p_get, p_post:
            t0 = time.perf_counter()
            resp = await app_client.post("/api/providers/test", json={
                "id": "xai",
                "api_key": "xai-valid-key",
                "chat_model": "fictional-grok-99",
            })
            elapsed = time.perf_counter() - t0

            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "模型名称不存在" in data.get("error", "")
            assert any(term in data.get("diagnostic", "") for term in ("模型名称", "推荐预设列表", "grok"))
            assert elapsed < 3.0

    @pytest.mark.asyncio
    async def test_network_timeout_diagnostics(self, app_client):
        """Network ConnectTimeout -> network advice diagnostic pointing to proxy/firewall."""
        async def raise_timeout(*args, **kwargs):
            raise httpx.ConnectTimeout("Connection timed out while probing target endpoint")

        with patch.object(httpx.AsyncClient, "get", raise_timeout):
            t0 = time.perf_counter()
            resp = await app_client.post("/api/providers/test", json={
                "id": "openai",
                "api_key": "sk-test-key",
            })
            elapsed = time.perf_counter() - t0

            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "超时" in data.get("error", "")
            assert "代理" in data.get("diagnostic", "")
            assert elapsed < 3.0

    @pytest.mark.asyncio
    async def test_connection_refused_diagnostics(self, app_client):
        """Connection refused -> network advice diagnostic pointing to port/reverse proxy."""
        async def raise_refused(*args, **kwargs):
            raise httpx.ConnectError("[WinError 10061] No connection could be made because target machine refused")

        with patch.object(httpx.AsyncClient, "get", raise_refused):
            t0 = time.perf_counter()
            resp = await app_client.post("/api/providers/test", json={
                "id": "openai",
                "api_key": "sk-test-key",
            })
            elapsed = time.perf_counter() - t0

            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "被拒绝" in data.get("error", "")
            assert any(term in data.get("diagnostic", "") for term in ("端口", "Base URL", "反向代理"))
            assert elapsed < 3.0


# ============================================================================
# Category 2: Settings Schema Persistence
# ============================================================================

class TestSettingsSchemaPersistenceChallenger:
    """Empirically tests stt_engine and telegram_chat_id persistence across fresh reads/restarts."""

    @pytest.mark.asyncio
    async def test_stt_engine_persists_through_restart_and_fresh_reads(self, app_client, isolate_test_database):
        """Tests stt_engine persistence through simulated restart / fresh DB connections."""
        # 1. Fresh read initial default
        resp = await app_client.get("/api/config")
        assert resp.status_code == 200
        assert resp.json()["settings"]["stt_engine"] == "browser"

        # 2. Update to sensevoice
        up1 = await app_client.post("/api/config", json={"stt_engine": "sensevoice"})
        assert up1.status_code == 200

        # Simulate service restart: open a completely distinct database connection
        async with get_db(isolate_test_database) as fresh_conn:
            fresh_settings = await crud.get_settings(fresh_conn)
            assert fresh_settings.stt_engine == "sensevoice"

        # 3. Read back from REST API
        resp1 = await app_client.get("/api/config")
        assert resp1.json()["settings"]["stt_engine"] == "sensevoice"

        # 4. Update to whisper via nested settings dictionary
        up2 = await app_client.post("/api/config", json={"settings": {"stt_engine": "whisper"}})
        assert up2.status_code == 200

        # Simulate second restart: fresh connection read
        async with get_db(isolate_test_database) as fresh_conn2:
            fresh_settings2 = await crud.get_settings(fresh_conn2)
            assert fresh_settings2.stt_engine == "whisper"

        resp2 = await app_client.get("/api/config")
        assert resp2.json()["settings"]["stt_engine"] == "whisper"

    @pytest.mark.asyncio
    async def test_telegram_chat_id_and_admin_ids_interchangeability(self, app_client, isolate_test_database):
        """Tests bidirectional interchangeability of telegram_chat_id and telegram_admin_ids."""
        # 1. Write via telegram_chat_id
        payload1 = {"settings": {"telegram_chat_id": "123456789"}}
        r1 = await app_client.post("/api/config", json=payload1)
        assert r1.status_code == 200

        # Verify fresh DB read: both fields are synchronized
        async with get_db(isolate_test_database) as conn:
            s1 = await crud.get_settings(conn)
            assert s1.telegram_chat_id == "123456789"
            assert s1.telegram_admin_ids == "123456789"

        # Verify GET /api/config returns both fields identically
        c1 = await app_client.get("/api/config")
        cfg1 = c1.json()["settings"]
        assert cfg1["telegram_chat_id"] == "123456789"
        assert cfg1["telegram_admin_ids"] == "123456789"

        # 2. Write via telegram_admin_ids
        payload2 = {"settings": {"telegram_admin_ids": "987654321,555666777"}}
        r2 = await app_client.post("/api/config", json=payload2)
        assert r2.status_code == 200

        # Verify fresh DB read
        async with get_db(isolate_test_database) as conn2:
            s2 = await crud.get_settings(conn2)
            assert s2.telegram_chat_id == "987654321,555666777"
            assert s2.telegram_admin_ids == "987654321,555666777"

        # Verify GET /api/config
        c2 = await app_client.get("/api/config")
        cfg2 = c2.json()["settings"]
        assert cfg2["telegram_chat_id"] == "987654321,555666777"
        assert cfg2["telegram_admin_ids"] == "987654321,555666777"

    def test_pydantic_model_interchangeability_unit(self):
        """Unit test verifying Pydantic models SettingsBase, SettingsUpdate, SettingsResponse handle both aliases."""
        # SettingsBase initialized from telegram_admin_ids
        b1 = SettingsBase(telegram_admin_ids="admin_123")
        assert b1.telegram_chat_id == "admin_123"

        # SettingsUpdate initialized from telegram_chat_id -> synchronizes telegram_admin_ids
        u1 = SettingsUpdate(telegram_chat_id="chat_789")
        assert u1.telegram_admin_ids == "chat_789"
        assert u1.telegram_chat_id == "chat_789"

        # SettingsUpdate initialized from telegram_admin_ids -> synchronizes telegram_chat_id
        u2 = SettingsUpdate(telegram_admin_ids="admin_999")
        assert u2.telegram_chat_id == "admin_999"
        assert u2.telegram_admin_ids == "admin_999"

        # SettingsResponse initialized from telegram_admin_ids -> synchronizes telegram_chat_id
        r1 = SettingsResponse(telegram_admin_ids="resp_101")
        assert r1.telegram_chat_id == "resp_101"


# ============================================================================
# Category 3: Adversarial Stress & Concurrency Validation
# ============================================================================

class TestAdversarialStressAndConcurrency:
    """Stress tests concurrent in-flight requests and execution bounds without stalling."""

    @pytest.mark.asyncio
    async def test_concurrent_connectivity_tests_no_stall(self, app_client):
        """Fires 10 concurrent in-flight connectivity requests to ensure no locks, stalls, or degradation."""
        mock_resp = httpx.Response(
            200,
            json={"data": [{"id": "grok-3"}, {"id": "llama-3.3-70b-versatile"}]},
            request=httpx.Request("GET", "https://api.openai.com/v1/models"),
        )
        p_get, p_post = make_http_mocks({"models": mock_resp})

        with p_get, p_post:
            t0 = time.perf_counter()

            async def probe(idx: int):
                provider = "xai" if idx % 2 == 0 else "groq"
                key = f"{provider}-concurrent-key-{idx}"
                return await app_client.post("/api/providers/test", json={
                    "id": provider,
                    "api_key": key,
                })

            responses = await asyncio.gather(*[probe(i) for i in range(10)])
            elapsed = time.perf_counter() - t0

            assert len(responses) == 10
            for r in responses:
                assert r.status_code == 200
            # All 10 requests completed without deadlocking or stalling
            assert elapsed < 20.0, f"10 concurrent requests took {elapsed:.2f}s, expected < 20.0s"

    @pytest.mark.asyncio
    async def test_zero_credential_leakage_in_error_payloads(self, app_client):
        """Ensures submitted raw API keys never leak into the diagnostic or error text under any failure."""
        secret_raw_key = "xai-ultra-secret-key-do-not-leak-999888"
        mock_error = httpx.Response(
            401,
            text=f'{{"error": "Invalid API key {secret_raw_key}"}}',
            request=httpx.Request("GET", "https://api.x.ai/v1/models"),
        )
        p_get, p_post = make_http_mocks({"api.x.ai": mock_error})

        with p_get, p_post:
            resp = await app_client.post("/api/providers/test", json={
                "id": "xai",
                "api_key": secret_raw_key,
            })
            assert resp.status_code == 200
            content_str = resp.text
            # The raw secret key must be sanitized or masked, not exposed raw
            assert secret_raw_key not in content_str
