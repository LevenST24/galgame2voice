"""
Tests for input validation, rate limiting integration, and DB concurrency fixes.
"""

import asyncio
from typing import AsyncIterator, List

import httpx
from httpx import ASGITransport, AsyncClient
import pytest

from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult
from galgame2voice.main import create_app
from galgame2voice.database.session import get_db
from galgame2voice.database import crud
from galgame2voice.database.models import (
    UserMemoryCreate, CharacterAffectionUpdate,
)
from galgame2voice.routers.chat import set_chat_service
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.gpt_sovits_client import (
    GptSovitsClient,
    validate_user_tts_options,
    resolve_tts_options,
)
from galgame2voice.services.tts_service import TtsService


@pytest.fixture
async def client(isolate_test_database):
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as c:
        yield c


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_prompt_over_limit_returns_422(self, client):
        resp = await client.post("/api/chat/stream", json={"prompt": "a" * 4001})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_oversized_session_id_returns_422(self, client):
        resp = await client.post("/api/chat/stream", json={"prompt": "hi", "session_id": "s" * 200})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_tts_speed_returns_422(self, client):
        resp = await client.post("/api/chat/stream", json={"prompt": "hi", "tts_options": {"speed": -5}})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_tts_top_k_returns_422(self, client):
        resp = await client.post("/api/chat/stream", json={"prompt": "hi", "tts_options": {"top_k": 9999}})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_null_byte_in_tts_options_rejected(self, client):
        resp = await client.post(
            "/api/chat/stream",
            json={"prompt": "hi", "tts_options": {"ref_audio_path": "C:\\x\x00.wav"}},
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

    @pytest.mark.asyncio
    async def test_chat_stream_empty_prompt_validation(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as c:
            resp = await c.post("/api/chat/stream", json={"prompt": "  "})
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_special_characters_and_html_injection_in_prompt(self, temp_db_path, mock_gpt_sovits, tmp_path):
        class _SpecialCharsStreamingLLMAdapter(BaseLLMAdapter):
            def __init__(self, chunks: List[str]):
                super().__init__(api_key="sk-test", base_url="http://mock-llm")
                self.chunks = chunks

            async def chat(self, messages: List[ChatMessage], model: str, **kwargs) -> LLMResponse:
                return LLMResponse(content="".join(self.chunks))

            async def stream_chat(self, messages: List[ChatMessage], model: str, **kwargs) -> AsyncIterator[str]:
                for c in self.chunks:
                    await asyncio.sleep(0.001)
                    yield c

            async def test_connection(self, model=None) -> TestResult:
                return TestResult(success=True, message="OK", latency_ms=1.0)

            async def list_models(self) -> List[str]:
                return ["adv-model"]

        app = create_app()
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        adapter = _SpecialCharsStreamingLLMAdapter([
            '{"chinese": "<script>alert(\'xss\')</script> & 特殊字符", "japanese": "<script>alert(\'xss\')</script> & 日本語"}'
        ])

        async def _mock_adapter(*args, **kwargs):
            return (adapter, "adv-model")

        chat_service._get_active_llm_adapter = _mock_adapter
        set_chat_service(chat_service)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as c:
            resp = await c.post("/api/chat/stream", json={
                "prompt": "<script>alert('xss')</script> & 特殊字符 < > & \" '",
                "session_id": "xss-test"
            })
            assert resp.status_code == 200
            body = resp.text
            assert "event: text" in body
            assert "event: done" in body


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
