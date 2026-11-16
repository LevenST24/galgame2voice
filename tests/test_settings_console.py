"""
Unit and Integration Tests for Web Management Console & Security Controls (Milestone 5).
Covers:
- Route and Redirect Support (/settings.html, /console, /settings with query string preservation)
- Frontend Web Management Console HTML Structure and Content Verification
- API Key Masking, Zero-Leakage, and Secure Retention on Update
- Provider Management API (CRUD, Activation, Connectivity Testing)
- Voice Profile Management API (CRUD, Switch, Presets, Slicing Methods)
- Inference Parameter Tuning & SQLite Persistence
- Telegram Bot Configuration & Testing API
- System Diagnostics Telemetry (/api/system/status)
"""

import asyncio
import json
import logging
import os
import random
import re
import tempfile
import time
from unittest.mock import AsyncMock, patch
import pytest
import httpx
from httpx import AsyncClient, ASGITransport
import aiosqlite

from galgame2voice.main import app as main_app, create_app
from galgame2voice.config import get_settings
from galgame2voice.database.session import get_db, init_db
from galgame2voice.database import crud
from galgame2voice.database.crud import mask_api_key, is_masked_key
from galgame2voice.database.models import SettingsUpdate, ProviderCreate, VoiceProfileCreate
from galgame2voice.security import url_guard
from galgame2voice.services.voice_manager import VoiceManager, set_voice_manager
from galgame2voice.utils.logger import MaskingFilter, MaskingFormatter, sanitize_error_detail


# ============================================================================
# 1. Route & Redirect Support Tests
# ============================================================================

class TestConsoleRoutesAndRedirects:
    """Verifies clean routing and redirection for /settings.html, /console, and /settings."""

    @pytest.mark.asyncio
    async def test_serve_settings_html_redirects_to_spa(self):
        """Verifies GET /settings.html redirects to the unified SPA /?settings=1 with HTTP 307."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            resp = await client.get("/settings.html")
            assert resp.status_code == 307
            assert resp.headers["location"] == "/?settings=1"

    @pytest.mark.asyncio
    async def test_console_redirect_to_settings_html(self):
        """Verifies GET /console returns a 307 redirect to /?settings=1."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            resp = await client.get("/console")
            assert resp.status_code == 307
            assert resp.headers["location"] == "/?settings=1"

    @pytest.mark.asyncio
    async def test_console_redirect_preserves_query_parameters(self):
        """Verifies GET /console?token=sec123&lang=zh preserves query string in redirect."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            resp = await client.get("/console?token=sec123&lang=zh")
            assert resp.status_code == 307
            assert resp.headers["location"] == "/?settings=1&token=sec123&lang=zh"

    @pytest.mark.asyncio
    async def test_settings_alias_redirect(self):
        """Verifies GET /settings alias redirect to /?settings=1."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            resp = await client.get("/settings")
            assert resp.status_code == 307
            assert resp.headers["location"] == "/?settings=1"

    @pytest.mark.asyncio
    async def test_redirect_with_adversarial_query_strings(self):
        """Redirect from /console and /settings should preserve special/adversarial query parameters."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            query = "token=%27%20OR%201=1--&script=%3Cscript%3Ealert(1)%3C/script%3E&name=%E5%A5%88%E9%A1%BB%E3%81%8D%E3%81%AE%E3%81%93"
            resp = await client.get(f"/console?{query}")
            assert resp.status_code == 307
            assert resp.headers["location"] == f"/?settings=1&{query}"

            resp2 = await client.get(f"/settings?{query}")
            assert resp2.status_code == 307
            assert resp2.headers["location"] == f"/?settings=1&{query}"

    @pytest.mark.asyncio
    async def test_follow_redirect_delivers_200_html(self):
        """Verifies following the 307 redirect lands successfully on / with HTTP 200."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True) as client:
            resp = await client.get("/console?tab=voice&profile=1")
            assert resp.status_code == 200
            assert "text/html" in resp.headers.get("content-type", "")
            assert "Gal2Voice" in resp.text or "galgame2voice" in resp.text


# ============================================================================
# 2. Key Masking & Security Retention Tests
# ============================================================================

