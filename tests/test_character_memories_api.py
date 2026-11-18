"""The memory tab's list and delete endpoints, checked against the prompt chat actually builds."""

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.main import create_app
from galgame2voice.services.chat_pipelines.context_builder import build_chat_context
from galgame2voice.services.memory_service import MemoryService
from galgame2voice.services.session_manager import SessionManager


@pytest.fixture
async def client(isolate_test_database):
    conn = sqlite3.connect(isolate_test_database)
    for pid, name in ((1, "芳乃"), (2, "夏目")):
        conn.execute(
            "INSERT OR IGNORE INTO voice_profiles (id, name, gpt_weights_path, sovits_weights_path, ref_audio_path,"
            " prompt_text, is_default) VALUES (?, ?, 'a.ckpt', 'a.pth', 'a.wav', 'テスト', ?);",
            (pid, name, int(pid == 1)),
        )
    conn.commit()
    conn.close()
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8080") as c:
        yield c


async def _say(db, text, character_id=1, session_id="s1"):
    """The player says `text` in a session bound to `character_id`; memories are stored the way chat stores them."""
    async with get_db(db) as conn:
        await crud.upsert_session(conn, session_id=session_id, voice_profile_id=character_id)
    await MemoryService(db_path=db).process_user_message("default_user", character_id, text)


async def _listed(client, **params):
    res = await client.get("/api/characters/memories", params={"session_id": "s1", **params})
    assert res.status_code == 200
    return res.json()


async def _system_prompt(db, user_prompt):
    async with get_db(db) as conn:
        messages = await build_chat_context(
            conn=conn, session_id="s1", user_prompt=user_prompt,
            session_manager=SessionManager(db_path=db), memory_service=MemoryService(db_path=db),
        )
    return messages[0].content


async def test_list_shows_the_chat_characters_memories_newest_first(client, isolate_test_database):
    await _say(isolate_test_database, "我喜欢红茶，我讨厌香菜")
    await _say(isolate_test_database, "我喜欢抹茶", character_id=2, session_id="s2")
    data = await _listed(client)
    assert (data["character_id"], data["character_name"]) == (1, "芳乃")
    assert [(m["category"], m["fact_value"]) for m in data["memories"]] == [("taboo", "香菜"), ("preference", "红茶")]
    # The browser re-bound s1 to 夏目. Chat sends that binding with the next message and prefers it over the stored
    # session, so the list follows it too.
    assert [m["fact_value"] for m in (await _listed(client, character_id=2))["memories"]] == ["抹茶"]


async def test_deleted_memory_leaves_the_prompt(client, isolate_test_database):
    await _say(isolate_test_database, "我喜欢红茶，我讨厌香菜")
    tea = next(m for m in (await _listed(client))["memories"] if m["fact_value"] == "红茶")
    assert "红茶" in await _system_prompt(isolate_test_database, "喝什么好呢")
    res = await client.delete(f"/api/characters/1/memories/{tea['id']}", params={"session_id": "s1"})
    assert res.status_code == 200
    prompt = await _system_prompt(isolate_test_database, "喝什么好呢")
    assert "红茶" not in prompt and "香菜" in prompt


async def test_delete_refuses_another_characters_memory(client, isolate_test_database):
    await _say(isolate_test_database, "我喜欢抹茶", character_id=2, session_id="s2")
    async with get_db(isolate_test_database) as conn:
        [matcha] = await crud.list_character_memories(conn, "default_user", 2)
    assert (await client.delete(f"/api/characters/1/memories/{matcha.id}")).status_code == 404
    assert (await client.delete("/api/characters/1/memories/999999")).status_code == 404
    async with get_db(isolate_test_database) as conn:
        assert (await crud.get_memory(conn, matcha.id)).fact_value == "抹茶"


async def test_deleting_the_name_stops_the_character_using_it(client, isolate_test_database):
    await _say(isolate_test_database, "你好，我叫小路。")
    [name] = (await _listed(client))["memories"]
    assert (name["category"], name["fact_value"]) == ("nickname", "小路")
    assert "小路" in await _system_prompt(isolate_test_database, "早安")
    res = await client.delete(f"/api/characters/1/memories/{name['id']}", params={"session_id": "s1"})
    assert res.status_code == 200
    async with get_db(isolate_test_database) as conn:
        affection = await crud.get_character_affection(conn, user_id="default_user", character_id=1)
    assert affection.custom_nickname is None
    prompt = await _system_prompt(isolate_test_database, "早安")
    assert "小路" not in prompt and "称呼玩家为" not in prompt
