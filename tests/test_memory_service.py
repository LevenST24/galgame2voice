"""
Unit and integration tests for Long-term Memory & RAG Retrieval Engine (MemoryService).
Covers heuristic fact extraction, SQLite persistence & upsert deduplication,
dynamic composite relevance scoring, top-K RAG retrieval, and prompt injection.
"""

import pytest
import aiosqlite
from galgame2voice.database import crud
from galgame2voice.database.models import UserMemoryCreate, UserMemoryUpdate
from galgame2voice.database.session import get_db
from galgame2voice.services.memory_service import MemoryService
from galgame2voice.services.session_manager import SessionManager


@pytest.fixture
def memory_service(isolate_test_database):
    import sqlite3
    conn = sqlite3.connect(isolate_test_database)
    conn.execute("INSERT OR IGNORE INTO voice_profiles (id, name, gpt_weights_path, sovits_weights_path, is_default) VALUES (1, 'default_char', 'default.ckpt', 'default.pth', 1);")
    conn.commit()
    conn.close()
    return MemoryService(db_path=isolate_test_database)


class TestHeuristicFactExtraction:
    def test_extract_nickname(self, memory_service):
        facts = memory_service.extract_facts_heuristic("你好呀，我叫昂晴就好。")
        assert len(facts) >= 1
        name_fact = next((f for f in facts if f["category"] == "nickname"), None)
        assert name_fact is not None
        assert name_fact["fact_key"] == "player_name"
        assert name_fact["fact_value"] == "昂晴"
        assert name_fact["confidence"] == 1.0

        facts2 = memory_service.extract_facts_heuristic("我的名字是高坂京介。")
        assert any(f["fact_value"] == "高坂京介" and f["fact_key"] == "player_name" for f in facts2)

    def test_extract_preference(self, memory_service):
        facts = memory_service.extract_facts_heuristic("夏目，我平时喜欢在咖啡里加大量牛奶和糖！")
        pref_fact = next((f for f in facts if f["category"] == "preference"), None)
        assert pref_fact is not None
        assert "like_" in pref_fact["fact_key"]
        assert "牛奶" in pref_fact["fact_value"] or "咖啡" in pref_fact["fact_value"]
        assert pref_fact["confidence"] >= 0.9

    def test_extract_taboo_dislike(self, memory_service):
        facts = memory_service.extract_facts_heuristic("其实我不喜欢苦黑咖啡和青椒。")
        taboo_fact = next((f for f in facts if f["category"] == "taboo"), None)
        assert taboo_fact is not None
        assert "dislike_" in taboo_fact["fact_key"]
        assert "苦黑咖啡" in taboo_fact["fact_value"] or "青椒" in taboo_fact["fact_value"]

    def test_extract_promise(self, memory_service):
        facts = memory_service.extract_facts_heuristic("我们约定这周末一起去正统酒吧小酌。")
        promise_fact = next((f for f in facts if f["category"] == "promise"), None)
        assert promise_fact is not None
        assert "promise_" in promise_fact["fact_key"]
        assert "酒吧" in promise_fact["fact_value"] or "小酌" in promise_fact["fact_value"]

    def test_extract_identity(self, memory_service):
        facts = memory_service.extract_facts_heuristic("我是一名程序员，每天都在写代码。")
        id_fact = next((f for f in facts if f["category"] == "identity"), None)
        assert id_fact is not None
        assert id_fact["fact_key"] == "occupation"
        assert id_fact["fact_value"] == "程序员"

    def test_extract_empty_or_neutral(self, memory_service):
        facts = memory_service.extract_facts_heuristic("今天的天气真好啊，阳光明媚。")
        assert len(facts) == 0


class TestMemoryPersistenceAndUpsert:
    @pytest.mark.asyncio
    async def test_process_user_message_and_upsert(self, memory_service, isolate_test_database):
        user_id = "test_player_1"
        char_id = 1

        # Turn 1: Declare nickname
        saved1 = await memory_service.process_user_message(
            user_id=user_id,
            character_id=char_id,
            message_text="初次见面，我叫昂晴。",
        )
        assert len(saved1) >= 1
        assert saved1[0].fact_key == "player_name"
        assert saved1[0].fact_value == "昂晴"

        # Verify nickname synced to character_affection
        async with get_db(isolate_test_database) as conn:
            aff = await crud.get_character_affection(conn, user_id=user_id, character_id=char_id)
            assert aff is not None
            assert aff.custom_nickname == "昂晴"

        # Turn 2: Change nickname -> upsert should update value, not duplicate
        saved2 = await memory_service.process_user_message(
            user_id=user_id,
            character_id=char_id,
            message_text="以后你可以叫我昂晴店长。",
        )
        assert len(saved2) >= 1
        assert saved2[0].fact_key == "player_name"
        assert saved2[0].fact_value == "昂晴店长"

        # Check in DB only 1 nickname record exists
        async with get_db(isolate_test_database) as conn:
            all_mems = await crud.list_memories(conn, user_id=user_id, character_id=char_id)
            player_name_mems = [m for m in all_mems if m.fact_key == "player_name"]
            assert len(player_name_mems) == 1
            assert player_name_mems[0].fact_value == "昂晴店长"


