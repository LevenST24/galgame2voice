"""Streaming chat cancellation test and shared MockStreamingLLMAdapter."""

import asyncio
import json
from typing import Any, AsyncIterator, List, Optional

import pytest

from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_service import TtsService


class MockStreamingLLMAdapter(BaseLLMAdapter):
    def __init__(self, stream_chunks: Optional[List[str]] = None, sync_response: Optional[str] = None):
        super().__init__(api_key="sk-test-key", base_url="http://mock-llm")
        self.stream_chunks = stream_chunks or [
            '{"chinese": "你好，指挥官！',
            '今天的天气很适合出海呢。',
            '", "japanese": "こんにちは、指揮官！',
            '今日はいい天気ですね。',
            '"}',
        ]
        self.sync_response = sync_response or json.dumps({
            "chinese": "早上好，今天也要元气满满哦！",
            "japanese": "おはようございます！今日も一日頑張りましょう。",
        }, ensure_ascii=False)

    async def chat(self, messages: List[ChatMessage], model: str, temperature: float = 1.0, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content=self.sync_response)

    async def stream_chat(self, messages: List[ChatMessage], model: str, temperature: float = 1.0, **kwargs: Any) -> AsyncIterator[str]:
        for chunk in self.stream_chunks:
            await asyncio.sleep(0.005)
            yield chunk

    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        return TestResult(success=True, message="Connected", latency_ms=10.0)

    async def list_models(self) -> List[str]:
        return ["mock-chat"]


class TestChatServiceM4:
    """Tests pre-lock stream cancellation for ChatService."""

    @pytest.mark.asyncio
    async def test_chat_service_cancellation(self, temp_db_path, mock_gpt_sovits, tmp_path):
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        mock_adapter = MockStreamingLLMAdapter(stream_chunks=["chunk1", "chunk2", "chunk3", "chunk4"])

        async def _mock_adapter(*args, **kwargs):
            return (mock_adapter, "mock-chat")

        chat_service._get_active_llm_adapter = _mock_adapter

        cancel_ev = asyncio.Event()
        cancel_ev.set()  # Cancel immediately

        events = []
        async for ev in chat_service.stream_chat(prompt="Hello", session_id="test-cancel", cancel_event=cancel_ev):
            events.append(ev)

        assert len(events) == 0