class TestSecurityAndKeyMasking:
    """Verifies zero plaintext leakage and secure retention on form submissions."""

    def test_mask_api_key_rules(self):
        """Verifies formatting of mask_api_key helper."""
        assert mask_api_key(None) == ""
        assert mask_api_key("") == ""
        assert mask_api_key("12345") == "********"
        assert mask_api_key("sk-1234567890abcdef") == "sk-****cdef"
        assert mask_api_key("1234567890:ABCdefGhI") == "123****fGhI"
        assert mask_api_key("   ") == ""
        assert mask_api_key("1") == "********"
        assert mask_api_key("1234") == "********"
        assert mask_api_key("12345678") == "********"
        assert mask_api_key("sk-") == "********"
        assert mask_api_key("sk-1234") == "********"
        k9 = "sk-123456"
        assert len(k9) == 9
        assert mask_api_key(k9) == "sk-****3456"
        g9 = "abcdefghi"
        assert len(g9) == 9
        assert mask_api_key(g9) == "abc****fghi"
        assert mask_api_key("0123456789") == "012****6789"
        special_key = "sk-!@#$%^&*()_+=-~`{}[]"
        masked_sp = mask_api_key(special_key)
        assert masked_sp.startswith("sk-****")
        assert masked_sp.endswith("[]")
        unicode_key = "sk-秘密のキーTokyoJapan2026"
        masked_uni = mask_api_key(unicode_key)
        assert masked_uni.startswith("sk-****")
        assert masked_uni.endswith("2026")
        emoji_key = "key_🔑🔒🛡️_secret_token_1234"
        assert "****" in mask_api_key(emoji_key)
        assert is_masked_key("sk-****cdef") is True
        assert is_masked_key("123****GhI") is True
        assert is_masked_key("sk-plain-secret-key-123") is False
        assert is_masked_key(None) is False
        assert is_masked_key("") is False
        assert is_masked_key("********") is True
        assert is_masked_key("****") is True
        assert is_masked_key("Bearer secret_token") is False

    @pytest.mark.asyncio
    async def test_provider_api_key_masked_on_retrieval(self):
        """Verifies GET /api/providers and GET /api/providers/{id} return masked keys."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # Create a provider with plaintext key
            await client.post("/api/providers", json={
                "id": "security_test_provider",
                "name": "Security Test",
                "api_base_url": "https://api.test.com/v1",
                "api_key": "sk-mysecretapikey99887766",
                "chat_model": "test-model"
            })

            # List providers
            list_resp = await client.get("/api/providers")
            assert list_resp.status_code == 200
            providers = list_resp.json()["providers"]
            p = next((x for x in providers if x["id"] == "security_test_provider"), None)
            assert p is not None
            assert p["api_key"] == "sk-****7766"
            assert "mysecretapikey" not in p["api_key"]

            # Get single provider
            single_resp = await client.get("/api/providers/security_test_provider")
            assert single_resp.status_code == 200
            single_p = single_resp.json()["provider"]
            assert single_p["api_key"] == "sk-****7766"

    @pytest.mark.asyncio
    async def test_provider_masked_key_submission_retains_secret(self):
        """Verifies submitting masked key (e.g. sk-****7766) does NOT overwrite plaintext secret in DB."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # 1. Create with secret
            await client.post("/api/providers", json={
                "id": "retain_test_prov",
                "name": "Retain Test",
                "api_base_url": "https://api.test.com/v1",
                "api_key": "sk-supersecret12345678",
                "chat_model": "model-v1"
            })

            # 2. Submit update with masked key as would come from frontend form
            update_resp = await client.post("/api/providers", json={
                "id": "retain_test_prov",
                "name": "Retain Test Updated",
                "api_base_url": "https://api.test.com/v1",
                "api_key": "sk-****5678",  # Masked placeholder submitted
                "chat_model": "model-v2"
            })
            assert update_resp.status_code == 200

            # 3. Verify in database raw that secret was preserved
            async with get_db() as conn:
                raw_prov = await crud.get_provider_raw(conn, "retain_test_prov")
                assert raw_prov.api_key == "sk-supersecret12345678"
                assert raw_prov.chat_model == "model-v2"

    @pytest.mark.asyncio
    async def test_provider_empty_key_submission_retains_secret(self):
        """Verifies submitting empty key on update does not erase stored secret."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            await client.post("/api/providers", json={
                "id": "empty_key_prov",
                "name": "Empty Key Test",
                "api_base_url": "https://api.test.com/v1",
                "api_key": "sk-existingsecretkey999",
                "chat_model": "model-v1"
            })

            # Update with empty string api_key
            await client.post("/api/providers", json={
                "id": "empty_key_prov",
                "api_key": "",
                "chat_model": "model-v3"
            })

            async with get_db() as conn:
                raw_prov = await crud.get_provider_raw(conn, "empty_key_prov")
                assert raw_prov.api_key == "sk-existingsecretkey999"
                assert raw_prov.chat_model == "model-v3"

    @pytest.mark.asyncio
    async def test_provider_new_key_submission_updates_secret(self):
        """Verifies submitting a new valid plaintext key updates the secret in DB."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            await client.post("/api/providers", json={
                "id": "change_key_prov",
                "name": "Change Key Test",
                "api_base_url": "https://api.test.com/v1",
                "api_key": "sk-oldsecretkey1111",
                "chat_model": "model-v1"
            })

            # Update with brand new key
            await client.post("/api/providers", json={
                "id": "change_key_prov",
                "api_key": "sk-newsecretkey2222",
                "chat_model": "model-v1"
            })

            async with get_db() as conn:
                raw_prov = await crud.get_provider_raw(conn, "change_key_prov")
                assert raw_prov.api_key == "sk-newsecretkey2222"


