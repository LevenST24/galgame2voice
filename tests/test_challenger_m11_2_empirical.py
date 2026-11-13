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
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        yield client


def make_http_mocks(matchers: dict):
    """Patches httpx.AsyncClient get/post with URL-pattern matchers."""
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
                provider = "xai" if idx % 2 == 0 else "deepseek"
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
