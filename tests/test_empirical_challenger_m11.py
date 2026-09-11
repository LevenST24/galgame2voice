"""
Empirical Challenger M11-1 Stress & UI Boundary Test Probe.
Focus:
1. Switching across all 8 canonical providers and verifying prefill (Base URL, models, active state).
2. Non-destructive secret retention across multi-round mutations (mask preservation, raw DB integrity).
3. Secret isolation & zero-leakage guarantee across all GET endpoints.
4. Base URL reset logic across all providers (with private endpoint security policy enforcement).
5. UI Boundary stress, adversarial payloads, SSRF protection on stored secrets, and concurrency stress.
"""

import asyncio
import json
import os
import random
import sqlite3
import time
from typing import Dict, Any, List, Optional
from unittest.mock import patch

import httpx
import pytest
from httpx import AsyncClient, ASGITransport

from galgame2voice.main import create_app
from galgame2voice.database.session import get_db, init_db
from galgame2voice.database import crud
from galgame2voice.database.models import (
    ProviderCreate,
    ProviderUpdate,
    SettingsUpdate,
)
from galgame2voice.adapters.registry import (
    PROVIDER_PRESETS,
    get_provider_preset,
    list_provider_presets,
)
from galgame2voice.security import url_guard
from galgame2voice.utils.error_diagnostics import format_provider_error


# Canonical 8 providers for M11 testing
CANONICAL_8_PROVIDERS = [
    "gemini",
    "openai",
    "anthropic",
    "deepseek",
    "xai",
    "groq",
    "siliconflow",
    "custom",
]


@pytest.fixture(autouse=True)
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

    monkeypatch.setattr(url_guard, "_resolve_host", fast_resolve_host)


@pytest.fixture
async def app_client(isolate_test_database):
    """Provides an ASGI test client connected to an initialized, isolated SQLite test DB."""
    await init_db(isolate_test_database)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ============================================================================
# 1. Switching Between All 8 Providers & Prefill Correctness
# ============================================================================

class TestProviderSwitchingAndPrefill:
    """Empirically verifies switching across all 8 canonical providers, prefill correctness, and active state."""

    @pytest.mark.asyncio
    async def test_all_eight_providers_presets_and_prefill_contract(self, app_client):
        """Verifies that all 8 providers exist in presets or list_providers with correct default base URLs and preset models."""
        resp = await app_client.get("/api/providers")
        assert resp.status_code == 200
        data = resp.json()
        assert "providers" in data
        assert "presets" in data

        providers_by_id = {p["id"]: p for p in data["providers"]}
        presets_by_id = {p["id"]: p for p in data["presets"]}

        for pid in CANONICAL_8_PROVIDERS:
            assert pid in presets_by_id, f"Provider '{pid}' missing from presets"
            preset = presets_by_id[pid]
            assert preset["default_base_url"], f"Preset '{pid}' missing default_base_url"
            assert isinstance(preset["preset_models"], list), f"Preset '{pid}' preset_models is not a list"
            assert len(preset["preset_models"]) > 0, f"Preset '{pid}' preset_models is empty"

            # Check specific canonical models
            if pid == "gemini":
                assert any("gemini-2.5" in m for m in preset["preset_models"])
            elif pid == "openai":
                assert any("gpt-4o" in m for m in preset["preset_models"])
            elif pid == "anthropic":
                assert any("claude" in m for m in preset["preset_models"])
            elif pid == "deepseek":
                assert "deepseek-chat" in preset["preset_models"]
            elif pid == "xai":
                assert "grok-3" in preset["preset_models"]
            elif pid == "groq":
                assert any("llama" in m.lower() for m in preset["preset_models"])
            elif pid == "siliconflow":
                assert any("deepseek" in m.lower() for m in preset["preset_models"])
            elif pid == "custom":
                assert preset["default_base_url"].startswith("http://")

    @pytest.mark.asyncio
    async def test_rapid_succession_switching_and_mutual_exclusion(self, app_client):
        """Tests rapid switching between all 8 providers in cyclic order, asserting mutual exclusivity."""
        for cycle in range(3):
            for pid in CANONICAL_8_PROVIDERS:
                # Activate provider
                act_resp = await app_client.post(f"/api/providers/{pid}/activate")
                assert act_resp.status_code == 200, f"Failed to activate '{pid}': {act_resp.text}"
                act_data = act_resp.json()
                assert act_data["status"] == "success"
                assert act_data["active_provider"]["id"] == pid
                assert act_data["active_provider"]["is_active"] is True

                # Check /api/config reflection
                cfg_resp = await app_client.get("/api/config")
                assert cfg_resp.status_code == 200
                cfg_data = cfg_resp.json()
                assert cfg_data["active_provider"]["id"] == pid
                assert cfg_data["settings"]["active_provider_id"] == pid

                # Check full list: exactly 1 provider must be active
                list_resp = await app_client.get("/api/providers")
                assert list_resp.status_code == 200
                providers = list_resp.json()["providers"]
                active_providers = [p for p in providers if p.get("is_active")]
                assert len(active_providers) == 1, f"Expected exactly 1 active provider, found {len(active_providers)}"
                assert active_providers[0]["id"] == pid


