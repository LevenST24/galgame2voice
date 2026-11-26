"""/openapi.json is served only when GALGAME2VOICE_ENABLE_DOCS turns the docs on."""

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.main import create_app


async def _status(path: str) -> int:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1") as client:
        return (await client.get(path)).status_code


@pytest.mark.asyncio
@pytest.mark.parametrize(("enabled", "expected"), [("false", 404), ("true", 200)])
async def test_schema_and_docs_follow_the_setting(monkeypatch, enabled, expected):
    monkeypatch.setenv("GALGAME2VOICE_ENABLE_DOCS", enabled)
    get_settings.cache_clear()
    try:
        assert await _status("/openapi.json") == expected
        assert await _status("/docs") == expected
    finally:
        get_settings.cache_clear()
