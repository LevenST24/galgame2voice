"""
Tests for Multi-Turn Conversational Memory, Session Truncation, and Prompt Templates.
Covers Tier 1 (Session CRUD, Multi-Turn History, Prompt Formatting)
and Tier 2 (Token Limit Truncation, Session Isolation, Prompt Template Interpolation, Empty States).
"""

import asyncio
from typing import List, Dict, Any, Optional
import pytest
import aiosqlite

from galgame2voice.services.session_manager import SessionManager, SessionTurn


# ============================================================================
# Tier 1: Multi-Turn Session Memory Feature Tests
# ============================================================================

class TestSessionManagerTier1:
    """Tier 1: Verify adding turns, retrieving chronological history, clearing session, and formatting prompt."""

    @pytest.mark.asyncio
    async def test_add_and_get_history(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        await sm.add_turn("sess-01", "user", "你好！", "こんにちは！")
        await sm.add_turn("sess-01", "assistant", "你好，老师！", "こんにちは、先生！")

        history = await sm.get_history("sess-01")
        assert len(history) == 2
        assert history[0].role == "user"
        assert history[0].content_chinese == "你好！"
        assert history[1].role == "assistant"
        assert history[1].content_japanese == "こんにちは、先生！"

    @pytest.mark.asyncio
    async def test_clear_session_messages(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        await sm.add_turn("sess-02", "user", "消息1", "msg1")
        await sm.add_turn("sess-02", "user", "消息2", "msg2")

        history_before = await sm.get_history("sess-02")
        assert len(history_before) == 2

        await sm.clear_session("sess-02")
        history_after = await sm.get_history("sess-02")
        assert len(history_after) == 0

    def test_format_llm_messages_template(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        history = [
            SessionTurn(role="user", content_chinese="早上好", content_japanese="おはよう"),
            SessionTurn(role="assistant", content_chinese="早上好呀", content_japanese="おはようございます")
        ]
        messages = sm.format_llm_messages("Arona", history, "今天有什么日程？")
        assert len(messages) == 4
        assert messages[0]["role"] == "system"
        assert "Arona" in messages[0]["content"]
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "今天有什么日程？"

    @pytest.mark.asyncio
    async def test_empty_session_returns_empty_history(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        history = await sm.get_history("nonexistent-session")
        assert history == []


# ============================================================================
# Tier 2: Boundary, Token Truncation, and Session Isolation Tests
# ============================================================================

class TestSessionManagerTier2:
    """Tier 2: Max message limit, token truncation, session isolation, and custom template variables."""

    @pytest.mark.asyncio
    async def test_max_messages_limit(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        for i in range(15):
            await sm.add_turn("sess-limit", "user", f"User msg {i}", f"Ja msg {i}")

        # Limit to 5 most recent messages
        history = await sm.get_history("sess-limit", max_messages=5)
        assert len(history) == 5
        assert history[0].content_chinese == "User msg 10"
        assert history[-1].content_chinese == "User msg 14"

    @pytest.mark.asyncio
    async def test_token_limit_truncation(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        # Add 3 turns, first one very large
        large_content = "长文本" * 500  # ~750 tokens
        await sm.add_turn("sess-token", "user", large_content, "Ja")
        await sm.add_turn("sess-token", "assistant", "短回复1", "Ja1")
        await sm.add_turn("sess-token", "user", "短输入2", "Ja2")

        # Set max_tokens to 100, which should discard the oldest large message
        history = await sm.get_history("sess-token", max_tokens=100)
        assert len(history) == 2
        assert history[0].content_chinese == "短回复1"
        assert history[1].content_chinese == "短输入2"

    @pytest.mark.asyncio
    async def test_session_isolation(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        await sm.add_turn("user-alice", "user", "我是Alice", "Aliceです")
        await sm.add_turn("user-bob", "user", "我是Bob", "Bobです")

        alice_hist = await sm.get_history("user-alice")
        bob_hist = await sm.get_history("user-bob")

        assert len(alice_hist) == 1
        assert alice_hist[0].content_chinese == "我是Alice"

        assert len(bob_hist) == 1
        assert bob_hist[0].content_chinese == "我是Bob"

    def test_custom_template_with_special_characters(self, temp_db_path):
        custom_tpl = "角色设定：【{character_name}】。\n特殊符号测试：{{}} %% // \\n"
        sm = SessionManager(temp_db_path, default_system_template=custom_tpl)
        messages = sm.format_llm_messages("Plana", [], "Hello")
        assert "【Plana】" in messages[0]["content"]
        assert "{}" in messages[0]["content"]

    @pytest.mark.parametrize("persona_name,persona_prompt", [
        ("Tsundere", "You are a Tsundere girl named {character_name}. Act haughty but caring."),
        ("Kuudere", "You are a quiet, stoic companion named {character_name}."),
        ("Maid", "You are a loyal maid serving your master. Name: {character_name}."),
        ("Genki", "You are an energetic, hyper cheerful companion named {character_name}!"),
    ])
    def test_archetype_persona_prompt_formatting(self, temp_db_path, persona_name, persona_prompt):
        sm = SessionManager(temp_db_path, default_system_template=persona_prompt)
        msgs = sm.format_llm_messages(persona_name, [], "Hello")
        assert len(msgs) == 2
        assert persona_name in msgs[0]["content"]
        assert msgs[-1]["role"] == "user"

    @pytest.mark.asyncio
    async def test_session_pagination_and_sliding_window(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        for i in range(25):
            await sm.add_turn("sess-window", "user" if i % 2 == 0 else "assistant", f"Turn_{i}", f"Ja_{i}")

        hist_10 = await sm.get_history("sess-window", max_messages=10)
        assert len(hist_10) == 10
        assert hist_10[0].content_chinese == "Turn_15"
        assert hist_10[-1].content_chinese == "Turn_24"

    @pytest.mark.asyncio
    async def test_build_chat_messages(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        await sm.add_turn("sess-bcm", "user", "你好", "こんにちは")
        await sm.add_turn("sess-bcm", "assistant", "你好呀", "こんにちは")

        chat_msgs = await sm.build_chat_messages(
            session_id="sess-bcm",
            user_prompt="今天天气如何？",
            character_name="四季夏目",
        )
        assert len(chat_msgs) == 4
        assert chat_msgs[0].role == "system"
        assert "四季夏目" in chat_msgs[0].content
        assert chat_msgs[1].role == "user"
        assert chat_msgs[2].role == "assistant"
        assert chat_msgs[3].role == "user"
        assert chat_msgs[3].content == "今天天气如何？"
