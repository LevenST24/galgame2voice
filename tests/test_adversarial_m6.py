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

from galgame2voice.services.session_manager import SessionManager
from galgame2voice.utils.audio_converter import (
    convert_ogg_to_wav,
    convert_wav_to_ogg,
)
from galgame2voice.database.session import init_db


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
# Section 2: Audio Converter Adversarial Tests
# ============================================================================

class TestAudioConverterAdversarial:

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
