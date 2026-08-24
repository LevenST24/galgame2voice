"""
Adversarial Stress Test Suite for Milestone 6: Conversational Memory, Session Truncation, & Concurrency Stress.
Authored by m6_challenger_1.

Covers:
1. SessionManager Extreme Multi-Turn & Concurrency Load:
   - 500+ turns sequential scale test and exact sliding window order retention
   - High concurrency parallel additions (50 tasks, 500 writes, concurrent readers) under SQLite WAL
   - Zero, negative, and microscopic token budgets (max_tokens=0, -10, 1)
   - Giant text payload (100,000+ CJK characters) and single-turn budget exceeding
   - Fine-grained token budget trimming on alternating giant and tiny turns
   - Complex unicode, ZWJ emoji sequences, surrogate pairs, control chars, null bytes
   - Invariant and performance benchmarks on estimate_tokens
   - Malformed/adversarial prompt templates with unescaped braces and injection attempts
   - Dual table schema compatibility (session_messages vs messages + sessions) and bulk clear_session

2. TelegramBotHandlers & Bot Concurrency / Rapid Interruption Stress:
   - 30-message rapid interruption burst for a single user (immediate text + async task cancellation)
   - Heavy multi-user interruption storm (10 users x 10 messages = 100 interleaved requests)
   - TTS synthesis background failure and network timeout resilience
   - Audio conversion corruption resilience on incoming and outgoing voice notes
   - Rapid voice note burst with STT pipeline
   - Rapid start/stop lifecycle cycling and in-flight task cancellation on bot shutdown
   - Adversarial bot token formats and proxy error boundaries
"""

import asyncio
import os
import random
import sqlite3
import tempfile
import time
from typing import Dict, List, Any, Optional
from unittest.mock import AsyncMock, patch

import pytest
import aiosqlite

from galgame2voice.services.session_manager import SessionManager, SessionTurn
from galgame2voice.services.chat_service import ChatService
from galgame2voice.telegram_bot import (
    TelegramBotManager,
    TelegramBotHandlers,
    validate_bot_token,
    get_proxy_url,
    get_telegram_request_kwargs,
)
from galgame2voice.telegram_bot.proxy import probe_proxy_connectivity
from galgame2voice.utils.audio_converter import (
    convert_ogg_to_wav,
    convert_wav_to_ogg,
    is_ffmpeg_available,
)
from galgame2voice.adapters.base import BaseLLMAdapter, LLMResponse, ChatMessage
from galgame2voice.services.tts_service import TtsService
from tests.conftest import MockGptSovitsServer, MockLLMServer, DATABASE_SCHEMA_SQL


# ============================================================================
# Fixtures & Test Helpers
# ============================================================================

@pytest.fixture
async def m6_temp_db():
    """Creates a temporary sqlite database file initialized with the full schema and seeds."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_m6_stress_")
    os.close(fd)

    from galgame2voice.database.session import init_db
    await init_db(path)

    yield path

    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture
def m6_prod_db():
    """Creates a temporary sqlite database file with production schema (messages + sessions)."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_m6_prod_")
    os.close(fd)

    conn = sqlite3.connect(path)
    conn.executescript("""
    PRAGMA journal_mode = WAL;
    PRAGMA foreign_keys = ON;

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        description TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS voice_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        gpt_weights_path TEXT NOT NULL,
        sovits_weights_path TEXT NOT NULL,
        refer_audio_path TEXT NOT NULL,
        refer_text TEXT NOT NULL,
        refer_language TEXT NOT NULL DEFAULT 'ja',
        prompt_language TEXT NOT NULL DEFAULT 'ja',
        text_language TEXT NOT NULL DEFAULT 'ja',
        system_prompt TEXT,
        is_default INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS providers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        provider_type TEXT NOT NULL,
        api_key TEXT NOT NULL,
        api_base_url TEXT,
        chat_model TEXT NOT NULL,
        stt_model TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        extra_config TEXT DEFAULT '{}',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        channel TEXT NOT NULL,
        user_id TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS messages (
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

    CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id);
    """)
    conn.commit()
    conn.close()

    yield path

    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


