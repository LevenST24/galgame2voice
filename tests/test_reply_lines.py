"""Legacy reply line alignment and portable persistence contracts."""

import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate, decode_lines
from galgame2voice.database.migrations import CURRENT_SCHEMA_VERSION
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.services.chat_pipelines.line_builder import (
    MAX_LINE_LENGTH,
    LineAligner,
    build_lines,
    split_chinese_sentences,
)
from galgame2voice.services.chat_pipelines.stream_coordinator import StreamCoordinator
from galgame2voice.services.session_manager import SessionManager
from galgame2voice.utils.text_splitter import split_japanese_sentences


@pytest.mark.parametrize(("zh", "ja", "pairs"), [
    ("你好。再见！", "こんにちは。さようなら！", [("你好。", "こんにちは。"), ("再见！", "さようなら！")]),
    ("甲。乙。丙。丁。", "ああ。いい。", [("甲。", "ああ。"), ("乙。丙。丁。", "いい。")]),
    ("甲甲。乙乙。", "あ。い。う。え。", [("甲甲。", "あ。"), ("乙乙。", "い。う。え。")]),
    ("沉默……", "……", [("沉默……", "……")]),
    ("她说「你好！『等等？』再见。」然后离开。真的？", "彼女が話した。そう？",
     [("她说「你好！『等等？』再见。」然后离开。", "彼女が話した。"), ("真的？", "そう？")]),
    ("你好😀！走吧🚀。", "やあ😀！行こう🚀。", [("你好😀！", "やあ😀！"), ("走吧🚀。", "行こう🚀。")]),
    ("第一段。\n第二段。", "段落一。\n段落二。", [("第一段。", "段落一。"), ("第二段。", "段落二。")]),
    ("", "", []),
    ("只剩中文。再一句。", "", [("只剩中文。", ""), ("再一句。", "")]),
    ("", "一。二。", [("", "一。"), ("", "二。")]),
])
def test_builder(zh, ja, pairs):
    lines = build_lines(zh, ja, "gentle")
    assert [(line["zh"], line["ja"]) for line in lines] == pairs
    assert [line["index"] for line in lines] == list(range(len(lines)))
    assert all(line["act"] == {} and line["emotion"] for line in lines)
    assert lines == build_lines(zh, ja, "gentle")


def test_chinese_quotes_newlines_and_prosody():
    assert split_chinese_sentences("开头……——继续。\n「一！\n二？」结束！\n尾巴") == [
        "开头……——继续。", "「一！\n二？」结束！", "尾巴",
    ]


def test_online_equals_offline_property():
    cases = [
        ("你好。再见！", "こんにちは。さようなら！"),
        ("甲。乙。丙。丁。", "ああ。いい。"),
        ("甲甲。乙乙。", "あ。い。う。え。"),
        ("开头……——继续。\n「一！\n二？」结束！\n尾巴", "始まり。そして「引用。」終わり。"),
        ("只剩中文。再一句。", ""),
        ("", "一。二。三。"),
        ("", ""),
    ]
    for zh, ja in cases:
        offline = build_lines(zh, ja, "gentle")
        aligner = LineAligner(default_emotion="gentle")
        aligner.set_chinese(zh)
        online_lines = []
        for i in range(1, len(ja) + 1):
            online_lines.extend(aligner.feed_japanese(ja[:i], final=(i == len(ja))))
        if not ja:
            online_lines.extend(aligner.feed_japanese("", final=True))
        assert online_lines == offline


def test_japanese_sentence_splitter_prefix_stability():
    text = "吾輩は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。何でも薄暗いじめじめした所でニャーニャー泣いていた事だけは記憶している。"
    prev_stable: list[str] = []
    for i in range(1, len(text) + 1):
        sub = text[:i]
        curr = split_japanese_sentences(sub, is_first_chunk=False)
        if len(curr) > 1:
            stable = curr[:-1]
            assert stable[:len(prev_stable)] == prev_stable
            prev_stable = stable


def test_chunk_attribution_with_early_comma_and_whitespace():
    ja = "こんにちは、先生、今日は晴れですね。\n\nさようなら。"
    cursor = 0
    s0, cursor = StreamCoordinator._find_containing_sentence_idx(ja, "こんにちは、", cursor)
    assert s0 == 0
    s1, cursor = StreamCoordinator._find_containing_sentence_idx(ja, "先生、今日は晴れですね。", cursor)
    assert s1 == 0
    s2, cursor = StreamCoordinator._find_containing_sentence_idx(ja, "さようなら。", cursor)
    assert s2 == 1


