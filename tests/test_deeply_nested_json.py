"""A request body nested a few thousand levels deep gets a 400, not a 500.

Python's JSON parser accepts it, but the 422 body that echoes it back overflows the
recursion limit while being encoded.
"""

import httpx
import pytest

from galgame2voice.main import create_app

DEPTH = 3000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/voice/profiles", "[" * DEPTH + "]" * DEPTH),
        ("/api/chat/stream", '{"prompt":' * DEPTH + "1" + "}" * DEPTH),
    ],
    ids=["list", "object"],
)
async def test_deeply_nested_body_is_a_400(isolate_test_database, path, body):
    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        resp = await client.post(path, content=body.encode(), headers={"content-type": "application/json"})
    assert resp.status_code == 400, resp.text[:200]
    assert "嵌套" in resp.json()["detail"]