class TestMemoryRAGRetrieval:
    @pytest.mark.asyncio
    async def test_retrieve_relevant_memories_with_anchor_and_context(self, memory_service, isolate_test_database):
        user_id = "test_player_rag"
        char_id = 1

        async with get_db(isolate_test_database) as conn:
            # Seed anchor memory
            await crud.create_memory(conn, UserMemoryCreate(
                user_id=user_id,
                character_id=char_id,
                category="nickname",
                fact_key="player_name",
                fact_value="昂晴",
                confidence=1.0
            ))
            # Seed identity memory
            await crud.create_memory(conn, UserMemoryCreate(
                user_id=user_id,
                character_id=char_id,
                category="identity",
                fact_key="occupation",
                fact_value="语言学系大学生",
                confidence=1.0
            ))
            # Seed coffee preference
            await crud.create_memory(conn, UserMemoryCreate(
                user_id=user_id,
                character_id=char_id,
                category="preference",
                fact_key="like_sweet_coffee",
                fact_value="喜欢在咖啡里加两包糖和牛奶",
                confidence=0.9
            ))
            # Seed irrelevant memory
            await crud.create_memory(conn, UserMemoryCreate(
                user_id=user_id,
                character_id=char_id,
                category="preference",
                fact_key="like_gaming",
                fact_value="喜欢在周末打格斗游戏",
                confidence=0.8
            ))

        # Query about coffee
        retrieved = await memory_service.retrieve_relevant_memories(
            user_id=user_id,
            character_id=char_id,
            prompt="夏目，帮我泡一杯咖啡吧，你知道我口味的。",
            top_k=3
        )

        assert len(retrieved) <= 3
        # Anchor 'player_name' must be present
        keys = [m.fact_key for m in retrieved]
        assert "player_name" in keys
        # Coffee preference must be retrieved
        assert "like_sweet_coffee" in keys

        # Verify recall count incremented
        async with get_db(isolate_test_database) as conn:
            mem = await crud.get_memory(conn, retrieved[0].id)
            assert mem.recall_count >= 1
            assert mem.last_recalled_at is not None

    def test_format_memory_prompt_block(self, memory_service):
        from galgame2voice.database.models import UserMemoryResponse
        memories = [
            UserMemoryResponse(
                id=1, user_id="u1", character_id=1, category="nickname",
                fact_key="player_name", fact_value="昂晴", confidence=1.0, recall_count=1
            ),
            UserMemoryResponse(
                id=2, user_id="u1", character_id=1, category="preference",
                fact_key="like_coffee", fact_value="加双份糖的拿铁", confidence=0.9, recall_count=2
            ),
            UserMemoryResponse(
                id=3, user_id="u1", character_id=1, category="promise",
                fact_key="promise_weekend", fact_value="这周末一起去买咖啡豆", confidence=0.9, recall_count=0
            ),
        ]
        aff_info = {
            "level": 3,
            "level_name": "友好/信任",
            "emotion": "gentle",
            "nickname": "昂晴",
        }
        prompt_block = memory_service.format_memory_prompt_block(memories, aff_info)

        assert "【角色长程记忆（关于玩家的事实与约定）】" in prompt_block
        assert "- 玩家称呼：昂晴" in prompt_block
        assert "- 玩家喜好：加双份糖的拿铁" in prompt_block
        assert "- 重要约定：这周末一起去买咖啡豆" in prompt_block
        assert "【当前关系与好感度】" in prompt_block
        assert "Lv.3 (友好/信任)" in prompt_block
        assert "gentle" in prompt_block


class TestSessionManagerPromptInjection:
    def test_session_manager_injects_memory_block(self, isolate_test_database):
        sm = SessionManager(db_path=isolate_test_database)
        memory_block = "【角色长程记忆】\n- 玩家称呼：昂晴"
        msgs = sm.format_llm_messages(
            character_name="四季夏目",
            history=[],
            new_user_prompt="你好夏目",
            memory_prompt_block=memory_block
        )

        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert "【角色长程记忆】" in msgs[0]["content"]
        assert "四季夏目" in msgs[0]["content"]
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "你好夏目"