# ============================================================================
# 2. Non-Destructive Secret Retention
# ============================================================================

class TestNonDestructiveSecretRetention:
    """Empirically stress-tests non-destructive key retention across multiple mutation passes."""

    @pytest.mark.asyncio
    async def test_multi_pass_mutation_preserves_raw_secret(self, app_client, isolate_test_database):
        """
        Flow:
        1. Create/save provider with real secret key.
        2. Query GET (verify masking).
        3. Mutate model with api_key=None. Verify secret unchanged in DB.
        4. Mutate base_url with api_key=masked_key. Verify secret unchanged in DB.
        5. Mutate name with api_key=''. Verify secret unchanged in DB.
        6. Mutate model with api_key='   '. Verify secret unchanged in DB.
        7. Provide a NEW explicit secret. Verify DB raw key updates cleanly.
        """
        raw_secret = "sk-test-super-secret-key-abcdef1234567890"

        # 1. Save provider with secret
        create_resp = await app_client.post(
            "/api/providers",
            json={
                "id": "openai",
                "name": "OpenAI Production",
                "api_base_url": "https://api.openai.com/v1",
                "api_key": raw_secret,
                "chat_model": "gpt-4o",
            },
        )
        assert create_resp.status_code == 200
        created = create_resp.json()["provider"]
        assert created["api_key"] == "sk-****7890"
        assert created["api_key"] != raw_secret

        # Check DB raw value directly
        async with get_db() as conn:
            stored_raw = await crud.get_provider_raw(conn, "openai")
            assert stored_raw is not None
            assert stored_raw.api_key == raw_secret

        # 2. Mutation Pass 1: Update chat_model only (omit api_key)
        up1_resp = await app_client.post(
            "/api/providers",
            json={
                "id": "openai",
                "chat_model": "gpt-4o-mini",
            },
        )
        assert up1_resp.status_code == 200
        up1_data = up1_resp.json()["provider"]
        assert up1_data["chat_model"] == "gpt-4o-mini"
        assert up1_data["api_key"] == "sk-****7890"

        async with get_db() as conn:
            stored_raw = await crud.get_provider_raw(conn, "openai")
            assert stored_raw.api_key == raw_secret
            assert stored_raw.chat_model == "gpt-4o-mini"

        # 3. Mutation Pass 2: Send masked key string back
        up2_resp = await app_client.post(
            "/api/providers",
            json={
                "id": "openai",
                "api_key": "sk-****7890",
                "api_base_url": "https://api.openai.com/v1",
                "chat_model": "o3-mini",
            },
        )
        assert up2_resp.status_code == 200
        async with get_db() as conn:
            stored_raw = await crud.get_provider_raw(conn, "openai")
            assert stored_raw.api_key == raw_secret, "Masked string overwrote raw secret in DB!"
            assert stored_raw.chat_model == "o3-mini"

        # 4. Mutation Pass 3: Send empty string api_key
        up3_resp = await app_client.post(
            "/api/providers",
            json={
                "id": "openai",
                "name": "OpenAI Renamed",
                "api_key": "",
            },
        )
        assert up3_resp.status_code == 200
        async with get_db() as conn:
            stored_raw = await crud.get_provider_raw(conn, "openai")
            assert stored_raw.api_key == raw_secret, "Empty string wiped raw secret in DB!"
            assert stored_raw.name == "OpenAI Renamed"

        # 5. Mutation Pass 4: Send whitespace api_key
        up4_resp = await app_client.post(
            "/api/providers",
            json={
                "id": "openai",
                "api_key": "   ",
            },
        )
        assert up4_resp.status_code == 200
        async with get_db() as conn:
            stored_raw = await crud.get_provider_raw(conn, "openai")
            assert stored_raw.api_key == raw_secret, "Whitespace wiped raw secret in DB!"

        # 6. Explicit replacement with a brand new key
        new_secret = "sk-brand-new-different-key-998877665544"
        up5_resp = await app_client.post(
            "/api/providers",
            json={
                "id": "openai",
                "api_key": new_secret,
            },
        )
        assert up5_resp.status_code == 200
        up5_data = up5_resp.json()["provider"]
        assert up5_data["api_key"] == "sk-****5544"

        async with get_db() as conn:
            stored_raw = await crud.get_provider_raw(conn, "openai")
            assert stored_raw.api_key == new_secret, "New key was not persisted properly!"

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


