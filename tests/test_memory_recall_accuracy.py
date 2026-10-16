"""Long-term memory keeps the right facts: names, opposite preferences, old anchors and stages."""

from datetime import datetime, timezone
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.models import CharacterAffectionUpdate, UserMemoryCreate, UserMemoryResponse
from galgame2voice.database.session import get_db
from galgame2voice.services.memory_service import MemoryService


@pytest.fixture
def memory_service(isolate_test_database):
    conn = sqlite3.connect(isolate_test_database)
    conn.execute(
        "INSERT OR IGNORE INTO voice_profiles (id, name, gpt_weights_path, sovits_weights_path, is_default) "
        "VALUES (1, 'default_char', 'default.ckpt', 'default.pth', 1);"
    )
    conn.commit()
    conn.close()
    return MemoryService(db_path=isolate_test_database)


def _learned_name(service, text):
    facts = service.extract_facts_heuristic(text)
    return next((f["fact_value"] for f in facts if f["fact_key"] == "player_name"), None)


@pytest.mark.parametrize("text", [
    "我叫了个外卖", "我叫你别闹了。", "别叫我笨蛋！", "明天早上叫我起床", "你叫我干嘛？",
    "谁叫我是你哥呢", "你不可以叫我笨蛋", "以后别再叫我小鬼了", "再叫我一声哥哥", "叫我去买东西",
])
def test_everyday_phrases_are_not_taken_as_a_name(memory_service, text):
    assert _learned_name(memory_service, text) is None


@pytest.mark.parametrize("text,name", [
    ("我叫昂晴", "昂晴"), ("初次见面，我叫昂晴。", "昂晴"), ("以后你可以叫我昂晴店长。", "昂晴店长"),
    ("叫我翔太吧", "翔太"), ("以后叫我主人", "主人"), ("我叫做小明", "小明"),
    ("别叫我笨蛋，我叫昂晴。", "昂晴"),
])
def test_names_are_still_learned(memory_service, text, name):
    assert _learned_name(memory_service, text) == name


async def test_an_everyday_phrase_keeps_the_learned_nickname(memory_service, isolate_test_database):
    await memory_service.process_user_message("u1", 1, "我叫昂晴。")
    await memory_service.process_user_message("u1", 1, "我叫了个外卖，明天早上叫我起床")

    async with get_db(isolate_test_database) as conn:
        memories = await crud.list_memories(conn, user_id="u1", character_id=1)
        affection = await crud.get_character_affection(conn, user_id="u1", character_id=1)
    assert [m.fact_value for m in memories if m.fact_key == "player_name"] == ["昂晴"]
    assert affection.custom_nickname == "昂晴"


async def test_a_new_preference_replaces_the_opposite_one(memory_service, isolate_test_database):
    async def keys():
        async with get_db(isolate_test_database) as conn:
            return {m.fact_key for m in await crud.list_memories(conn, user_id="u1", character_id=1)}

    await memory_service.process_user_message("u1", 1, "我喜欢猫")
    await memory_service.process_user_message("u1", 1, "我讨厌猫")
    assert await keys() == {"dislike_猫"}

    await memory_service.process_user_message("u1", 1, "我最喜欢猫")
    assert await keys() == {"like_猫"}


async def test_the_name_is_recalled_behind_many_newer_memories(memory_service, isolate_test_database):
    async with get_db(isolate_test_database) as conn:
        await crud.upsert_memory(conn, UserMemoryCreate(
            user_id="u1", character_id=1, category="nickname", fact_key="player_name", fact_value="昂晴",
        ))
        await conn.execute("UPDATE user_memories SET updated_at = '2020-01-01 00:00:00';")
        await conn.executemany(
            "INSERT INTO user_memories (user_id, character_id, category, fact_key, fact_value, confidence) "
            "VALUES ('u1', 1, 'preference', ?, ?, 0.9);",
            [(f"like_{i}", f"爱好{i}") for i in range(210)],
        )
        await conn.commit()

    recalled = await memory_service.retrieve_relevant_memories("u1", 1, "今天做什么好呢", top_k=5)

    assert recalled[0].fact_key == "player_name"
    assert len(recalled) == 5


def test_a_memory_written_just_now_counts_as_recent(memory_service):
    now = datetime.now(timezone.utc)
    memory = UserMemoryResponse(
        id=1, user_id="u1", character_id=1, category="preference", fact_key="like_x", fact_value="x",
        confidence=1.0, updated_at=now.strftime("%Y-%m-%d %H:%M:%S"),
    )

    ((score, _),) = memory_service._score_candidate_memories([memory], "", now.timestamp())

    # Empty prompt, so no overlap: 0.3 * recency + 0.2 * confidence, with SQLite's UTC
    # timestamp read as UTC whatever the local time zone is.
    assert score == pytest.approx(0.5, abs=0.01)


def test_a_dislike_key_still_matches_its_topic(memory_service):
    # Only the key names the topic; the value shares one of its four characters.
    assert memory_service._calculate_overlap_score("你喜欢猫吗", "猫咪掉毛", "dislike_猫") == pytest.approx(0.8)


@pytest.mark.parametrize("score,stage", [
    (0, "阶段一"), (19, "阶段一"), (20, "阶段二"), (49, "阶段二"),
    (50, "阶段三"), (79, "阶段三"), (80, "阶段四"), (100, "阶段四"),
])
def test_the_stage_switches_with_the_level(memory_service, score, stage):
    block = memory_service.format_memory_prompt_block(
        None, {"score": score, "level": 1, "level_name": "x", "emotion": "normal"}
    )
    assert f"阶段行为准则：{stage}" in block


def test_a_level_without_a_score_maps_to_the_matching_stage(memory_service):
    block = memory_service.format_memory_prompt_block(
        None, {"level": 5, "level_name": "恋慕/誓约", "emotion": "normal"}
    )
    assert "阶段行为准则：阶段四" in block


async def test_level_up_follows_the_score_not_a_manually_written_level(memory_service, isolate_test_database):
    async with get_db(isolate_test_database) as conn:
        await crud.update_character_affection(conn, "u1", 1, CharacterAffectionUpdate(affection_score=50))
        await crud.update_character_affection(conn, "u1", 1, CharacterAffectionUpdate(affection_level=1))
        _, _, level_up = await crud.increment_affection(conn, "u1", 1, 1)
        assert level_up is False  # 50 -> 51 stays at Lv.3

        await crud.update_character_affection(conn, "u1", 1, CharacterAffectionUpdate(affection_score=75))
        await crud.update_character_affection(conn, "u1", 1, CharacterAffectionUpdate(affection_level=5))
        updated, gained, level_up = await crud.increment_affection(conn, "u1", 1, 5)
        assert gained == 5 and updated.affection_score == 80
        assert level_up is True  # 75 -> 80 reaches Lv.5


async def test_memory_api_rejects_values_that_sanitize_to_nothing(memory_service):
    from galgame2voice.main import create_app

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        created = await client.post(
            "/api/memory", json={"user_id": "u1", "character_id": 1, "fact_key": "k", "fact_value": "ok"}
        )
        assert created.status_code == 201

        res = await client.post(
            "/api/memory", json={"user_id": "u1", "character_id": 1, "fact_key": "k2", "fact_value": "[]"}
        )
        assert res.status_code == 422

        res = await client.put(f"/api/memory/{created.json()['id']}", json={"fact_value": "<>"})
        assert res.status_code == 422