@pytest.mark.asyncio
async def test_migration_adds_nullable_column_without_version_bump(tmp_path):
    database = tmp_path / "legacy.db"
    await init_db(database)
    with sqlite3.connect(database) as conn:
        conn.execute("ALTER TABLE messages DROP COLUMN lines_json")
        conn.execute("INSERT INTO sessions(id) VALUES ('old')")
        conn.execute("INSERT INTO messages(session_id, role, content_chinese) VALUES ('old', 'assistant', '旧消息')")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    await init_db(database)
    async with get_db(database) as conn:
        columns = await (await conn.execute("PRAGMA table_info(messages)")).fetchall()
        assert "lines_json" in [row[1] for row in columns]
        assert (await (await conn.execute("PRAGMA user_version")).fetchone())[0] == version == CURRENT_SCHEMA_VERSION
        assert (await crud.get_recent_messages(conn, "old"))[0].lines is None


@pytest.mark.asyncio
async def test_lines_roundtrip_crud_history_backup_import(isolate_test_database):
    await init_db()
    lines = build_lines("你好！再见。", "こんにちは！さようなら。", "happy", with_audio=True)
    lines[0]["audio"] = {"status": "ready", "chunks": [{
        "index": 0, "line": 0, "audio_url": "/audio/cached.wav", "sentence": lines[0]["ja"],
    }]}
    portable_lines = [{**line, "audio": {"status": "skipped", "chunks": []}} for line in lines]
    async with get_db() as conn:
        created = await crud.add_message(conn, MessageCreate(
            session_id="original", role="assistant", content_chinese="你好！再见。",
            content_japanese="こんにちは！さようなら。", lines=lines,
        ))
        assert created.lines == lines
        raw = await (await conn.execute("SELECT lines_json FROM messages WHERE id=?", (created.id,))).fetchone()
        assert json.loads(raw[0]) == lines
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        history = await client.get("/api/chat/history", params={"session_id": "original"})
        assert history.status_code == 200
        assert history.json()["messages"][0]["lines"] == lines
        backup = await client.get("/api/chat/export")
        assert backup.status_code == 200
        session = next(s for s in backup.json()["sessions"] if s["id"] == "original")
        assert session["messages"][0]["lines"] == portable_lines
        session["id"] = "s_import_lines"
        imported = await client.post("/api/chat/import", json={"sessions": [session]})
        assert imported.status_code == 200, imported.text
        restored = await client.get("/api/chat/history", params={"session_id": session["id"]})
        assert restored.json()["messages"][0]["lines"] == portable_lines


@pytest.mark.asyncio
async def test_malformed_lines_json_tolerated(tmp_path):
    database = tmp_path / "malformed.db"
    await init_db(database)
    async with get_db(database) as conn:
        await conn.execute("INSERT INTO sessions(id) VALUES ('bad')")
        await conn.execute(
            "INSERT INTO messages(session_id, role, content_chinese, lines_json) VALUES ('bad', 'assistant', '测试', '{not-json}')"
        )
        await conn.execute(
            "INSERT INTO messages(session_id, role, content_chinese, lines_json) VALUES ('bad', 'assistant', '测试2', '\"not-a-list\"')"
        )
        await conn.commit()
        messages = await crud.get_recent_messages(conn, "bad")
        assert len(messages) == 2
        assert messages[0].lines is None
        assert messages[1].lines is None


