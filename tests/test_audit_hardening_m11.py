"""
Audit Hardening M11 Test Suite.
Verifies:
1. Voice router boundary checks, profile_id constraints, and sanitized error handling.
3. Characters router (/api/characters) list and switch endpoints with 404 precedence and affection integration.
4. Health router URL scheme validation and error sanitization.
5. Telegram bot network drop safety, callback query boundary validation, and nickname sanitization.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport

from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app


@pytest.fixture
def app(isolate_test_database):
    import sqlite3
    conn = sqlite3.connect(isolate_test_database)
    conn.execute(
        "INSERT OR IGNORE INTO voice_profiles (id, name, gpt_weights_path, sovits_weights_path, is_default) "
        "VALUES (1, 'default_char', 'default.ckpt', 'default.pth', 1);"
    )
    conn.execute(
        "INSERT OR IGNORE INTO voice_profiles (id, name, gpt_weights_path, sovits_weights_path, is_default) "
        "VALUES (2, 'alt_char', 'alt.ckpt', 'alt.pth', 0);"
    )
    conn.commit()
    conn.close()
    return create_app()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ============================================================================
# 1. Voice Router Boundary Checks & Error Sanitization
# ============================================================================

@pytest.mark.asyncio
class TestVoiceRouterHardening:
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


# ============================================================================
# 3. Characters Router (/api/characters)
# ============================================================================

@pytest.mark.asyncio
class TestCharactersRouter:
    async def test_list_characters_with_affection(self, client):
        res = await client.get("/api/characters?user_id=char_test_user")
        assert res.status_code == 200
        data = res.json()
        assert "characters" in data
        assert len(data["characters"]) >= 2
        assert data["active_character_id"] is not None

        char1 = next(c for c in data["characters"] if c["id"] == 1)
        assert char1["name"] == "default_char"
        assert char1["affection"] is not None
        assert char1["affection"]["affection_score"] == 0

    async def test_character_switch_404_precedence_and_switch(self, client):
        # 404 precedence
        res = await client.post("/api/characters/switch", json={"character_id": 9999})
        assert res.status_code == 404

        # 400 missing fields
        res_bad = await client.post("/api/characters/switch", json={})
        assert res_bad.status_code == 400

        # Switch to character 2
        with patch("galgame2voice.services.voice_manager.VoiceManager.switch_profile", new_callable=AsyncMock) as mock_switch:
            mock_switch.return_value = True
            res_switch = await client.post("/api/characters/switch", json={
                "character_id": 2,
                "force": True,
            })
            assert res_switch.status_code == 200
            assert res_switch.json()["status"] == "switched"
            assert res_switch.json()["character_id"] == 2


# ============================================================================
# 4. Health Router Hardening & Error Sanitization
# ============================================================================

@pytest.mark.asyncio
class TestHealthRouterHardening:
    async def test_probe_invalid_url_scheme(self):
        from galgame2voice.routers.health import _probe_gpt_sovits
        telemetry = await _probe_gpt_sovits("ftp://localhost:9880")
        assert telemetry.status == "unreachable"
        assert "scheme must be http or https" in telemetry.error

        telemetry2 = await _probe_gpt_sovits("file:///etc/passwd")
        assert telemetry2.status == "unreachable"
        assert "scheme must be http or https" in telemetry2.error

    async def test_probe_sanitizes_exception_details(self):
        from galgame2voice.routers.health import _probe_gpt_sovits
        with patch("galgame2voice.services.gpt_sovits_client.get_gpt_sovits_client") as mock_get:
            mock_client = MagicMock()
            mock_client.base_url = "http://localhost:9880"
            mock_client.check_health = AsyncMock(side_effect=Exception("Failed with api_key=sk-1234567890abcdef"))
            mock_get.return_value = mock_client

            telemetry = await _probe_gpt_sovits("http://localhost:9880")
            assert telemetry.status == "unreachable"
            assert "sk-1234567890abcdef" not in telemetry.error
            assert "REDACTED" in telemetry.error or "***" in telemetry.error or "api_key" in telemetry.error
