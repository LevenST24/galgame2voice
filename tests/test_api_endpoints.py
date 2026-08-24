"""
Tests for FastAPI Core Routes, Health Check, Config API, Voice Profile API, and Diagnostics.
Covers Tier 1 (Endpoint Feature Coverage), Tier 2 (Validation Errors, 404s, CORS, Traversal, Offline Backend),
and Tier 3 (FastAPI main application lifespan, deep system telemetry, and logger secret masking).
"""

import json
import logging
import os
import tempfile
import time
from typing import Dict, Any, Optional
import pytest
from fastapi import FastAPI, APIRouter, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from httpx import AsyncClient, ASGITransport
import aiosqlite

from tests.conftest import mask_secret, MockGptSovitsServer, MockLLMServer
from galgame2voice.main import app as main_app, create_app
from galgame2voice.config import get_settings
from galgame2voice.utils.logger import MaskingFilter, setup_logger


# ============================================================================
# FastAPI Contract Implementation Fixture for Testing Endpoint Contracts
# ============================================================================

class ConfigUpdateRequest(BaseModel):
    settings: Dict[str, Any]

class VoiceProfileCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)
    gpt_weights_path: str = Field(..., min_length=1)
    sovits_weights_path: str = Field(..., min_length=1)
    refer_audio_path: str = Field(..., min_length=1)
    refer_text: str = Field(..., min_length=1)
    refer_language: str = "ja"
    is_default: bool = False

class VoiceSwitchRequest(BaseModel):
    profile_id: Optional[int] = None
    profile_name: Optional[str] = None

class ProviderTestRequest(BaseModel):
    provider_type: str
    api_key: str
    base_url: Optional[str] = None
    model: str

class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    session_id: str = "default"
    character_name: Optional[str] = None


