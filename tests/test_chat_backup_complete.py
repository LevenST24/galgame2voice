"""Portable backups must cover unloaded history and remain safe to restore."""
import json
import sqlite3
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, init_db, immediate_transaction
from galgame2voice.main import create_app


@pytest.mark.asyncio
async def test_complete_backup_restores_unloaded_history_and_original_dates(tmp_path, monkeypatch):
    db = tmp_path / "backup.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db))
    await init_db(db)
    async with get_db(db) as conn, immediate_transaction(conn):
        await conn.executemany(
            "INSERT INTO sessions(id, title, settings_json) VALUES (?, ?, ?)",
            [(f"s_{index:03}", f"对话 {index}", json.dumps({"temperature": 0.8, "apiKey": "secret-value"})) for index in range(205)],
        )
        await conn.executemany(
            "INSERT INTO messages(session_id, role, content_chinese, content_japanese, created_at) VALUES (?, ?, ?, ?, ?)",
            [("s_000", "user", f"消息 {index}", "日本語", "2024-03-02 01:02:03.456") for index in range(601)],
        )
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        pages = [(await client.get(f"/api/chat/sessions?limit=200&offset={offset}")).json() for offset in (0, 200)]
        assert pages[0]["has_more"] is True and pages[1]["has_more"] is False
        assert len({s["id"] for page in pages for s in page["sessions"]}) == 205
        result = await client.get("/api/chat/export")
        assert result.status_code == 200
        backup = result.json()
        assert len(backup["sessions"]) == 205
        assert "secret-value" not in result.text
        session = next(s for s in backup["sessions"] if s["id"] == "s_000")
        assert len(session["messages"]) == 601
        assert session["messages"][0]["content"] == "消息 0"
        assert session["messages"][-1]["content"] == "消息 600"
        restored = await client.post("/api/chat/import", json={"sessions": [{
            **session, "id": "s_import_restored", "created_at": session["createdAt"], "updated_at": session["updatedAt"],
        }]})
        assert restored.status_code == 200, restored.text
        again = (await client.get("/api/chat/export")).json()
        copy = next(s for s in again["sessions"] if s["id"] == "s_import_restored")
        assert copy["createdAt"] == session["createdAt"]
        assert copy["updatedAt"] == session["updatedAt"]
        assert [(m["content"], m["japanese"], m["ts"]) for m in copy["messages"]] == [
            (m["content"], m["japanese"], m["ts"]) for m in session["messages"]
        ]


@pytest.mark.asyncio
async def test_oversize_backup_fails_explicitly_instead_of_truncating(tmp_path, monkeypatch):
    from galgame2voice.services import chat_backup
    db = tmp_path / "size.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db))
    monkeypatch.setattr(chat_backup, "MAX_MESSAGES", 1)
    await init_db(db)
    async with get_db(db) as conn:
        for text in ("保留第一句", "保留第二句"):
            await crud.add_message(conn, MessageCreate(session_id="s_full", role="user", content_chinese=text))
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        response = await client.get("/api/chat/export")
        assert response.status_code == 413
        assert "data" in response.json()["detail"]
        assert (await client.get("/api/chat/history?session_id=s_full")).json()["count"] == 2


@pytest.mark.asyncio
async def test_reply_format_setting_survives_export_and_import(tmp_path, monkeypatch):
    from galgame2voice.services.chat_backup import SESSION_SETTINGS

    assert "advReplyFormat" in SESSION_SETTINGS
    db = tmp_path / "reply-format.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db))
    await init_db(db)
    async with get_db(db) as conn, immediate_transaction(conn):
        await conn.execute(
            "INSERT INTO sessions(id, title, settings_json) VALUES (?, ?, ?)",
            ("s_format", "逐句", json.dumps({"advReplyFormat": "lines", "temperature": 0.8, "apiKey": "secret"})),
        )
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        backup = (await client.get("/api/chat/export")).json()
        session = next(s for s in backup["sessions"] if s["id"] == "s_format")
        assert session["settings"]["advReplyFormat"] == "lines"
        assert "apiKey" not in session["settings"]
        restored = await client.post("/api/chat/import", json={"sessions": [{
            **session, "id": "s_import_format",
            "created_at": session["createdAt"], "updated_at": session["updatedAt"],
        }]})
        assert restored.status_code == 200, restored.text
        again = (await client.get("/api/chat/export")).json()
        copy = next(s for s in again["sessions"] if s["id"] == "s_import_format")
        assert copy["settings"]["advReplyFormat"] == "lines"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete_session", "clear_session_messages"])
