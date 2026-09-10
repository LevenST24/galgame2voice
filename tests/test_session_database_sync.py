import pytest
from httpx import AsyncClient, ASGITransport
from galgame2voice.main import create_app
from galgame2voice.database.session import get_db, init_db
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate


@pytest.mark.asyncio
async def test_session_overview_and_history_sync(tmp_path, monkeypatch):
    test_db = tmp_path / "test_sessions.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(test_db))
    await init_db(test_db)

    app = create_app()

    # Pre-populate some sessions and messages directly
    async with get_db(test_db) as conn:
        s1 = await crud.upsert_session(conn, session_id="s_test_1", title="夏目初次见面")
        await crud.add_message(conn, MessageCreate(
            session_id="s_test_1",
            role="user",
            content_chinese="夏目你好！",
            content_japanese="こんにちは",
        ))
        await crud.add_message(conn, MessageCreate(
            session_id="s_test_1",
            role="assistant",
            content_chinese="你好呀，欢迎光临咖啡馆！",
            content_japanese="こんにちは、いらっしゃいませ！",
            audio_url="/audio/chunk_1.wav",
        ))

        # Second session with NO explicit title (should auto-infer from first user message)
        s2 = await crud.get_or_create_session(conn, session_id="s_test_2")
        await crud.add_message(conn, MessageCreate(
            session_id="s_test_2",
            role="user",
            content_chinese="今天天气真好，想去散步",
            content_japanese="",
        ))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. GET /api/chat/sessions
        resp = await client.get("/api/chat/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 2
        sessions = {s["id"]: s for s in data["sessions"]}

        assert "s_test_1" in sessions
        assert sessions["s_test_1"]["title"] == "夏目初次见面"
        assert sessions["s_test_1"]["message_count"] == 2
        assert "欢迎光临" in sessions["s_test_1"]["last_message"]

        assert "s_test_2" in sessions
        # Inferred title from first user message
        assert sessions["s_test_2"]["title"] == "今天天气真好，想去散步"
        assert sessions["s_test_2"]["message_count"] == 1

        # 2. GET /api/chat/history
        h_resp = await client.get("/api/chat/history?session_id=s_test_1")
        assert h_resp.status_code == 200
        h_data = h_resp.json()
        assert h_data["count"] == 2
        assert h_data["messages"][0]["role"] == "user"
        assert h_data["messages"][0]["content_chinese"] == "夏目你好！"
        assert h_data["messages"][1]["role"] == "assistant"
        assert h_data["messages"][1]["audio_url"] == "/audio/chunk_1.wav"

        # 3. POST /api/chat/sessions (create/update session)
        up_resp = await client.post("/api/chat/sessions", json={
            "id": "s_test_3",
            "title": "新建自定义会话",
            "custom_system_prompt": "你是一只猫娘",
            "settings": {"temperature": 0.85, "top_p": 0.9},
        })
        assert up_resp.status_code == 200
        up_data = up_resp.json()
        assert up_data["session"]["id"] == "s_test_3"
        assert up_data["session"]["title"] == "新建自定义会话"
        assert "猫娘" in up_data["session"]["custom_system_prompt"]

        # Verify it shows up in GET /api/chat/sessions with settings
        resp2 = await client.get("/api/chat/sessions")
        s3 = next(s for s in resp2.json()["sessions"] if s["id"] == "s_test_3")
        assert s3["settings"]["temperature"] == 0.85

        # 4. DELETE /api/chat/sessions/s_test_1
        del_resp = await client.delete("/api/chat/sessions/s_test_1")
        assert del_resp.status_code == 200
        assert del_resp.json()["success"] is True

        # Verify messages also cleared
        h_del = await client.get("/api/chat/history?session_id=s_test_1")
        assert h_del.json()["count"] == 0
