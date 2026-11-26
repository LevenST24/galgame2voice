"""A request body nested thousands of levels deep gets a Chinese 400.

Depending on the platform it used to fail in the JSON parser (an English 400) or in
the encoder of the 422 body that echoes it back (a 500).
"""

import httpx
import pytest

from galgame2voice.main import create_app



def _nested(kind: str, depth: int) -> str:
    if kind == "list":
        return "[" * depth + "]" * depth
    return '{"prompt":' * depth + "1" + "}" * depth


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [3000, 200000], ids=["encode", "parse"])
@pytest.mark.parametrize(
    "path,kind",
    [("/api/voice/profiles", "list"), ("/api/chat/stream", "object")],
    ids=["list", "object"],
)
async def test_deeply_nested_body_is_a_400(isolate_test_database, path, kind, depth):
    body = _nested(kind, depth)
    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        resp = await client.post(path, content=body.encode(), headers={"content-type": "application/json"})
    assert resp.status_code == 400, resp.text[:200]
    assert "嵌套" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_other_http_errors_keep_their_detail(isolate_test_database):
    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        resp = await client.get("/api/no-such-route")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}