# ============================================================================
# 3. Secret Isolation & Zero-Leakage Guarantee
# ============================================================================

class TestSecretIsolationAndZeroLeakage:
    """Empirically asserts that NO endpoint leaks plaintext API keys."""

    @pytest.mark.asyncio
    async def test_no_get_endpoint_leaks_plaintext_keys(self, app_client):
        """Registers secret keys across providers and scans all GET endpoints for leakage."""
        secrets = {}
        for pid in CANONICAL_8_PROVIDERS:
            secret = f"high-entropy-secret-{pid}-Z7x9Q2wE4rT6yU8i"
            secrets[pid] = secret
            await app_client.post(
                "/api/providers",
                json={
                    "id": pid,
                    "api_key": secret,
                    "custom_headers": {"Authorization": f"Bearer {secret}"},
                },
            )

        # 1. Scan GET /api/providers
        resp_list = await app_client.get("/api/providers")
        assert resp_list.status_code == 200
        text_list = resp_list.text
        for pid, secret in secrets.items():
            assert secret not in text_list, f"Plaintext secret for '{pid}' leaked in GET /api/providers!"

        # 2. Scan GET /api/providers/{id} for each provider
        for pid, secret in secrets.items():
            resp_single = await app_client.get(f"/api/providers/{pid}")
            assert resp_single.status_code == 200
            text_single = resp_single.text
            assert secret not in text_single, f"Plaintext secret for '{pid}' leaked in GET /api/providers/{pid}!"

        # 3. Scan GET /api/config
        # Activate one of the providers
        await app_client.post("/api/providers/xai/activate")
        resp_cfg = await app_client.get("/api/config")
        assert resp_cfg.status_code == 200
        text_cfg = resp_cfg.text
        for pid, secret in secrets.items():
            assert secret not in text_cfg, f"Plaintext secret for '{pid}' leaked in GET /api/config!"

        # 4. Scan GET /api/system/status
        resp_status = await app_client.get("/api/system/status")
        assert resp_status.status_code == 200
        text_status = resp_status.text
        for pid, secret in secrets.items():
            assert secret not in text_status, f"Plaintext secret for '{pid}' leaked in GET /api/system/status!"

    @pytest.mark.asyncio
    async def test_error_responses_do_not_leak_submitted_secrets(self, app_client):
        """Verifies that failed /api/providers/test or invalid updates do not reflect plaintext keys in error messages."""
        secret = "secret-super-canary-key-999988887777"
        # Test connection against a dummy endpoint that will fail or trigger error
        test_resp = await app_client.post(
            "/api/providers/test",
            json={
                "id": "openai",
                "api_base_url": "https://api.openai.com/v1",
                "api_key": secret,
                "chat_model": "non-existent-model",
            },
        )
        assert secret not in test_resp.text, "Plaintext secret was echoed in /api/providers/test response!"


# ============================================================================
# 4. Base URL Reset Logic Across All Providers
# ============================================================================

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

        presets_resp = await app_client.get("/api/providers/presets")
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

    @pytest.mark.asyncio
    async def test_custom_provider_loopback_blocked_when_private_disabled(self, app_client):
        """Verifies SSRF boundary: when allow_private_llm_endpoints is False, loopback URLs are rejected."""
        # Ensure allow_private_llm_endpoints is False
        await app_client.post("/api/config", json={"settings": {"allow_private_llm_endpoints": False}})

        resp = await app_client.post(
            "/api/providers",
            json={
                "id": "custom",
                "api_base_url": "http://127.0.0.1:11434/v1",
            },
        )
        assert resp.status_code == 400
        assert "允许私网 LLM 端点" in resp.text


# ============================================================================
# 5. UI Boundary & Adversarial Injection Stress
# ============================================================================

