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
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(audio_dir=audio))
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
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(audio_dir=tmp_path))
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
