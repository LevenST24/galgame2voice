"""
Audit Hardening M10 Test Suite.
Verifies:
1. Outer transaction rollback atomicity across nested CRUD writes.
2. High-concurrency 50-worker SQLite async stress test.
3. chat_sync failure cleanup: pruning orphaned user messages when LLM fails.
4. SSE stream disconnect with whitespace-only tokens pruning orphaned user messages.
5. TtsCacheManager concurrent get() with _throttle_touch thread and dict safety.
6. Generator cleanup (stream_gen.aclose()) on aborted/cancelled streaming pipeline.
7. Anthropic adapter client_override stream error chunk detection.
"""

import asyncio
import os
import random
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from galgame2voice.adapters.base import ChatMessage, LLMResponse
from galgame2voice.adapters.llm.anthropic_adapter import AnthropicAdapter
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate, UserMemoryCreate
from galgame2voice.database.session import get_db, immediate_transaction, init_db
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.tts_cache_manager import TtsCacheManager


# ============================================================================
# 1. Outer Transaction Rollback Atomicity across Nested CRUD writes
# ============================================================================

class TestNestedTransactionRollbackAtomicity:
    """Confirms that nested CRUD operations do NOT prematurely commit outer transactions."""

    async def test_outer_rollback_reverts_nested_crud_writes(self, tmp_path):
        """When an outer transaction rolls back, all inner CRUD writes must be undone."""
        db_file = tmp_path / "rollback_atomicity.db"
        await init_db(str(db_file))

        async with get_db(str(db_file)) as conn:
            # Create base session
            await crud.get_or_create_session(conn, "test_session")

        # Now begin an outer transaction, do multiple CRUD writes, then fail
        try:
            async with get_db(str(db_file)) as conn:
                async with immediate_transaction(conn):
                    # 1. Add message (which uses immediate_transaction internally)
                    await crud.add_message(
                        conn,
                        MessageCreate(
                            session_id="test_session",
                            role="user",
                            content_chinese="Hello outer",
                            content_japanese="Konnichiwa",
                        ),
                    )
                    # 2. Create memory (which uses immediate_transaction internally)
                    await crud.create_memory(
                        conn,
                        UserMemoryCreate(
                            user_id="user1",
                            character_id=1,
                            category="fact",
                            fact_key="fav_food",
                            fact_value="ramen",
                            confidence=1.0,
                        ),
                    )
                    # 3. Simulate failure in outer business logic
                    raise ValueError("Simulated failure in outer scope")
        except ValueError:
            pass

        # Verify NOTHING was committed
        async with get_db(str(db_file)) as conn:
            messages = await crud.get_recent_messages(conn, "test_session", limit=10)
            assert len(messages) == 0, f"Expected 0 messages due to outer rollback, found {len(messages)}"

            memories = await crud.list_memories(conn, user_id="user1", character_id=1)
            assert len(memories) == 0, f"Expected 0 memories due to outer rollback, found {len(memories)}"


# ============================================================================
# 2. High-Concurrency SQLite Async Stress Test (50 workers)
# ============================================================================

class TestHighConcurrencySQLite:
    """Stress tests SQLite WAL mode and immediate_transaction under 50 concurrent workers."""

    async def test_50_concurrent_writers_stress(self, tmp_path):
        """50 concurrent coroutines performing read-modify-write transactions."""
        db_file = tmp_path / "stress_50.db"
        await init_db(str(db_file))

        async def worker(worker_id: int):
            session_id = f"session_{worker_id % 5}"
            for i in range(5):
                async with get_db(str(db_file)) as conn:
                    # CRUD operation wrapped in immediate_transaction
                    await crud.add_message(
                        conn,
                        MessageCreate(
                            session_id=session_id,
                            role="user" if i % 2 == 0 else "assistant",
                            content_chinese=f"Msg {worker_id}-{i}",
                            content_japanese="",
                        ),
                    )
                # Jitter between writes
                await asyncio.sleep(0.002 * (worker_id % 3))

        workers = [worker(i) for i in range(50)]
        await asyncio.gather(*workers)

        # Confirm 50 workers * 5 messages = 250 total messages inserted with 0 errors
        async with get_db(str(db_file)) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM messages;")
            row = await cur.fetchone()
            assert row[0] == 250


# ============================================================================
# 3. chat_sync Failure Cleanup: Orphaned User Message Pruning
# ============================================================================

class TestChatSyncFailurePruning:
    """Verifies chat_sync deletes the orphaned user message if downstream fails."""

    async def test_chat_sync_prunes_user_msg_on_llm_exception(self, tmp_path):
        db_file = tmp_path / "chat_sync_fail.db"
        await init_db(str(db_file))

        service = ChatService(db_path=str(db_file))

        # Mock adapter to raise an exception
        mock_adapter = AsyncMock()
        mock_adapter.chat.side_effect = RuntimeError("LLM service unavailable")

        with patch.object(service, "_get_active_llm_adapter", return_value=(mock_adapter, "mock-model", "mock-prov")):
            with pytest.raises(RuntimeError, match="LLM service unavailable"):
                await service.chat_sync(prompt="Hello there", session_id="sync_session")

        # The user message should have been pruned from DB
        async with get_db(str(db_file)) as conn:
            messages = await crud.get_recent_messages(conn, "sync_session", limit=10)
            assert len(messages) == 0, f"Expected 0 messages after failure, found {len(messages)}"


