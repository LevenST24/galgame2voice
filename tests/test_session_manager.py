"""
Tests for Multi-Turn Conversational Memory, Session Truncation, and Prompt Templates.
Covers Tier 1 (Session CRUD, Multi-Turn History, Prompt Formatting)
and Tier 2 (Token Limit Truncation, Session Isolation, Prompt Template Interpolation, Empty States).
"""

import asyncio
import os
import sqlite3
import tempfile
import pytest

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
    async def test_empty_session_returns_empty_history(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        history = await sm.get_history("nonexistent-session")
        assert history == []


# ============================================================================
# Tier 2: Boundary, Token Truncation, and Session Isolation Tests
# ============================================================================

class TestSessionManagerTier2:
    """Tier 2: Max message limit, token truncation, session isolation, and custom template variables."""


    def test_custom_template_with_special_characters(self, temp_db_path):
        custom_tpl = "角色设定：【{character_name}】。\n特殊符号测试：{{}} %% // \\n"
        sm = SessionManager(temp_db_path, default_system_template=custom_tpl)
        messages = sm.format_llm_messages("Plana", [], "Hello")
        assert "【Plana】" in messages[0]["content"]
        assert "{}" in messages[0]["content"]


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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prompt_saved", [True, False])
    async def test_context_window_carries_whole_exchanges(self, temp_db_path, prompt_saved):
        sm = SessionManager(temp_db_path)
        for i in range(3):
            await sm.add_turn("sess-win", "user", f"问题{i}", "")
            await sm.add_turn("sess-win", "assistant", f"回答{i}", "")
        if prompt_saved:
            await sm.add_turn("sess-win", "user", "新问题", "")

        chat_msgs = await sm.build_chat_messages(
            session_id="sess-win", user_prompt="新问题", character_name="夏目", max_messages=2,
        )
        assert [(m.role, m.content) for m in chat_msgs[1:]] == [
            ("user", "问题2"), ("assistant", chat_msgs[2].content), ("user", "新问题"),
        ]
        assert "回答2" in chat_msgs[2].content


@pytest.fixture
def messages_table_db():
    """messages + sessions only, so SessionManager uses the production messages table."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_messages_table_")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        PRAGMA foreign_keys = ON;

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            user_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content_chinese TEXT,
            content_japanese TEXT,
            audio_url TEXT,
            latency_ms INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        """
    )
    conn.commit()
    conn.close()
    yield path
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


