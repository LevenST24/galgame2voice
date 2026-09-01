"""
Adversarial Stress Test Suite for Milestone 5: Web Management Console & Security Controls.
Challenger: m5_challenger_2.

Verification Domains:
1. Redirect preservation of query strings for /console and /settings (complex, unicode, multi-param, empty).
2. Network timeout and error recovery in provider connectivity tests (/api/providers/test) and system status (/api/system/status).
3. Voice Profile preset application with conflicting or extreme parameters (extreme speed, top_k, temperature, preset overrides, alias normalization, fallback).
"""

import asyncio
import json
import logging
import urllib.parse
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.adapters.base import TestResult
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter
from galgame2voice.adapters.registry import get_llm_adapter, list_provider_presets
from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import ProviderCreate, SettingsUpdate, VoiceProfileCreate
from galgame2voice.database.session import get_db
from galgame2voice.main import create_app
from galgame2voice.services.gpt_sovits_client import (
    GptSovitsClient,
    TTS_PRESETS,
    clean_japanese_parentheses,
    resolve_tts_options,
)
from galgame2voice.services.voice_manager import get_voice_manager


# ============================================================================
# 1. Adversarial Tests: Redirect Query String Preservation
# ============================================================================

class TestRedirectQueryPreservation:
    """Stress-tests /console and /settings redirects with complex query parameters."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "route",
        ["/console", "/settings"],
    )
    async def test_redirect_basic_query_preservation(self, route):
        """Verifies query parameters like ?tab=providers&filter=openai are strictly preserved."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            resp = await client.get(f"{route}?tab=providers&filter=openai")
            assert resp.status_code == 307
            assert resp.headers["location"] == "/settings.html?tab=providers&filter=openai"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query_string",
        [
            "tab=providers&filter=openai&page=1&limit=50&active=true",
            "search=%E6%97%A5%E6%9C%AC%E8%AA%9E&category=%E9%9F%B3%E5%A3%B0",  # UTF-8 encoded
            "param_with_equals=a%3Db%3Dc&special_chars=%21%40%23%24%25",
            "token=sk-1234567890abcdef&masked=sk-****cdef",
            "empty_val=&another_empty=",
            "array_like=item1&array_like=item2&array_like=item3",
        ],
    )
    async def test_redirect_complex_and_escaped_queries(self, query_string):
        """Stress-tests intricate and URL-encoded query strings on both /console and /settings."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            for route in ["/console", "/settings"]:
                resp = await client.get(f"{route}?{query_string}")
                assert resp.status_code == 307
                expected_target = f"/settings.html?{query_string}"
                assert resp.headers["location"] == expected_target

    @pytest.mark.asyncio
    async def test_redirect_no_query_has_no_trailing_question_mark(self):
        """Verifies naked /console and /settings redirect to /settings.html without dangling '?'."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            for route in ["/console", "/settings"]:
                resp = await client.get(route)
                assert resp.status_code == 307
                assert resp.headers["location"] == "/settings.html"

    @pytest.mark.asyncio
    async def test_follow_redirect_delivers_200_html(self):
        """Verifies following the 307 redirect lands successfully on /settings.html with HTTP 200."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True) as client:
            resp = await client.get("/console?tab=voice&profile=1")
            assert resp.status_code == 200
            assert "text/html" in resp.headers.get("content-type", "")
            assert "galgame2voice" in resp.text


# ============================================================================
# 2. Adversarial Tests: Network Timeouts & Error Recovery
# ============================================================================

class TestNetworkTimeoutsAndErrorRecovery:
    """Stress-tests network timeouts, unreachable endpoints, DNS errors, and HTTP fault codes."""

    @pytest.mark.asyncio
    async def test_provider_test_connection_timeout_handling(self):
        """Simulates remote provider connection timeout and ensures graceful TestResult failure."""
        app = create_app()
        transport = ASGITransport(app=app)

        orig_get = httpx.AsyncClient.get

        async def conditional_get(self_client, url, *args, **kwargs):
            if "api.openai.com" in str(url) or "models" in str(url):
                raise httpx.ReadTimeout("Simulated read timeout after 10 seconds")
            return await orig_get(self_client, url, *args, **kwargs)

        with patch.object(httpx.AsyncClient, "get", conditional_get):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/providers/test", json={
                    "id": "openai",
                    "api_key": "sk-realvalidkey12345678",
                    "base_url": "https://api.openai.com/v1",
                })
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is False
                assert "ReadTimeout" in data["message"] or "timeout" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_provider_test_connection_refused_or_unreachable(self):
        """Tests provider connectivity endpoint with dead local port and unreachable base URL."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Opt in to private endpoints: this test intentionally probes
            # loopback/unreachable hosts.
            await client.post("/api/config", json={"allow_private_llm_endpoints": True})

            # 1. Non-existent local port (connection refused / 502 / error)
            resp = await client.post("/api/providers/test", json={
                "id": "custom",
                "api_key": "dummy",
                "base_url": "http://127.0.0.1:59998/v1",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "ConnectError" in data["message"] or "Connection" in data["message"] or "error" in data["message"].lower() or "502" in data["message"]

            # 2. Host with unreachable keyword
            resp2 = await client.post("/api/providers/test", json={
                "id": "openai",
                "api_key": "sk-test12345678",
                "base_url": "http://unreachable-host.local/v1",
            })
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert data2["success"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [401, 403, 404, 429, 500, 502, 503])
    async def test_provider_test_http_error_statuses(self, status_code):
        """Verifies /api/providers/test gracefully converts various HTTP error status codes into informative responses."""
        app = create_app()
        transport = ASGITransport(app=app)

        orig_get = httpx.AsyncClient.get
        orig_post = httpx.AsyncClient.post

        mock_response = httpx.Response(
            status_code=status_code,
            text=f"Simulated HTTP {status_code} Error from Upstream Provider",
            request=httpx.Request("GET", "https://api.openai.com/v1/models"),
        )

        async def conditional_get(self_client, url, *args, **kwargs):
            if "api.openai.com" in str(url):
                return mock_response
            return await orig_get(self_client, url, *args, **kwargs)

        async def conditional_post(self_client, url, *args, **kwargs):
            if "api.openai.com" in str(url):
                return mock_response
            return await orig_post(self_client, url, *args, **kwargs)

        with patch.object(httpx.AsyncClient, "get", conditional_get), \
             patch.object(httpx.AsyncClient, "post", conditional_post):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/providers/test", json={
                    "id": "openai",
                    "api_key": "sk-testkey12345678",
                    "base_url": "https://api.openai.com/v1",
                })
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is False
                assert str(status_code) in data["message"] or "Authentication" in data["message"] or "Provider test returned" in data["message"] or "Invalid credentials" in data["message"]

    @pytest.mark.asyncio
    async def test_provider_test_with_db_masked_key_lookup(self):
        """Verifies /api/providers/test retrieves raw key from DB when frontend sends masked sk-**** key."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Upsert provider in DB with raw secret key
            await client.post("/api/providers", json={
                "id": "mock_db_prov",
                "name": "Mock DB Prov",
                "api_base_url": "https://api.example.com/v1",
                "api_key": "sk-unmasked-real-secret-123456",
                "chat_model": "test-model",
            })

            # 2. Call test with masked key
            mock_resp = httpx.Response(
                status_code=200,
                json={"data": [{"id": "model-a"}, {"id": "model-b"}]},
                request=httpx.Request("GET", "https://api.example.com/v1/models"),
            )
            orig_get = httpx.AsyncClient.get

            async def conditional_get(self_client, url, *args, **kwargs):
                if "api.example.com" in str(url):
                    return mock_resp
                return await orig_get(self_client, url, *args, **kwargs)

            with patch.object(httpx.AsyncClient, "get", conditional_get):
                test_resp = await client.post("/api/providers/test", json={
                    "id": "mock_db_prov",
                    "api_key": "sk-****3456",  # Masked
                })
                assert test_resp.status_code == 200
                test_data = test_resp.json()
                assert test_data["success"] is True
                assert test_data["models"] == ["model-a", "model-b"]

    @pytest.mark.asyncio
    async def test_system_status_timeout_and_recovery(self):
        """Verifies /api/system/status gracefully reports degraded status on GPT-SoVITS timeout and recovers."""
        app = create_app()
        transport = ASGITransport(app=app)

        # 1. Simulate GPT-SoVITS probe timeout
        with patch("galgame2voice.routers.health._probe_gpt_sovits", new_callable=AsyncMock) as mock_probe:
            from galgame2voice.routers.health import GptSovitsTelemetry
            mock_probe.return_value = GptSovitsTelemetry(
                status="unreachable",
                base_url="http://127.0.0.1:9880",
                latency_ms=2000.0,
                error="ConnectTimeout",
            )
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/system/status")
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "degraded"
                assert data["gpt_sovits"]["status"] == "unreachable"
                assert data["gpt_sovits"]["error"] == "ConnectTimeout"

        # 2. Simulate GPT-SoVITS recovery
        with patch("galgame2voice.routers.health._probe_gpt_sovits", new_callable=AsyncMock) as mock_probe2:
            from galgame2voice.routers.health import GptSovitsTelemetry
            mock_probe2.return_value = GptSovitsTelemetry(
                status="reachable",
                base_url="http://127.0.0.1:9880",
                latency_ms=12.5,
                error=None,
            )
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp2 = await client.get("/api/system/status")
                assert resp2.status_code == 200
                data2 = resp2.json()
                assert data2["status"] == "healthy"
                assert data2["gpt_sovits"]["status"] == "reachable"
                assert data2["gpt_sovits"]["error"] is None

    @pytest.mark.asyncio
    async def test_telegram_test_timeout_and_error_recovery(self):
        """Stress-tests /api/telegram/test under connection timeout, proxy failure, and malformed responses."""
        app = create_app()
        transport = ASGITransport(app=app)

        orig_get = httpx.AsyncClient.get

        async def tg_timeout(self_client, url, *args, **kwargs):
            if "api.telegram.org" in str(url):
                raise httpx.ReadTimeout("Telegram API connection timed out")
            return await orig_get(self_client, url, *args, **kwargs)

        with patch.object(httpx.AsyncClient, "get", tg_timeout):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/telegram/test", json={
                    "token": "123456789:ABCDefgh1234567890abcdef",
                    "proxy_enabled": True,
                    "proxy_host": "127.0.0.1",
                    "proxy_port": 10809,
                })
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is False
                assert "ReadTimeout" in data["message"]


# ============================================================================
# 3. Adversarial Tests: Extreme & Conflicting Voice Profile Parameters
# ============================================================================

class TestVoiceProfileExtremeParameters:
    """Stress-tests Voice Profile preset application with conflicting or extreme parameters."""

    def test_preset_application_with_extreme_values(self):
        """Tests resolve_tts_options across extreme boundary values."""
        # Case 1: Extreme low vs extreme high speed
        low_speed = resolve_tts_options({"preset": "high_quality", "speed": 0.1})
        assert low_speed["speed"] == 0.1
        assert low_speed["top_k"] == 20  # from high_quality preset

        high_speed = resolve_tts_options({"preset": "low_latency", "speed": 3.0})
        assert high_speed["speed"] == 3.0
        assert high_speed["top_k"] == 5  # from low_latency preset

        # Case 2: Extreme temperature 0.0 vs 2.0
        zero_temp = resolve_tts_options({"temperature": 0.0})
        assert zero_temp["temperature"] == 0.0

        high_temp = resolve_tts_options({"temperature": 2.0})
        assert high_temp["temperature"] == 2.0

        # Case 3: Extreme top_k 1 vs 100
        min_topk = resolve_tts_options({"top_k": 1})
        assert min_topk["top_k"] == 1

        max_topk = resolve_tts_options({"top_k": 100})
        assert max_topk["top_k"] == 100

        # Case 4: Extreme top_p 0.01 vs 1.0
        low_topp = resolve_tts_options({"top_p": 0.01})
        assert low_topp["top_p"] == 0.01

        max_topp = resolve_tts_options({"top_p": 1.0})
        assert max_topp["top_p"] == 1.0

    def test_conflicting_preset_and_explicit_overrides(self):
        """Verifies explicit user parameters take precedence over preset defaults."""
        # high_quality default: speed=0.9, top_k=20, top_p=1.0, temperature=0.8, text_split_method=cut5
        # User supplies conflicting values:
        overrides = {
            "preset": "high_quality",
            "speed": 1.8,
            "top_k": 3,
            "temperature": 1.9,
            "text_split_method": "cut2",
            "seed": 9999,
        }
        resolved = resolve_tts_options(overrides)
        assert resolved["speed"] == 1.8
        assert resolved["top_k"] == 3
        assert resolved["temperature"] == 1.9
        assert resolved["text_split_method"] == "cut2"
        assert resolved["seed"] == 9999
        assert resolved["batch_size"] == 1  # preserved from preset

    def test_alias_normalization_in_options(self):
        """Verifies aliases (speed_factor, prompt_lang, cut_option) normalize cleanly."""
        options = {
            "speed_factor": 1.35,
            "prompt_lang": "zh",
            "text_lang": "en",
            "cut_option": "cut4",
        }
        resolved = resolve_tts_options(options)
        assert resolved["speed"] == 1.35
        assert resolved["prompt_language"] == "zh"
        assert resolved["text_language"] == "en"
        assert resolved["text_split_method"] == "cut4"

    def test_invalid_or_unknown_preset_fallback(self):
        """Verifies non-existent preset name falls back to 'balanced' without raising exception."""
        resolved = resolve_tts_options({"preset": "non_existent_preset_xyz"})
        assert resolved["speed"] == TTS_PRESETS["balanced"]["speed"]
        assert resolved["top_k"] == TTS_PRESETS["balanced"]["top_k"]
        assert resolved["temperature"] == TTS_PRESETS["balanced"]["temperature"]

    @pytest.mark.asyncio
    async def test_synthesize_endpoint_with_extreme_parameters(self):
        """Verifies /api/voice/synthesize accepts and forwards extreme parameters correctly."""
        app = create_app()
        transport = ASGITransport(app=app)

        with patch("galgame2voice.services.voice_manager.VoiceManager.synthesize", new_callable=AsyncMock) as mock_synth:
            mock_synth.return_value = b"RIFFmockwavdata"
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/voice/synthesize", json={
                    "text": "こんにちは、テストです。",
                    "speed": 0.1,
                    "temperature": 2.0,
                    "top_k": 100,
                    "top_p": 0.05,
                    "preset": "low_latency",
                })
                assert resp.status_code == 200
                assert resp.content == b"RIFFmockwavdata"

                # Verify passed options to synthesize
                assert mock_synth.called
                called_args, called_kwargs = mock_synth.call_args
                passed_options = called_kwargs.get("options", {})
                assert passed_options["speed"] == 0.1
                assert passed_options["temperature"] == 2.0
                assert passed_options["top_k"] == 100
                assert passed_options["top_p"] == 0.05
                assert passed_options["preset"] == "low_latency"

    def test_japanese_parentheses_cleaning_extreme_cases(self):
        """Stress-tests Japanese stage direction cleaner against deeply nested and corrupt brackets."""
        # 1. 5-level nested brackets
        nested = "（（（（（深層の心の声）））））こんにちは、先生！"
        assert clean_japanese_parentheses(nested) == "こんにちは、先生！"

        # 2. Mixed ASCII and fullwidth brackets
        mixed = "(sigh) （微笑んで） (whispers: 'hello') 元気ですか？"
        assert clean_japanese_parentheses(mixed) == "元気ですか？"

        # 3. Unclosed bracket
        unclosed = "（突然の雷鳴 こんにちは"
        assert clean_japanese_parentheses(unclosed) == "突然の雷鸣 こんにちは" or clean_japanese_parentheses(unclosed) == "突然の雷鳴 こんにちは"

        # 4. Only stage directions -> becomes empty
        only_stage = "（手を振る）（微笑む）(leaves)"
        assert clean_japanese_parentheses(only_stage) == ""
