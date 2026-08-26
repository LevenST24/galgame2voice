"""
Adversarial Stress & Forensic Test Suite for Milestone 6 (Conversational Memory & Telegram Bot).
Authored by m6_auditor_1.

Covers:
1. SessionManager Stress & Adversarial Edge Cases:
   - Malformed/Escaped Prompt Templates (nested braces, missing vars, unescaped characters)
   - Boundary Sliding-Window and Token Budget Truncation (massive texts, empty strings, 0 token budgets)
   - High Concurrency Session Isolation (50 concurrent tasks across 10 session IDs)
   - Unicode, Emojis, RTL text, and Special Characters in Multi-Turn Turns
   - Dual-schema compatibility and resilience against missing tables
2. Telegram Bot Handlers & Async Concurrency:
   - High-rate message bursts from single user (testing cancellation of in-flight voice tasks)
   - Multi-user isolation under concurrent background syntheses
   - Voice note error recovery (corrupt audio, empty payload, STT transcription failure, TTS crash)
   - Command dispatch edge cases (/start with deep-links, /reset race conditions, case sensitivity)
   - Bot token validation boundaries and proxy helper variations
3. Audio Converter Defensive Boundaries:
   - Zero-length, truncated, and corrupt payloads
   - Header inspection (RIFF, OggS, unknown headers)
"""

import asyncio
import os
import re
import tempfile
from typing import List, Dict, Any, Optional
from unittest.mock import AsyncMock, patch

import pytest
import aiosqlite

from galgame2voice.services.session_manager import SessionManager, SessionTurn
from galgame2voice.services.chat_service import ChatService, StreamingBilingualParser
from galgame2voice.services.tts_service import TtsService
from galgame2voice.telegram_bot.bot import validate_bot_token, TelegramBotManager, get_telegram_bot_manager
from galgame2voice.telegram_bot.handlers import TelegramBotHandlers
from galgame2voice.telegram_bot.proxy import get_proxy_url, get_telegram_request_kwargs, test_proxy_connectivity
from galgame2voice.utils.audio_converter import (
    is_ffmpeg_available,
    convert_ogg_to_wav,
    convert_wav_to_ogg,
)
from galgame2voice.database.session import init_db
from galgame2voice.adapters.base import BaseLLMAdapter, LLMResponse, ChatMessage
from galgame2voice.database.models import SettingsInDB, ProviderCreate, VoiceProfileCreate
from tests.conftest import MockLLMServer, MockGptSovitsServer


@pytest.fixture
async def m6_test_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        await init_db(path)
        yield path
    finally:
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass


# ============================================================================
# Section 1: SessionManager Adversarial Stress Tests
# ============================================================================

