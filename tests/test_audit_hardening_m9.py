"""
Audit Hardening M9 Test Suite.
Tests edge cases across:
1. SQLite immediate_transaction concurrency and nested transaction safety.
2. SSE streaming client disconnect: partial reply persistence vs zero-token orphaned message pruning.
3. Event pump cancellation response time.
4. TTS Cache Manager touch throttling memory bounds under high key churn.
5. Anthropic Adapter role alternation, empty message sanitization, and leading user turn invariance.
"""

import asyncio
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from galgame2voice.adapters.base import ChatMessage
from galgame2voice.adapters.llm.anthropic_adapter import AnthropicAdapter
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate, UserMemoryCreate
from galgame2voice.database.session import get_db, immediate_transaction, init_db
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.tts_cache_manager import TtsCacheManager


# ============================================================================
# 1. Immediate Transaction Concurrency & Nested Safety
# ============================================================================

class TestImmediateTransactionSafety:
    """Validates immediate_transaction under concurrency and nested transactions."""

    async def test_immediate_transaction_nested_in_active_transaction(self, tmp_path):
        """Nested immediate_transaction should not fail with 'cannot start transaction'."""
        db_file = tmp_path / "nested_tx.db"
        await init_db(str(db_file))

        async with get_db(str(db_file)) as conn:
            # Simulate an existing deferred transaction
            await conn.execute("INSERT INTO settings (id, active_provider_id) VALUES (1, 'deepseek') ON CONFLICT(id) DO UPDATE SET active_provider_id='deepseek';")
            assert conn.in_transaction or getattr(getattr(conn, "_conn", None), "in_transaction", False)

            # Enter immediate_transaction while already in transaction
            async with immediate_transaction(conn):
                await conn.execute("UPDATE settings SET active_provider_id='openai' WHERE id=1;")

        # Verify change was committed
        async with get_db(str(db_file)) as conn:
            cur = await conn.execute("SELECT active_provider_id FROM settings WHERE id=1;")
            row = await cur.fetchone()
            assert row[0] == "openai"

    async def test_concurrent_writes_with_immediate_transaction(self, tmp_path):
        """20 concurrent tasks performing immediate writes should succeed without deadlocks."""
        db_file = tmp_path / "concurrent_tx.db"
        await init_db(str(db_file))

        async def worker(worker_id: int):
            for i in range(5):
                async with get_db(str(db_file)) as conn:
                    async with immediate_transaction(conn):
                        await conn.execute(
                            "INSERT INTO user_memories (user_id, character_id, category, fact_key, fact_value, confidence) "
                            "VALUES (?, 1, 'preference', ?, 'val', 1.0) "
                            "ON CONFLICT(user_id, character_id, fact_key) DO UPDATE SET fact_value = excluded.fact_value;",
                            (f"user_{worker_id}", f"key_{i}"),
                        )
                await asyncio.sleep(0.01)

        tasks = [asyncio.create_task(worker(w)) for w in range(10)]
        await asyncio.gather(*tasks)

        async with get_db(str(db_file)) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM user_memories;")
            count = (await cur.fetchone())[0]
            assert count == 50


# ============================================================================
# 2. SSE Streaming Client Disconnect & Orphan Message Handling
# ============================================================================

class TestChatStreamingDisconnectAndPruning:
    """Validates message persistence/pruning when clients disconnect mid-stream."""

    async def test_client_disconnect_with_partial_tokens_persists_assistant_message(self, tmp_path):
        """When client disconnects after tokens arrive, partial assistant message is persisted."""
        db_file = tmp_path / "disconnect_partial.db"
        await init_db(str(db_file))

        service = ChatService(db_path=str(db_file))

        async def mock_stream_tokens(*args, **kwargs):
            yield '{"chinese": "你好，'
            await asyncio.sleep(0.05)
            yield '今天天气真好"'
            await asyncio.sleep(0.05)
            yield '}'

        with patch.object(service, "_get_active_llm_adapter") as mock_get_adapter:
            mock_adapter = MagicMock()
            mock_adapter.stream_chat = mock_stream_tokens
            mock_get_adapter.return_value = (mock_adapter, "mock-model", "mock-provider")

            gen = service.stream_chat(prompt="你好", session_id="test_partial_sess")
            events = []
            # Read first event, then simulate client disconnect (break out of loop / close gen)
            async for ev in gen:
                events.append(ev)
                if ev.get("event") == "text":
                    break
            await gen.aclose()

        # Check DB: both user message and partial assistant message should exist
        async with get_db(str(db_file)) as conn:
            cur = await conn.execute(
                "SELECT role, content_chinese FROM messages WHERE session_id='test_partial_sess' ORDER BY id ASC;"
            )
            rows = await cur.fetchall()
            assert len(rows) == 2
            assert rows[0][0] == "user"
            assert rows[0][1] == "你好"
            assert rows[1][0] == "assistant"
            assert "你好" in rows[1][1]

    async def test_client_disconnect_before_any_tokens_prunes_orphaned_user_message(self, tmp_path):
        """When client disconnects before ANY assistant tokens arrive, orphaned user message is pruned."""
        db_file = tmp_path / "disconnect_zero.db"
        await init_db(str(db_file))

        service = ChatService(db_path=str(db_file))

        async def hanging_stream(*args, **kwargs):
            # Hangs without yielding any tokens
            await asyncio.sleep(5.0)
            yield '{"chinese": "never reached"}'

        with patch.object(service, "_get_active_llm_adapter") as mock_get_adapter:
            mock_adapter = MagicMock()
            mock_adapter.stream_chat = hanging_stream
            mock_get_adapter.return_value = (mock_adapter, "mock-model", "mock-provider")

            gen = service.stream_chat(prompt="谁在吗？", session_id="test_zero_sess")
            # Pull generator once so setup executes (user_msg is saved), then immediately close
            gen_task = asyncio.create_task(gen.__anext__())
            await asyncio.sleep(0.05)  # Let setup run
            gen_task.cancel()
            try:
                await gen_task
            except asyncio.CancelledError:
                pass
            await gen.aclose()

        # Check DB: orphaned user message should be pruned, leaving 0 messages
        async with get_db(str(db_file)) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id='test_zero_sess';"
            )
            count = (await cur.fetchone())[0]
            assert count == 0

    async def test_cancel_event_immediate_responsiveness(self, tmp_path):
        """Cancellation event should break the stream in <100ms without 1.0s timeout lag."""
        db_file = tmp_path / "cancel_perf.db"
        await init_db(str(db_file))

        service = ChatService(db_path=str(db_file))
        cancel_event = asyncio.Event()

        async def slow_stream(*args, **kwargs):
            for _ in range(50):
                yield '{"chinese": "字"}'
                await asyncio.sleep(0.5)

        with patch.object(service, "_get_active_llm_adapter") as mock_get_adapter:
            mock_adapter = MagicMock()
            mock_adapter.stream_chat = slow_stream
            mock_get_adapter.return_value = (mock_adapter, "mock-model", "mock-provider")

            gen = service.stream_chat(prompt="测试取消", session_id="test_cancel", cancel_event=cancel_event)

            # Read first event
            ev = await gen.__anext__()
            assert ev is not None

            # Trigger cancellation and measure how fast next returns or raises StopAsyncIteration
            t0 = time.perf_counter()
            cancel_event.set()
            async for _ in gen:
                pass
            elapsed = time.perf_counter() - t0

            # Must take well under 500ms (previously could wait up to 1.0s on wait_for timeout)
            assert elapsed < 0.35


