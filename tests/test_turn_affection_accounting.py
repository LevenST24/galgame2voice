"""A chat turn earns affection only when its reply is saved.

An empty or unsaved reply withdraws the user's message so it can be sent
again; awarding points for it as well would count the same message twice.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from galgame2voice.adapters.base import LLMResponse
from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_service import ChatService

# Base turn (+1) plus a compliment (+2) when the turn counts.
PROMPT = "你今天真可爱"
EMPTY_REPLY = json.dumps({"chinese": "", "japanese": ""})
# No Japanese line, so the turn needs no voice engine.
TEXT_REPLY = json.dumps({"chinese": "嗯，谢谢你～", "japanese": ""}, ensure_ascii=False)


def _service(database, reply: str) -> ChatService:
    async def stream_chat(*args, **kwargs):
        yield reply

    async def chat(*args, **kwargs):
        return LLMResponse(content=reply)

    service = ChatService(db_path=database)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(stream_chat=stream_chat, chat=chat, api_key="synthetic"), "test", "custom",
    ))
    service._extract_memory_safe = AsyncMock()
    return service


async def _state(database):
    async with get_db(database) as conn:
        points = await (await conn.execute(
            "SELECT COALESCE(SUM(affection_score), 0), COALESCE(SUM(daily_points_earned), 0)"
            " FROM character_affection"
        )).fetchone()
        roles = [row[0] for row in await (await conn.execute("SELECT role FROM messages ORDER BY id")).fetchall()]
    return tuple(points), roles


async def _stream_turn(service: ChatService) -> list[dict]:
    events = [event async for event in service.stream_chat(PROMPT, session_id="affection")]
    await service.aclose()
    return events


@pytest.mark.asyncio
async def test_streamed_reply_earns_affection(tmp_path):
    database = tmp_path / "chat.db"
    await init_db(database)
    await _stream_turn(_service(database, TEXT_REPLY))
    assert await _state(database) == ((3, 3), ["user", "assistant"])


@pytest.mark.asyncio
async def test_streamed_empty_reply_earns_no_affection(tmp_path):
    database = tmp_path / "chat.db"
    await init_db(database)
    events = await _stream_turn(_service(database, EMPTY_REPLY))
    done = [event for event in events if event["event"] == "done"]
    assert done and done[0]["data"]["affection"]["points_earned"] == 0
    assert await _state(database) == ((0, 0), [])


@pytest.mark.asyncio
async def test_sync_reply_earns_affection(tmp_path):
    database = tmp_path / "chat.db"
    await init_db(database)
    result = await _service(database, TEXT_REPLY).chat_sync(PROMPT, session_id="affection")
    assert result["affection"]["points_earned"] == 3
    assert await _state(database) == ((3, 3), ["user", "assistant"])


@pytest.mark.asyncio
async def test_sync_empty_reply_is_withdrawn_without_affection(tmp_path):
    database = tmp_path / "chat.db"
    await init_db(database)
    result = await _service(database, EMPTY_REPLY).chat_sync(PROMPT, session_id="affection")
    assert result["affection"]["points_earned"] == 0
    assert await _state(database) == ((0, 0), [])


@pytest.mark.asyncio
async def test_sync_reply_that_fails_to_save_earns_no_affection(tmp_path, monkeypatch):
    database = tmp_path / "chat.db"
    await init_db(database)
    add_message = crud.add_message

    async def failing_assistant_write(conn, message):
        if message.role == "assistant":
            raise RuntimeError("disk full")
        return await add_message(conn, message)

    monkeypatch.setattr(crud, "add_message", failing_assistant_write)
    with pytest.raises(RuntimeError, match="disk full"):
        await _service(database, TEXT_REPLY).chat_sync(PROMPT, session_id="affection")
    assert await _state(database) == ((0, 0), [])
