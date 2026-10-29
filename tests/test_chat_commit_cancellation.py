"""A successful assistant commit must remain owned when its caller is cancelled."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_pipelines.stream_coordinator import StreamCoordinator
from galgame2voice.services.chat_service import ChatService


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["stream", "sync"])
async def test_assistant_commit_followed_by_cancellation_keeps_exactly_one_turn(tmp_path, monkeypatch, mode):
    """Cancellation after SQLite commit cannot duplicate a reply or prune its user."""
    database = tmp_path / "commit-cancellation.db"
    await init_db(database)

    async def tokens(*args, **kwargs):
        yield '{"chinese":"Synthetic answer","japanese":""}'

    metrics = SimpleNamespace(record_chat_turn=AsyncMock(return_value={}))
    service = ChatService(
        db_path=database,
        tts_service=SimpleNamespace(audio_dir=tmp_path / "audio"),
        metrics_collector=metrics,
    )
    service._resolve_adapter_and_messages = AsyncMock(return_value=(
        SimpleNamespace(stream_chat=tokens, api_key="synthetic"), "synthetic", "synthetic", [],
    ))
    service._execute_sync_llm = AsyncMock(return_value=(
        '{"chinese":"Synthetic answer","japanese":""}', 0,
    ))
    service._synthesize_sync_audio = AsyncMock(return_value=("/audio/synthetic.wav", 0, 0, 0))
    service._spawn_background = lambda coro: coro.close()
    monkeypatch.setattr(StreamCoordinator, "_concat_audio_chunks", AsyncMock(return_value="/audio/synthetic.wav"))

    real_add, real_commit = crud.add_message, aiosqlite.Connection.commit
    assistant_pending = False
    cancelled_after_commit = asyncio.Event()
    operation = None

    async def add_and_arm(conn, message):
        nonlocal assistant_pending
        result = await real_add(conn, message)
        if message.role == "assistant":
            assistant_pending = True
        return result

    async def commit_and_cancel(conn):
        nonlocal assistant_pending
        await real_commit(conn)
        if assistant_pending:
            assistant_pending = False
            cancelled_after_commit.set()
            operation.cancel()

    monkeypatch.setattr(crud, "add_message", add_and_arm)
    monkeypatch.setattr(aiosqlite.Connection, "commit", commit_and_cancel)

    async def consume():
        return [event async for event in service.stream_chat("Synthetic question", session_id="synthetic")]

    try:
        operation = asyncio.create_task(consume() if mode == "stream" else service.chat_sync(
            "Synthetic question", session_id="synthetic"
        ))
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, timeout=10)
        assert cancelled_after_commit.is_set(), "test did not reach the assistant's durable commit"
        async with get_db(database) as conn:
            rows = await (await conn.execute(
                "SELECT role, content_chinese, audio_url FROM messages WHERE session_id=? ORDER BY id",
                ("synthetic",),
            )).fetchall()
        assert [(r[0], r[1], r[2]) for r in rows] == [
            ("user", "Synthetic question", ""),
            ("assistant", "Synthetic answer", "/audio/synthetic.wav"),
        ]
    finally:
        if operation is not None and not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_failed_assistant_commit_does_not_mark_reply_as_saved(tmp_path, monkeypatch, partial):
    database = tmp_path / "commit-failure.db"
    await init_db(database)
    async with get_db(database) as conn:
        user = await crud.add_message(conn, MessageCreate(session_id="test", role="user", content_chinese="Question"))

    coordinator = StreamCoordinator.__new__(StreamCoordinator)
    coordinator.db_path, coordinator.session_id, coordinator.user_msg = database, "test", user
    coordinator.persisted_assistant, coordinator.persisted_message_id = False, None

    async def fail_commit(conn):
        raise sqlite3.OperationalError("synthetic commit failure")

    monkeypatch.setattr(aiosqlite.Connection, "commit", fail_commit)
    if partial:
        await coordinator._persist_partial_message("Answer", "", 0)
    else:
        with pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"):
            await coordinator._persist_turn_message("Answer", "", "/audio/test.wav", 1)
    assert not coordinator.persisted_assistant
    assert coordinator.persisted_message_id is None
    async with get_db(database) as conn:
        rows = await (await conn.execute("SELECT role FROM messages ORDER BY id")).fetchall()
    assert [row[0] for row in rows] == ["user"]


@pytest.mark.asyncio
async def test_partial_reply_commit_followed_by_cancellation_marks_its_real_id(tmp_path, monkeypatch):
    database = tmp_path / "partial-cancel.db"
    await init_db(database)
    async with get_db(database) as conn:
        user = await crud.add_message(conn, MessageCreate(session_id="test", role="user", content_chinese="Question"))
    coordinator = StreamCoordinator.__new__(StreamCoordinator)
    coordinator.db_path, coordinator.session_id, coordinator.user_msg = database, "test", user
    coordinator.persisted_assistant, coordinator.persisted_message_id = False, None
    real_commit = aiosqlite.Connection.commit
    operation = None

    async def commit_then_cancel(conn):
        await real_commit(conn)
        operation.cancel()

    monkeypatch.setattr(aiosqlite.Connection, "commit", commit_then_cancel)
    operation = asyncio.create_task(coordinator._persist_partial_message("Partial answer", "", 0))
    with pytest.raises(asyncio.CancelledError):
        await operation
    async with get_db(database) as conn:
        row = await (await conn.execute("SELECT id FROM messages WHERE role='assistant'")).fetchone()
    assert coordinator.persisted_assistant
    assert row is not None and coordinator.persisted_message_id == row[0]
