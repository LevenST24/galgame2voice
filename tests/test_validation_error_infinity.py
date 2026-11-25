"""A request whose invalid field holds Infinity, NaN or 1e400 gets a 422, not a 500.

FastAPI's 422 body echoes the rejected input, and JSON responses refuse non-finite
floats, so these requests used to end in an internal server error.
"""

import httpx
import pytest

from galgame2voice.main import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["1e400", "-1e400", "Infinity", "NaN"])
async def test_non_finite_number_in_an_invalid_field_is_a_422(isolate_test_database, raw):
    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        resp = await client.post(
            "/api/voice/profiles",
            content=('{"name": ' + raw + '}').encode(),
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail
    assert any(error.get("loc", [None])[-1] == "name" for error in detail)


@pytest.mark.asyncio
async def test_ordinary_validation_errors_keep_their_shape(isolate_test_database):
    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        resp = await client.post("/api/chat/stream", json={"prompt": "a" * 4001})
    assert resp.status_code == 422
    error = resp.json()["detail"][0]
    assert error["loc"] == ["body", "prompt"] and "input" in error
