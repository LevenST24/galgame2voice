"""Comprehensive tests for turn reply linkage and interruption repair workflow."""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate, VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.routers.line_audio import LineAudioRequest, regenerate_line
from galgame2voice.services.chat_service import ChatService


@pytest.mark.asyncio
async def test_reproduce_business_defect_and_verify_fix(tmp_path):
    """
    1. Verifies the business defect when assistant ID is missing:
       regenerate_line with message_id=None returns persisted=False and lines_json is unchanged.
    2. Verifies the fix:
       When linked via reply_to_message_id, /api/chat/turn-reply resolves the assistant message_id,
       enabling regenerate_line to persist and update SQLite lines_json.
    """
    database = tmp_path / "defect_repro.db"
    await init_db(database)

    # 1. Setup user message and partial assistant message linked via reply_to_message_id
    async with get_db(database) as conn:
        user_msg = await crud.add_message(conn, MessageCreate(
            session_id="test_repro",
            role="user",
            content_chinese="你好",
            content_japanese="",
        ))
        initial_lines = [{
            "index": 0,
            "zh": "你好！",
            "ja": "こんにちは！",
            "emotion": "happy",
            "act": {"face": "smile", "pose": "normal", "motion": None, "blush": False, "fx": None},
            "audio": {"status": "skipped", "chunks": []},
        }]
        assistant_msg = await crud.add_message(conn, MessageCreate(
            session_id="test_repro",
            role="assistant",
            content_chinese="你好！",
            content_japanese="こんにちは！",
            lines=initial_lines,
            reply_to_message_id=user_msg.id,
        ))
        # Ensure active voice profile exists
        profile = await crud.get_active_voice_profile(conn)
        if not profile:
            await crud.create_voice_profile(
                conn,
                VoiceProfileCreate(
                    name="TestVoice",
                    gpt_weights_path="dummy.ckpt",
                    sovits_weights_path="dummy.pth",
                    ref_audio_path="dummy.wav",
                    prompt_text="dummy",
                ),
            )

    class FakeTtsService:
        db_path = database
        audio_dir = tmp_path / "audio"
        async def synthesize_to_file(self, *args, **kwargs):
            return "/audio/repaired.wav", tmp_path / "audio" / "repaired.wav", 1024

    fake_manager = SimpleNamespace(tts_service=FakeTtsService())
    disconnect_event = asyncio.Event()
    fake_request = SimpleNamespace(
        is_disconnected=AsyncMock(return_value=False),
        receive=disconnect_event.wait,
    )

    # Defect assertion: without message_id, persisted is False and DB is NOT updated
    req_null = LineAudioRequest(
        session_id="test_repro",
        message_id=None,
        line_index=0,
        text="こんにちは！",
        voice_profile_id=1,
    )
    result_null = await regenerate_line(req_null, fake_request, fake_manager)
    assert result_null["persisted"] is False
    async with get_db(database) as conn:
        row = await (await conn.execute("SELECT lines_json FROM messages WHERE id=?", (assistant_msg.id,))).fetchone()
        assert json.loads(row["lines_json"])[0]["audio"]["status"] == "skipped"

    # Fix verification: Client queries /api/chat/turn-reply using session_id and user_message_id
    os.environ["GALGAME2VOICE_DB_PATH"] = str(database)
    try:
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/api/chat/turn-reply?session_id=test_repro&user_message_id={user_msg.id}"
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ready"
            assert data["session_id"] == "test_repro"
            assert data["user_message_id"] == user_msg.id
            assert data["message_id"] == assistant_msg.id

        # Now client performs repair with the accurately resolved assistant message_id
        req_repaired = LineAudioRequest(
            session_id="test_repro",
            message_id=data["message_id"],
            line_index=0,
            text="こんにちは！",
            voice_profile_id=1,
        )
        result_repaired = await regenerate_line(req_repaired, fake_request, fake_manager)
        assert result_repaired["persisted"] is True
        assert result_repaired["audio"]["status"] == "ready"

        # Check DB lines_json was persisted!
        async with get_db(database) as conn:
            row_after = await (await conn.execute("SELECT lines_json FROM messages WHERE id=?", (assistant_msg.id,))).fetchone()
            repaired_lines = json.loads(row_after["lines_json"])
            assert repaired_lines[0]["audio"]["status"] == "ready"
            assert repaired_lines[0]["audio"]["chunks"][0]["audio_url"] == "/audio/repaired.wav"
    finally:
        os.environ.pop("GALGAME2VOICE_DB_PATH", None)


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
async def test_chat_sync_reply_link(tmp_path):
    """
    Tests that synchronous chat also records reply_to_message_id link.
    """
    database = tmp_path / "sync_link.db"
    await init_db(database)

    service = ChatService(db_path=database)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(api_key="synthetic"), "test", "custom",
    ))
    service._execute_sync_llm = AsyncMock(return_value=(
        json.dumps({"chinese": "同步回复", "japanese": "返事"}), 10,
    ))
    service._synthesize_sync_audio = AsyncMock(return_value=("", 0, 0, 0))
    service._extract_memory_safe = AsyncMock()

    try:
        res = await service.chat_sync("你好呀", session_id="sync_sess")
        assert res["chinese"] == "同步回复"

        async with get_db(database) as conn:
            rows = await (await conn.execute(
                "SELECT id, role, reply_to_message_id FROM messages WHERE session_id='sync_sess' ORDER BY id"
            )).fetchall()

        assert len(rows) == 2
        user_row = rows[0]
        assistant_row = rows[1]
        assert user_row["role"] == "user"
        assert assistant_row["role"] == "assistant"
        assert assistant_row["reply_to_message_id"] == user_row["id"]
    finally:
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
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