# ============================================================================
# 3. Provider Management API Tests
# ============================================================================

class TestProviderManagementEndpoints:
    """Verifies provider activation and deletion."""

    @pytest.mark.asyncio
    async def test_activate_provider_endpoint(self):
        """Verifies POST /api/providers/{id}/activate sets active provider and syncs settings."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            resp = await client.post("/api/providers/openai/activate")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            assert data["active_provider"]["id"] == "openai"

            # Check config sync
            cfg_resp = await client.get("/api/config")
            assert cfg_resp.json()["settings"]["active_provider_id"] == "openai"
            assert cfg_resp.json()["active_provider"]["id"] == "openai"

    @pytest.mark.asyncio
    async def test_provider_delete_endpoint(self):
        """Verifies DELETE /api/providers/{id} deletes a configured provider."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # Create
            await client.post("/api/providers", json={
                "id": "prov_to_delete",
                "name": "Delete Me",
                "api_base_url": "https://api.example.com",
                "chat_model": "test"
            })

            # Delete
            del_resp = await client.delete("/api/providers/prov_to_delete")
            assert del_resp.status_code == 200
            assert del_resp.json()["status"] == "deleted"

            # Verify 404
            get_resp = await client.get("/api/providers/prov_to_delete")
            assert get_resp.status_code == 404


# ============================================================================
# 4. Voice Profile & Parameter Tuning Tests
# ============================================================================

class TestVoiceAndInferenceTuning:
    """Verifies voice profile management, atomic model switching, and inference parameter tuning."""

    @pytest.mark.asyncio
    async def test_voice_profiles_crud(self):
        """Verifies voice profiles listing, creation, and updates."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # 1. Create Profile
            create_resp = await client.post("/api/voice/profiles", json={
                "name": "ConsoleTestCharacter",
                "description": "Character for Console UI Test",
                "gpt_weights_path": "weights/char.ckpt",
                "sovits_weights_path": "weights/char.pth",
                "refer_audio_path": "audio/ref.ogg",
                "refer_text": "テストテキストです。",
                "refer_language": "ja"
            })
            assert create_resp.status_code == 201
            profile_id = create_resp.json()["id"]

            # 2. Update Profile
            update_resp = await client.put(f"/api/voice/profiles/{profile_id}", json={
                "description": "Updated character description",
                "prompt_lang": "zh"
            })
            assert update_resp.status_code == 200
            assert update_resp.json()["profile"]["description"] == "Updated character description"

            # 3. List Profiles
            list_resp = await client.get("/api/voice/profiles")
            assert list_resp.status_code == 200
            assert any(p["name"] == "ConsoleTestCharacter" for p in list_resp.json()["profiles"])

            # 4. Delete Profile
            del_resp = await client.delete(f"/api/voice/profiles/{profile_id}")
            assert del_resp.status_code == 200

    @pytest.mark.asyncio
    async def test_inference_parameters_persistence(self):
        """Verifies POST /api/config updates all fine-tuning knobs and persists them."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            knobs = {
                "text_split_method": "cut5",
                "speed_factor": 1.15,
                "temperature": 0.85,
                "top_k": 20,
                "top_p": 0.95,
                "fragment_interval": 0.25,
                "seed": 42,
                "batch_size": 2,
                "max_history_messages": 15,
                "audio_retention_minutes": 45,
                "gpt_sovits_url": "http://127.0.0.1:9880"
            }

            post_resp = await client.post("/api/config", json={"settings": knobs})
            assert post_resp.status_code == 200

            get_resp = await client.get("/api/config")
            assert get_resp.status_code == 200
            s = get_resp.json()["settings"]
            assert s["text_split_method"] == "cut5"
            assert s["speed_factor"] == 1.15
            assert s["temperature"] == 0.85
            assert s["top_k"] == 20
            assert s["top_p"] == 0.95
            assert s["fragment_interval"] == 0.25
            assert s["seed"] == 42
            assert s["batch_size"] == 2
            assert s["max_history_messages"] == 15
            assert s["audio_retention_minutes"] == 45


