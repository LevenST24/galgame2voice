"""Stage moves (act.move) survive saving, reloading and a backup round trip."""

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database.models import MessageCreate, decode_lines
from galgame2voice.database.session import init_db
from galgame2voice.main import create_app


def test_saved_line_keeps_its_move():
    line = {"index": 0, "zh": "你好", "ja": "こんにちは", "emotion": "happy",
            "act": {"face": "smile", "move": "approach"},
            "audio": {"status": "skipped", "chunks": []}}
    msg = MessageCreate(session_id="s1", role="assistant", content_chinese="你好", lines=[line])
    assert msg.lines is not None
    assert msg.lines[0]["act"].get("move") == "approach", msg.lines[0]["act"]


def test_reloaded_line_keeps_its_move():
    raw = '[{"index":0,"zh":"a","ja":"b","emotion":"happy","act":{"move":"left"},"audio":{"status":"skipped","chunks":[]}}]'
    lines = decode_lines(raw)
    assert lines is not None and lines[0]["act"].get("move") == "left", lines


@pytest.mark.asyncio
async def test_backup_round_trip_keeps_moves_and_drops_unknown_ones(tmp_path, monkeypatch):
    db = tmp_path / "move.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db))
    await init_db(db)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8080") as client:
        r = await client.post("/api/chat/import", json={"sessions": [{
            "id": "s_import_move", "title": "t", "settings": {},
            "messages": [{"role": "assistant", "content": "你好", "lines": [
                {"index": 0, "zh": "你好", "ja": "こんにちは", "emotion": "happy",
                 "act": {"face": "smile", "move": "approach"}},
                {"index": 1, "zh": "再见", "ja": "さよなら", "emotion": "happy",
                 "act": {"face": "smile", "move": "jump"}}]}],
        }]})
        assert r.status_code == 200, r.text
        exported = (await client.get("/api/chat/export")).json()
        lines = exported["sessions"][0]["messages"][0]["lines"]
        assert lines[0]["act"] == {"face": "smile", "move": "approach"}, lines[0]["act"]
        assert lines[1]["act"] == {"face": "smile"}, lines[1]["act"]
