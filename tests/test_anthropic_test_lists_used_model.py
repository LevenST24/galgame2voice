"""The Anthropic connection test lists the model it just chatted with.

The settings page only calls a test passed when the chosen model is in `models`, and the
fixed preset list made every other working model (a dated snapshot, a relay id) look broken."""
import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database.session import init_db
from galgame2voice.main import create_app


@pytest.fixture
async def app_client(isolate_test_database):
    await init_db(isolate_test_database)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        yield client


async def test_anthropic_test_lists_the_model_it_just_used(app_client, monkeypatch):
    orig_post = httpx.AsyncClient.post
    sent = {}

    async def mocked_post(self_client, url, *args, **kwargs):
        url_str = str(url)
        if url_str.startswith("https://api.anthropic.com"):
            sent["model"] = (kwargs.get("json") or {}).get("model")
            return httpx.Response(200, json={
                "id": "msg_1", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": "Hi!"}], "stop_reason": "end_turn",
            }, request=httpx.Request("POST", url_str))
        return await orig_post(self_client, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", mocked_post)
    model = "claude-sonnet-4-5"  # a real model id outside the three presets
    resp = await app_client.post("/api/providers/test", json={
        "id": "anthropic", "api_key": "sk-ant-test-key-123456",
        "api_base_url": "https://api.anthropic.com/v1", "chat_model": model,
    })
    data = resp.json()
    assert data["success"] is True, data
    assert sent["model"] == model  # the probe really chatted with this model
    # The UI's describeProviderModelCheck needs the name in this list to call it ready.
    assert model in (data.get("models") or []), data.get("models")
