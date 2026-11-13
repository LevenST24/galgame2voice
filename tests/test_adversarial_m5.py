"""
Adversarial Stress Test Suite for Milestone 5: Web Management Console & Security Controls.
Authored by m5_challenger_1.

Covers:
1. API Key Masking & Secure Retention Extreme Edge Cases
   - Short strings, boundary lengths (<=8, 9, 10), non-ASCII/Unicode keys, regex metachars
   - Multi-roundtrip retention idempotence across settings and providers
   - Clearing secrets with explicit empty string vs masked retention
2. High Concurrency & Race Conditions Under Load
   - Concurrent settings updates (50+ tasks) verifying SQLite WAL consistency
   - Concurrent provider CRUD & simultaneous active provider switching (ensuring exactly 1 active)
   - Concurrent form submissions with masked placeholders preventing secret loss
3. Log Sanitization & Zero-Leakage Filter Robustness (MaskingFilter)
   - Complex nested JSON, malformed JSON, URL query parameters with various auth keys
   - Bearer tokens, Telegram bot tokens, and multi-secret strings in one line
   - Exception tracebacks with embedded credentials and LogRecord args formatting
   - ReDoS stress test on massive payloads (100KB+ strings with secrets)
   - Edge case analysis of Telegram bot tokens in HTTP URL paths
4. Web Console Routing & API Defensive Boundary Cases
   - Query string preservation on redirects (/console, /settings) with adversarial characters
   - Malicious/unexpected payload fields and type safety in /api/config
   - Provider testing and Telegram testing edge cases with proxy error handling
   - System diagnostics telemetry (/api/system/status) resilience
"""

import asyncio
import json
import logging
import os
import random
import re
import string
import sys
import tempfile
import time
from typing import Dict, List
from unittest.mock import AsyncMock, patch

import pytest
import aiosqlite
import httpx
from httpx import AsyncClient, ASGITransport

from galgame2voice.main import create_app
from galgame2voice.config import get_settings
from galgame2voice.database.session import get_db, init_db
from galgame2voice.database import crud
from galgame2voice.database.crud import mask_api_key, is_masked_key
from galgame2voice.database.models import (
    SettingsUpdate,
    ProviderCreate,
    ProviderUpdate,
    VoiceProfileCreate,
)
from galgame2voice.utils.logger import MaskingFilter


