"""Tests for /api/characters list and switch endpoints."""

import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.main import create_app


@pytest.fixture
def app(isolate_test_database):
    conn = sqlite3.connect(isolate_test_database)
    conn.execute(
        "INSERT OR IGNORE INTO voice_profiles (id, name, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text, is_default) "
        "VALUES (1, 'default_char', 'default.ckpt', 'default.pth', 'default.wav', 'テスト', 1);"
    )
    conn.execute(
        "INSERT OR IGNORE INTO voice_profiles (id, name, gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text, is_default) "
        "VALUES (2, 'alt_char', 'alt.ckpt', 'alt.pth', 'alt.wav', 'テスト', 0);"
    )
    conn.commit()
    conn.close()
    return create_app()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as c:
        yield c


@pytest.mark.asyncio
class TestCharactersRouter:
    async def test_list_characters_with_affection(self, client):
        res = await client.get("/api/characters?user_id=char_test_user")
        assert res.status_code == 200
        data = res.json()
        assert "characters" in data
        assert len(data["characters"]) >= 2
        assert data["active_character_id"] is not None

        char1 = next(c for c in data["characters"] if c["id"] == 1)
        assert char1["name"] == "default_char"
        assert char1["affection"] is not None
        assert char1["affection"]["affection_score"] == 0

    async def test_character_switch_404_precedence_and_switch(self, client):
        # 404 precedence
        res = await client.post("/api/characters/switch", json={"character_id": 9999})
        assert res.status_code == 404

        # 400 missing fields
        res_bad = await client.post("/api/characters/switch", json={})
        assert res_bad.status_code == 400

        # Switch to character 2
        with patch("galgame2voice.services.voice_manager.VoiceManager.switch_profile", new_callable=AsyncMock) as mock_switch:
            mock_switch.return_value = True
            res_switch = await client.post("/api/characters/switch", json={
                "character_id": 2,
                "force": True,
            })
            assert res_switch.status_code == 200
            assert res_switch.json()["status"] == "switched"
            assert res_switch.json()["character_id"] == 2