# ============================================================================
# 5. System Diagnostics Tests
# ============================================================================

class TestSystemDiagnostics:
    """Verifies comprehensive diagnostic telemetry."""

    @pytest.mark.asyncio
    async def test_system_status_diagnostics_endpoint(self):
        """Verifies GET /api/system/status provides full telemetry response."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            resp = await client.get("/api/system/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] in ("healthy", "degraded")
            assert "app" in data
            assert data["app"]["name"] == "galgame2voice"
            assert "database" in data
            assert data["database"]["wal_mode"] is True
            assert "gpt_sovits" in data
            assert "storage" in data


# ============================================================================
# 6. MaskingFilter & Security Log Sanitization Tests
# ============================================================================

class TestMaskingFilterLogSanitization:
    """Verifies zero plaintext leakage in application and server logs via MaskingFilter."""

    def test_masking_filter_openai_key(self):
        """Verifies OpenAI sk- keys are redacted from logs."""
        raw_msg = "Attempting connection with OpenAI key sk-1234567890abcdef123456"
        sanitized = MaskingFilter.sanitize(raw_msg)
        assert "sk-1234567890abcdef123456" not in sanitized
        assert "sk-123****3456" in sanitized or "sk-****" in sanitized

    def test_masking_filter_bearer_auth(self):
        """Verifies Bearer Authorization headers are redacted."""
        raw_msg = "Request headers: {'Authorization': 'Bearer secret_access_token_12345678'}"
        sanitized = MaskingFilter.sanitize(raw_msg)
        assert "secret_access_token_12345678" not in sanitized
        assert "Bearer [MASKED_TOKEN]" in sanitized

    def test_masking_filter_telegram_token(self):
        """Verifies Telegram bot tokens are redacted."""
        raw_msg = "Initialized bot with token 123456789:ABCDefgh-1234567890abcdefghijklmn"
        sanitized = MaskingFilter.sanitize(raw_msg)
        assert "ABCDefgh-1234567890abcdefghijklmn" not in sanitized
        assert "[MASKED_TELEGRAM_TOKEN]" in sanitized

    def test_masking_filter_json_key_value(self):
        """Verifies JSON key-value secret fields are redacted."""
        raw_msg = 'Config payload: {"api_key": "sk-real-secret-12345678", "password": "superpassword123"}'
        sanitized = MaskingFilter.sanitize(raw_msg)
        assert "sk-real-secret-12345678" not in sanitized
        assert "superpassword123" not in sanitized
        assert '"api_key": "****"' in sanitized or '"api_key": "****"' in sanitized.lower()

    def test_masking_filter_url_query(self):
        """Verifies sensitive URL query parameters are redacted."""
        raw_msg = "GET /v1/models?api_key=sk-1234567890&token=secrettoken123 HTTP/1.1"
        sanitized = MaskingFilter.sanitize(raw_msg)
        assert "sk-1234567890" not in sanitized
        assert "secrettoken123" not in sanitized
        assert "api_key=[MASKED]" in sanitized
        assert "token=[MASKED]" in sanitized
        extra_queries = [
            ("https://api.provider.com/v1/chat?key=mysecretkey123&user=test", "mysecretkey123", "key=[MASKED]"),
            ("https://api.provider.com/v1/chat?secret=shhhhhhh12345&mode=fast", "shhhhhhh12345", "secret=[MASKED]"),
            ("https://api.provider.com/v1/chat?password=mypassword999&auth=1", "mypassword999", "password=[MASKED]"),
        ]
        for raw_url, secret, needle in extra_queries:
            query_sanitized = MaskingFilter.sanitize(f"Requesting URL: {raw_url}")
            assert secret not in query_sanitized
            assert needle in query_sanitized

    def test_masking_filter_log_record_args_processing(self):
        """Verifies MaskingFilter properly filters LogRecord instance msg and arguments."""
        filter_instance = MaskingFilter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="User login with token: %s",
            args=("sk-supersecret1234567890",),
            exc_info=None,
        )
        filter_instance.filter(record)
        assert "sk-supersecret1234567890" not in str(record.args)

    SECRET_CORPUS = {
        "openai_std": "sk-1234567890abcdefghijklmnopqrstuvwxyzAB",
        "openai_long": "sk-proj-1234567890abcdefghijklmnopqrstuvwxyz1234567890",
        "google_gemini": "AIzaSyD_abcdefghijklmnopqrstuvwxyz12345",
        "huggingface": "hf_abcdefghijklmnopqrstuvwxyz12345678",
        "bearer_token": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDcSemACt8x4iTMCda8Yhe3iZaWbvV5XKSTbuAn0M",
        "tg_url": "https://api.telegram.org/bot1234567890:AAH_abcdefghijklmnopqrstuvwxyz12345/getMe",
        "tg_token_standalone": "1234567890:AAH_abcdefghijklmnopqrstuvwxyz12345",
        "url_query_key": "https://api.example.com/v1?api_key=secret_query_key_99999&user=alice",
        "url_query_token": "https://api.example.com/v1?token=secret_query_token_88888&user=bob",
        "json_api_key": '"api_key": "sec_json_api_key_77777"',
        "json_password": '"password": "MySuperSecretPassword123!"',
        "unquoted_token": "Console token: sec_unquoted_token_66666",
        "unquoted_api_key": "api_key=sec_unquoted_key_55555",
        "unquoted_secret": "client_secret: sec_client_secret_44444",
        "unquoted_auth_token": "auth_token: sec_auth_token_33333",
    }

    def test_masking_filter_gemini_and_huggingface_keys(self):
        """AIzaSy and hf_ tokens are redacted, not only URL-query or Bearer wrappers."""
        for name in ("google_gemini", "huggingface"):
            raw_secret = self.SECRET_CORPUS[name]
            sanitized = MaskingFilter.sanitize(raw_secret)
            assert raw_secret not in sanitized, f"Secret leak for {name}: {raw_secret} found in {sanitized}"
            assert ("****" in sanitized or "[MASKED" in sanitized), f"Masking marker missing for {name}: {sanitized}"

    def test_masking_filter_telegram_url(self):
        """Telegram bot tokens embedded in api.telegram.org URLs are redacted."""
        raw_secret = self.SECRET_CORPUS["tg_url"]
        sanitized = MaskingFilter.sanitize(raw_secret)
        assert raw_secret not in sanitized, f"Secret leak for tg_url: {raw_secret} found in {sanitized}"
        assert "1234567890:AAH_abcdefghijklmnopqrstuvwxyz12345" not in sanitized
        assert ("****" in sanitized or "[MASKED" in sanitized)

    def test_masking_filter_unquoted_token_edge_cases(self):
        """Unquoted key/value assignments stay masked across delimiters."""
        cases = [
            ("token: 987654321_secret_token", "987654321_secret_token"),
            ("api_key = abcdef12345678_sec", "abcdef12345678_sec"),
            ("console_token: sec_console_token_9999", "sec_console_token_9999"),
            ("auth_token: bearer_xyz_99887766", "bearer_xyz_99887766"),
            ("client_secret: clnt_sec_1122334455", "clnt_sec_1122334455"),
            ("Config: {api_key: secret_key_in_braces, user: alice}", "secret_key_in_braces"),
            ("Status: password=my_plain_pass_123; role=admin", "my_plain_pass_123"),
        ]
        for text, secret in cases:
            sanitized = MaskingFilter.sanitize(text)
            assert secret not in sanitized, f"Failed to mask unquoted secret '{secret}' in '{text}'. Got: '{sanitized}'"

    def test_masking_filter_dict_args(self):
        """MaskingFilter.filter sanitizes dict log arguments."""
        logger_filter = MaskingFilter()
        record_dict = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="Config loaded",
            args={"key": self.SECRET_CORPUS["google_gemini"], "val": "normal"},
            exc_info=None,
        )
        assert logger_filter.filter(record_dict) is True
        assert self.SECRET_CORPUS["google_gemini"] not in str(record_dict.args)

    def test_sanitize_error_detail_null_and_diverse_types(self):
        """sanitize_error_detail handles None, empty text, numbers, and custom objects."""
        assert sanitize_error_detail(None) == ""
        assert sanitize_error_detail("") == ""
        assert sanitize_error_detail(12345) == "12345"

        class CustomObj:
            def __str__(self):
                return f"CustomObj with api_key={TestMaskingFilterLogSanitization.SECRET_CORPUS['openai_std']}"

        sanitized = sanitize_error_detail(CustomObj())
        assert TestMaskingFilterLogSanitization.SECRET_CORPUS["openai_std"] not in sanitized
        assert "api_key=****" in sanitized

    def test_masking_formatter_nested_exception_chain(self):
        """Secrets in a chained traceback stay out of format and formatException output."""
        keys_list = list(self.SECRET_CORPUS.values())

        def recursive_raise(depth: int):
            if depth >= len(keys_list):
                raise ValueError(f"Deepest exception: {keys_list[-1]}")
            try:
                recursive_raise(depth + 1)
            except Exception as inner:
                key = keys_list[depth]
                raise RuntimeError(f"Level {depth} failed: {key}") from inner

        try:
            recursive_raise(0)
        except Exception as exc:
            captured_exc = exc

        formatter = MaskingFormatter(fmt="%(asctime)s [%(levelname)s]: %(message)s")
        record = logging.LogRecord(
            name="test.deep_exc",
            level=logging.ERROR,
            pathname=__file__,
            lineno=100,
            msg="Exception caught in pipeline",
            args=(),
            exc_info=(type(captured_exc), captured_exc, captured_exc.__traceback__),
        )
        formatted_output = formatter.format(record)
        exception_output = formatter.formatException(record.exc_info)

        for output in (formatted_output, exception_output):
            for name, raw_secret in self.SECRET_CORPUS.items():
                if name == "bearer_token":
                    token_body = raw_secret.split()[1]
                    assert token_body not in output, f"Bearer token body leaked in traceback: {token_body}"
                elif name in ("json_api_key", "json_password"):
                    val = raw_secret.split(":")[1].replace('"', "").strip()
                    assert val not in output, f"JSON secret value leaked in traceback: {val}"
                elif name.startswith("unquoted_"):
                    val = raw_secret.split(":")[1].strip() if ":" in raw_secret else raw_secret.split("=")[1].strip()
                    assert val not in output, f"Unquoted secret leaked in traceback: {val}"
                elif name == "tg_url":
                    assert "1234567890:AAH_abcdefghijklmnopqrstuvwxyz12345" not in output
                else:
                    assert raw_secret not in output, f"Secret {name} leaked in formatted traceback!"

    # Strict 2MB log-sanitization wall-clock throughput budget is sensitive to multi-worker CPU contention.
    @pytest.mark.serial
    @pytest.mark.wall_clock
    def test_massive_log_payload_throughput_and_redos_safety(self):
        """
        Tests 2MB massive log string containing 10,000 lines and embedded secrets
        at the beginning, middle, and end.
        Ensures linear execution time (ReDoS safe) and zero memory corruption.
        """
        lines = ["Normal application log line processing user request sequence ID #100234.\n"] * 10000
        lines[0] = f"Start line with token: {self.SECRET_CORPUS['openai_std']}\n"
        lines[5000] = f"Middle line with google key: {self.SECRET_CORPUS['google_gemini']}\n"
        lines[9999] = f"End line with tg: {self.SECRET_CORPUS['tg_url']}\n"
        payload = "".join(lines)

        timings = []
        sanitized = ""
        for _ in range(3):
            t0 = time.perf_counter()
            sanitized = MaskingFilter.sanitize(payload)
            timings.append(time.perf_counter() - t0)

        best_elapsed = min(timings)
        assert best_elapsed < 3.0, (
            f"Sanitization of payload took too long: best={best_elapsed:.3f}s, "
            f"runs={[f'{t:.3f}s' for t in timings]} (ReDoS risk)"
        )
        assert self.SECRET_CORPUS["openai_std"] not in sanitized
        assert self.SECRET_CORPUS["google_gemini"] not in sanitized
        assert "1234567890:AAH_abcdefghijklmnopqrstuvwxyz12345" not in sanitized

    # Tight wall-clock budget on 150KB MaskingFilter regex pass is sensitive to multi-worker CPU contention.
    @pytest.mark.serial
    @pytest.mark.wall_clock
    def test_redos_and_massive_payload_stress(self):
        """
        Verify MaskingFilter does not hang (ReDoS) or crash on a massive 150KB string
        containing repeated patterns and multiple embedded secrets.
        """
        junk = "The quick brown fox jumps over the lazy dog. " * 2000
        secrets = [
            "sk-1234567890abcdef12345678",
            "Bearer super_secret_token_1234567890",
            '{"api_key": "sk-embedded-998877665544"}',
            "https://api.test.com?api_key=sk-query-param-1234567890",
        ]
        massive_text = junk + "\n".join(secrets) + junk

        t0 = time.perf_counter()
        sanitized = MaskingFilter.sanitize(massive_text)
        elapsed = time.perf_counter() - t0

        assert elapsed < 0.5, f"Sanitization took too long: {elapsed:.3f}s"
        assert "sk-1234567890abcdef12345678" not in sanitized
        assert "super_secret_token_1234567890" not in sanitized
        assert "sk-embedded-998877665544" not in sanitized
        assert "sk-query-param-1234567890" not in sanitized


@pytest.mark.asyncio
class TestLogMaskingSecurity:
    """Adversarial validation of zero-leak log masking."""

    async def test_api_key_masking_filter_sanitizes_all_sensitive_tokens(self):
        """Verifies that MaskingFilter catches OpenAI, Bearer, Telegram tokens, and URL params without leakage."""
        filter_instance = MaskingFilter()

        # Sensitive strings to sanitize
        samples = [
            ("Connecting with key sk-proj-1234567890abcdef1234567890 to API", "sk-pro****7890"),
            ("Authorization: Bearer mySecretToken1234567890xyz", "Bearer [MASKED_TOKEN]"),
            ("Bot initialized with token 1234567890:ABCDefgh-123456789012345678901234567890", "1234567890:[MASKED_TELEGRAM_TOKEN]"),
            ('{"api_key": "super_secret_key_value_999"}', '{"api_key": "****"}'),
            ("https://api.provider.com/v1/chat?api_key=secretKey123456", "https://api.provider.com/v1/chat?api_key=[MASKED]"),
        ]

        for raw, expected_substr in samples:
            sanitized = filter_instance.sanitize(raw)
            assert expected_substr in sanitized
            assert "1234567890abcdef1234567890" not in sanitized
            assert "super_secret_key_value_999" not in sanitized
            assert "secretKey123456" not in sanitized

        # Verify LogRecord sanitization in Python logging pipeline
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="Sending request with key: %s and header %s",
            args=("sk-proj-9876543210abcdef9876543210", "Bearer secretToken12345678"),
            exc_info=None,
        )

        filter_instance.filter(record)
        assert "9876543210abcdef" not in str(record.args)
        assert "secretToken12345678" not in str(record.args)


# ============================================================================
# 7. Console Voice & Provider Integration Edge Cases
# ============================================================================

class TestConsoleVoiceAndProviderIntegration:
    """Verifies voice switching and provider connectivity resolution from console."""

    @pytest.mark.asyncio
    async def test_switch_voice_profile_api_success(self):
        """Verifies POST /api/voice/switch successfully changes active voice profile."""
        import uuid
        from unittest.mock import AsyncMock, patch
        app = create_app()
        transport = ASGITransport(app=app)
        unique_name = f"SwitchableProfile_{uuid.uuid4().hex[:6]}"
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # Create a test profile
            create_resp = await client.post("/api/voice/profiles", json={
                "name": unique_name,
                "gpt_weights_path": "weights/char.ckpt",
                "sovits_weights_path": "weights/char.pth",
                "refer_audio_path": "audio/ref.ogg",
                "refer_text": "こんにちは",
                "refer_language": "ja"
            })
            assert create_resp.status_code == 201
            profile_id = create_resp.json()["id"]

            try:
                # Switch to it with mocked backend
                with patch("galgame2voice.services.voice_manager.VoiceManager.switch_profile", new_callable=AsyncMock) as mock_switch:
                    mock_switch.return_value = True
                    switch_resp = await client.post("/api/voice/switch", json={"profile_id": profile_id})
                    assert switch_resp.status_code == 200
                    data = switch_resp.json()
                    assert data["status"] == "switched"
                    assert data["profile_id"] == profile_id
            finally:
                await client.delete(f"/api/voice/profiles/{profile_id}")

    @pytest.mark.asyncio
    async def test_switch_voice_profile_not_found(self):
        """Verifies POST /api/voice/switch with non-existent ID returns 404."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            switch_resp = await client.post("/api/voice/switch", json={"profile_id": 99999})
            assert switch_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Verifies GET /api/health endpoint."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # Health
            h_resp = await client.get("/api/health")
            assert h_resp.status_code == 200
            h_data = h_resp.json()
            assert h_data["status"] == "ok"
            assert "uptime_seconds" in h_data
            assert h_data["app"] == "galgame2voice"


