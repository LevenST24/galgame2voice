"""
Integration and API tests for Memory and Affection REST endpoints (/api/memory, /api/affection)
and chat integration.
"""

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
class TestMemoryApi:
    async def test_memory_crud_flow(self, client):
        # 1. Initially empty
        res = await client.get("/api/memory?user_id=test_api_user")
        assert res.status_code == 200
        assert res.json() == []

        # 2. Create memory
        create_payload = {
            "user_id": "test_api_user",
            "character_id": 1,
            "category": "preference",
            "fact_key": "like_coffee",
            "fact_value": "喜欢在咖啡里加糖和鲜牛奶",
            "confidence": 0.95
        }
        res_create = await client.post("/api/memory", json=create_payload)
        assert res_create.status_code == 201
        created_data = res_create.json()
        assert created_data["id"] is not None
        assert created_data["fact_key"] == "like_coffee"
        mem_id = created_data["id"]

        # 3. List memories with filter
        res_list = await client.get("/api/memory?user_id=test_api_user&category=preference")
        assert res_list.status_code == 200
        items = res_list.json()
        assert len(items) == 1
        assert items[0]["id"] == mem_id

        # 4. Update memory
        update_payload = {
            "fact_value": "喜欢在咖啡里加三份糖和奶泡",
            "confidence": 1.0
        }
        res_update = await client.put(f"/api/memory/{mem_id}", json=update_payload)
        assert res_update.status_code == 200
        updated_data = res_update.json()
        assert updated_data["fact_value"] == "喜欢在咖啡里加三份糖和奶泡"
        assert updated_data["confidence"] == 1.0

        # 5. Delete specific memory
        res_del = await client.delete(f"/api/memory/{mem_id}")
        assert res_del.status_code == 200
        assert res_del.json()["status"] == "deleted"

        # Verify not found on subsequent delete
        res_del404 = await client.delete(f"/api/memory/{mem_id}")
        assert res_del404.status_code == 404

        # 6. Bulk clear memories
        await client.post("/api/memory", json=create_payload)
        res_clear = await client.delete("/api/memory?user_id=test_api_user")
        assert res_clear.status_code == 200
        assert res_clear.json()["status"] == "cleared"

        res_after_clear = await client.get("/api/memory?user_id=test_api_user")
        assert res_after_clear.json() == []

    async def test_memory_validation(self, client):
        # Empty fact_key should return 422
        res = await client.post("/api/memory", json={
            "user_id": "test_api_user",
            "fact_key": "",
            "fact_value": "some value"
        })
        assert res.status_code == 422


@pytest.mark.asyncio
class TestAffectionApi:
    async def test_affection_query_and_update(self, client):
        # 1. Get initial affection
        res = await client.get("/api/affection?user_id=u_aff_api&character_id=1")
        assert res.status_code == 200
        aff = res.json()
        assert aff["affection_level"] == 1
        assert aff["affection_score"] == 0
        assert aff["current_emotion"] == "normal"

        # 2. Update affection
        update_payload = {
            "user_id": "u_aff_api",
            "character_id": 1,
            "affection_score": 65,
            "current_emotion": "shy",
            "custom_nickname": "昂晴店长"
        }
        res_update = await client.post("/api/affection/update", json=update_payload)
        assert res_update.status_code == 200
        updated = res_update.json()
        assert updated["affection_score"] == 65
        assert updated["affection_level"] == 4  # Lv.4 Intimate for 65
        assert updated["current_emotion"] == "shy"
        assert updated["custom_nickname"] == "昂晴店长"

        # 3. Get dialogue gallery
        res_gallery = await client.get("/api/affection/dialogues?user_id=u_aff_api&character_id=1")
        assert res_gallery.status_code == 200
        gallery_data = res_gallery.json()
        assert gallery_data["total_count"] >= 9
        assert gallery_data["unlocked_count"] >= 4  # Lv.1 to Lv.4 milestones unlocked

        # 4. Reset affection
        res_reset = await client.post("/api/affection/reset", json={"user_id": "u_aff_api", "character_id": 1})
        assert res_reset.status_code == 200
        reset_data = res_reset.json()
        assert reset_data["affection_score"] == 0
        assert reset_data["affection_level"] == 1
        assert reset_data["current_emotion"] == "normal"


@pytest.mark.asyncio
class TestChatServiceMemoryAffectionIntegration:
    async def test_chat_sync_affection_and_fact_recording(self, client, monkeypatch):
        from galgame2voice.adapters.base import LLMResponse
        from galgame2voice.services.chat_service import ChatService

        class MockSyncAdapter:
            async def chat(self, messages, model=None, **kwargs):
                return LLMResponse(
                    content='{"chinese": "你好昂晴，我是四季夏目。请多关照！", "japanese": "こんにちは、昂晴。四季ナツメです。"}'
                )

        async def _mock_get_adapter(self_cs, conn=None, provider_id=None):
            return MockSyncAdapter(), "mock-model"

        monkeypatch.setattr(ChatService, "_get_active_llm_adapter", _mock_get_adapter)

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

        # Verify fact memory was recorded
        res_mems = await client.get("/api/memory?user_id=default_user")
        assert res_mems.status_code == 200
        mems = res_mems.json()
        assert len(mems) > 0, f"Expected memories to be saved, got {mems}"
        assert any(m["fact_key"] == "player_name" and m["fact_value"] == "昂晴" for m in mems), f"mems was {mems}"

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