async def test_removing_chat_preserves_shared_cache_reference_and_outside_files(tmp_path, monkeypatch, operation):
    from galgame2voice import config
    db = tmp_path / "shared.db"
    audio = tmp_path / "audio"
    (audio / "cache").mkdir(parents=True)
    (audio / "references").mkdir()
    outside = tmp_path / "audio-other"
    outside.mkdir()
    paths = [audio / "cache" / "shared.wav", audio / "references" / "voice.wav", outside / "keep.wav"]
    for path in paths:
        path.write_bytes(b"keep this sound")
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(audio_dir=audio, data_dir=tmp_path / "data"))
    await init_db(db)
    async with get_db(db) as conn:
        for url in ("/audio/cache/shared.wav", "/audio/references/voice.wav", "/audio/../audio-other/keep.wav"):
            await crud.add_message(conn, MessageCreate(session_id="s_remove", role="assistant", content_chinese="声音", audio_url=url))
        await crud.add_message(conn, MessageCreate(session_id="s_keep", role="assistant", content_chinese="共用", audio_url="/audio/cache/shared.wav"))
        assert await getattr(crud, operation)(conn, "s_remove")
        assert len(await crud.get_recent_messages(conn, session_id="s_keep")) == 1
    assert all(path.read_bytes() == b"keep this sound" for path in paths)


@pytest.mark.asyncio
async def test_failed_delete_keeps_both_history_and_audio(tmp_path, monkeypatch):
    from galgame2voice import config
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(audio_dir=tmp_path, data_dir=tmp_path / "data"))
    audio = tmp_path / "reply.wav"
    audio.write_bytes(b"original")
    db = tmp_path / "failed.db"
    await init_db(db)
    async with get_db(db) as conn:
        await crud.add_message(conn, MessageCreate(session_id="s_keep", role="assistant", content_chinese="原记录", audio_url="/audio/reply.wav"))
        await conn.execute("CREATE TRIGGER prevent_delete BEFORE DELETE ON sessions BEGIN SELECT RAISE(ABORT, 'failure'); END")
        await conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            await crud.delete_session(conn, "s_keep")
        assert len(await crud.get_recent_messages(conn, session_id="s_keep")) == 1
        assert audio.read_bytes() == b"original"

@pytest.mark.asyncio
async def test_imported_lines_round_trip_through_history_and_export(tmp_path, monkeypatch):
    db = tmp_path / "import-lines.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db))
    await init_db(db)
    lines = [{"index": 0, "zh": "你好", "ja": "こんにちは", "emotion": "happy",
              "act": {"face": "smile"}, "audio": {"status": "skipped", "chunks": []}}]
    payload = {"sessions": [{"id": "s_import_lines", "title": "逐句", "messages": [
        {"role": "assistant", "content": "你好", "japanese": "こんにちは", "lines": lines}
    ]}]}
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        response = await client.post("/api/chat/import", json=payload)
        assert response.status_code == 200, response.text
        history = (await client.get("/api/chat/history?session_id=s_import_lines")).json()
        assert history["messages"][0]["lines"] == lines
        backup = (await client.get("/api/chat/export")).json()
        assert backup["sessions"][0]["messages"][0]["lines"] == lines


