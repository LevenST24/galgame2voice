"""POST /api/config with the flat body the settings page sends must not report
success when one value is invalid and nothing was saved."""

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.main import create_app


async def _client():
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8080")


@pytest.mark.asyncio
async def test_fractional_retention_is_rejected_with_its_name():
    async with await _client() as client:
        before = (await client.get("/api/config")).json()["settings"]["gpt_sovits_url"]
        resp = await client.post(
            "/api/config",
            json={"gpt_sovits_url": "http://127.0.0.1:9881", "audio_retention_minutes": 1.5},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "音频缓存保留时间" in detail and "整数" in detail and "没有保存" in detail
        after = (await client.get("/api/config")).json()["settings"]["gpt_sovits_url"]
        assert after == before


@pytest.mark.asyncio
async def test_out_of_range_flat_value_names_the_limit():
    async with await _client() as client:
        resp = await client.post("/api/config", json={"temperature": 5})
        assert resp.status_code == 422
        assert "「温度」不能大于 2.0" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_valid_flat_and_nested_bodies_still_save():
    async with await _client() as client:
        flat = await client.post("/api/config", json={"audio_retention_minutes": 45})
        assert flat.status_code == 200 and flat.json()["updated_count"] == 1
        nested = await client.post("/api/config", json={"settings": {"top_k": 7}})
        assert nested.status_code == 200
        settings = (await client.get("/api/config")).json()["settings"]
        assert settings["audio_retention_minutes"] == 45 and settings["top_k"] == 7