# ============================================================================
# 8. Migrated M11 Provider Isolation, Boundary, Concurrency & Latency Checks
# ============================================================================

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


class TestUIBoundaryAndAdversarialStress:
    """Stress tests boundary conditions and concurrent operations."""

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


# ============================================================================
# 9. Migrated M5 Console Concurrency, Boundary & Recovery Tests
# ============================================================================

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


class TestNetworkTimeoutsAndErrorRecovery:
    """Stress-tests network timeouts, unreachable endpoints, DNS errors, and HTTP fault codes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [403, 404, 500, 502, 503])
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
            async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
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
            async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
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
            async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
                resp2 = await client.get("/api/system/status")
                assert resp2.status_code == 200
                data2 = resp2.json()
                assert data2["status"] == "healthy"
                assert data2["gpt_sovits"]["status"] == "reachable"
                assert data2["gpt_sovits"]["error"] is None


@pytest.mark.asyncio
class TestVoiceRouterHardening:
    @pytest.fixture
    async def client(self):
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as c:
            yield c

    async def test_voice_profile_id_negative_or_zero_returns_422(self, client):
        res = await client.get("/api/voice/profiles/0")
        assert res.status_code == 422
        assert "positive integer" in res.json()["detail"]

        res = await client.get("/api/voice/profiles/-5")
        assert res.status_code == 422

        res = await client.put("/api/voice/profiles/0", json={"name": "test"})
        assert res.status_code == 422

        res = await client.delete("/api/voice/profiles/0")
        assert res.status_code == 422

    async def test_voice_switch_validation(self, client):
        # Missing both id and name
        res = await client.post("/api/voice/switch", json={})
        assert res.status_code == 400

        # Whitespace-only name
        res = await client.post("/api/voice/switch", json={"profile_name": "   "})
        assert res.status_code == 400

        # Negative profile_id
        res = await client.post("/api/voice/switch", json={"profile_id": -1})
        assert res.status_code == 422

        # Non-existent profile returns 404 (404 precedence)
        res = await client.post("/api/voice/switch", json={"profile_id": 9999})
        assert res.status_code == 404

    async def test_voice_profile_create_length_bounds(self, client):
        # Empty name
        res = await client.post("/api/voice/profiles", json={
            "name": "",
            "gpt_weights_path": "a.ckpt",
            "sovits_weights_path": "a.pth",
        })
        assert res.status_code == 422

        # Name too long (> 100)
        res = await client.post("/api/voice/profiles", json={
            "name": "x" * 101,
            "gpt_weights_path": "a.ckpt",
            "sovits_weights_path": "a.pth",
        })
        assert res.status_code == 422


class TestVoiceRouterEndpoints:
    """Test suite for /api/voice/* endpoints on the production FastAPI application."""

    @pytest.mark.asyncio
    async def test_voice_profiles_crud_and_switch_api(self, isolate_test_database, mock_gpt_sovits, sample_voice_profile):
        await init_db(isolate_test_database)
        # Configure VoiceManager singleton with mock server
        manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=isolate_test_database)
        set_voice_manager(manager)

        app = create_app()
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # 1. Create Voice Profile
            post_resp = await client.post("/api/voice/profiles", json=sample_voice_profile)
            assert post_resp.status_code == 201
            data = post_resp.json()
            profile_id = data["id"]
            assert data["name"] == "Arona"

            # 2. List Profiles
            list_resp = await client.get("/api/voice/profiles")
            assert list_resp.status_code == 200
            profiles = list_resp.json()["profiles"]
            assert len(profiles) >= 1

            # 3. Get Single Profile
            get_resp = await client.get(f"/api/voice/profiles/{profile_id}")
            assert get_resp.status_code == 200
            assert get_resp.json()["profile"]["name"] == "Arona"

            # 4. Switch Voice Profile
            switch_resp = await client.post("/api/voice/switch", json={"profile_id": profile_id})
            assert switch_resp.status_code == 200
            assert switch_resp.json()["status"] == "switched"
            assert mock_gpt_sovits.current_gpt_weights == sample_voice_profile["gpt_weights_path"]

            # 5. Delete Profile
            del_resp = await client.delete(f"/api/voice/profiles/{profile_id}")
            assert del_resp.status_code == 200
            assert del_resp.json()["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_voice_router_error_cases(self, temp_db_path, mock_gpt_sovits):
        manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
        set_voice_manager(manager)

        app = create_app()
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # Switch nonexistent profile
            resp_switch = await client.post("/api/voice/switch", json={"profile_id": 99999})
            assert resp_switch.status_code == 404

            # Switch missing id and name
            resp_missing = await client.post("/api/voice/switch", json={})
            assert resp_missing.status_code == 400


