import pytest
from httpx import AsyncClient, ASGITransport
from galgame2voice.main import create_app
from galgame2voice.database.session import get_db, init_db
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate


@pytest.mark.asyncio
async def test_session_explicit_null_unbinds_voice_and_empty_settings_clear(tmp_path, monkeypatch):
    from galgame2voice.database.models import VoiceProfileCreate
    db = tmp_path / "session_binding.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db))
    await init_db(db)
    async with get_db(db) as conn:
        profile = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="User voice", gpt_weights_path="voice.ckpt", sovits_weights_path="voice.pth",
        ))
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        bound = await client.post("/api/chat/sessions", json={
            "id": "s_binding", "voice_profile_id": profile.id, "settings": {"voiceProfileId": profile.id},
        })
        assert bound.status_code == 200
        # A title-only edit must retain a binding; explicit null must clear it.
        renamed = await client.post("/api/chat/sessions", json={"id": "s_binding", "title": "Renamed"})
        assert renamed.json()["session"]["voice_profile_id"] == profile.id
        unbound = await client.post("/api/chat/sessions", json={
            "id": "s_binding", "voice_profile_id": None, "settings": {},
        })
        assert unbound.status_code == 200
        assert unbound.json()["session"]["voice_profile_id"] is None
        overview = (await client.get("/api/chat/sessions")).json()["sessions"]
        session = next(item for item in overview if item["id"] == "s_binding")
        assert session["voice_profile_id"] is None
        assert session["settings"] == {}
        fresh = await client.post("/api/chat/sessions", json={"id": "s_unbound", "voice_profile_id": None})
        assert fresh.json()["session"]["voice_profile_id"] is None


@pytest.mark.asyncio
async def test_session_overview_and_history_sync(tmp_path, monkeypatch):
    test_db = tmp_path / "test_sessions.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(test_db))
    await init_db(test_db)

    app = create_app()

    # Pre-populate some sessions and messages directly
    async with get_db(test_db) as conn:
        await crud.upsert_session(conn, session_id="s_test_1", title="夏目初次见面")
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
        await crud.get_or_create_session(conn, session_id="s_test_2")
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


@pytest.mark.asyncio
async def test_session_history_cursor_pagination(tmp_path, monkeypatch):
    test_db = tmp_path / "test_history_pagination.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(test_db))
    await init_db(test_db)

    app = create_app()

    async with get_db(test_db) as conn:
        await crud.upsert_session(conn, session_id="s_long_chat", title="长对话分页测试")
        for idx in range(1, 26):
            await crud.add_message(
                conn,
                MessageCreate(
                    session_id="s_long_chat",
                    role="user" if idx % 2 == 1 else "assistant",
                    content_chinese=f"第 {idx} 条消息",
                    content_japanese=f"メッセージ {idx}",
                ),
            )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Page 1: latest 10 messages (16..25)
        p1 = await client.get("/api/chat/history?session_id=s_long_chat&limit=10")
        assert p1.status_code == 200
        d1 = p1.json()
        assert d1["count"] == 10
        assert d1["has_more"] is True
        assert [m["content_chinese"] for m in d1["messages"]] == [f"第 {i} 条消息" for i in range(16, 26)]
        oldest_1 = d1["oldest_id"]
        assert oldest_1 == d1["messages"][0]["id"]

        # Page 2: older 10 messages before oldest_1 (6..15)
        p2 = await client.get(f"/api/chat/history?session_id=s_long_chat&limit=10&before_id={oldest_1}")
        assert p2.status_code == 200
        d2 = p2.json()
        assert d2["count"] == 10
        assert d2["has_more"] is True
        assert [m["content_chinese"] for m in d2["messages"]] == [f"第 {i} 条消息" for i in range(6, 16)]
        oldest_2 = d2["oldest_id"]

        # Page 3: remaining 5 messages before oldest_2 (1..5)
        p3 = await client.get(f"/api/chat/history?session_id=s_long_chat&limit=10&before_id={oldest_2}")
        assert p3.status_code == 200
        d3 = p3.json()
        assert d3["count"] == 5
        assert d3["has_more"] is False
        assert [m["content_chinese"] for m in d3["messages"]] == [f"第 {i} 条消息" for i in range(1, 6)]


@pytest.mark.asyncio
async def test_characters_rescan_endpoint(tmp_path, monkeypatch):
    test_db = tmp_path / "test_rescan.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(test_db))
    await init_db(test_db)

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/characters/rescan")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "discovered_count" in data
        assert "total_profiles" in data


