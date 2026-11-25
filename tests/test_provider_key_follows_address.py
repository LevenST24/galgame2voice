"""A saved API key stays with the address it was saved for, and keyless endpoints can be tested."""

import httpx
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


async def test_moving_custom_to_a_keyless_address_drops_the_old_key(app_client, monkeypatch):
    await app_client.post("/api/config", json={"settings": {"allow_private_llm_endpoints": True}})
    saved = await app_client.post("/api/providers", json={
        "id": "custom", "api_base_url": "https://relay.example.com", "api_key": "sk-relay-secret-123456",
    })
    assert saved.status_code == 200

    sent = []
    orig_get, orig_post = httpx.AsyncClient.get, httpx.AsyncClient.post

    async def upstream(method, self_client, url, *args, **kwargs):
        if str(url).startswith("http://127.0.0.1:8080") or not str(url).startswith("http"):
            return await (orig_get if method == "GET" else orig_post)(self_client, url, *args, **kwargs)
        sent.append((str(url), dict(kwargs.get("headers") or self_client.headers)))
        return httpx.Response(200, json={"data": [{"id": "qwen3:latest"}], "choices": [{"message": {"content": "ok"}}]},
                              request=httpx.Request(method, str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", lambda s, u, *a, **k: upstream("GET", s, u, *a, **k))
    monkeypatch.setattr(httpx.AsyncClient, "post", lambda s, u, *a, **k: upstream("POST", s, u, *a, **k))

    # Fixing only the path on the same origin keeps and sends the stored key.
    tested_path = await app_client.post("/api/providers/test", json={
        "id": "custom", "api_base_url": "https://relay.example.com/v1", "chat_model": "qwen3",
    })
    assert tested_path.status_code == 200
    assert any("sk-relay-secret-123456" in str(headers) for _, headers in sent)
    moved_path = await app_client.put("/api/providers/custom", json={"api_base_url": "https://relay.example.com/v1"})
    assert moved_path.status_code == 200
    assert (await _stored("custom")).api_key == "sk-relay-secret-123456"
    sent.clear()

    # Testing the new keyless address works instead of asking for the relay's key.
    tested = await app_client.post("/api/providers/test", json={
        "id": "custom", "api_base_url": "http://127.0.0.1:11434/v1", "chat_model": "qwen3",
    })
    assert tested.json().get("diagnostic_code") != "CREDENTIAL_HOST_MISMATCH"
    assert sent and all("sk-relay-secret-123456" not in str(headers) for _, headers in sent)

    # Saving the new address without a key does not carry the relay's key over.
    moved = await app_client.put("/api/providers/custom", json={"api_base_url": "http://127.0.0.1:11434/v1"})
    assert moved.status_code == 200
    assert (await _stored("custom")).api_key == ""

    # Changing port on the same host also drops the old key.
    await app_client.post("/api/providers", json={
        "id": "custom", "api_base_url": "http://127.0.0.1:11434/v1", "api_key": "sk-local-secret-999999",
    })
    re_port = await app_client.put("/api/providers/custom", json={"api_base_url": "http://127.0.0.1:8000/v1"})
    assert re_port.status_code == 200
    assert (await _stored("custom")).api_key == ""


async def test_the_key_is_kept_when_the_address_stays_or_a_new_key_comes_with_it(app_client):
    await app_client.post("/api/providers", json={
        "id": "openai", "api_base_url": "https://api.openai.com/v1", "api_key": "sk-openai-secret-123456",
    })
    await app_client.put("/api/providers/openai", json={"api_base_url": "https://api.openai.com/v2", "chat_model": "gpt-x"})
    assert (await _stored("openai")).api_key == "sk-openai-secret-123456"

    await app_client.put("/api/providers/openai", json={
        "api_base_url": "https://relay.example.com/v1", "api_key": "sk-relay-secret-654321",
    })
    assert (await _stored("openai")).api_key == "sk-relay-secret-654321"

    # Official providers reject origin changes without a new key instead of clearing the saved one.
    for payload in (
        {"api_base_url": "https://api.openai.com/v1", "is_active": True},
        {"api_base_url": "https://api.openai.com/v1", "api_key": "sk-****4321"},
    ):
        resp = await app_client.put("/api/providers/openai", json=payload)
        assert resp.status_code == 400
        assert "重新填入完整密钥" in resp.json()["detail"]
        stored = await _stored("openai")
        assert (stored.api_key, stored.api_base_url, stored.is_active) == (
            "sk-relay-secret-654321", "https://relay.example.com/v1", False
        )
