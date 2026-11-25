"""Within one message, a later statement about the same thing replaces the earlier one,
so the character remembers what the player said last."""
import sqlite3

import pytest

from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.services.memory_service import MemoryService


@pytest.fixture
def service(isolate_test_database):
    conn = sqlite3.connect(isolate_test_database)
    conn.execute("INSERT OR IGNORE INTO voice_profiles (id, name, ref_audio_path, prompt_text) VALUES (1, '夏目', 'a.wav', 't')")
    conn.commit()
    conn.close()
    return MemoryService(db_path=isolate_test_database)


async def _memories(db):
    async with get_db(db) as conn:
        rows = await crud.list_character_memories(conn, "default_user", 1)
    return sorted((m.fact_key, m.fact_value) for m in rows)


async def test_later_like_replaces_earlier_dislike(service, isolate_test_database):
    await service.process_user_message("default_user", 1, "以前我讨厌香菜，现在我喜欢香菜。")
    assert await _memories(isolate_test_database) == [("like_香菜", "香菜")]


async def test_later_dislike_replaces_earlier_like(service, isolate_test_database):
    await service.process_user_message("default_user", 1, "以前我喜欢香菜，现在我讨厌香菜。")
    assert await _memories(isolate_test_database) == [("dislike_香菜", "香菜")]


async def test_corrected_name_wins(service, isolate_test_database):
    await service.process_user_message("default_user", 1, "我叫小明，不对不对，叫我阿明就好。")
    assert await _memories(isolate_test_database) == [("player_name", "阿明")]


def test_rejected_nickname_is_still_skipped(service):
    facts = service.extract_facts_heuristic("别叫我笨蛋，我叫昂晴。")
    assert [(f["fact_key"], f["fact_value"]) for f in facts] == [("player_name", "昂晴")]


def test_two_different_likes_are_both_kept(service):
    facts = service.extract_facts_heuristic("我喜欢猫，我最喜欢的还有狗。")
    keys = [f["fact_key"] for f in facts]
    assert "like_猫" in keys
    assert len(keys) == 2


def test_facts_follow_the_order_they_were_said(service):
    facts = service.extract_facts_heuristic("我讨厌下雨天。我喜欢晴天。我叫阿明。")
    assert [f["fact_key"] for f in facts] == ["dislike_下雨天", "like_晴天", "player_name"]


def test_one_pattern_is_capped(service):
    text = "，".join(f"我喜欢东西{i}" for i in range(20))
    assert len(service.extract_facts_heuristic(text)) == 5