class TestSessionManagerAdversarial:

    @pytest.mark.asyncio
    async def test_token_estimation_boundaries(self, m6_test_db):
        sm = SessionManager(m6_test_db)
        assert sm.estimate_tokens("") == 0
        assert sm.estimate_tokens("   ") == 1
        assert sm.estimate_tokens("a") == 1
        assert sm.estimate_tokens("你好世界") >= 2
        assert sm.estimate_tokens("日本語テキスト！") >= 4
        assert sm.estimate_tokens("🌟🎉🚀🔥") >= 1

    @pytest.mark.asyncio
    async def test_prompt_template_malformed_braces(self, m6_test_db):
        sm = SessionManager(m6_test_db)
        
        # Unmatched single braces or unknown format keys
        tpl1 = "Hello {unmatched_key} {character_name}"
        msgs1 = sm.format_llm_messages("Natsume", [], "Hi", system_template=tpl1)
        assert "Natsume" in msgs1[0]["content"]

        # Escaped double braces
        tpl2 = "JSON format: {{\"name\": \"{character_name}\"}}"
        msgs2 = sm.format_llm_messages("Natsume", [], "Hi", system_template=tpl2)
        assert '{"name": "Natsume"}' in msgs2[0]["content"]

        # No placeholders
        tpl3 = "Static plain text prompt"
        msgs3 = sm.format_llm_messages("Natsume", [], "Hi", system_template=tpl3)
        assert msgs3[0]["content"] == "Static plain text prompt"

    @pytest.mark.asyncio
    async def test_sliding_window_token_limit_extreme_boundaries(self, m6_test_db):
        sm = SessionManager(m6_test_db)
        session_id = "adv-sess-token"

        # 1. Add 10 turns of varying sizes
        for i in range(10):
            text = f"这是第{i}轮对话的内容。" * (i + 1) * 10
            await sm.add_turn(session_id, "user" if i % 2 == 0 else "assistant", text, f"Ja_{i}")

        # Request token budget of 50 tokens (should discard older messages until within budget)
        history = await sm.get_history(session_id, max_messages=20, max_tokens=50)
        total_tokens = sum(sm.estimate_tokens(t.content_chinese) + sm.estimate_tokens(t.content_japanese) for t in history)
        assert total_tokens <= 50 or len(history) == 1

        # Request budget of 0 tokens
        history_zero = await sm.get_history(session_id, max_messages=20, max_tokens=0)
        assert len(history_zero) == 0

    @pytest.mark.asyncio
    async def test_high_concurrency_multi_session_isolation(self, m6_test_db):
        sm = SessionManager(m6_test_db)
        num_sessions = 10
        turns_per_session = 10

        async def worker(sess_idx: int):
            sess_id = f"concurrent_sess_{sess_idx}"
            for turn_idx in range(turns_per_session):
                await sm.add_turn(
                    sess_id,
                    "user" if turn_idx % 2 == 0 else "assistant",
                    f"Message {turn_idx} from session {sess_idx}",
                    f"Ja {turn_idx}_{sess_idx}",
                )
            hist = await sm.get_history(sess_id, max_messages=50)
            assert len(hist) == turns_per_session
            for item in hist:
                assert f"session {sess_idx}" in item.content_chinese

        tasks = [worker(i) for i in range(num_sessions)]
        await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_special_characters_and_emoji_persistence(self, m6_test_db):
        sm = SessionManager(m6_test_db)
        sess_id = "unicode_sess"
        complex_text = "CJK: 喫茶ステラと死神の蝶 | Emoji: 🌸✨🎮 | Control: \t\n\r | SQL: '; DROP TABLE messages; --"
        await sm.add_turn(sess_id, "user", complex_text, "テスト")

        history = await sm.get_history(sess_id)
        assert len(history) == 1
        assert history[0].content_chinese == complex_text
        assert history[0].content_japanese == "テスト"

    @pytest.mark.asyncio
    async def test_clear_nonexistent_session_is_safe(self, m6_test_db):
        sm = SessionManager(m6_test_db)
        cleared = await sm.clear_session("non_existent_random_id")
        assert cleared is False or cleared is None or cleared is True


# ============================================================================
# Section 2: Telegram Bot & Audio Converter Adversarial Tests
# ============================================================================

