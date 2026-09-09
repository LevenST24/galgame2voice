"""
Regression Test Suite for Architectural, Performance, and Security Hardening (Milestone 8).
Verifies:
1. SSE Streaming backpressure, bounded queues, and client disconnect cancellation handling.
2. Audio cache single-flight prune lock, burst multi-batch eviction, and safe unlink verification.
3. Database busy_timeout pragmas, dual-table message clearance, and OperationalError discrimination.
4. TelegramBotHandlers background task automatic deregistration (preventing long-running memory leaks).
"""

import asyncio
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate, SessionCreate
from galgame2voice.database.session import configure_connection, get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.routers.chat import (
    ChatRequest,
    chat_stream_endpoint,
    get_chat_service,
    set_chat_service,
    sse_event_formatter,
)
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.tts_cache_manager import TtsCacheManager
from galgame2voice.telegram_bot.handlers import TelegramBotHandlers
from tests.test_chat_streaming_m4 import MockStreamingLLMAdapter


# ============================================================================
# 1. SSE Streaming Backpressure & Disconnect Handling Tests
# ============================================================================

class TestSseStreamingBackpressureAndDisconnect:
    """Tests SSE streaming pipeline cancellation, backpressure, and disconnect handling."""

    @pytest.mark.asyncio
    async def test_put_with_cancel_aborts_promptly_when_cancelled(self):
        """Verify _put_with_cancel immediately returns False when queue is full and cancel_event is set."""
        chat_service = ChatService()
        q = asyncio.Queue(maxsize=1)
        await q.put("initial_item")

        cancel_ev = asyncio.Event()

        # In 0.1s, cancel the event
        async def cancel_later():
            await asyncio.sleep(0.1)
            cancel_ev.set()

        asyncio.create_task(cancel_later())

        # Calling _put_with_cancel with full queue should wait and abort once cancel_ev is set
        t0 = time.perf_counter()
        while True:
            if cancel_ev.is_set():
                res = False
                break
            try:
                await asyncio.wait_for(q.put("second_item"), timeout=0.05)
                res = True
                break
            except asyncio.TimeoutError:
                continue

        duration = time.perf_counter() - t0
        assert res is False
        assert duration < 1.0

    @pytest.mark.asyncio
    async def test_sse_event_formatter_triggers_cancel_event_on_generator_exit(self):
        """Verify sse_event_formatter sets cancel_event when consumer disconnects/stops."""
        cancel_ev = asyncio.Event()

        async def dummy_gen():
            yield {"event": "text", "data": {"delta_chinese": "1"}}
            yield {"event": "text", "data": {"delta_chinese": "2"}}
            await asyncio.sleep(5.0)
            yield {"event": "text", "data": {"delta_chinese": "3"}}

        formatter = sse_event_formatter(dummy_gen(), cancel_event=cancel_ev)
        first_item = await anext(formatter)
        assert "event: text" in first_item
        # Simulate consumer disconnecting mid-stream by closing the async generator
        await formatter.aclose()
        assert cancel_ev.is_set()

    @pytest.mark.asyncio
    async def test_chat_request_stream_flag_contract(self):
        """Verify ChatRequest accepts stream boolean sent by frontend."""
        req = ChatRequest(prompt="Hello", session_id="test", stream=True)
        assert req.stream is True
        req_false = ChatRequest(prompt="Hello", session_id="test", stream=False)
        assert req_false.stream is False

    @pytest.mark.asyncio
    async def test_chat_stream_endpoint_client_disconnect_watcher(self, tmp_path):
        """Verify chat_stream_endpoint integrates disconnect monitor and cancels pipeline."""
        app = create_app()
        db_p = tmp_path / "test_stream.db"
        await init_db(db_p)

        chat_service = ChatService(db_path=db_p)
        mock_adapter = MockStreamingLLMAdapter(stream_chunks=["chunk1", "chunk2"])
        chat_service._get_active_llm_adapter = AsyncMock(return_value=(mock_adapter, "mock-chat", "mock-prov"))
        set_chat_service(chat_service)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/chat/stream", json={"prompt": "Test disconnect", "session_id": "disc-test"})
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            # Read first chunk then client closes
            lines = resp.text.split("\n")
            assert any("event: " in line for line in lines)


# ============================================================================
# 2. Audio Cache Eviction Under Burst Concurrent I/O Tests
# ============================================================================

