"""A saved provider API key can be removed again from the settings page."""

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app


@pytest.fixture
async def app_client(isolate_test_database):
    await init_db(isolate_test_database)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8080") as client:
        yield client


async def _stored(provider_id):
    async with get_db() as conn:
        return await crud.get_provider_raw(conn, provider_id)


async def test_clearing_removes_the_key_and_keeps_the_other_settings(app_client):
    saved = await app_client.post("/api/providers", json={
        "id": "deepseek", "api_base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-deepseek-secret-123456", "chat_model": "deepseek-chat",
    })
    assert saved.status_code == 200

    # Saving an empty key keeps the old one, so it cannot be used to remove it.
    await app_client.put("/api/providers/deepseek", json={"api_key": ""})
    assert (await _stored("deepseek")).api_key == "sk-deepseek-secret-123456"

    cleared = await app_client.delete("/api/providers/deepseek/api_key")
    assert cleared.status_code == 200
    assert cleared.json()["provider"]["api_key"] == ""

    row = await _stored("deepseek")
    assert row.api_key == ""
    assert row.chat_model == "deepseek-chat"
    assert row.api_base_url == "https://api.deepseek.com/v1"


async def test_clearing_an_unknown_provider_is_not_found(app_client):
    resp = await app_client.delete("/api/providers/no-such-provider/api_key")
    assert resp.status_code == 404
