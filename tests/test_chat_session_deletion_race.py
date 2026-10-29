"""A deleted conversation must not be recreated by an in-flight reply."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.chat_pipelines.turn_persistence import TurnUnavailableError


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
@pytest.mark.parametrize("change", ["delete", "recreate", "clear"])
async def test_sync_reply_does_not_write_after_turn_is_removed(tmp_path, change):
    database = tmp_path / "sync.db"
    await init_db(database)
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_llm(**kwargs):
        started.set()
        await release.wait()
        return '{"chinese":"Synthetic answer","japanese":""}', 0

    service = ChatService(db_path=database)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(api_key="synthetic"), "test", "custom",
    ))
    service._execute_sync_llm = AsyncMock(side_effect=delayed_llm)
    service._extract_memory_safe = AsyncMock()
    operation = asyncio.create_task(service.chat_sync("Original question", session_id="deleted"))
    try:
        await asyncio.wait_for(started.wait(), 5)
        async with get_db(database) as conn:
            if change == "clear":
                assert await crud.clear_session_messages(conn, "deleted")
            else:
                assert await crud.delete_session(conn, "deleted")
                if change == "recreate":
                    await crud.add_message(conn, MessageCreate(
                        session_id="deleted", role="user", content_chinese="New conversation",
                    ))
        release.set()
        with pytest.raises(TurnUnavailableError, match="对话已被删除或清空"):
            await asyncio.wait_for(operation, 5)
        async with get_db(database) as conn:
            sessions = await (await conn.execute("SELECT id FROM sessions WHERE id = ?", ("deleted",))).fetchall()
            rows = await (await conn.execute(
                "SELECT role, content_chinese FROM messages WHERE session_id = ? ORDER BY id", ("deleted",)
            )).fetchall()
        assert bool(sessions) is (change != "delete")
        assert [(row[0], row[1]) for row in rows] == ([('user', 'New conversation')] if change == "recreate" else [])
    finally:
        release.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        await service.aclose()
