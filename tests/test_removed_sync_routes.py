"""POST /api/chat and POST /api/voice/synthesize are gone; streaming chat and per-line regeneration remain."""

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.main import create_app
from galgame2voice.routers.chat import set_chat_service
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_service import TtsService
from tests.test_chat_streaming_m4 import MockStreamingLLMAdapter


@pytest.mark.asyncio
async def test_sync_routes_are_gone_while_streaming_routes_remain(temp_db_path, mock_gpt_sovits, tmp_path):
    app = create_app()
    tts_service = TtsService(client=GptSovitsClient(server=mock_gpt_sovits), audio_dir=tmp_path)
    chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)
    adapter = MockStreamingLLMAdapter()

    async def _mock_adapter(*args, **kwargs):
        return (adapter, "mock-chat")

    chat_service._get_active_llm_adapter = _mock_adapter
    set_chat_service(chat_service)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            removed_chat = await http.post("/api/chat", json={"prompt": "你好", "session_id": "removed-check"})
            assert removed_chat.status_code in (404, 405)

            removed_synthesize = await http.post("/api/voice/synthesize", json={"text": "こんにちは"})
            assert removed_synthesize.status_code in (404, 405)

            stream = await http.post("/api/chat/stream", json={"prompt": "你好", "session_id": "stream-check"})
            assert stream.status_code == 200
            assert "text/event-stream" in stream.headers["content-type"]
            assert "event: done" in stream.text

            # An empty body reaches the route's request validation instead of a 404.
            regenerate = await http.post("/api/voice/regenerate-line", json={})
            assert regenerate.status_code == 422
    finally:
        set_chat_service(None)
        await chat_service.aclose()