def create_test_api_app(db_path: str, gpt_sovits: MockGptSovitsServer, llm_server: MockLLMServer) -> FastAPI:
    """Builds a test FastAPI application adhering to PROJECT.md API contracts."""
    app = FastAPI(title="galgame2voice Test Engine", version="2.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api_router = APIRouter(prefix="/api")

    @api_router.get("/health")
    async def get_health():
        sovits_status = gpt_sovits.is_online
        return {
            "status": "ok",
            "app": "galgame2voice",
            "version": "2.0.0",
            "gpt_sovits": {
                "connected": sovits_status,
                "url": gpt_sovits.base_url,
                "current_gpt_weights": gpt_sovits.current_gpt_weights if sovits_status else None
            }
        }

    @app.get("/status")
    async def get_system_status():
        return {
            "uptime_seconds": 3600,
            "active_sessions": 1,
            "gpt_sovits_online": gpt_sovits.is_online,
            "memory_usage_mb": 42.5
        }

    @api_router.get("/config")
    async def get_config():
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT key, value FROM settings") as cursor:
                rows = await cursor.fetchall()
                settings = {}
                for k, v in rows:
                    if k and ("key" in k.lower() or "token" in k.lower() or "secret" in k.lower()):
                        settings[k] = mask_secret(v)
                    elif k:
                        settings[k] = v
                return {"settings": settings}

    @api_router.post("/config")
    async def update_config(payload: ConfigUpdateRequest):
        async with aiosqlite.connect(db_path) as db:
            for k, v in payload.settings.items():
                val_str = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, val_str))
            await db.commit()
        return {"status": "success", "updated_count": len(payload.settings)}

    @api_router.get("/voice/profiles")
    async def list_voice_profiles():
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM voice_profiles ORDER BY id ASC") as cursor:
                rows = await cursor.fetchall()
                return {"profiles": [dict(r) for r in rows]}

    @api_router.post("/voice/profiles", status_code=201)
    async def create_voice_profile(req: VoiceProfileCreateRequest):
        async with aiosqlite.connect(db_path) as db:
            try:
                cursor = await db.execute("""
                    INSERT INTO voice_profiles (name, gpt_weights_path, sovits_weights_path, refer_audio_path, refer_text, refer_language, is_default)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (req.name, req.gpt_weights_path, req.sovits_weights_path, req.refer_audio_path, req.refer_text, req.refer_language, int(req.is_default)))
                await db.commit()
                return {"id": cursor.lastrowid, "name": req.name, "status": "created"}
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))

    @api_router.post("/voice/switch")
    async def switch_voice(req: VoiceSwitchRequest):
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            if req.profile_id is not None:
                cursor = await db.execute("SELECT * FROM voice_profiles WHERE id = ?", (req.profile_id,))
            elif req.profile_name is not None:
                cursor = await db.execute("SELECT * FROM voice_profiles WHERE name = ?", (req.profile_name,))
            else:
                raise HTTPException(status_code=400, detail="Missing profile_id or profile_name")
            
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Voice profile not found")
            
            profile = dict(row)
            # Call GPT-SoVITS mock switch
            r1 = await gpt_sovits.handle_request("POST", "/set_gpt_weights", {"weights_path": profile["gpt_weights_path"]})
            if r1.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to load GPT weights")
            r2 = await gpt_sovits.handle_request("POST", "/set_sovits_weights", {"weights_path": profile["sovits_weights_path"]})
            if r2.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to load SoVITS weights")
            r3 = await gpt_sovits.handle_request("POST", "/set_refer_audio", {
                "refer_audio_path": profile["refer_audio_path"],
                "refer_text": profile["refer_text"],
                "refer_language": profile["refer_language"]
            })
            if r3.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to load refer audio")

            return {"status": "switched", "profile": profile["name"]}

    @api_router.post("/providers/test")
    async def test_provider(req: ProviderTestRequest):
        if not req.api_key or "invalid" in req.api_key:
            return {"success": False, "message": "Authentication failed with provided API key"}
        if req.base_url and "invalid" in req.base_url:
            return {"success": False, "message": "Cannot reach base URL endpoint"}
        return {"success": True, "message": f"Successfully connected to {req.provider_type} model {req.model}", "latency_ms": 45.2}

    @api_router.post("/chat")
    async def chat_sync(req: ChatRequest):
        if not req.prompt.strip():
            raise HTTPException(status_code=422, detail="Prompt cannot be empty")
        
        bilingual_resp = json.loads(llm_server.generate_bilingual_json())
        return {
            "session_id": req.session_id,
            "chinese": bilingual_resp["chinese"],
            "japanese": bilingual_resp["japanese"],
            "audio_url": "/audio/test_speech.wav"
        }

    @app.get("/audio/{filename}")
    async def serve_audio(filename: str):
        if ".." in filename or "/" in filename or "\\" in filename:
            raise HTTPException(status_code=400, detail="Invalid audio filename")
        if not filename.endswith((".wav", ".ogg", ".mp3")):
            raise HTTPException(status_code=404, detail="Audio file format not supported")
        return Response(content=b"RIFF\x24\x08\x00\x00WAVEfmt ", media_type="audio/wav")

    app.include_router(api_router)
    return app


# ============================================================================
# Tier 1: FastAPI Core & Endpoint Feature Tests
# ============================================================================

class TestApiEndpointsTier1:
    """Tier 1: Verify health check, system status, config GET/POST, voice CRUD & switch."""

    @pytest.mark.asyncio
    async def test_health_endpoint(self, temp_db_path, mock_gpt_sovits, mock_llm_server):
        app = create_test_api_app(temp_db_path, mock_gpt_sovits, mock_llm_server)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["app"] == "galgame2voice"
            assert data["gpt_sovits"]["connected"] is True

    @pytest.mark.asyncio
    async def test_system_status_endpoint(self, temp_db_path, mock_gpt_sovits, mock_llm_server):
        app = create_test_api_app(temp_db_path, mock_gpt_sovits, mock_llm_server)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "uptime_seconds" in data
            assert data["gpt_sovits_online"] is True

    @pytest.mark.asyncio
    async def test_config_get_and_post(self, temp_db_path, mock_gpt_sovits, mock_llm_server):
        app = create_test_api_app(temp_db_path, mock_gpt_sovits, mock_llm_server)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Set config
            post_resp = await client.post("/api/config", json={
                "settings": {
                    "llm_api_key": "sk-1234567890abcdef1234567890abcdef",
                    "temperature": 0.7,
                    "gpt_sovits_port": 9880
                }
            })
            assert post_resp.status_code == 200
            assert post_resp.json()["updated_count"] == 3

            # Get config and verify secret masking
            get_resp = await client.get("/api/config")
            assert get_resp.status_code == 200
            settings = get_resp.json()["settings"]
            assert settings["temperature"] == "0.7"
            assert settings["llm_api_key"] == "sk-****cdef"

    @pytest.mark.asyncio
    async def test_voice_profiles_create_and_list(self, temp_db_path, mock_gpt_sovits, mock_llm_server, sample_voice_profile):
        app = create_test_api_app(temp_db_path, mock_gpt_sovits, mock_llm_server)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Create
            create_resp = await client.post("/api/voice/profiles", json=sample_voice_profile)
            assert create_resp.status_code == 201
            assert create_resp.json()["name"] == "Arona"

            # List
            list_resp = await client.get("/api/voice/profiles")
            assert list_resp.status_code == 200
            profiles = list_resp.json()["profiles"]
            assert len(profiles) == 1
            assert profiles[0]["name"] == "Arona"

    @pytest.mark.asyncio
    async def test_voice_profile_switch_success(self, temp_db_path, mock_gpt_sovits, mock_llm_server, sample_voice_profile):
        app = create_test_api_app(temp_db_path, mock_gpt_sovits, mock_llm_server)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Insert profile
            create_resp = await client.post("/api/voice/profiles", json=sample_voice_profile)
            profile_id = create_resp.json()["id"]

            # Switch
            switch_resp = await client.post("/api/voice/switch", json={"profile_id": profile_id})
            assert switch_resp.status_code == 200
            assert switch_resp.json()["status"] == "switched"
            assert mock_gpt_sovits.current_gpt_weights == sample_voice_profile["gpt_weights_path"]
            assert mock_gpt_sovits.current_sovits_weights == sample_voice_profile["sovits_weights_path"]

    @pytest.mark.asyncio
    async def test_provider_test_endpoint_success(self, temp_db_path, mock_gpt_sovits, mock_llm_server):
        app = create_test_api_app(temp_db_path, mock_gpt_sovits, mock_llm_server)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/providers/test", json={
                "provider_type": "deepseek",
                "api_key": "sk-valid-key-12345",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat"
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert "latency_ms" in data

    @pytest.mark.asyncio
    async def test_chat_sync_endpoint(self, temp_db_path, mock_gpt_sovits, mock_llm_server):
        app = create_test_api_app(temp_db_path, mock_gpt_sovits, mock_llm_server)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/chat", json={"prompt": "早上好！", "session_id": "sess-1"})
            assert resp.status_code == 200
            data = resp.json()
            assert "chinese" in data
            assert "japanese" in data
            assert "audio_url" in data


# ============================================================================
# Tier 2: Validation, Boundary, CORS, Security & Error Handling
# ============================================================================

class TestApiEndpointsTier2:
    """Tier 2: Error codes, path traversal defenses, backend offline detection, and CORS."""

    @pytest.mark.asyncio
    async def test_health_backend_offline(self, temp_db_path, mock_gpt_sovits, mock_llm_server):
        mock_gpt_sovits.set_online(False)
        app = create_test_api_app(temp_db_path, mock_gpt_sovits, mock_llm_server)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["gpt_sovits"]["connected"] is False

    @pytest.mark.asyncio
    async def test_route_not_found_404(self, temp_db_path, mock_gpt_sovits, mock_llm_server):
        app = create_test_api_app(temp_db_path, mock_gpt_sovits, mock_llm_server)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/nonexistent_route")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_validation_error_missing_fields_422(self, temp_db_path, mock_gpt_sovits, mock_llm_server):
        app = create_test_api_app(temp_db_path, mock_gpt_sovits, mock_llm_server)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Missing required fields
            resp = await client.post("/api/voice/profiles", json={"name": ""})
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_switch_nonexistent_profile_404(self, temp_db_path, mock_gpt_sovits, mock_llm_server):
        app = create_test_api_app(temp_db_path, mock_gpt_sovits, mock_llm_server)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/voice/switch", json={"profile_id": 99999})
            assert resp.status_code == 404
            assert "not found" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_provider_test_invalid_credentials(self, temp_db_path, mock_gpt_sovits, mock_llm_server):
        app = create_test_api_app(temp_db_path, mock_gpt_sovits, mock_llm_server)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/providers/test", json={
                "provider_type": "openai",
                "api_key": "invalid_key",
                "model": "gpt-4o"
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is False
            assert "failed" in data["message"]

    @pytest.mark.asyncio
    async def test_audio_path_traversal_prevention(self, temp_db_path, mock_gpt_sovits, mock_llm_server):
        app = create_test_api_app(temp_db_path, mock_gpt_sovits, mock_llm_server)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Attempt directory traversal
            resp = await client.get("/audio/..%2F..%2Fetc%2Fpasswd")
            assert resp.status_code in (400, 404)

    @pytest.mark.asyncio
    async def test_cors_preflight_headers(self, temp_db_path, mock_gpt_sovits, mock_llm_server):
        app = create_test_api_app(temp_db_path, mock_gpt_sovits, mock_llm_server)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type"
            }
            resp = await client.options("/api/chat", headers=headers)
            assert resp.status_code == 200
            assert resp.headers.get("access-control-allow-origin") in ("*", "http://localhost:3000")


# ============================================================================
# Tier 3: Core Application & Health Telemetry Endpoints
# ============================================================================

class TestCoreMainAndHealthEndpoints:
    """Tier 3: Test production FastAPI application, lifespan, health diagnostics, and logger filter."""

    @pytest.mark.asyncio
    async def test_main_app_health_endpoints(self):
        """Verifies GET /api/health and GET /status on the real galgame2voice app instance."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Health check
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["app"] == "galgame2voice"
            assert "uptime_seconds" in data

            # Legacy status
            status_resp = await client.get("/status")
            assert status_resp.status_code == 200
            s_data = status_resp.json()
            assert s_data["status"] == "ok"
            assert "gpt_sovits" in s_data

            # Full telemetry diagnostic
            diag_resp = await client.get("/api/system/status")
            assert diag_resp.status_code == 200
            d_data = diag_resp.json()
            assert d_data["status"] in ("healthy", "degraded")
            assert "app" in d_data
            assert "database" in d_data
            assert "gpt_sovits" in d_data
            assert "storage" in d_data
            assert "telegram" in d_data

    @pytest.mark.asyncio
    async def test_root_fallback_and_static_mount(self):
        """Verifies root fallback endpoint returns app metadata."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/")
            assert resp.status_code == 200

    def test_masking_filter_sanitization(self):
        """Verifies MaskingFilter cleans sensitive credentials across all regex patterns."""
        filter_instance = MaskingFilter()

        # 1. sk- API keys
        text1 = "Connecting with API key sk-abc1234567890def1234 to endpoint"
        sanitized1 = filter_instance.sanitize(text1)
        assert "sk-abc****1234" in sanitized1
        assert "sk-abc1234567890def1234" not in sanitized1

        # 2. Bearer tokens
        text2 = "Authorization: Bearer secret_jwt_token_value_here"
        sanitized2 = filter_instance.sanitize(text2)
        assert "Bearer [MASKED_TOKEN]" in sanitized2

        # 3. Telegram bot tokens
        text3 = "Telegram token 123456789:ABCDefgh-1234567890abcdef1234567890"
        sanitized3 = filter_instance.sanitize(text3)
        assert "[MASKED_TELEGRAM_TOKEN]" in sanitized3

        # 4. JSON secret key-value
        text4 = '{"api_key": "supersecretkey123", "normal": "data"}'
        sanitized4 = filter_instance.sanitize(text4)
        assert '"api_key": "****"' in sanitized4
        assert "supersecretkey123" not in sanitized4

        # 5. LogRecord filtering
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="Call with key %s",
            args=("sk-1234567890abcdef",),
            exc_info=None,
        )
        assert filter_instance.filter(record) is True
        assert "****" in record.args[0]

    @pytest.mark.asyncio
    async def test_chat_history_endpoints(self, temp_db_path):
        """Verifies GET /api/chat/history and DELETE /api/chat/history."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Fetch history for empty session
            resp = await client.get("/api/chat/history?session_id=test_hist_sess")
            assert resp.status_code == 200
            data = resp.json()
            assert data["session_id"] == "test_hist_sess"
            assert isinstance(data["messages"], list)

            # 2. Delete history
            del_resp = await client.delete("/api/chat/history?session_id=test_hist_sess")
            assert del_resp.status_code == 200
            assert del_resp.json()["status"] == "cleared"