@pytest.mark.asyncio
async def test_import_sanitizes_invalid_act_values_without_dropping_lines(tmp_path, monkeypatch):
    db = tmp_path / "import-invalid-act.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db))
    await init_db(db)
    imported_lines = [
        {
            "index": 0,
            "zh": "早上好。",
            "ja": "おはよう。",
            "emotion": "happy",
            "act": {
                "face": "smile",
                "pose": "stand",
                "motion": "wave",
                "blush": True,
                "fx": "sparkle",
                "extra": "ignored",
            },
            "audio": {"status": "skipped", "chunks": []},
        },
        {
            "index": 1,
            "zh": "今天天气真好。",
            "ja": "いい天気だね。",
            "emotion": "gentle",
            "act": {"face": None, "motion": ""},
            "audio": {"status": "skipped", "chunks": []},
        },
        {
            "index": 2,
            "zh": "我们出发吧。",
            "ja": "出かけよう。",
            "emotion": "cool",
            "act": {
                "face": "",
                "pose": 42,
                "motion": "nod",
                "blush": False,
                "fx": "unknown_fx",
            },
            "audio": {"status": "skipped", "chunks": []},
        },
        {
            "index": 3,
            "zh": "请多指教。",
            "ja": "よろしくね。",
            "emotion": "shy",
            "act": {"face": 123, "pose": None, "motion": None, "blush": 0, "fx": ""},
            "audio": {"status": "skipped", "chunks": []},
        },
        {
            "index": 4,
            "zh": "走吧。",
            "ja": "行こう。",
            "emotion": "happy",
            "act": {"face": "wink", "pose": "", "motion": [], "blush": None, "fx": None},
            "audio": {"status": "skipped", "chunks": []},
        },
    ]
    expected_lines = [
        {
            "index": 0,
            "zh": "早上好。",
            "ja": "おはよう。",
            "emotion": "happy",
            "act": {"face": "smile", "pose": "stand", "blush": True, "fx": "sparkle"},
            "audio": {"status": "skipped", "chunks": []},
        },
        {
            "index": 1,
            "zh": "今天天气真好。",
            "ja": "いい天気だね。",
            "emotion": "gentle",
            "act": {},
            "audio": {"status": "skipped", "chunks": []},
        },
        {
            "index": 2,
            "zh": "我们出发吧。",
            "ja": "出かけよう。",
            "emotion": "cool",
            "act": {"motion": "nod", "blush": False},
            "audio": {"status": "skipped", "chunks": []},
        },
        {
            "index": 3,
            "zh": "请多指教。",
            "ja": "よろしくね。",
            "emotion": "shy",
            "act": {},
            "audio": {"status": "skipped", "chunks": []},
        },
        {
            "index": 4,
            "zh": "走吧。",
            "ja": "行こう。",
            "emotion": "happy",
            "act": {"face": "wink"},
            "audio": {"status": "skipped", "chunks": []},
        },
    ]
    payload = {
        "sessions": [
            {
                "id": "s_import_invalid_act",
                "title": "非标准演出导入",
                "messages": [
                    {"role": "user", "content": "你好"},
                    {
                        "role": "assistant",
                        "content": "早上好。今天天气真好。我们出发吧。请多指教。走吧。",
                        "japanese": "おはよう。いい天気だね。出かけよう。よろしくね。行こう。",
                        "lines": imported_lines,
                    },
                ],
            }
        ]
    }
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        response = await client.post("/api/chat/import", json=payload)
        assert response.status_code == 200, response.text
        history = (await client.get("/api/chat/history", params={"session_id": "s_import_invalid_act"})).json()
        assert history["messages"][1]["lines"] == expected_lines
        backup = (await client.get("/api/chat/export")).json()
        session = next(s for s in backup["sessions"] if s["id"] == "s_import_invalid_act")
        assert session["messages"][1]["lines"] == expected_lines