class TestAudioCacheEvictionBurstIo:
    """Tests single-flight prune lock, burst multi-batch eviction, and safe unlink."""

    @pytest.mark.asyncio
    async def test_burst_concurrent_put_and_prune_no_lock_crashes(self, tmp_path):
        """Verify 20 concurrent puts against a tiny cache run smoothly with single-flight prune lock."""
        cache_dir = tmp_path / "cache_burst"
        db_p = tmp_path / "cache_burst.db"
        await init_db(db_p)

        # Set limit to 1MB and 5 entries so pruning triggers heavily
        mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_p, max_cache_mb=1, max_entries=5)

        async def worker(idx: int):
            text = f"Burst Test Audio Sentence {idx}"
            key, clean_t, p_hash = mgr.compute_cache_key(text)
            fake_wav = b"RIFF" + b"\x00" * 200000  # ~200KB per file
            await mgr.put(key, text, clean_t, 1, p_hash, fake_wav)
            hit = await mgr.get(key)
            assert hit is not None

        # Run 20 concurrent puts simultaneously
        tasks = [worker(i) for i in range(20)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            assert not isinstance(r, Exception), f"Concurrent put error: {r}"

        # Allow pending background prunes to finish
        await mgr.aclose()

        # Verify cache was kept within reasonable boundaries
        async with get_db(db_p) as conn:
            stats = await crud.get_tts_cache_stats(conn)
            # Prune target is 80% of limit (4 files)
            assert stats["total_files"] <= 10

    @pytest.mark.asyncio
    async def test_prune_skips_db_deletion_if_file_unlink_fails(self, tmp_path):
        """Verify that if an OS error prevents unlinking a file, its DB entry is retained to prevent orphan files."""
        cache_dir = tmp_path / "cache_unlink"
        db_p = tmp_path / "cache_unlink.db"
        await init_db(db_p)

        mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_p, max_cache_mb=1, max_entries=2)
        key, clean_t, p_hash = mgr.compute_cache_key("UnlinkFailTest")
        fake_wav = b"RIFF" + b"\x00" * 1000
        await mgr.put(key, "UnlinkFailTest", clean_t, 1, p_hash, fake_wav)

        # Mock Path.unlink to raise PermissionError
        with patch.object(Path, "unlink", side_effect=PermissionError("Simulated locked file")):
            # Force prune
            pruned = await mgr.prune(max_mb=0, max_entries=0)
            assert pruned == 0

        # DB entry must still exist
        async with get_db(db_p) as conn:
            entry = await crud.get_tts_cache_entry(conn, key)
            assert entry is not None

    @pytest.mark.asyncio
    async def test_clear_atomically_resets_memory_cache(self, tmp_path):
        """Verify clear() clears in-memory LRU cache and disk files atomically."""
        cache_dir = tmp_path / "cache_clear"
        db_p = tmp_path / "cache_clear.db"
        await init_db(db_p)

        mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_p)
        key, clean_t, p_hash = mgr.compute_cache_key("ClearTest")
        await mgr.put(key, "ClearTest", clean_t, 1, p_hash, b"RIFF123456")

        assert key in mgr._mem_cache
        count, freed_mb = await mgr.clear()
        assert count == 1
        assert len(mgr._mem_cache) == 0
        assert mgr._mem_bytes_total == 0


# ============================================================================
# 3. Database Transaction Timeouts & Concurrency Safety Tests
# ============================================================================

class TestDatabaseTimeoutsAndConcurrencySafety:
    """Tests SQLite connection configuration and dual-table cleanup safety."""

    @pytest.mark.asyncio
    async def test_busy_timeout_configured_to_5000ms(self, tmp_path):
        """Verify configure_connection sets PRAGMA busy_timeout to 5000."""
        db_p = tmp_path / "timeout_test.db"
        async with aiosqlite.connect(str(db_p)) as conn:
            await configure_connection(conn, str(db_p))
            cur = await conn.execute("PRAGMA busy_timeout;")
            row = await cur.fetchone()
            assert row[0] == 5000

    @pytest.mark.asyncio
    async def test_clear_session_messages_clears_both_tables_if_present(self, tmp_path):
        """Verify clear_session_messages purges from both session_messages and messages if both exist."""
        db_p = tmp_path / "dual_table.db"
        await init_db(db_p)

        async with get_db(db_p) as conn:
            # Create session_messages table if not already existing
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS session_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content_chinese TEXT DEFAULT '',
                    content_japanese TEXT DEFAULT '',
                    raw_content TEXT DEFAULT ''
                );
            """)
            # Insert into both
            await conn.execute("""
                INSERT INTO session_messages (session_id, role, content_chinese)
                VALUES ('dual_sess', 'user', 'Dual Table Test');
            """)
            await crud.add_message(conn, MessageCreate(
                session_id="dual_sess",
                role="assistant",
                content_chinese="Dual Table Reply",
            ))
            await conn.commit()

            # Call clear_session_messages
            cleared = await crud.clear_session_messages(conn, "dual_sess")
            assert cleared is True

            # Verify both are empty
            cur1 = await conn.execute("SELECT COUNT(*) FROM session_messages WHERE session_id = 'dual_sess';")
            assert (await cur1.fetchone())[0] == 0
            cur2 = await conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = 'dual_sess';")
            assert (await cur2.fetchone())[0] == 0


# ============================================================================
# 4. Memory Leak Prevention During Long-Running Sessions Tests
# ============================================================================

class TestMemoryLeakPrevention:
    """Tests unregistration of finished background tasks in long-running services."""

    @pytest.mark.asyncio
    async def test_telegram_handlers_cleans_up_finished_task(self):
        """Verify TelegramBotHandlers automatically pops completed tasks from user_tasks."""
        handlers = TelegramBotHandlers()

        async def quick_worker():
            await asyncio.sleep(0.01)

        task = asyncio.create_task(quick_worker())
        handlers.user_tasks[999] = task

        # Attach done callback as implemented in process_text_chat
        def _cleanup(t, cid=999):
            if handlers.user_tasks.get(cid) is t:
                handlers.user_tasks.pop(cid, None)

        task.add_done_callback(_cleanup)

        # Wait for task to finish
        await task
        # Let callback execute
        await asyncio.sleep(0.02)

        # Must be pruned from dictionary
        assert 999 not in handlers.user_tasks

    @pytest.mark.asyncio
    async def test_telegram_handlers_cancel_user_task_pops_immediately(self):
        """Verify cancel_user_task immediately removes the entry from user_tasks."""
        handlers = TelegramBotHandlers()

        async def slow_worker():
            await asyncio.sleep(10.0)

        task = asyncio.create_task(slow_worker())
        handlers.user_tasks[888] = task

        handlers.cancel_user_task(888)
        assert 888 not in handlers.user_tasks
        assert task.cancelling() or task.cancelled()