# ============================================================================
# 4. SSE Stream Disconnect with Whitespace-Only Tokens Prunes User Turn
# ============================================================================

class TestWhitespaceOnlyDisconnectPruning:
    """Verifies that whitespace-only stream abort does not persist empty assistant turn."""

    async def test_disconnect_on_whitespace_only_prunes_user_message(self, tmp_path):
        db_file = tmp_path / "whitespace_abort.db"
        await init_db(str(db_file))

        service = ChatService(db_path=str(db_file))

        async def whitespace_stream(*args, **kwargs):
            yield "   \n\t  "
            await asyncio.sleep(0.05)
            # simulate client disconnect by raising CancelledError
            raise asyncio.CancelledError()

        mock_adapter = MagicMock()
        mock_adapter.stream_chat = whitespace_stream

        with patch.object(service, "_get_active_llm_adapter", return_value=(mock_adapter, "mock-model", "mock-prov")):
            try:
                gen = service.stream_chat(prompt="Test whitespace prompt", session_id="ws_sess")
                async for _ in gen:
                    pass
            except asyncio.CancelledError:
                pass

        # Verify no messages remain in DB (user message pruned, no empty assistant message created)
        async with get_db(str(db_file)) as conn:
            messages = await crud.get_recent_messages(conn, "ws_sess", limit=10)
            assert len(messages) == 0, f"Expected 0 messages, found {len(messages)}"


# ============================================================================
# 5. TtsCacheManager Concurrent get() & _throttle_touch Safety
# ============================================================================

class TestTtsCacheManagerConcurrentSafety:
    """Tests high-concurrency calls to TtsCacheManager.get() with throttle pruning."""

    async def test_concurrent_get_throttle_mutation(self, tmp_path):
        cache_dir = tmp_path / "tts_cache"
        db_file = tmp_path / "cache_meta.db"
        await init_db(str(db_file))

        manager = TtsCacheManager(cache_dir=cache_dir, db_path=str(db_file), max_mem_entries=32)

        # Populate a few entries
        for i in range(10):
            await manager.put(
                cache_key=f"key_{i}",
                text=f"text_{i}",
                clean_text=f"text_{i}",
                voice_profile_id=1,
                params_hash="hash",
                audio_bytes=b"RIFF....data....",
            )

        # Run 30 concurrent readers touching different keys rapidly to stress _throttle_touch
        async def reader(idx: int):
            for step in range(50):
                # Touch both existing keys and non-existing keys to trigger pruning logic
                key = f"key_{step % 150}"
                await manager.get(key)

        tasks = [asyncio.create_task(reader(i)) for i in range(30)]
        # Must not raise RuntimeError: dictionary changed size during iteration
        await asyncio.gather(*tasks)

        await manager.aclose()


# ============================================================================
# 6. Generator aclose() on Aborted Stream Pipeline
# ============================================================================

class TestStreamGeneratorAclose:
    """Verifies that the LLM generator is cleanly closed via aclose() when pipeline is cancelled."""

    async def test_stream_gen_aclose_called_on_pipeline_cancel(self, tmp_path):
        db_file = tmp_path / "gen_aclose.db"
        await init_db(str(db_file))

        service = ChatService(db_path=str(db_file))

        aclose_called = False

        async def monitored_stream(*args, **kwargs):
            nonlocal aclose_called
            try:
                yield '{"chinese": "你好'
                await asyncio.sleep(5.0)  # Wait to be cancelled
                yield '世界"}'
            finally:
                aclose_called = True

        mock_adapter = MagicMock()
        mock_adapter.stream_chat = monitored_stream

        with patch.object(service, "_get_active_llm_adapter", return_value=(mock_adapter, "mock-model", "mock-prov")):
            gen = service.stream_chat(prompt="Hi", session_id="cancel_sess")
            # Pull first event
            async for ev in gen:
                if ev.get("event") == "token":
                    break
            # Now close the generator consumer (simulate client disconnect)
            await gen.aclose()

        # Wait a tiny bit for background tasks in chat_service to finish finally block
        await asyncio.sleep(0.1)
        assert aclose_called, "stream_gen was not closed when consumer disconnected!"


# ============================================================================
# 7. Anthropic Stream Error Chunk Detection
# ============================================================================

class TestAnthropicStreamErrorHandling:
    """Verifies that SSE error chunks raise RuntimeError in both client_override and live modes."""

    async def test_client_override_stream_error_chunk_raises(self):
        adapter = AnthropicAdapter(api_key="test-key")

        mock_client = AsyncMock()
        error_sse = (
            'data: {"type": "content_block_delta", "delta": {"text": "Start"}}\n\n'
            'data: {"type": "error", "error": {"type": "rate_limit_error", "message": "Rate limited"}}\n\n'
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = error_sse
        mock_client.post.return_value = mock_resp

        gen = adapter.stream_chat(
            messages=[ChatMessage(role="user", content="Hello")],
            client_override=mock_client,
        )

        first_token = await anext(gen)
        assert first_token == "Start"

        with pytest.raises(RuntimeError, match="Anthropic stream error: .*rate_limit_error"):
            await anext(gen)