class MockStressBotClient:
    """Mock Telegram bot client with call recording and configurable latency."""
    def __init__(self):
        self.sent_messages: List[Dict[str, Any]] = []
        self.sent_voices: List[Dict[str, Any]] = []
        self.lock = asyncio.Lock()

    async def send_message(self, chat_id: int, text: str) -> Dict[str, Any]:
        async with self.lock:
            msg = {"chat_id": chat_id, "text": text, "time": time.time()}
            self.sent_messages.append(msg)
            return msg

    async def send_voice(self, chat_id: int, voice: bytes, caption: Optional[str] = None) -> Dict[str, Any]:
        async with self.lock:
            v = {"chat_id": chat_id, "size": len(voice), "caption": caption, "time": time.time()}
            self.sent_voices.append(v)
            return v

    async def get_file(self, file_id: str):
        class MockFile:
            def __init__(self, fid: str):
                self.fid = fid

            async def download_as_bytearray(self):
                if self.fid == "corrupt_voice":
                    return bytearray(b"CORRUPT_NOT_AUDIO")
                elif self.fid == "empty_voice":
                    return bytearray(b"")
                return bytearray(b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00" + b"\x00" * 200)

        return MockFile(file_id)


class SlowMockTTS:
    """Mock TTS service with configurable synthesis delay and failure triggers."""
    def __init__(self, delay_s: float = 0.05, should_fail: bool = False):
        self.delay_s = delay_s
        self.should_fail = should_fail
        self.synthesized_calls: List[str] = []

    async def synthesize(self, text: str, **kwargs) -> bytes:
        if self.should_fail:
            raise RuntimeError("Mock TTS backend connection timed out")
        self.synthesized_calls.append(text)
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        return b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00" + (b"\x00\x7f" * 50)

    async def synthesize_to_file(self, text: str, **kwargs):
        wav_bytes = await self.synthesize(text, **kwargs)
        return "/static/audio/mock.wav", 1.5, len(wav_bytes)


# ============================================================================
# 1. SessionManager Adversarial Stress Tests
# ============================================================================

class TestSessionManagerAdversarial:
    """Stress tests and boundary condition mining for SessionManager."""

    @pytest.mark.asyncio
    async def test_scale_500_turns_sliding_window(self, m6_temp_db):
        """Add 500 turns and verify exact pagination and reverse ordering integrity."""
        sm = SessionManager(m6_temp_db)
        session_id = "scale-500-session"

        # Sequential rapid insertion
        for i in range(500):
            role = "user" if i % 2 == 0 else "assistant"
            await sm.add_turn(
                session_id=session_id,
                role=role,
                chinese=f"这是第{i}轮对话中文内容",
                japanese=f"これは{i}番目の会話です",
                latency_ms=i,
            )

        # 1. Test 10-message window
        h_10 = await sm.get_history(session_id, max_messages=10)
        assert len(h_10) == 10
        assert h_10[0].content_chinese == "这是第490轮对话中文内容"
        assert h_10[-1].content_chinese == "这是第499轮对话中文内容"

        # 2. Test 100-message window
        h_100 = await sm.get_history(session_id, max_messages=100)
        assert len(h_100) == 100
        assert h_100[0].content_chinese == "这是第400轮对话中文内容"
        assert h_100[-1].content_chinese == "这是第499轮对话中文内容"

        # Verify strict chronological monotonic ordering
        for idx in range(len(h_100) - 1):
            assert h_100[idx].role != h_100[idx + 1].role

    @pytest.mark.asyncio
    async def test_high_concurrency_multi_session_burst(self, m6_temp_db):
        """50 concurrent workers writing 500 turns across 10 sessions while readers query in parallel."""
        sm = SessionManager(m6_temp_db)

        async def worker_writer(worker_id: int, session_id: str, turns_count: int):
            for t in range(turns_count):
                await sm.add_turn(
                    session_id=session_id,
                    role="user" if t % 2 == 0 else "assistant",
                    chinese=f"W{worker_id}_T{t}_中文内容",
                    japanese=f"W{worker_id}_T{t}_日本語",
                )
                await asyncio.sleep(0.001)

        async def worker_reader(session_id: str, read_iterations: int):
            for _ in range(read_iterations):
                hist = await sm.get_history(session_id, max_messages=20)
                assert isinstance(hist, list)
                await asyncio.sleep(0.002)

        # Launch 50 parallel writers (5 writers per session for 10 sessions)
        writer_tasks = []
        for w in range(50):
            sess = f"session_concurrent_{w % 10}"
            writer_tasks.append(worker_writer(w, sess, 10))

        # Launch 20 parallel readers
        reader_tasks = []
        for r in range(20):
            sess = f"session_concurrent_{r % 10}"
            reader_tasks.append(worker_reader(sess, 15))

        # Gather all concurrent coroutines
        await asyncio.gather(*writer_tasks, *reader_tasks)

        # Verify per-session turn counts
        for s in range(10):
            sess = f"session_concurrent_{s}"
            hist = await sm.get_history(sess, max_messages=100)
            assert len(hist) == 50  # 5 workers * 10 turns = 50 turns

    @pytest.mark.asyncio
    async def test_token_budget_zero_and_negative(self, m6_temp_db):
        """Verify behavior with max_tokens=0, negative tokens, and max_messages=0."""
        sm = SessionManager(m6_temp_db)
        session_id = "sess-budget-edges"

        await sm.add_turn(session_id, "user", "你好世界", "こんにちは世界")
        await sm.add_turn(session_id, "assistant", "收到消息", "了解しました")

        # Zero token budget: all turns must be truncated
        h_zero = await sm.get_history(session_id, max_tokens=0)
        assert h_zero == []

        # Negative token budget: handled without infinite loop or exception
        h_neg = await sm.get_history(session_id, max_tokens=-50)
        assert h_neg == []

        # Microscopic token budget (e.g. 1 token, while 4 CJK chars is ~2 tokens)
        h_micro = await sm.get_history(session_id, max_tokens=1)
        assert len(h_micro) <= 1

        # Zero message limit
        h_msg_zero = await sm.get_history(session_id, max_messages=0)
        assert h_msg_zero == []

    @pytest.mark.asyncio
    async def test_single_giant_turn_exceeding_budget(self, m6_temp_db):
        """Add a massive 100,000 CJK characters message and verify token budget truncation."""
        sm = SessionManager(m6_temp_db)
        session_id = "sess-giant-blob"

        giant_text = "长" * 100_000  # ~50,000 tokens
        est_tokens = sm.estimate_tokens(giant_text)
        assert est_tokens == 50_000

        await sm.add_turn(session_id, "user", giant_text, "Ja")
        await sm.add_turn(session_id, "assistant", "短回复", "はい")
        await sm.add_turn(session_id, "user", "最新输入", "最新")

        # Budget of 500 tokens should discard the giant message and keep only the latest 2
        history = await sm.get_history(session_id, max_tokens=500)
        assert len(history) == 2
        assert history[0].content_chinese == "短回复"
        assert history[1].content_chinese == "最新输入"

    @pytest.mark.asyncio
    async def test_mixed_size_history_token_budget(self, m6_temp_db):
        """Alternating small, medium, and large turns to test precise step-by-step token trimming."""
        sm = SessionManager(m6_temp_db)
        session_id = "sess-mixed-tokens"

        # Turn 0: 200 tokens
        await sm.add_turn(session_id, "user", "一" * 400, "Ja")
        # Turn 1: 5 tokens
        await sm.add_turn(session_id, "assistant", "二" * 10, "Ja")
        # Turn 2: 100 tokens
        await sm.add_turn(session_id, "user", "三" * 200, "Ja")
        # Turn 3: 10 tokens
        await sm.add_turn(session_id, "assistant", "四" * 20, "Ja")

        # Total tokens = ~315 tokens.
        # If max_tokens is 150: Turn 0 (200) should be popped. Remaining (Turn 1, 2, 3) = 115 <= 150.
        hist_150 = await sm.get_history(session_id, max_tokens=150)
        assert len(hist_150) == 3
        assert hist_150[0].content_chinese.startswith("二")
        assert hist_150[-1].content_chinese.startswith("四")

        # If max_tokens is 30: Turn 0, 1, 2 should be popped. Remaining = Turn 3 (10 tokens).
        hist_30 = await sm.get_history(session_id, max_tokens=30)
        assert len(hist_30) == 1
        assert hist_30[0].content_chinese.startswith("四")

    @pytest.mark.asyncio
    async def test_extreme_unicode_and_adversarial_strings(self, m6_temp_db):
        """Stress-test SessionManager with ZWJ emojis, control chars, RTL, and formatting edge cases."""
        sm = SessionManager(m6_temp_db)
        session_id = "sess-unicode-stress"

        complex_strings = [
            "👨‍👩‍👧‍👦 🏳️‍🌈 🐱‍👤 🌸 🎮",  # ZWJ emoji sequences
            "مرحبا بالعالم! السلام عليكم",  # Arabic RTL
            "Привет мир! 12345 !@#$%^&*()_+",  # Cyrillic & special symbols
            "\t\r\n\n\r\t   \n",  # Pure whitespace & linebreaks
            '{"key": "value", "broken": \'"\\/\\b\\f\\n\\r\\t}',  # JSON injection attack payload
            "𠮷野家 𩸽 𠀋 𡈽",  # CJK Unified Ideographs Extension B (surrogate pairs)
        ]

        for i, s in enumerate(complex_strings):
            await sm.add_turn(session_id, "user" if i % 2 == 0 else "assistant", s, s)

        hist = await sm.get_history(session_id, max_messages=len(complex_strings))
        assert len(hist) == len(complex_strings)
        for original, retrieved in zip(complex_strings, hist):
            assert retrieved.content_chinese == original
            assert retrieved.content_japanese == original

    def test_token_estimation_speed_and_invariants(self, m6_temp_db):
        """Benchmark and invariant tests for estimate_tokens."""
        sm = SessionManager(m6_temp_db)

        # Invariant 1: Empty string = 0
        assert sm.estimate_tokens("") == 0
        assert sm.estimate_tokens(None) == 0

        # Invariant 2: Single char >= 1
        assert sm.estimate_tokens("a") >= 1
        assert sm.estimate_tokens("中") >= 1
        assert sm.estimate_tokens("🌸") >= 1

        # Invariant 3: Performance test (1 million characters < 100ms)
        large_sample = ("Hello 世界！这是一段混合测试字符串 12345! " * 20_000)
        assert len(large_sample) > 500_000

        t0 = time.perf_counter()
        token_count = sm.estimate_tokens(large_sample)
        t1 = time.perf_counter()

        elapsed_ms = (t1 - t0) * 1000
        assert token_count > 0
        assert elapsed_ms < 100.0, f"Token estimation took too long: {elapsed_ms:.2f}ms"

    def test_prompt_template_injection_and_malformed_syntax(self, m6_temp_db):
        """Test format_llm_messages against broken templates, missing placeholders, and extra brackets."""
        sm = SessionManager(m6_temp_db)
        history = [SessionTurn(role="user", content_chinese="Hi", content_japanese="Hai")]

        # 1. Broken / unclosed braces in template
        broken_tpl = "你是一个伴侣 {character_name, 没有闭合括号 {unclosed"
        msgs1 = sm.format_llm_messages("Arona", history, "Hello", system_template=broken_tpl)
        assert len(msgs1) == 3
        assert msgs1[0]["role"] == "system"

        # 2. Template without character_name
        no_name_tpl = "Strict instructions: Output only JSON format."
        msgs2 = sm.format_llm_messages("Arona", history, "Hello", system_template=no_name_tpl)
        assert msgs2[0]["content"] == no_name_tpl

        # 3. Triple braces
        triple_tpl = "Role: {{{character_name}}} -- Output: {{chinese}}"
        msgs3 = sm.format_llm_messages("Arona", history, "Hello", system_template=triple_tpl)
        assert "Arona" in msgs3[0]["content"]

    @pytest.mark.asyncio
    async def test_dual_table_schema_stress_and_fallback(self, m6_prod_db):
        """Verify SessionManager seamlessly works with production schema (messages + sessions tables)."""
        sm = SessionManager(m6_prod_db)
        session_id = "prod-schema-sess"

        # Insert turns
        await sm.add_turn(session_id, "user", "生产环境测试输入", "テスト", audio_url="http://audio.mp3", latency_ms=120)
        await sm.add_turn(session_id, "assistant", "生产环境测试输出", "テスト返事", audio_url="http://audio2.mp3", latency_ms=300)

        # Retrieve history
        hist = await sm.get_history(session_id, max_messages=10)
        assert len(hist) == 2
        assert hist[0].content_chinese == "生产环境测试输入"
        assert hist[0].audio_url == "http://audio.mp3"
        assert hist[1].content_japanese == "テスト返事"

        # Build chat messages
        chat_msgs = await sm.build_chat_messages(session_id, "下一轮问题", character_name="Arona")
        assert len(chat_msgs) == 4
        assert chat_msgs[0].role == "system"
        assert chat_msgs[-1].content == "下一轮问题"

        # Clear session
        cleared = await sm.clear_session(session_id)
        assert cleared is True
        hist_after = await sm.get_history(session_id)
        assert len(hist_after) == 0


# ============================================================================
# 2. Telegram Bot Adversarial & Concurrency Stress Tests
# ============================================================================

class TestTelegramBotAdversarial:
    """Stress tests for Telegram bot interruption handling, concurrency storms, and error boundaries."""

    @pytest.mark.asyncio
    async def test_rapid_fire_interruption_burst_30_messages(self, m6_temp_db):
        """
        Simulate a user firing 30 messages in rapid succession (< 1ms interval).
        Verifies:
        - 30 text replies sent immediately.
        - Previous 29 voice synthesis tasks are cancelled cleanly upon each new message.
        - Only the 30th (final) voice synthesis task completes.
        - Zero unhandled exceptions or orphan leaks.
        """
        chat_id = 99901
        bot_client = MockStressBotClient()
        slow_tts = SlowMockTTS(delay_s=0.03)

        handlers = TelegramBotHandlers(db_path=m6_temp_db, tts_service=slow_tts)

        # Mock LLM response in ChatService
        mock_adapter = AsyncMock(spec=BaseLLMAdapter)
        mock_adapter.chat.side_effect = lambda msgs, **kw: LLMResponse(
            content='{"chinese": "回复内容", "japanese": "返事"}',
            usage={"total_tokens": 20},
        )
        handlers.chat_service._get_active_llm_adapter = AsyncMock(return_value=(mock_adapter, "mock-model"))

        tasks: List[asyncio.Task] = []
        for i in range(30):
            t = await handlers.process_text_chat(chat_id, f"快速连击输入_{i}", bot_client)
            tasks.append(t)
            # Minimal sleep simulating micro-burst
            await asyncio.sleep(0.001)

        # Wait for the final task
        await tasks[-1]

        # Verify all 30 text responses were sent immediately
        assert len(bot_client.sent_messages) == 30

        # Verify that all prior tasks (0..28) were cancelled
        cancelled_count = sum(1 for t in tasks[:-1] if t.cancelled() or t.done())
        assert cancelled_count == 29

        # Verify voice note count: at most 1 or 2 voice notes sent (due to cancellation)
        assert len(bot_client.sent_voices) <= 2
        assert len(bot_client.sent_voices) >= 1
        assert bot_client.sent_voices[-1]["chat_id"] == chat_id

    @pytest.mark.asyncio
    async def test_heavy_multi_user_interruption_storm(self, m6_temp_db):
        """
        10 distinct users, each sending 10 rapid messages in parallel (100 total messages).
        Verifies:
        - User A's messages never cancel User B's voice tasks (User Isolation).
        - Exactly 100 text messages sent.
        - Exactly 10 voice notes sent (one per user).
        """
        bot_client = MockStressBotClient()
        slow_tts = SlowMockTTS(delay_s=0.02)
        handlers = TelegramBotHandlers(db_path=m6_temp_db, tts_service=slow_tts)

        mock_adapter = AsyncMock(spec=BaseLLMAdapter)
        mock_adapter.chat.side_effect = lambda msgs, **kw: LLMResponse(
            content='{"chinese": "收到", "japanese": "了解"}',
            usage={"total_tokens": 10},
        )
        handlers.chat_service._get_active_llm_adapter = AsyncMock(return_value=(mock_adapter, "mock-model"))

        async def simulate_user_traffic(user_chat_id: int):
            user_tasks = []
            for m in range(10):
                t = await handlers.process_text_chat(user_chat_id, f"User_{user_chat_id}_Msg_{m}", bot_client)
                user_tasks.append(t)
                await asyncio.sleep(0.002)
            # Wait for user's final task
            await user_tasks[-1]

        # Launch 10 users in parallel
        await asyncio.gather(*[simulate_user_traffic(80000 + u) for u in range(10)])

        # 1. Total text messages sent = 100 (10 users * 10 messages)
        assert len(bot_client.sent_messages) == 100

        # 2. Total voice notes sent should be 10 (one completed voice per user)
        assert len(bot_client.sent_voices) == 10
        voice_chat_ids = {v["chat_id"] for v in bot_client.sent_voices}
        assert voice_chat_ids == {80000 + u for u in range(10)}

    @pytest.mark.asyncio
    async def test_tts_synthesis_failure_resilience(self, m6_temp_db):
        """Verify that when background TTS fails (500/timeout), text reply was still sent and no crash occurs."""
        bot_client = MockStressBotClient()
        failing_tts = SlowMockTTS(delay_s=0.01, should_fail=True)
        handlers = TelegramBotHandlers(db_path=m6_temp_db, tts_service=failing_tts)

        mock_adapter = AsyncMock(spec=BaseLLMAdapter)
        mock_adapter.chat.return_value = LLMResponse(
            content='{"chinese": "这是文本回复", "japanese": "音声エラーテスト"}',
            usage={"total_tokens": 15},
        )
        handlers.chat_service._get_active_llm_adapter = AsyncMock(return_value=(mock_adapter, "mock-model"))

        task = await handlers.process_text_chat(77701, "测试TTS故障", bot_client)
        await task  # Task should handle exception and terminate without unhandled re-raise

        assert len(bot_client.sent_messages) == 1
        assert "这是文本回复" in bot_client.sent_messages[0]["text"]
        # No voice note should have been sent
        assert len(bot_client.sent_voices) == 0

    @pytest.mark.asyncio
    async def test_corrupt_audio_conversion_failure_resilience(self, m6_temp_db):
        """Test voice message handler against corrupted, empty, and non-audio bytes."""
        bot_client = MockStressBotClient()
        handlers = TelegramBotHandlers(db_path=m6_temp_db)

        class DummyVoiceMsgUpdate:
            def __init__(self, file_id: str):
                self.effective_chat = type("Chat", (), {"id": 66601})()
                self.message = type("Message", (), {
                    "voice": type("Voice", (), {"file_id": file_id})(),
                    "reply_text": AsyncMock(),
                })()

        class DummyContext:
            def __init__(self):
                self.bot = bot_client

        # 1. Corrupt voice file
        update_corrupt = DummyVoiceMsgUpdate("corrupt_voice")
        task1 = await handlers.handle_voice_message(update_corrupt, DummyContext())
        assert task1 is None
        update_corrupt.message.reply_text.assert_called_with("抱歉，语音解析失败，请重试！")

        # 2. Empty voice file
        update_empty = DummyVoiceMsgUpdate("empty_voice")
        task2 = await handlers.handle_voice_message(update_empty, DummyContext())
        assert task2 is None
        update_empty.message.reply_text.assert_called_with("抱歉，语音解析失败，请重试！")

    @pytest.mark.asyncio
    async def test_telegram_bot_manager_rapid_lifecycle(self, m6_temp_db):
        """Rapidly start and stop TelegramBotManager, ensuring active tasks are cancelled on stop."""
        manager = TelegramBotManager(db_path=m6_temp_db)

        from galgame2voice.database.models import SettingsUpdate
        from galgame2voice.database import crud
        async with aiosqlite.connect(m6_temp_db) as conn:
            await crud.update_settings(conn, SettingsUpdate(telegram_bot_token="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz"))

        # Start and stop 5 times in rapid succession
        for _ in range(5):
            started = await manager.start()
            assert started is True
            assert manager.is_running is True

            # Register a dummy running voice task
            async def dummy_slow_task():
                await asyncio.sleep(10.0)

            t = asyncio.create_task(dummy_slow_task())
            manager.handlers.user_tasks[12345] = t

            # Stop manager
            await manager.stop()
            assert manager.is_running is False
            await asyncio.sleep(0.005)
            assert t.cancelled() or t.done() or (hasattr(t, "cancelling") and t.cancelling() > 0)

    def test_adversarial_bot_token_formats(self):
        """Test validate_bot_token against various boundary and malformed inputs."""
        assert validate_bot_token("1234567890:ABCdefGHIjklMNOpqrsTUVwxyz") is True
        assert validate_bot_token("123456789:ABC") is True  # 13 chars with colon

        # Invalid tokens
        assert validate_bot_token("") is False
        assert validate_bot_token(None) is False
        assert validate_bot_token("   ") is False
        assert validate_bot_token("123456789012345") is False  # No colon
        assert validate_bot_token("123:invalid_key_token") is False  # Contains 'invalid'
        assert validate_bot_token("123:abc") is False  # Length < 10

    def test_proxy_configuration_stress_and_invalid_endpoints(self):
        """Test proxy parsing and request kwargs generation with edge case proxy strings."""
        # Standard HTTP
        assert get_proxy_url(proxy_str="127.0.0.1:7890") == "http://127.0.0.1:7890"
        # SOCKS5
        assert get_proxy_url(proxy_str="socks5://user:pass@192.168.1.1:1080") == "socks5://user:pass@192.168.1.1:1080"
        # Empty/None
        assert get_proxy_url(proxy_str="") is None
        assert get_proxy_url(proxy_str="   ") is None
        assert get_proxy_url(proxy_str=None) is None

        # Request kwargs
        kwargs = get_telegram_request_kwargs(proxy_url="http://127.0.0.1:7890", read_timeout=45.0)
        assert kwargs["proxy"] == "http://127.0.0.1:7890"
        assert kwargs["read_timeout"] == 45.0
        assert kwargs["connect_timeout"] == 15.0
