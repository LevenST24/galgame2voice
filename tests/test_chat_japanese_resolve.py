import pytest
from httpx import ASGITransport, AsyncClient
from galgame2voice.main import app
from galgame2voice.services.chat_service import ChatService
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db

@pytest.mark.asyncio
async def test_resolve_message_japanese_db_match(tmp_path):
    db_file = tmp_path / "test_resolve.db"
    async with get_db(db_file) as conn:
        await crud.add_message(
            conn,
            MessageCreate(
                session_id="test_sess_ja",
                role="assistant",
                content_chinese="深夜故事的话……我想讲一个关于星星的传说。",
                content_japanese="夜更けの話、ですか……星の伝説をお話ししましょう。",
                audio_url="/audio/test.wav",
                latency_ms=100,
            ),
        )

    service = ChatService(db_path=db_file)
    # Exact match with session
    ja = await service.resolve_message_japanese("深夜故事的话……我想讲一个关于星星的传说。", session_id="test_sess_ja")
    assert ja == "夜更けの話、ですか……星の伝説をお話ししましょう。"

    # Substring / fuzzy match without session
    ja_fuzzy = await service.resolve_message_japanese("深夜故事的话……我想讲一个关于星星的传说。")
    assert ja_fuzzy == "夜更けの話、ですか……星の伝説をお話ししましょう。"

    # Empty text
    assert await service.resolve_message_japanese("") == ""


@pytest.mark.asyncio
async def test_api_chat_japanese_endpoint(tmp_path, monkeypatch):
    db_file = tmp_path / "test_api_ja.db"
    async with get_db(db_file) as conn:
        await crud.add_message(
            conn,
            MessageCreate(
                session_id="api_sess_1",
                role="assistant",
                content_chinese="今天天气真好呢。",
                content_japanese="今日は本当にいい天気ですね。",
                audio_url="",
                latency_ms=50,
            ),
        )

    from galgame2voice.routers.chat import get_chat_service, set_chat_service
    test_svc = ChatService(db_path=db_file)
    set_chat_service(test_svc)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/japanese",
            json={"text": "今天天气真好呢。", "session_id": "api_sess_1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["japanese"] == "今日は本当にいい天気ですね。"

    set_chat_service(None)
