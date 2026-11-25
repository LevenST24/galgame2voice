"""Long-term memory only learns from prompts whose reply was kept."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from galgame2voice.adapters.base import LLMResponse
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_service import ChatService


def _service(database, reply: str) -> ChatService:
    async def stream_chat(messages, *args, **kwargs):
        # Give background work started with the turn time to finish first.
        await asyncio.sleep(0.2)
        yield reply

    async def chat(*args, **kwargs):
        return LLMResponse(content=reply)

    service = ChatService(db_path=database)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(stream_chat=stream_chat, chat=chat, api_key="synthetic"), "test", "custom",
    ))
    return service


async def _run(database, reply: str):
    await init_db(database)
    service = _service(database, reply)
    events = [e async for e in service.stream_chat("我叫小明", session_id="memory")]
    await service.aclose()
    async with get_db(database) as conn:
        roles = [r[0] for r in await (await conn.execute("SELECT role FROM messages ORDER BY id")).fetchall()]
        facts = [tuple(r) for r in await (await conn.execute("SELECT fact_key, fact_value FROM user_memories")).fetchall()]
        nicks = [r[0] for r in await (await conn.execute("SELECT custom_nickname FROM character_affection")).fetchall()]
    return events, roles, facts, nicks


@pytest.mark.asyncio
async def test_withdrawn_prompt_teaches_no_memory(tmp_path):
    events, roles, facts, nicks = await _run(tmp_path / "chat.db", json.dumps({"chinese": "", "japanese": ""}))
    assert events[-1]["event"] == "error"
    assert roles == []
    assert facts == []
    assert all(n is None for n in nicks)


@pytest.mark.asyncio
async def test_kept_turn_still_teaches_memory(tmp_path):
    reply = json.dumps({"chinese": "嗯，好的。", "japanese": ""}, ensure_ascii=False)
    events, roles, facts, _ = await _run(tmp_path / "chat.db", reply)
    assert events[-1]["event"] == "done"
    assert roles == ["user", "assistant"]
    assert ("player_name", "小明") in facts
