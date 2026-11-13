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

import json
import logging
import re
import time
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

