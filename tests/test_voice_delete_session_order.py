"""Deleting a voice unbinds its sessions without making them the most recent ones."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app


@pytest.mark.asyncio
async def test_deleting_a_voice_keeps_the_session_order(tmp_path, monkeypatch):
    db = tmp_path / "order.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db))
    await init_db(db)
    async with get_db(db) as conn:
        old = await crud.create_voice_profile(
            conn, VoiceProfileCreate(name="old", gpt_weights_path="o.ckpt", sovits_weights_path="o.pth")
        )
        current = await crud.create_voice_profile(
            conn, VoiceProfileCreate(name="current", gpt_weights_path="c.ckpt", sovits_weights_path="c.pth")
        )
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        await client.post(
            "/api/chat/sessions",
            json={"id": "s_old", "title": "old chat", "voice_profile_id": old.id,
                  "settings": {"voiceProfileId": old.id}},
        )
        async with get_db(db) as conn:
            await conn.execute("UPDATE sessions SET updated_at = '2026-01-01 00:00:00' WHERE id = 's_old';")
            await conn.commit()
        await client.post(
            "/api/chat/sessions",
            json={"id": "s_current", "title": "current chat", "voice_profile_id": current.id,
                  "settings": {"voiceProfileId": current.id}},
        )
        before = [s["id"] for s in (await client.get("/api/chat/sessions")).json()["sessions"]]
        assert before == ["s_current", "s_old"]

        await asyncio.sleep(1.1)  # CURRENT_TIMESTAMP has one-second resolution
        deleted = await client.delete(f"/api/voice/profiles/{old.id}")
        assert deleted.status_code == 200, deleted.text

        after = (await client.get("/api/chat/sessions")).json()["sessions"]
    assert [s["id"] for s in after] == before
    unbound = next(s for s in after if s["id"] == "s_old")
    assert unbound["voice_profile_id"] is None
    assert unbound["settings"].get("voiceProfileId") is None