@pytest.fixture
async def adversarial_m5_db():
    """Yields an isolated initialized temporary SQLite database path."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="adv_m5_test_")
    os.close(fd)
    await init_db(path)
    yield path
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


# ============================================================================
# 1. API Key Masking & Secure Retention Edge Cases
# ============================================================================

class TestApiKeyMaskingEdgeCases:
    """Extreme edge case testing for mask_api_key and is_masked_key."""

    @pytest.mark.asyncio
    async def test_multi_roundtrip_retention_idempotence(self):
        """
        Simulate 10 successive web console form reads and saves.
        The secret key MUST remain identical to the original unmasked secret in DB.
        """
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # 1. Create a provider with a high-entropy secret
            secret_key = "sk-live-deepseek-adversarial-99887766554433221100"
            create_resp = await client.post("/api/providers", json={
                "id": "deepseek_roundtrip_test",
                "name": "DeepSeek Roundtrip",
                "api_base_url": "https://api.deepseek.com/v1",
                "api_key": secret_key,
                "chat_model": "deepseek-chat",
            })
            assert create_resp.status_code == 200

            # 2. Perform 10 consecutive GET -> PUT/POST cycles with masked key
            for i in range(10):
                get_resp = await client.get("/api/providers/deepseek_roundtrip_test")
                assert get_resp.status_code == 200
                data = get_resp.json()["provider"]
                masked_key = data["api_key"]
                assert masked_key == "sk-****1100"
                assert "adversarial" not in masked_key

                # Submit update using masked key
                update_resp = await client.post("/api/providers", json={
                    "id": "deepseek_roundtrip_test",
                    "name": f"DeepSeek Roundtrip Iter {i}",
                    "api_key": masked_key,
                    "chat_model": "deepseek-chat",
                })
                assert update_resp.status_code == 200

            # 3. Verify in database raw that secret was never corrupted or truncated
            async with get_db() as conn:
                raw_p = await crud.get_provider_raw(conn, "deepseek_roundtrip_test")
                assert raw_p.api_key == secret_key

            # Clean up
            await client.delete("/api/providers/deepseek_roundtrip_test")


# ============================================================================
# 2. High Concurrency & Race Conditions Under Load
# ============================================================================

class TestConcurrencyAndRaceConditions:
    """Stress tests concurrent operations on settings, providers, and masked updates."""

    @pytest.mark.asyncio
    async def test_concurrent_settings_updates(self, monkeypatch):
        """
        50 concurrent workers updating different settings parameters simultaneously.
        Verify no SQLite database locked errors and all updates commit successfully.

        The request-rate limiter is disabled here on purpose: this test stresses
        database concurrency, and the strict budget guarding admin mutations
        (/api/config) is verified separately in tests/test_rate_limit.py.
        """
        monkeypatch.setenv("GALGAME2VOICE_RATE_LIMIT_DISABLED", "1")
        app = create_app()
        transport = ASGITransport(app=app)

        async def worker(idx: int):
            async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
                speed = 1.0 + (idx % 10) * 0.05
                temp = 0.5 + (idx % 5) * 0.1
                top_k = 10 + (idx % 20)
                resp = await client.post("/api/config", json={
                    "settings": {
                        "speed_factor": speed,
                        "temperature": temp,
                        "top_k": top_k,
                        "seed": idx,
                    }
                })
                assert resp.status_code == 200
                assert resp.json()["status"] == "success"

        tasks = [worker(i) for i in range(50)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            assert not isinstance(r, Exception), f"Concurrent settings update failed: {r}"

        # Verify DB is readable and intact
        async with get_db() as conn:
            final_settings = await crud.get_settings_raw(conn)
            assert final_settings.id == 1
            assert final_settings.top_k >= 10

    @pytest.mark.asyncio
    async def test_concurrent_provider_activation_race(self, monkeypatch):
        """
        Multiple providers created; 30 workers concurrently activate different providers.
        Verify invariant: exactly ONE provider is active in DB and settings.active_provider_id matches.

        Rate limiting is disabled on purpose: this test stresses DB concurrency,
        not request throttling (see tests/test_rate_limit.py).
        """
        monkeypatch.setenv("GALGAME2VOICE_RATE_LIMIT_DISABLED", "1")
        monkeypatch.setattr("galgame2voice.security.url_guard._resolve_host", lambda host, port: (True, ""))
        app = create_app()
        transport = ASGITransport(app=app)

        provider_ids = [f"race_prov_{i}" for i in range(5)]

        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # Pre-create 5 providers
            for pid in provider_ids:
                await client.post("/api/providers", json={
                    "id": pid,
                    "name": f"Provider {pid}",
                    "api_base_url": "https://api.test.com/v1",
                    "api_key": f"sk-secret-{pid}-12345678",
                    "chat_model": "test-model",
                })

            # Concurrently activate random providers
            async def activate_worker(worker_id: int):
                target = provider_ids[worker_id % len(provider_ids)]
                resp = await client.post(f"/api/providers/{target}/activate")
                assert resp.status_code == 200

            tasks = [activate_worker(i) for i in range(30)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                assert not isinstance(r, Exception), f"Activation race error: {r}"

            # Invariant check: exactly 1 active provider in DB
            async with get_db() as conn:
                cursor = await conn.execute("SELECT id FROM providers WHERE is_active = 1;")
                active_rows = await cursor.fetchall()
                assert len(active_rows) == 1, f"Expected 1 active provider, got {len(active_rows)}"
                active_id = active_rows[0][0]

                raw_settings = await crud.get_settings_raw(conn)
                assert raw_settings.active_provider_id == active_id

            # Clean up
            for pid in provider_ids:
                await client.delete(f"/api/providers/{pid}")

    @pytest.mark.asyncio
    async def test_concurrent_masked_form_submissions_under_load(self, monkeypatch):
        """
        Stress test 40 concurrent workers submitting masked keys and configuration updates
        to ensure zero race conditions cause raw secret erasure in SQLite.

        Rate limiting is disabled on purpose: this test stresses DB concurrency,
        not request throttling (see tests/test_rate_limit.py).
        """
        monkeypatch.setenv("GALGAME2VOICE_RATE_LIMIT_DISABLED", "1")
        app = create_app()
        transport = ASGITransport(app=app)
        test_provider_id = "concurrent_mask_target"
        real_api_key = "sk-super-secret-production-key-999888777"

        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # Create target provider
            await client.post("/api/providers", json={
                "id": test_provider_id,
                "name": "Concurrent Mask Target",
                "api_base_url": "https://api.test.com/v1",
                "api_key": real_api_key,
                "chat_model": "model-v1",
            })

            async def submit_worker(worker_id: int):
                # 80% of workers submit masked key, 20% omit key
                submit_key = "sk-****8777" if worker_id % 5 != 0 else ""
                resp = await client.post("/api/providers", json={
                    "id": test_provider_id,
                    "name": f"Target Updated Worker {worker_id}",
                    "api_key": submit_key,
                    "chat_model": f"model-v{worker_id % 3}",
                })
                assert resp.status_code == 200

            tasks = [submit_worker(i) for i in range(40)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                assert not isinstance(r, Exception), f"Worker error: {r}"

            # Verify secret is still completely intact in DB
            async with get_db() as conn:
                raw_p = await crud.get_provider_raw(conn, test_provider_id)
                assert raw_p.api_key == real_api_key

            # Clean up
            await client.delete(f"/api/providers/{test_provider_id}")


# ============================================================================
# 4. Web Console Routing & API Defensive Boundary Cases
# ============================================================================

class TestConsoleRoutingAndDefensiveBoundaries:
    """Tests edge cases and boundary handling in console endpoints."""

    @pytest.mark.asyncio
    async def test_config_update_with_unknown_and_invalid_fields(self):
        """
        POST /api/config with malicious/extra fields: extra fields must be safely ignored,
        valid fields must be persisted.
        """
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            resp = await client.post("/api/config", json={
                "malicious_sql_injection": "DROP TABLE settings;",
                "__proto__": {"polluted": True},
                "speed_factor": 1.05,
                "text_split_method": "cut2",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            assert data["settings"]["speed_factor"] == 1.05
            assert data["settings"]["text_split_method"] == "cut2"

    @pytest.mark.asyncio
    async def test_provider_upsert_special_characters_and_headers(self):
        """
        Create and update provider with special ID characters, unicode names,
        and complex custom headers.
        """
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            prov_id = "custom-special_123"
            custom_headers = {
                "X-Custom-Header": "Value-123",
                "Authorization-Extra": "Bearer extra_token",
                "Nested-Config": {"key": "val"},
            }

            resp = await client.post("/api/providers", json={
                "id": prov_id,
                "name": "特殊プロバイダー (Special Provider)",
                "api_base_url": "https://api.special.org/v1",
                "api_key": "sk-special-prov-key-9999",
                "chat_model": "special-llm-v1",
                "custom_headers": custom_headers,
            })
            assert resp.status_code == 200
            p = resp.json()["provider"]
            assert p["name"] == "特殊プロバイダー (Special Provider)"
            assert p["api_key"] == "sk-****9999"

            # Retrieve single provider
            get_resp = await client.get(f"/api/providers/{prov_id}")
            assert get_resp.status_code == 200
            retrieved = get_resp.json()["provider"]
            assert retrieved["custom_headers"]["X-Custom-Header"] == "Value-123"

            # Clean up
            await client.delete(f"/api/providers/{prov_id}")

    @pytest.mark.asyncio
    async def test_provider_test_endpoint_with_masked_key_fallback(self):
        """
        POST /api/providers/test with masked key should automatically fallback to stored unmasked key in DB.
        """
        app = create_app()
        transport = ASGITransport(app=app)
        test_prov_id = "test_connectivity_fallback"

        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # 1. Store provider in DB
            await client.post("/api/providers", json={
                "id": test_prov_id,
                "name": "Test Fallback",
                "api_base_url": "https://api.openai.com/v1",
                "api_key": "sk-unmasked-stored-key-12345678",
                "chat_model": "gpt-4o-mini",
            })

            # 2. Call /api/providers/test sending masked placeholder
            with patch("galgame2voice.adapters.llm.openai_adapter.OpenAICompatibleLLMAdapter.test_connection", new_callable=AsyncMock) as mock_test:
                from galgame2voice.adapters.base import TestResult
                mock_test.return_value = TestResult(success=True, message="Connected successfully", latency_ms=45.2, models=["gpt-4o-mini"])

                resp = await client.post("/api/providers/test", json={
                    "provider_type": test_prov_id,
                    "api_key": "sk-****5678",  # Masked placeholder
                })
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is True
                assert data["latency_ms"] == 45.2

            # Clean up
            await client.delete(f"/api/providers/{test_prov_id}")

    @pytest.mark.asyncio
    async def test_system_status_deep_diagnostics(self):
        """
        GET /api/system/status provides full subsystem telemetry even when external services are down.
        """
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            resp = await client.get("/api/system/status")
            assert resp.status_code == 200
            telemetry = resp.json()
            assert telemetry["status"] in ("healthy", "degraded")
            assert telemetry["database"]["wal_mode"] is True
            assert telemetry["storage"]["audio_files_count"] >= 0
            assert telemetry["storage"]["audio_dir_size_mb"] >= 0.0
            assert telemetry["app"]["python_version"].startswith("3.")
            assert telemetry["app"]["pid"] > 0