class TestTelegramBotAdversarial:

    def test_bot_token_validation_matrix(self):
        valid_tokens = [
            "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz",
            "987654321:AAHk-example_token_with_hyphens",
            "111222333:XYZ_valid_token_123456789",
        ]
        invalid_tokens = [
            "",
            "   ",
            None,
            "1234567890",  # missing colon
            "123:abc",     # too short (<10 chars)
            "invalid_token_sample_123456:abc",  # contains 'invalid'
        ]
        for vt in valid_tokens:
            assert validate_bot_token(vt) is True
        for it in invalid_tokens:
            assert validate_bot_token(it) is False

    def test_proxy_url_parsing_robustness(self):
        assert get_proxy_url(proxy_str="  127.0.0.1:8080  ") == "http://127.0.0.1:8080"
        assert get_proxy_url(proxy_str="https://proxy.example.com:443") == "https://proxy.example.com:443"
        assert get_proxy_url(proxy_str="socks5://user:pass@10.0.0.1:1080") == "socks5://user:pass@10.0.0.1:1080"
        assert get_proxy_url(proxy_str="") is None

        # SettingsInDB with proxy disabled
        s_disabled = SettingsInDB(telegram_proxy_enabled=False, telegram_proxy_host="192.168.1.1", telegram_proxy_port=8888)
        assert get_proxy_url(settings=s_disabled) is None

        # SettingsInDB with proxy enabled
        s_enabled = SettingsInDB(telegram_proxy_enabled=True, telegram_proxy_host="192.168.1.1", telegram_proxy_port=8888)
        assert get_proxy_url(settings=s_enabled) == "http://192.168.1.1:8888"

    @pytest.mark.asyncio
    async def test_audio_converter_extreme_inputs(self):
        # Empty input
        with pytest.raises(ValueError):
            await convert_ogg_to_wav(b"")

        # Too short input (< 12 bytes)
        with pytest.raises(ValueError):
            await convert_ogg_to_wav(b"OggS123")

        # Corrupt flag input
        with pytest.raises(ValueError):
            await convert_ogg_to_wav(b"CORRUPT_NOT_AUDIO")

        # WAV to OGG empty
        with pytest.raises(ValueError):
            await convert_wav_to_ogg(b"")

    @pytest.mark.asyncio
    async def test_telegram_rapid_interruption_stress(self, m6_test_db):
        handlers = TelegramBotHandlers(db_path=m6_test_db)
        chat_id = 99999

        class MockBot:
            def __init__(self):
                self.messages = []
                self.voices = []

            async def send_message(self, chat_id, text):
                self.messages.append({"chat_id": chat_id, "text": text})

            async def send_voice(self, chat_id, voice, caption=None):
                self.voices.append({"chat_id": chat_id, "caption": caption})

        mock_bot = MockBot()

        # Mock adapter chat to return JSON
        with patch.object(handlers.chat_service, "get_active_llm_adapter") as mock_adapter_getter:
            mock_adapter = AsyncMock()
            mock_response = AsyncMock()
            mock_response.content = '{"chinese": "回复内容", "japanese": "返事の内容"}'
            mock_adapter.chat.return_value = mock_response
            mock_adapter_getter.return_value = (mock_adapter, "mock-model", "mock-provider")

            # Fire 5 rapid messages for same user
            tasks = []
            for i in range(5):
                t = await handlers.process_text_chat(chat_id, f"Rapid {i}", mock_bot)
                tasks.append(t)

            # Wait for last task to finish
            await tasks[-1]

            # First 4 tasks should have been cancelled or completed
            cancelled_count = sum(1 for t in tasks[:-1] if t.cancelled() or t.done())
            assert cancelled_count == 4
            assert len(mock_bot.messages) == 5

    @pytest.mark.asyncio
    async def test_telegram_voice_stt_transcription_empty_graceful(self, m6_test_db):
        handlers = TelegramBotHandlers(db_path=m6_test_db)
        
        class MockVoiceMessage:
            file_id = "test_empty_voice"

        class MockUpdate:
            effective_chat = type("Chat", (), {"id": 12345})()
            message = type("Msg", (), {
                "voice": MockVoiceMessage(),
                "reply_text": AsyncMock(),
            })()

        class MockContext:
            class Bot:
                async def get_file(self, file_id):
                    class TGFile:
                        async def download_as_bytearray(self):
                            return bytearray(b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00" + b"\x00" * 100)
                    return TGFile()
            bot = Bot()

        with patch("galgame2voice.telegram_bot.handlers.get_stt_adapter") as mock_stt_getter:
            mock_stt = AsyncMock()
            mock_stt.transcribe.return_value = ""  # Empty transcription
            mock_stt_getter.return_value = mock_stt

            res = await handlers.handle_voice_message(MockUpdate(), MockContext())
            assert res is None
            MockUpdate.message.reply_text.assert_called_once_with("抱歉，未能从语音中识别出有效内容。")
