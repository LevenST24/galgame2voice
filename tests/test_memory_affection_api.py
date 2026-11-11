"""
Integration tests for chat-driven long-term memory extraction and character affection progression.
"""

import asyncio

import pytest
from httpx import AsyncClient, ASGITransport
from galgame2voice.main import create_app
from galgame2voice.database import crud
from galgame2voice.database.session import get_db


@pytest.fixture
def app(isolate_test_database):
    import sqlite3
    conn = sqlite3.connect(isolate_test_database)
    conn.execute("INSERT OR IGNORE INTO voice_profiles (id, name, gpt_weights_path, sovits_weights_path, is_default) VALUES (1, 'default_char', 'default.ckpt', 'default.pth', 1);")
    conn.commit()
    conn.close()
    return create_app()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
class TestChatServiceMemoryAffectionIntegration:
    async def test_chat_sync_affection_and_fact_recording(self, client, isolate_test_database, monkeypatch):
        from galgame2voice.adapters.base import LLMResponse
        from galgame2voice.routers.chat import get_chat_service
        from galgame2voice.services.chat_service import ChatService
        from galgame2voice.services.memory_service import MemoryService

        class MockSyncAdapter:
            async def chat(self, messages, model=None, **kwargs):
                return LLMResponse(
                    content='{"chinese": "你好昂晴，我是四季夏目。请多关照！", "japanese": "こんにちは、昂晴。四季ナツメです。"}'
                )

        async def _mock_get_adapter(self_cs, conn=None, provider_id=None):
            return MockSyncAdapter(), "mock-model"

        monkeypatch.setattr(ChatService, "_get_active_llm_adapter", _mock_get_adapter)

        # Fact extraction runs in a background task after the reply is sent. Delay it the way a
        # busy runner can, so the test has to wait for it.
        extract = MemoryService.process_user_message

        async def _slow_extract(self_ms, **kwargs):
            await asyncio.sleep(0.2)
            return await extract(self_ms, **kwargs)

        monkeypatch.setattr(MemoryService, "process_user_message", _slow_extract)

        # 1. First chat turn: user declares name
        res1 = await client.post("/api/chat", json={
            "prompt": "你好呀，我叫昂晴。",
            "session_id": "sess_aff_test",
        })
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["chinese"] == "你好昂晴，我是四季夏目。请多关照！"
        assert "affection" in data1
        assert data1["affection"]["score"] >= 1
        assert data1["affection"]["level"] == 1

        # Verify fact memory was recorded once the background extraction has finished
        await get_chat_service().aclose()
        async with get_db(isolate_test_database) as conn:
            mems = await crud.list_memories(conn, user_id="default_user")
        assert len(mems) > 0, f"Expected memories to be saved, got {mems}"
        assert any(m.fact_key == "player_name" and m.fact_value == "昂晴" for m in mems), f"mems was {mems}"

    async def test_chat_stream_done_event_contains_affection(self, client, monkeypatch):
        from galgame2voice.services.chat_service import ChatService

        class MockStreamAdapter:
            async def stream_chat(self, messages, model=None, **kwargs):
                yield '{"chinese": "枣子姐才不是我的名字呢！", '
                yield '"japanese": "その名前で呼ばないでって言ってるでしょ！"}'

        async def _mock_get_adapter(self_cs, conn=None, provider_id=None):
            return MockStreamAdapter(), "mock-model"

        monkeypatch.setattr(ChatService, "_get_active_llm_adapter", _mock_get_adapter)

        # Send Easter egg prompt via SSE stream
        res = await client.post("/api/chat/stream", json={
            "prompt": "枣子姐最可爱了！",
            "session_id": "sess_stream_egg",
        })
        assert res.status_code == 200
        stream_text = res.text
        assert "event: done" in stream_text
        assert "affection" in stream_text
        assert "tsundere" in stream_text

