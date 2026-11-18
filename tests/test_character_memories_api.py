"""Integration tests for character memory list & delete endpoints and prompt reflection."""

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.models import UserMemoryCreate
from galgame2voice.database.session import get_db
from galgame2voice.main import create_app
from galgame2voice.services.chat_pipelines.context_builder import build_chat_context
from galgame2voice.services.memory_service import MemoryService
from galgame2voice.services.session_manager import SessionManager


@pytest.fixture
def app(isolate_test_database):
    conn = sqlite3.connect(isolate_test_database)
    conn.execute(
        "INSERT OR IGNORE INTO voice_profiles (id, name, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text, is_default) "
        "VALUES (1, '芳乃', 'yoshino.ckpt', 'yoshino.pth', 'yoshino.wav', 'テスト', 1);"
    )
    conn.execute(
        "INSERT OR IGNORE INTO voice_profiles (id, name, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text, is_default) "
        "VALUES (2, '夏目', 'natsume.ckpt', 'natsume.pth', 'natsume.wav', 'テスト', 0);"
    )
    conn.commit()
    conn.close()
    return create_app()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as c:
        yield c


@pytest.mark.asyncio
class TestCharacterMemoriesApi:
    async def test_list_only_returns_target_character_memories_in_desc_order(self, client, isolate_test_database):
        async with get_db(isolate_test_database) as conn:
            await crud.upsert_session(conn, session_id="s_yoshino", voice_profile_id=1)
            await crud.upsert_session(conn, session_id="s_natsume", voice_profile_id=2)

            m_old = await crud.create_memory(conn, UserMemoryCreate(
                user_id="default_user",
                character_id=1,
                category="preference",
                fact_key="like_红茶",
                fact_value="红茶",
            ))
            m_new = await crud.create_memory(conn, UserMemoryCreate(
                user_id="default_user",
                character_id=1,
                category="taboo",
                fact_key="dislike_香菜",
                fact_value="香菜",
            ))
            await crud.create_memory(conn, UserMemoryCreate(
                user_id="default_user",
                character_id=2,
                category="preference",
                fact_key="like_抹茶",
                fact_value="抹茶",
            ))
            await conn.execute(
                "UPDATE user_memories SET updated_at = '2026-11-18 09:00:00' WHERE id = ?;",
                (m_old.id,),
            )
            await conn.execute(
                "UPDATE user_memories SET updated_at = '2026-11-18 10:00:00' WHERE id = ?;",
                (m_new.id,),
            )
            await conn.commit()

        res = await client.get("/api/characters/memories", params={"session_id": "s_yoshino"})
        assert res.status_code == 200
        data = res.json()
        assert data["character_id"] == 1
        assert data["character_name"] == "芳乃"
        assert [m["fact_value"] for m in data["memories"]] == ["香菜", "红茶"]

    async def test_delete_memory_removes_fact_from_context_prompt(self, client, isolate_test_database):
        ms = MemoryService(db_path=isolate_test_database)
        sm = SessionManager(db_path=isolate_test_database)

        async with get_db(isolate_test_database) as conn:
            await crud.upsert_session(conn, session_id="s_tea", voice_profile_id=1)
        await ms.process_user_message("default_user", 1, "我喜欢红茶，我讨厌香菜")

        res_list = await client.get("/api/characters/memories", params={"session_id": "s_tea"})
        assert res_list.status_code == 200
        tea_mem = next(m for m in res_list.json()["memories"] if m["fact_value"] == "红茶")

        async with get_db(isolate_test_database) as conn:
            before_msgs = await build_chat_context(
                conn=conn,
                session_id="s_tea",
                user_prompt="喝什么好呢",
                session_manager=sm,
                memory_service=ms,
            )
        assert "红茶" in before_msgs[0].content

        res_del = await client.delete(
            f"/api/characters/1/memories/{tea_mem['id']}",
            params={"session_id": "s_tea"},
        )
        assert res_del.status_code == 200

        async with get_db(isolate_test_database) as conn:
            after_msgs = await build_chat_context(
                conn=conn,
                session_id="s_tea",
                user_prompt="喝什么好呢",
                session_manager=sm,
                memory_service=ms,
            )
        assert "红茶" not in after_msgs[0].content
        assert "香菜" in after_msgs[0].content

    async def test_delete_other_character_or_missing_memory_returns_404(self, client, isolate_test_database):
        async with get_db(isolate_test_database) as conn:
            m_other = await crud.create_memory(conn, UserMemoryCreate(
                user_id="default_user",
                character_id=2,
                category="preference",
                fact_key="like_抹茶",
                fact_value="抹茶",
            ))

        res_wrong_char = await client.delete(f"/api/characters/1/memories/{m_other.id}")
        assert res_wrong_char.status_code == 404

        res_missing = await client.delete("/api/characters/1/memories/999999")
        assert res_missing.status_code == 404

        async with get_db(isolate_test_database) as conn:
            still_there = await crud.get_memory(conn, m_other.id)
        assert still_there is not None
        assert still_there.fact_value == "抹茶"

    async def test_delete_nickname_clears_matching_custom_nickname_and_prompt(self, client, isolate_test_database):
        ms = MemoryService(db_path=isolate_test_database)
        sm = SessionManager(db_path=isolate_test_database)

        async with get_db(isolate_test_database) as conn:
            await crud.upsert_session(conn, session_id="s_nick", voice_profile_id=1)
        await ms.process_user_message("default_user", 1, "你好，我叫小路。")

        res_list = await client.get("/api/characters/memories", params={"session_id": "s_nick"})
        assert res_list.status_code == 200
        nick_mem = next(m for m in res_list.json()["memories"] if m["category"] == "nickname")
        assert nick_mem["fact_value"] == "小路"

        async with get_db(isolate_test_database) as conn:
            aff_before = await crud.get_character_affection(conn, user_id="default_user", character_id=1)
            assert aff_before is not None
            assert aff_before.custom_nickname == "小路"
            before_msgs = await build_chat_context(
                conn=conn,
                session_id="s_nick",
                user_prompt="早安",
                session_manager=sm,
                memory_service=ms,
            )
        assert "小路" in before_msgs[0].content

        res_del = await client.delete(
            f"/api/characters/1/memories/{nick_mem['id']}",
            params={"session_id": "s_nick"},
        )
        assert res_del.status_code == 200

        async with get_db(isolate_test_database) as conn:
            aff_after = await crud.get_character_affection(conn, user_id="default_user", character_id=1)
            assert aff_after is not None
            assert aff_after.custom_nickname is None
            after_msgs = await build_chat_context(
                conn=conn,
                session_id="s_nick",
                user_prompt="早安",
                session_manager=sm,
                memory_service=ms,
            )
        assert "小路" not in after_msgs[0].content
        assert "称呼玩家为" not in after_msgs[0].content
