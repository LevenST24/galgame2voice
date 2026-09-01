"""
Tests for input validation, rate limiting integration, and DB concurrency fixes.
"""

import asyncio

import httpx
import pytest

from galgame2voice.main import create_app
from galgame2voice.database.session import get_db
from galgame2voice.database import crud
from galgame2voice.database.models import (
    UserMemoryCreate, CharacterAffectionUpdate,
)
from galgame2voice.services.gpt_sovits_client import (
    validate_user_tts_options, resolve_tts_options,
)


@pytest.fixture
async def client(isolate_test_database):
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_prompt_over_limit_returns_422(self, client):
        resp = await client.post("/api/chat", json={"prompt": "a" * 4001})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_oversized_session_id_returns_422(self, client):
        resp = await client.post("/api/chat", json={"prompt": "hi", "session_id": "s" * 200})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_tts_speed_returns_422(self, client):
        resp = await client.post("/api/chat", json={"prompt": "hi", "tts_options": {"speed": -5}})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_tts_top_k_returns_422(self, client):
        resp = await client.post("/api/chat", json={"prompt": "hi", "tts_options": {"top_k": 9999}})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_oversized_tts_text_returns_422(self, client):
        resp = await client.post("/api/voice/synthesize", json={"text": "あ" * 2001})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_null_byte_in_options_rejected(self, client):
        resp = await client.post(
            "/api/voice/synthesize",
            json={"text": "テスト", "options": {"ref_audio_path": "C:\\x\x00.wav"}},
        )
        assert resp.status_code == 422

    def test_validate_user_tts_options_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            validate_user_tts_options({"speed": 99})
        with pytest.raises(ValueError):
            validate_user_tts_options({"temperature": "not-a-number"})
        assert validate_user_tts_options({"speed": 1.5}) == {"speed": 1.5}
        assert validate_user_tts_options(None) == {}

    def test_resolve_tts_options_clamps_values(self):
        resolved = resolve_tts_options({"speed": 100, "top_k": 0, "temperature": 50})
        assert resolved["speed_factor"] == 3.0
        assert resolved["top_k"] == 1
        assert resolved["temperature"] == 2.0


class TestMemoryUpsertConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_upsert_creates_single_row(self, isolate_test_database):
        async def worker():
            async with get_db(isolate_test_database) as conn:
                return await crud.upsert_memory(conn, UserMemoryCreate(
                    user_id="u1", character_id=1,
                    fact_key="likes_coffee", fact_value="喜欢加糖的咖啡",
                ))

        results = await asyncio.gather(*[worker() for _ in range(10)])

        async with get_db(isolate_test_database) as conn:
            conn.row_factory = None
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM user_memories WHERE user_id='u1' AND fact_key='likes_coffee';"
            )
            count = (await cursor.fetchone())[0]
        assert count == 1
        assert all(r.id for r in results)

    @pytest.mark.asyncio
    async def test_upsert_updates_existing_value(self, isolate_test_database):
        async with get_db(isolate_test_database) as conn:
            first = await crud.upsert_memory(conn, UserMemoryCreate(
                user_id="u1", fact_key="k", fact_value="v1"))
            second = await crud.upsert_memory(conn, UserMemoryCreate(
                user_id="u1", fact_key="k", fact_value="v2"))
        assert second.id == first.id
        assert second.fact_value == "v2"


class TestAffectionAtomicIncrement:
    @pytest.mark.asyncio
    async def test_concurrent_increments_not_lost(self, isolate_test_database):
        async def worker():
            async with get_db(isolate_test_database) as conn:
                return await crud.increment_affection(conn, "u1", 1, 3)

        results = await asyncio.gather(*[worker() for _ in range(10)])

        async with get_db(isolate_test_database) as conn:
            cursor = await conn.execute(
                "SELECT interaction_count, daily_points_earned FROM character_affection "
                "WHERE user_id='u1' AND character_id=1;"
            )
            row = await cursor.fetchone()

        # Every increment must be counted (no lost updates)
        assert row[0] == 10
        # Daily cap of 15 enforced exactly: 10 x 3 = 30 requested -> 15 earned
        assert row[1] == 15
        # Every caller saw some gain reported or capped consistently
        total_reported = sum(r[1] for r in results)
        assert total_reported == 15
