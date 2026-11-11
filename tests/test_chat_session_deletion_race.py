"""A deleted conversation must not be recreated by an in-flight reply."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_service import ChatService


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [False, True])
@pytest.mark.parametrize("recreate", [False, True])
async def test_deleted_session_is_not_resurrected_by_stream(tmp_path, finish, recreate):
    database = tmp_path / "chat.db"
    await init_db(database)
    release = asyncio.Event()

    async def tokens(*args, **kwargs):
        yield '{"chinese":"Synthetic partial'
        await release.wait()
        yield ' answer","japanese":""}'

    service = ChatService(db_path=database)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(stream_chat=tokens, api_key="synthetic"), "test", "custom",
    ))
    service._extract_memory_safe = AsyncMock()
    stream = service.stream_chat("Original question", session_id="deleted")
    try:
        started = await asyncio.wait_for(stream.__anext__(), 5)
        assert started["event"] == "turn_started"
        assert started["data"]["session_id"] == "deleted"
        assert isinstance(started["data"]["user_message_id"], int) and started["data"]["user_message_id"] > 0
        assert (await asyncio.wait_for(stream.__anext__(), 5))["event"] == "text"
        async with get_db(database) as conn:
            assert await crud.delete_session(conn, "deleted")
            if recreate:
                await crud.add_message(conn, MessageCreate(
                    session_id="deleted", role="user", content_chinese="New conversation",
                ))
        if finish:
            release.set()
            events = [event async for event in stream]
            assert not any(event["event"] == "done" for event in events)
        else:
            await stream.aclose()
        async with get_db(database) as conn:
            sessions = await (await conn.execute("SELECT id FROM sessions WHERE id = ?", ("deleted",))).fetchall()
            rows = await (await conn.execute(
                "SELECT role, content_chinese FROM messages WHERE session_id = ? ORDER BY id", ("deleted",)
            )).fetchall()
        assert bool(sessions) is recreate
        assert [(row[0], row[1]) for row in rows] == ([('user', 'New conversation')] if recreate else [])
    finally:
        release.set()
        await stream.aclose()
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["clear", "delete", "recreate"])
async def test_removed_stream_turn_reports_error(tmp_path, change):
    database = tmp_path / "stream.db"
    await init_db(database)
    release = asyncio.Event()

    async def tokens(*args, **kwargs):
        yield '{"chinese":"Partial'
        await release.wait()
        yield ' answer","japanese":""}'

    service = ChatService(db_path=database)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(stream_chat=tokens, api_key="synthetic"), "test", "custom",
    ))
    service._extract_memory_safe = AsyncMock()
    stream = service.stream_chat("Question", session_id="s")
    try:
        started = await asyncio.wait_for(stream.__anext__(), 5)
        assert started["event"] == "turn_started"
        assert started["data"]["session_id"] == "s"
        assert isinstance(started["data"]["user_message_id"], int) and started["data"]["user_message_id"] > 0
        assert (await asyncio.wait_for(stream.__anext__(), 5))["event"] == "text"
        async with get_db(database) as conn:
            if change == "clear":
                assert await crud.clear_session_messages(conn, "s")
            else:
                assert await crud.delete_session(conn, "s")
                if change == "recreate":
                    await crud.add_message(conn, MessageCreate(
                        session_id="s", role="user", content_chinese="New conversation",
                    ))
        release.set()
        events = [event async for event in stream]
        errors = [event["data"] for event in events if event["event"] == "error"]
        assert errors == [{"error": "对话已被删除或清空，本次回复未保存。", "error_code": "turn_unavailable"}]
        assert not any(event["event"] == "done" for event in events)
        async with get_db(database) as conn:
            rows = await (await conn.execute(
                "SELECT role, content_chinese FROM messages WHERE session_id = ? ORDER BY id", ("s",)
            )).fetchall()
        assert [(row[0], row[1]) for row in rows] == (
            [("user", "New conversation")] if change == "recreate" else []
        )
    finally:
        release.set()
        await stream.aclose()
        await service.aclose()