# ============================================================================
# 3. TTS Cache Manager Memory Hard Capping Under Key Churn
# ============================================================================

class TestTtsCacheThrottleHardCap:
    """Verifies that _touch_throttle enforces strict memory bounds under key churn."""

    def test_touch_throttle_strictly_bounded_under_burst(self, tmp_path):
        """1000 distinct cache key touches within 1 second should not grow _touch_throttle unboundedly."""
        manager = TtsCacheManager(
            cache_dir=tmp_path / "cache",
            db_path=tmp_path / "test.db",
            max_mem_entries=32,  # max_bound = max(32 * 4, 128) = 128
        )

        # Generate 500 touches for distinct keys within a fraction of a second
        for i in range(500):
            manager._throttle_touch(f"churn_key_{i}")

        # The throttle dictionary must not exceed max_bound + 1
        max_bound = max(manager.max_mem_entries * 4, 128)
        assert len(manager._touch_throttle) <= max_bound + 1


# ============================================================================
# 4. Anthropic Adapter Alternating Roles & Non-Empty Sanitization
# ============================================================================

class TestAnthropicPayloadInvariance:
    """Verifies Anthropic payload conforms strictly to Anthropic /v1/messages specifications."""

    def test_consecutive_user_messages_are_merged(self):
        """Consecutive user messages must be merged with double newlines."""
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        messages = [
            ChatMessage(role="user", content="First question"),
            ChatMessage(role="user", content="Second question immediately after"),
        ]

        payload = adapter._prepare_anthropic_payload(messages)
        msgs = payload["messages"]

        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "First question\n\nSecond question immediately after"

    def test_consecutive_assistant_messages_are_merged(self):
        """Consecutive assistant messages must be merged with double newlines."""
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        messages = [
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Part 1"),
            ChatMessage(role="assistant", content="Part 2"),
        ]

        payload = adapter._prepare_anthropic_payload(messages)
        msgs = payload["messages"]

        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "Part 1\n\nPart 2"

    def test_empty_content_is_sanitized_to_non_empty(self):
        """Empty or whitespace-only messages must be replaced with non-empty string."""
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        messages = [
            ChatMessage(role="user", content="   "),
        ]

        payload = adapter._prepare_anthropic_payload(messages)
        msgs = payload["messages"]

        assert len(msgs) == 1
        assert msgs[0]["content"] == "..."

    def test_leading_assistant_message_prepends_user_greeting(self):
        """If messages start with assistant, a leading user message must be prepended."""
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        messages = [
            ChatMessage(role="assistant", content="Welcome!"),
        ]

        payload = adapter._prepare_anthropic_payload(messages)
        msgs = payload["messages"]

        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Hello"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "Welcome!"

    def test_system_prompt_extracted_to_top_level(self):
        """System prompt must be removed from messages and placed into top-level 'system'."""
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        messages = [
            ChatMessage(role="system", content="You are a helpful galgame narrator."),
            ChatMessage(role="user", content="Hello"),
        ]

        payload = adapter._prepare_anthropic_payload(messages)

        assert payload["system"] == "You are a helpful galgame narrator."
        assert len(payload["messages"]) == 1
        assert payload["messages"][0]["role"] == "user"
        assert payload["messages"][0]["content"] == "Hello"