class TestUIBoundaryAndAdversarialStress:
    """Stress tests boundary conditions, SSRF guards on stored keys, and concurrent operations."""

    @pytest.mark.asyncio
    async def test_ssrf_stored_secret_hijack_prevention(self, app_client):
        """
        Crucial security check:
        If a provider has a stored API key in DB (configured for official base_url),
        an attacker must NOT be able to call /api/providers/test with a malicious base_url
        and an empty or masked api_key to coerce the backend into sending the stored key
        to the attacker's server!
        """
        # 1. Store secret under official base_url
        await app_client.post(
            "/api/providers",
            json={
                "id": "xai",
                "api_base_url": "https://api.x.ai/v1",
                "api_key": "xai-real-secret-token-1234567890",
            },
        )

        # 2. Attempt to test against a different external URL while omitting or masking key
        attacker_url = "https://evil-attacker.com/v1"
        test_resp = await app_client.post(
            "/api/providers/test",
            json={
                "id": "xai",
                "api_base_url": attacker_url,
                "api_key": "",  # Attempt to force backend to use stored key
            },
        )
        # Backend MUST reject with 400 Bad Request
        assert test_resp.status_code == 400
        assert "Cannot test custom base_url with stored API key" in test_resp.text

    @pytest.mark.asyncio
    async def test_malformed_and_adversarial_provider_ids(self, app_client):
        """Verifies rejection of invalid or malicious provider IDs."""
        # Empty string ID
        r1 = await app_client.post("/api/providers", json={"id": ""})
        assert r1.status_code in (400, 422)

        # Extremely long ID (> 64 chars)
        r2 = await app_client.post("/api/providers", json={"id": "a" * 65})
        assert r2.status_code in (400, 422)

        # SQL Injection attempt in ID
        sqli_id = "openai' OR '1'='1"
        r3 = await app_client.post(
            "/api/providers",
            json={"id": sqli_id, "chat_model": "test"},
        )
        assert r3.status_code in (200, 422)  # Either safely parameterized or rejected, NEVER 500
        if r3.status_code == 200:
            # Check parameterized retrieval
            r3_get = await app_client.get(f"/api/providers/{sqli_id}")
            assert r3_get.status_code in (200, 404)

    @pytest.mark.asyncio
    async def test_concurrent_provider_switching_burst_on_seeded_providers(self, app_client):
        """Fires 20 concurrent activation requests across seeded providers to verify SQLite WAL transaction isolation."""
        # Pre-seed all canonical providers sequentially first
        for pid in CANONICAL_8_PROVIDERS:
            act = await app_client.post(f"/api/providers/{pid}/activate")
            assert act.status_code == 200

        async def activate_worker(provider_id: str):
            res = await app_client.post(f"/api/providers/{provider_id}/activate")
            return res.status_code, res.json()

        providers_to_test = [random.choice(CANONICAL_8_PROVIDERS) for _ in range(20)]
        results = await asyncio.gather(*(activate_worker(pid) for pid in providers_to_test))

        for status_code, data in results:
            assert status_code == 200
            assert data["status"] == "success"

        # Final verification: exactly 1 active provider in DB
        list_resp = await app_client.get("/api/providers")
        assert list_resp.status_code == 200
        active = [p for p in list_resp.json()["providers"] if p.get("is_active")]
        assert len(active) == 1

    @pytest.mark.asyncio
    async def test_concurrent_activation_of_unseeded_providers_reveals_toctou_race_condition(self, app_client):
        """
        EMPIRICAL FINDING PROBE:
        When multiple concurrent requests activate an unseeded provider (e.g. 'siliconflow' which is not yet in SQLite),
        both see existing=None and attempt `crud.create_provider`.
        Because `crud.create_provider` uses plain `INSERT INTO providers` rather than `INSERT OR IGNORE`,
        the concurrent runner triggers `sqlite3.IntegrityError: UNIQUE constraint failed: providers.id`.
        """
        async def activate_unseeded(provider_id: str):
            try:
                res = await app_client.post(f"/api/providers/{provider_id}/activate")
                return res.status_code
            except Exception:
                return 500

        # Launch 10 concurrent requests targeting the same unseeded provider 'siliconflow'
        tasks = [activate_unseeded("siliconflow") for _ in range(10)]
        codes = await asyncio.gather(*tasks)
        has_500 = any(c == 500 for c in codes)
        all_200 = all(c == 200 for c in codes)
        assert all_200 and not has_500
