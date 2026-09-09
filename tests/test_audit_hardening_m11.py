"""
Audit Hardening M11 Test Suite.
Verifies:
1. Voice router boundary checks, profile_id constraints, and sanitized error handling.
2. Memory router input validation, character_id >= 1, fact value sanitization, and 422/404 handling.
3. Characters router (/api/characters) list, detail, and switch endpoints with 404 precedence and affection integration.
4. Health router URL scheme validation and error sanitization.
5. Telegram bot network drop safety, callback query boundary validation, and nickname sanitization.
6. Memory RAG empty key stem fix, prompt injection defense, and extreme length clamping.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport

from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.services.memory_service import MemoryService


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
# 2. Memory Router Hardening & Boundary Validation
# ============================================================================

@pytest.mark.asyncio
class TestMemoryRouterHardening:
    async def test_memory_id_bounds(self, client):
        res = await client.put("/api/memory/0", json={"fact_value": "new"})
        assert res.status_code == 422
        assert "positive integer" in res.json()["detail"]

        res = await client.delete("/api/memory/-1")
        assert res.status_code == 422

    async def test_memory_empty_user_id_returns_422(self, client):
        res = await client.get("/api/memory?user_id=   ")
        assert res.status_code == 422
        assert "user_id cannot be empty" in res.json()["detail"]

        res = await client.delete("/api/memory?user_id=   ")
        assert res.status_code == 422

    async def test_memory_character_id_bounds(self, client):
        res = await client.post("/api/memory", json={
            "user_id": "test_user",
            "character_id": 0,
            "fact_key": "fav_color",
            "fact_value": "blue",
        })
        assert res.status_code == 422

    async def test_memory_fact_value_sanitized_on_create_and_update(self, client):
        # Inject bidi override and curly braces
        dirty_val = "secret\u202e\u202d{injection}value"
        res = await client.post("/api/memory", json={
            "user_id": "sanitize_test_user",
            "character_id": 1,
            "fact_key": "test_key",
            "fact_value": dirty_val,
        })
        assert res.status_code == 201
        created = res.json()
        assert "\u202e" not in created["fact_value"]
        assert "{" not in created["fact_value"]
        assert "}" not in created["fact_value"]
        assert "injection" in created["fact_value"]

        # Update test
        res_up = await client.put(f"/api/memory/{created['id']}", json={
            "fact_value": "new\u200b{payload}"
        })
        assert res_up.status_code == 200
        assert "{" not in res_up.json()["fact_value"]
        assert "}" not in res_up.json()["fact_value"]
        assert "payload" in res_up.json()["fact_value"]


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

    async def test_get_character_detail(self, client):
        res = await client.get("/api/characters/1?user_id=char_test_user")
        assert res.status_code == 200
        data = res.json()
        assert data["id"] == 1
        assert data["name"] == "default_char"
        assert data["gpt_weights_path"] == "default.ckpt"
        assert data["affection"] is not None

        # 404 for non-existent character
        res404 = await client.get("/api/characters/9999")
        assert res404.status_code == 404

        # 422 for invalid character_id
        res422 = await client.get("/api/characters/0")
        assert res422.status_code == 422

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


# ============================================================================
# 5. Telegram Bot Hardening & Callback Query Bounds
# ============================================================================

@pytest.mark.asyncio
class TestTelegramBotHardening:
    async def test_safe_send_message_absorbs_drop(self):
        from galgame2voice.telegram_bot.handlers import TelegramBotHandlers
        handlers = TelegramBotHandlers()
        mock_update = MagicMock()
        mock_update.message.reply_text = AsyncMock(side_effect=Exception("Network connection lost"))
        # Should not raise exception
        result = await handlers._safe_send_message(mock_update, None, "test message")
        assert result is False

    async def test_telegram_callback_boundary_checks(self):
        from galgame2voice.telegram_bot.handlers import TelegramBotHandlers
        from telegram import Update, CallbackQuery, User

        handlers = TelegramBotHandlers()
        mock_update = MagicMock(spec=Update)
        mock_query = MagicMock(spec=CallbackQuery)
        mock_update.callback_query = mock_query
        mock_update.effective_chat.id = 12345
        mock_query.from_user = MagicMock(spec=User, id=12345, first_name="Test")
        mock_query.answer = AsyncMock()
        mock_query.edit_message_reply_markup = AsyncMock()

        # Test speed out of bounds (> 3.0)
        mock_query.data = "set_speed_99.9"
        await handlers.handle_callback_query(mock_update, MagicMock())
        mock_query.answer.assert_called_with("⚠️ 语速参数超出范围 (0.1~3.0)", show_alert=True)

        # Test temperature out of bounds (> 2.0)
        mock_query.data = "set_temp_5.0"
        await handlers.handle_callback_query(mock_update, MagicMock())
        mock_query.answer.assert_called_with("⚠️ 发音温度超出范围 (0.0~2.0)", show_alert=True)

        # Test history out of bounds (> 100)
        mock_query.data = "set_history_999"
        await handlers.handle_callback_query(mock_update, MagicMock())
        mock_query.answer.assert_called_with("⚠️ 记忆轮数超出范围 (1~100)", show_alert=True)

    async def test_telegram_nickname_sanitization(self):
        from galgame2voice.services.memory_service import MemoryService
        dirty_nick = "Admin\u202e\u202d{Master}12345678901234567890"
        clean = MemoryService.sanitize_fact_value(dirty_nick, max_len=20)
        assert len(clean) <= 20
        assert "\u202e" not in clean
        assert "{" not in clean
        assert "}" not in clean


# ============================================================================
# 6. Memory RAG Empty Key Stem Fix & Extreme String Clamping
# ============================================================================

class TestMemoryRAGRobustness:
    def test_empty_key_stem_does_not_match_all_prompts(self):
        from galgame2voice.services.memory_service import MemoryService
        svc = MemoryService()
        # If fact_key has no suffix after strip, stem should be guarded
        score = svc._calculate_overlap_score(
            prompt="今天天气真好",
            fact_key="like_",
            fact_value="喜欢",
        )
        # Should not falsely match with 0.8
        assert score == 0.0

    def test_extreme_prompt_length_clamping(self):
        from galgame2voice.services.memory_service import MemoryService
        svc = MemoryService()
        extreme_prompt = "a" * 100000
        # Should complete instantaneously without hanging or blowing memory
        score = svc._calculate_overlap_score(
            prompt=extreme_prompt,
            fact_key="like_apple",
            fact_value="苹果",
        )
        assert score >= 0.0

    def test_sanitize_fact_value_bidi_and_injection(self):
        from galgame2voice.services.memory_service import MemoryService
        raw = "User\u202e\u2066\u2069{prompt_override: true}【secret】\n\r"
        clean = MemoryService.sanitize_fact_value(raw, max_len=100)
        assert "\u202e" not in clean
        assert "\u2066" not in clean
        assert "{" not in clean
        assert "}" not in clean
        assert "【" not in clean
        assert "\n" not in clean
        assert "prompt_override: true" in clean