class TestSessionManagerHistoryEdges:
    """Assertions moved from the Milestone 6 session adversarial suites."""


    @pytest.mark.asyncio
    async def test_token_budget_zero_and_negative(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        session_id = "sess-budget-edges"
        await sm.add_turn(session_id, "user", "你好世界", "こんにちは世界")
        await sm.add_turn(session_id, "assistant", "收到消息", "了解しました")

        h_zero = await sm.get_history(session_id, max_tokens=0)
        assert h_zero == []

        h_neg = await sm.get_history(session_id, max_tokens=-50)
        assert h_neg == []

        h_micro = await sm.get_history(session_id, max_tokens=1)
        assert len(h_micro) <= 1

        h_msg_zero = await sm.get_history(session_id, max_messages=0)
        assert h_msg_zero == []

    @pytest.mark.asyncio
    async def test_mixed_size_history_token_budget(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        session_id = "sess-mixed-tokens"

        # Turn 0: 442 tokens ("一" * 400 = 441 + "Ja" = 1)
        await sm.add_turn(session_id, "user", "一" * 400, "Ja")
        # Turn 1: 13 tokens ("二" * 10 = 12 + "Ja" = 1)
        await sm.add_turn(session_id, "assistant", "二" * 10, "Ja")
        # Turn 2: 222 tokens ("三" * 200 = 221 + "Ja" = 1)
        await sm.add_turn(session_id, "user", "三" * 200, "Ja")
        # Turn 3: 24 tokens ("四" * 20 = 23 + "Ja" = 1)
        await sm.add_turn(session_id, "assistant", "四" * 20, "Ja")

        # Total tokens = 701 (Turn0=442, Turn1=13, Turn2=222, Turn3=24).
        # Budget 300: pop Turn 0 (442) -> remaining 259 <= 300. Keeps Turns 1-3.
        hist_300 = await sm.get_history(session_id, max_tokens=300)
        assert len(hist_300) == 3
        assert hist_300[0].content_chinese.startswith("二")
        assert hist_300[-1].content_chinese.startswith("四")

        # Budget 50: pop Turns 0-2 -> only Turn 3 (24 tokens) fits.
        hist_50 = await sm.get_history(session_id, max_tokens=50)
        assert len(hist_50) == 1
        assert hist_50[0].content_chinese.startswith("四")

    @pytest.mark.asyncio
    async def test_extreme_unicode_and_adversarial_strings(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        session_id = "sess-unicode-stress"
        complex_strings = [
            "👨‍👩‍👧‍👦 🏳️‍🌈 🐱‍👤 🌸 🎮",
            "مرحبا بالعالم! السلام عليكم",
            "Привет мир! 12345 !@#$%^&*()_+",
            "\t\r\n\n\r\t   \n",
            '{"key": "value", "broken": \'"\\/\\b\\f\\n\\r\\t}',
            "𠮷野家 𩸽 𠀋 𡈽",
        ]
        for i, text in enumerate(complex_strings):
            await sm.add_turn(session_id, "user" if i % 2 == 0 else "assistant", text, text)

        hist = await sm.get_history(session_id, max_messages=len(complex_strings))
        assert len(hist) == len(complex_strings)
        for original, retrieved in zip(complex_strings, hist, strict=True):
            assert retrieved.content_chinese == original
            assert retrieved.content_japanese == original

    # Micro-benchmark wall-clock budget for estimate_tokens is sensitive to multi-worker CPU contention.

    def test_prompt_template_injection_and_malformed_syntax(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        history = [SessionTurn(role="user", content_chinese="Hi", content_japanese="Hai")]

        unmatched = "Hello {unmatched_key} {character_name}"
        msgs_unmatched = sm.format_llm_messages("Natsume", [], "Hi", system_template=unmatched)
        assert "Natsume" in msgs_unmatched[0]["content"]

        broken_tpl = "你是一个伴侣 {character_name, 没有闭合括号 {unclosed"
        msgs_broken = sm.format_llm_messages("Arona", history, "Hello", system_template=broken_tpl)
        assert len(msgs_broken) == 3
        assert msgs_broken[0]["role"] == "system"

        no_name_tpl = "Strict instructions: Output only JSON format."
        msgs_literal = sm.format_llm_messages("Arona", history, "Hello", system_template=no_name_tpl)
        assert msgs_literal[0]["content"] == no_name_tpl

        triple_tpl = "Role: {{{character_name}}} -- Output: {{chinese}}"
        msgs_triple = sm.format_llm_messages("Arona", history, "Hello", system_template=triple_tpl)
        assert "Arona" in msgs_triple[0]["content"]

    @pytest.mark.asyncio
    async def test_high_concurrency_multi_session_isolation(self, temp_db_path):
        sm = SessionManager(temp_db_path)
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

        await asyncio.gather(*[worker(i) for i in range(num_sessions)])

    @pytest.mark.asyncio
    async def test_high_concurrency_multi_session_burst(self, temp_db_path):
        sm = SessionManager(temp_db_path)

        async def worker_writer(worker_id: int, session_id: str, turns_count: int):
            for turn_idx in range(turns_count):
                await sm.add_turn(
                    session_id=session_id,
                    role="user" if turn_idx % 2 == 0 else "assistant",
                    chinese=f"W{worker_id}_T{turn_idx}_中文内容",
                    japanese=f"W{worker_id}_T{turn_idx}_日本語",
                )
                await asyncio.sleep(0.001)

        async def worker_reader(session_id: str, read_iterations: int):
            for _ in range(read_iterations):
                hist = await sm.get_history(session_id, max_messages=20)
                assert isinstance(hist, list)
                await asyncio.sleep(0.002)

        writer_tasks = []
        for worker_id in range(50):
            sess = f"session_concurrent_{worker_id % 10}"
            writer_tasks.append(worker_writer(worker_id, sess, 10))

        reader_tasks = []
        for reader_id in range(20):
            sess = f"session_concurrent_{reader_id % 10}"
            reader_tasks.append(worker_reader(sess, 15))

        await asyncio.gather(*writer_tasks, *reader_tasks)

        for sess_idx in range(10):
            sess = f"session_concurrent_{sess_idx}"
            hist = await sm.get_history(sess, max_messages=100)
            assert len(hist) == 50

    @pytest.mark.asyncio
    async def test_messages_table_reads_audio_url_and_latency(self, messages_table_db):
        sm = SessionManager(messages_table_db)
        session_id = "prod-schema-sess"
        await sm.add_turn(
            session_id, "user", "生产环境测试输入", "テスト", audio_url="http://audio.mp3", latency_ms=120
        )
        await sm.add_turn(
            session_id,
            "assistant",
            "生产环境测试输出",
            "テスト返事",
            audio_url="http://audio2.mp3",
            latency_ms=300,
        )

        hist = await sm.get_history(session_id, max_messages=10)
        assert len(hist) == 2
        assert hist[0].content_chinese == "生产环境测试输入"
        assert hist[0].audio_url == "http://audio.mp3"
        assert hist[0].latency_ms == 120
        assert hist[1].content_japanese == "テスト返事"
        assert hist[1].audio_url == "http://audio2.mp3"
        assert hist[1].latency_ms == 300

        cleared = await sm.clear_session(session_id)
        assert cleared is True
        hist_after = await sm.get_history(session_id)
        assert len(hist_after) == 0


class TestSessionManagerEmotion:
    """Tests for SessionTurn emotion persistence and system template formatting."""

    @pytest.mark.asyncio
    async def test_session_turn_emotion_retention(self, temp_db_path):
        sm = SessionManager(temp_db_path)
        turn = await sm.add_turn(
            session_id="sess_emo_1",
            role="assistant",
            chinese="傲娇台词",
            japanese="ツンデレ台詞",
            emotion="tsundere",
        )
        assert turn.emotion == "tsundere"

        # Default template stays format-agnostic because render_lines_contract supplies the schema
        assert "{character_name}" in sm.DEFAULT_SYSTEM_TEMPLATE
        assert '"chinese"' not in sm.DEFAULT_SYSTEM_TEMPLATE
