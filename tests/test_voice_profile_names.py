"""A voice name that is already taken is explained in Chinese, not as SQLite's UNIQUE text."""

import httpx
import pytest

from galgame2voice.main import create_app


@pytest.fixture
async def client(temp_db_path):
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as http:
        yield http


async def test_creating_a_second_voice_with_the_same_name_says_the_name_is_taken(client, sample_voice_profile):
    first = await client.post("/api/voice/profiles", json=sample_voice_profile)
    assert first.status_code in (200, 201)

    again = await client.post("/api/voice/profiles", json={**sample_voice_profile, "is_default": 0})

    assert again.status_code == 409
    assert again.json()["detail"] == "已有名为「Arona」的音色，请换一个名称。"


async def test_renaming_a_voice_to_a_taken_name_says_the_name_is_taken(client, sample_voice_profile):
    await client.post("/api/voice/profiles", json=sample_voice_profile)
    other = await client.post("/api/voice/profiles", json={**sample_voice_profile, "name": "Hoshino", "is_default": 0})

    renamed = await client.put(f"/api/voice/profiles/{other.json()['id']}", json={"name": "Arona"})

    assert renamed.status_code == 409
    assert "已有名为「Arona」的音色" in renamed.json()["detail"]