@pytest.mark.asyncio
@pytest.mark.parametrize("malformation", [
    {"act": None}, {"act": {"blush": "yes"}}, {"audio": []},
    {"audio": {"status": "ready", "chunks": [None]}},
    {"audio": {"status": "ready", "chunks": [{"index": 0}]}},
    {"audio": {"status": "unknown", "chunks": []}},
    {"index": "0"}, {"zh": None}, {"emotion": []}, {"missing": "ja"},
])
async def test_nested_malformed_lines_are_null_in_history_and_backup(isolate_test_database, malformation):
    from galgame2voice.database.models import decode_lines
    from galgame2voice.services.chat_backup import export_chat_backup

    await init_db()
    good = build_lines("你好！", "こんにちは！", "happy", with_audio=True)[0]
    bad = {**good, **malformation}
    if "missing" in bad:
        bad.pop(bad.pop("missing"))
    raw_lines = [good, bad]
    async with get_db() as conn:
        await crud.add_message(conn, MessageCreate(session_id="nested_bad", role="assistant", content_chinese="测试"))
        await conn.execute("UPDATE messages SET lines_json=? WHERE session_id='nested_bad'", (json.dumps(raw_lines),))
        await conn.commit()
    # Export first: act=null previously raised dict(None) and broke the backup.
    backup = await export_chat_backup()
    session = next(s for s in backup["sessions"] if s["id"] == "nested_bad")
    assert session["messages"][0]["lines"] is None
    async with get_db() as conn:
        assert (await crud.get_recent_messages(conn, "nested_bad"))[0].lines is None
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        history = await client.get("/api/chat/history", params={"session_id": "nested_bad"})
        assert history.status_code == 200
        assert history.json()["messages"][0]["lines"] is None
    assert decode_lines(raw_lines) is None
    assert decode_lines(json.dumps(raw_lines)) is None


@pytest.mark.asyncio
async def test_session_manager_stores_assistant_lines(tmp_path):
    database = tmp_path / "session.db"
    await init_db(database)
    manager = SessionManager(db_path=database)
    result = await manager.add_turn("manager", "assistant", "你好！", "こんにちは！")
    history = await manager.get_history("manager")
    assert result.lines == history[0].lines == build_lines("你好！", "こんにちは！", "gentle", with_audio=True)


def test_overlong_sentences_split_by_punct_comma_or_hard_limit():
    unclosed_quote_zh = f"「早安。{'哈' * 6000}，{'嘿' * 5000}。"
    zh_parts = split_chinese_sentences(unclosed_quote_zh)
    assert zh_parts == ["「早安。", f"{'哈' * 6000}，", f"{'嘿' * 5000}。"]
    assert "".join(zh_parts) == unclosed_quote_zh

    comma_ja = f"{'あ' * 6000}、{'い' * 5000}。"
    comma_lines = build_lines("早安。长句。", f"おはよう。{comma_ja}", "gentle", with_audio=True)
    assert [(line["zh"], line["ja"]) for line in comma_lines] == [
        ("早安。", "おはよう。"),
        ("长句。", f"{'あ' * 6000}、"),
        ("", f"{'い' * 5000}。"),
    ]
    assert decode_lines(comma_lines) == comma_lines

    hard_ja = f"{'あ' * 10005}。"
    hard_lines = build_lines("早安。长句。", f"おはよう。{hard_ja}", "gentle", with_audio=True)
    assert [(line["zh"], line["ja"]) for line in hard_lines] == [
        ("早安。", "おはよう。"),
        ("长句。", "あ" * MAX_LINE_LENGTH),
        ("", "あ" * 5 + "。"),
    ]
    assert decode_lines(hard_lines) == hard_lines

    exact_lines = build_lines("哈" * MAX_LINE_LENGTH, "あ" * MAX_LINE_LENGTH, "gentle", with_audio=True)
    assert len(exact_lines) == 1
    assert decode_lines(exact_lines) == exact_lines


def test_overlong_tail_merge_and_online_offline_equivalence():
    tail_ja = "".join(["あ" * 3000 + "。" for _ in range(4)])
    tail_lines = build_lines("总结。", tail_ja, "gentle", with_audio=True)
    assert [(line["zh"], line["ja"]) for line in tail_lines] == [
        ("总结。", ("あ" * 3000 + "。") * 3),
        ("", "あ" * 3000 + "。"),
    ]
    assert decode_lines(tail_lines) == tail_lines

    zh = "长句。早安。"
    ja = f"{'あ' * 10005}。おはよう。"
    offline = build_lines(zh, ja, "gentle")
    aligner = LineAligner(default_emotion="gentle")
    aligner.set_chinese(zh)
    online = []
    pos = 0
    while pos < len(ja):
        pos = min(len(ja), pos + 97)
        online.extend(aligner.feed_japanese(ja[:pos], final=(pos == len(ja))))
    assert online == offline
    assert online[-1]["index"] == 2
    assert online[-1]["zh"] == "早安。" and online[-1]["ja"] == "おはよう。"
