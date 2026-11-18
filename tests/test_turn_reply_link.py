"""Comprehensive tests for turn reply linkage and interruption repair workflow."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.services.chat_service import ChatService


@pytest.mark.asyncio
async def test_stream_chat_turn_started_and_interrupted_reply_link(tmp_path):
    """
    Tests that:
    1. SSE stream emits turn_started with user_message_id before any line event.
    2. If the turn is interrupted mid-stream, partial reply is persisted with reply_to_message_id.
    """
    database = tmp_path / "stream_link.db"
    await init_db(database)

    # Provider yields a line, then hangs until cancelled
    release_hang = asyncio.Event()

    async def fake_stream_tokens(*args, **kwargs):
        chunk = '{"chinese": "你好啊", "japanese": "こんにちは", "lines": [{"zh": "你好啊", "ja": "こんにちは", "emotion": "happy"}]}'
        yield chunk
        await release_hang.wait()

    service = ChatService(db_path=database)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(stream_chat=fake_stream_tokens, api_key="synthetic"), "test", "custom",
    ))
    service._extract_memory_safe = AsyncMock()

    cancel = asyncio.Event()
    events = []
    stream = service.stream_chat("测试问题", session_id="stream_sess", cancel_event=cancel)
    try:
        # 1. First event must be turn_started
        turn_started_ev = await asyncio.wait_for(stream.__anext__(), 2)
        assert turn_started_ev["event"] == "turn_started"
        user_msg_id = turn_started_ev["data"]["user_message_id"]
        assert isinstance(user_msg_id, int) and user_msg_id > 0
        assert turn_started_ev["data"]["session_id"] == "stream_sess"

        # 2. Next events can be text or line
        seen_line = False
        while True:
            ev = await asyncio.wait_for(stream.__anext__(), 2)
            events.append(ev)
            if ev.get("event") == "line":
                seen_line = True
                # Cancel stream after first line event
                cancel.set()
                break

        assert seen_line, "Expected to receive at least one line event before interruption"

        # Consume remaining teardown events
        async for ev in stream:
            events.append(ev)

        # 3. Check database persistence inside connection block
        async with get_db(database) as conn:
            rows = await (await conn.execute(
                "SELECT id, role, content_chinese, reply_to_message_id FROM messages WHERE session_id='stream_sess' ORDER BY id"
            )).fetchall()

            assert len(rows) >= 2
            user_row = rows[0]
            assistant_row = rows[1]
            assert user_row["role"] == "user"
            assert user_row["id"] == user_msg_id
            assert assistant_row["role"] == "assistant"
            # Crucial link assertion:
            assert assistant_row["reply_to_message_id"] == user_msg_id

            # 4. Query turn-reply
            lookup = await crud.lookup_turn_reply(conn, "stream_sess", user_msg_id)
            assert lookup["status"] == "ready"
            assert lookup["message_id"] == assistant_row["id"]
    finally:
        release_hang.set()
        await stream.aclose()
        await service.aclose()


@pytest.mark.asyncio
async def test_import_chat_does_not_populate_reply_to_link(tmp_path):
    """
    Tests protocol item 7:
    Importing portable chat backup does not link old reply_to IDs to new DB records.
    """
    database = tmp_path / "import_link.db"
    await init_db(database)

    os.environ["GALGAME2VOICE_DB_PATH"] = str(database)
    try:
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            import_payload = {
                "sessions": [
                    {
                        "id": "s_import_test1",
                        "title": "导入测试",
                        "settings": {},
                        "createdAt": 1700000000000,
                        "updatedAt": 1700000000000,
                        "messages": [
                            {"role": "user", "content": "历史用户", "japanese": "", "ts": 1700000000000},
                            {"role": "assistant", "content": "历史回复", "japanese": "", "ts": 1700000001000},
                        ],
                    }
                ]
            }
            resp = await client.post("/api/chat/import", json=import_payload)
            assert resp.status_code == 200

        async with get_db(database) as conn:
            rows = await (await conn.execute(
                "SELECT id, role, reply_to_message_id FROM messages WHERE session_id='s_import_test1' ORDER BY id"
            )).fetchall()

        assert len(rows) == 2
        assert rows[1]["role"] == "assistant"
        assert rows[1]["reply_to_message_id"] is None
    finally:
        os.environ.pop("GALGAME2VOICE_DB_PATH", None)
