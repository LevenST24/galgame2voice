"""
Unit and Integration Tests for Web Management Console & Security Controls (Milestone 5).
Covers:
- Route and Redirect Support (/settings.html, /console, /settings with query string preservation)
- Frontend Web Management Console HTML Structure and Content Verification
- API Key Masking, Zero-Leakage, and Secure Retention on Update
- Provider Management API (Presets, CRUD, Activation, Connectivity Testing, Model Discovery)
- Voice Profile Management API (CRUD, Switch, Presets, Slicing Methods)
- Inference Parameter Tuning & SQLite Persistence
- Telegram Bot Configuration & Testing API
- System Diagnostics Telemetry (/api/system/status)
"""

import json
import logging
import re
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
from galgame2voice.utils.logger import MaskingFilter


# ============================================================================
# 1. Route & Redirect Support Tests
# ============================================================================

class TestConsoleRoutesAndRedirects:
    """Verifies clean routing and redirection for /settings.html, /console, and /settings."""

    @pytest.mark.asyncio
    async def test_serve_settings_html_200(self):
        """Verifies GET /settings.html serves the Web Management Console with HTTP 200."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/settings.html")
            assert resp.status_code == 200
            assert "text/html" in resp.headers.get("content-type", "")
            content = resp.text
            assert "galgame2voice - 控制台与系统配置" in content
            assert "系统状态看板" in content
            assert "LLM / STT 模型" in content
            assert "角色音色管理" in content
            assert "推理与切分参数" in content
            assert "Telegram 机器人" in content
            assert "安全与密钥控制" in content

    @pytest.mark.asyncio
    async def test_console_redirect_to_settings_html(self):
        """Verifies GET /console returns a 307 redirect to /settings.html."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            resp = await client.get("/console")
            assert resp.status_code == 307
            assert resp.headers["location"] == "/settings.html"

    @pytest.mark.asyncio
    async def test_console_redirect_preserves_query_parameters(self):
        """Verifies GET /console?token=sec123&lang=zh preserves query string in redirect."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            resp = await client.get("/console?token=sec123&lang=zh")
            assert resp.status_code == 307
            assert resp.headers["location"] == "/settings.html?token=sec123&lang=zh"

    @pytest.mark.asyncio
    async def test_settings_alias_redirect(self):
        """Verifies GET /settings alias redirect to /settings.html."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            resp = await client.get("/settings")
            assert resp.status_code == 307
            assert resp.headers["location"] == "/settings.html"


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
        assert is_masked_key("sk-****cdef") is True
        assert is_masked_key("123****GhI") is True
        assert is_masked_key("sk-plain-secret-key-123") is False

    @pytest.mark.asyncio
    async def test_get_config_masks_telegram_token(self):
        """Verifies GET /api/config returns masked telegram token."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Update with plaintext secret token
            await client.post("/api/config", json={
                "settings": {
                    "telegram_bot_token": "987654321:AAHkjlmnopqrstuvwxyz12345"
                }
            })

            # Retrieve config and verify masking
            resp = await client.get("/api/config")
            assert resp.status_code == 200
            data = resp.json()
            token = data["settings"]["telegram_bot_token"]
            assert token.startswith("987****")
            assert "AAHkjl" not in token
            assert "****" in token

    @pytest.mark.asyncio
    async def test_provider_api_key_masked_on_retrieval(self):
        """Verifies GET /api/providers and GET /api/providers/{id} return masked keys."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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

    @pytest.mark.asyncio
    async def test_telegram_masked_token_submission_retains_secret(self):
        """Verifies submitting masked Telegram token preserves raw token in settings table."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Set secret token
            await client.post("/api/config", json={
                "settings": {"telegram_bot_token": "123456789:ABCDefgh-1234567890abcdef"}
            })

            # 2. Update with masked token
            await client.post("/api/config", json={
                "settings": {
                    "telegram_bot_token": "123****cdef",
                    "telegram_bot_username": "new_bot_name"
                }
            })

            # 3. Verify raw in DB
            async with get_db() as conn:
                raw_settings = await crud.get_settings_raw(conn)
                assert raw_settings.telegram_bot_token == "123456789:ABCDefgh-1234567890abcdef"
                assert raw_settings.telegram_bot_username == "new_bot_name"


# ============================================================================
# 3. Provider Management API Tests
# ============================================================================

class TestProviderManagementEndpoints:
    """Verifies preset listings, provider activation, connectivity tests, and model discovery."""

    @pytest.mark.asyncio
    async def test_list_provider_presets(self):
        """Verifies GET /api/providers/presets returns major provider presets."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/providers/presets")
            assert resp.status_code == 200
            presets = resp.json()["presets"]
            assert len(presets) >= 8
            preset_ids = {p["id"] for p in presets}
            assert "deepseek" in preset_ids
            assert "openai" in preset_ids
            assert "qwen" in preset_ids
            assert "glm" in preset_ids
            assert "gemini" in preset_ids
            assert "anthropic" in preset_ids
            assert "xai" in preset_ids
            assert "custom" in preset_ids

    @pytest.mark.asyncio
    async def test_activate_provider_endpoint(self):
        """Verifies POST /api/providers/{id}/activate sets active provider and syncs settings."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
    async def test_provider_model_discovery(self):
        """Verifies GET /api/providers/{id}/models returns models list."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/providers/deepseek/models")
            assert resp.status_code == 200
            data = resp.json()
            assert data["provider_id"] == "deepseek"
            assert isinstance(data["models"], list)
            assert len(data["models"]) > 0

    @pytest.mark.asyncio
    async def test_provider_delete_endpoint(self):
        """Verifies DELETE /api/providers/{id} deletes a configured provider."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
    async def test_voice_profiles_crud_and_presets(self):
        """Verifies voice profiles listing, creation, updates, and TTS presets."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Presets
            presets_resp = await client.get("/api/voice/presets")
            assert presets_resp.status_code == 200
            p_data = presets_resp.json()
            assert "presets" in p_data
            assert "high_quality" in p_data["presets"]
            assert "balanced" in p_data["presets"]
            assert "low_latency" in p_data["presets"]
            assert "slicing_methods" in p_data
            assert "cut5" in p_data["slicing_methods"]

            # 2. Create Profile
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

            # 3. Update Profile
            update_resp = await client.put(f"/api/voice/profiles/{profile_id}", json={
                "description": "Updated character description",
                "prompt_lang": "zh"
            })
            assert update_resp.status_code == 200
            assert update_resp.json()["profile"]["description"] == "Updated character description"

            # 4. List Profiles
            list_resp = await client.get("/api/voice/profiles")
            assert list_resp.status_code == 200
            assert any(p["name"] == "ConsoleTestCharacter" for p in list_resp.json()["profiles"])

            # 5. Delete Profile
            del_resp = await client.delete(f"/api/voice/profiles/{profile_id}")
            assert del_resp.status_code == 200

    @pytest.mark.asyncio
    async def test_inference_parameters_persistence(self):
        """Verifies POST /api/config updates all fine-tuning knobs and persists them."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
# 5. Telegram Testing & Diagnostics Tests
# ============================================================================

class TestTelegramAndSystemDiagnostics:
    """Verifies Telegram test endpoint and comprehensive diagnostic telemetry."""

    @pytest.mark.asyncio
    async def test_telegram_test_without_token(self):
        """Verifies POST /api/telegram/test returns error when no token is provided or stored."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Clear token in DB
            await client.post("/api/config", json={"settings": {"telegram_bot_token": ""}})

            resp = await client.post("/api/telegram/test", json={"token": ""})
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "未配置" in data["message"]

    @pytest.mark.asyncio
    async def test_system_status_diagnostics_endpoint(self):
        """Verifies GET /api/system/status provides full telemetry response."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
            assert "telegram" in data


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
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            switch_resp = await client.post("/api/voice/switch", json={"profile_id": 99999})
            assert switch_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_health_check_and_legacy_status(self):
        """Verifies GET /api/health and GET /status endpoints."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Health
            h_resp = await client.get("/api/health")
            assert h_resp.status_code == 200
            h_data = h_resp.json()
            assert h_data["status"] == "ok"
            assert "uptime_seconds" in h_data
            assert h_data["app"] == "galgame2voice"

            # Legacy status
            s_resp = await client.get("/status")
            assert s_resp.status_code == 200
            s_data = s_resp.json()
            assert s_data["status"] == "ok"
            assert "gpt_sovits" in s_data

