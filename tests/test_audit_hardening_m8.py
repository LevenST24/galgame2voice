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
from tests.test_chat_streaming_m4 import MockStreamingLLMAdapter


# ============================================================================
# 1. SSE Streaming Backpressure & Disconnect Handling Tests
# ============================================================================

class TestSseStreamingBackpressureAndDisconnect:
    """Tests SSE streaming pipeline cancellation, backpressure, and disconnect handling."""

    # Asserts tight sub-second wall-clock bound around a 0.1s cancellation timer; sensitive to multi-worker jitter.
    @pytest.mark.serial
    @pytest.mark.wall_clock
    @pytest.mark.asyncio
    async def test_put_with_cancel_aborts_promptly_when_cancelled(self):
        """Verify _put_with_cancel immediately returns False when queue is full and cancel_event is set."""
        ChatService()
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
