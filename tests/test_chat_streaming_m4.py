"""
Comprehensive tests for Milestone 4 (Streaming Pipeline & Web Audio Player).
Covers:
 - galgame2voice.utils.text_splitter.split_japanese_sentences
 - galgame2voice.services.chat_service.StreamingBilingualParser
 - Fallback parsing for non-JSON/broken LLM outputs
 - ChatService stream_chat & chat_sync end-to-end pipelines
 - /api/chat/stream SSE endpoint (events: text, audio_chunk, done, error)
 - /api/chat and /ai/chat (GET/POST) endpoints
 - Static UI assets and Web Audio Player file serving
"""

import asyncio
import json
import os
import tempfile
from typing import AsyncIterator, Dict, Any, List, Optional
import pytest
from httpx import AsyncClient, ASGITransport
import aiosqlite

from galgame2voice.utils.text_splitter import split_japanese_sentences
from galgame2voice.services.chat_service import StreamingBilingualParser, ChatService
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult
from galgame2voice.routers.chat import set_chat_service, get_chat_service, router as chat_router
from galgame2voice.main import create_app
from galgame2voice.database.session import init_db
from tests.conftest import MockGptSovitsServer, MockLLMServer


# ============================================================================
# Mock Streaming LLM Adapter for Unit Tests
# ============================================================================

class MockStreamingLLMAdapter(BaseLLMAdapter):
    def __init__(self, stream_chunks: Optional[List[str]] = None, sync_response: Optional[str] = None):
        super().__init__(api_key="sk-test-key", base_url="http://mock-llm")
        self.stream_chunks = stream_chunks or [
            '{"chinese": "你好，指挥官！',
            '今天的天气很适合出海呢。',
            '", "japanese": "こんにちは、指揮官！',
            '今日はいい天気ですね。',
            '"}'
        ]
        self.sync_response = sync_response or json.dumps({
            "chinese": "早上好，今天也要元气满满哦！",
            "japanese": "おはようございます！今日も一日頑張りましょう。"
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


# ============================================================================
# Text Splitter & Parser Unit Tests
# ============================================================================

class TestTextSplitterAndParserM4:
    """Tests Japanese sentence splitting and streaming bilingual JSON parser."""

    def test_split_japanese_sentences_comprehensive(self):
        text = "おはよう！今日もいい天気ですね。一緒に行きましょう？\n次はこれ！"
        sentences = split_japanese_sentences(text)
        assert len(sentences) == 4
        assert sentences[0] == "おはよう！"
        assert sentences[1] == "今日もいい天气ですね。" or "今日もいい天気ですね。" in sentences[1]
        assert sentences[2] == "一緒に行きましょう？"
        assert sentences[3] == "次はこれ！"

    def test_split_empty_and_whitespace(self):
        assert split_japanese_sentences("") == []
        assert split_japanese_sentences("   \n\n  ") == []

    def test_parser_incremental_chinese_and_japanese(self):
        parser = StreamingBilingualParser()
        chunks = [
            '{"chinese": "早',
            '安！", "japanese": "お',
            'はよう！',
            '今日もよろしくね。',
            '"}'
        ]
        emitted_chinese = []
        emitted_japanese = []

        for c in chunks:
            ch_delta, ja_sents = parser.feed_chunk(c)
            if ch_delta:
                emitted_chinese.append(ch_delta)
            if ja_sents:
                emitted_japanese.extend(ja_sents)

        ch, ja, rem = parser.finalize()
        emitted_japanese.extend(rem)

        assert "".join(emitted_chinese) == "早安！"
        assert ch == "早安！"
        assert ja == "おはよう！今日もよろしくね。"
        assert len(emitted_japanese) == 2
        assert "おはよう！" in emitted_japanese[0]
        assert "今日もよろしくね。" in emitted_japanese[1]

    def test_parser_markdown_codeblock_wrapper(self):
        parser = StreamingBilingualParser()
        chunks = [
            '`json\n',
            '{"chinese": "很高兴见到你。", ',
            '"japanese": "お会いできて嬉しいです。"}\n',
            '`'
        ]
        for c in chunks:
            parser.feed_chunk(c)
        ch, ja, _ = parser.finalize()
        assert ch == "很高兴见到你。"
        assert ja == "お会いできて嬉しいです。"

    def test_parser_escaped_quotes_and_newlines(self):
        parser = StreamingBilingualParser()
        chunks = [
            '{"chinese": "她说：\\"你好！\\"\\n新行。", ',
            '"japanese": "彼女は「こんにちは！」と言った。\\n新しい行。"}'
        ]
        for c in chunks:
            parser.feed_chunk(c)
        ch, ja, _ = parser.finalize()
        assert '她说："你好！"' in ch
        assert "\n新行。" in ch
        assert "彼女は「こんにちは！」と言った。" in ja

    def test_parser_fallback_plain_text(self):
        """When LLM returns non-JSON plain text, parser extracts gracefully."""
        parser = StreamingBilingualParser()
        chunks = [
            "这是纯文本回复，没有包含JSON格式。",
            "但是系统应该优雅回退处理。"
        ]
        for c in chunks:
            parser.feed_chunk(c)
        ch, ja, _ = parser.finalize()
        assert "纯文本回复" in ch
        assert ch != ""

    def test_parser_fallback_custom_labels(self):
        """When LLM returns 中文: ... 日文: ... instead of JSON."""
        parser = StreamingBilingualParser()
        chunks = [
            "中文：你好呀，指挥官！\n",
            "日文：こんにちは、指揮官！"
        ]
        for c in chunks:
            parser.feed_chunk(c)
        ch, ja, _ = parser.finalize()
        assert "你好呀，指挥官！" in ch
        assert "こんにちは、指揮官！" in ja


# ============================================================================
# ChatService End-to-End Tests
# ============================================================================

class TestChatServiceM4:
    """Tests ChatService streaming and synchronous pipelines."""

    @pytest.mark.asyncio
    async def test_chat_service_stream_chat(self, temp_db_path, mock_gpt_sovits, tmp_path):
        audio_dir = tmp_path / "audio_test"
        audio_dir.mkdir()

        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=audio_dir)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        mock_adapter = MockStreamingLLMAdapter()
        async def _mock_adapter(*args, **kwargs):
            return (mock_adapter, "mock-chat")
        chat_service._get_active_llm_adapter = _mock_adapter

        events = []
        async for ev in chat_service.stream_chat(prompt="测试提问", session_id="test-sess-1"):
            events.append(ev)

        assert len(events) > 0
        event_types = [e["event"] for e in events]
        assert "text" in event_types
        assert "audio_chunk" in event_types
        assert "done" in event_types

        # Verify done payload
        done_ev = [e for e in events if e["event"] == "done"][0]
        assert "chinese" in done_ev["data"]
        assert "japanese" in done_ev["data"]
        assert "audio_url" in done_ev["data"]
        assert len(done_ev["data"]["chunks"]) >= 1

    @pytest.mark.asyncio
    async def test_chat_service_chat_sync(self, temp_db_path, mock_gpt_sovits, tmp_path):
        audio_dir = tmp_path / "audio_test_sync"
        audio_dir.mkdir()

        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=audio_dir)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        mock_adapter = MockStreamingLLMAdapter()
        async def _mock_adapter(*args, **kwargs):
            return (mock_adapter, "mock-chat")
        chat_service._get_active_llm_adapter = _mock_adapter

        result = await chat_service.chat_sync(prompt="早上好", session_id="test-sess-2")
        assert "chinese" in result
        assert "japanese" in result
        assert "audio_url" in result
        assert "audioUrl" in result
        assert "早上好" in result["chinese"]

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


# ============================================================================
# FastAPI Chat Router Endpoints Tests
# ============================================================================

class TestChatRouterEndpointsM4:
    """Tests FastAPI router endpoints /api/chat/stream, /api/chat, /ai/chat, and static assets."""

    @pytest.mark.asyncio
    async def test_post_chat_stream_sse_endpoint(self, temp_db_path, mock_gpt_sovits, tmp_path):
        app = create_app()
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        mock_adapter = MockStreamingLLMAdapter()
        async def _mock_adapter(*args, **kwargs):
            return (mock_adapter, "mock-chat")
        chat_service._get_active_llm_adapter = _mock_adapter
        set_chat_service(chat_service)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/chat/stream", json={"prompt": "你好！", "session_id": "sse-test"})
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            
            body = resp.text
            assert "event: text" in body
            assert "event: audio_chunk" in body
            assert "event: done" in body
            assert "data: " in body

    @pytest.mark.asyncio
    async def test_post_chat_sync_endpoint(self, temp_db_path, mock_gpt_sovits, tmp_path):
        app = create_app()
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        mock_adapter = MockStreamingLLMAdapter()
        async def _mock_adapter(*args, **kwargs):
            return (mock_adapter, "mock-chat")
        chat_service._get_active_llm_adapter = _mock_adapter
        set_chat_service(chat_service)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/chat", json={"prompt": "今天怎么样？", "session_id": "sync-test"})
            assert resp.status_code == 200
            data = resp.json()
            assert "chinese" in data
            assert "japanese" in data
            assert "audio_url" in data

    @pytest.mark.asyncio
    async def test_get_ai_chat_legacy_endpoint(self, temp_db_path, mock_gpt_sovits, tmp_path):
        app = create_app()
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        mock_adapter = MockStreamingLLMAdapter()
        async def _mock_adapter(*args, **kwargs):
            return (mock_adapter, "mock-chat")
        chat_service._get_active_llm_adapter = _mock_adapter
        set_chat_service(chat_service)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/ai/chat?prompt=LegacyPrompt&session_id=legacy-test")
            assert resp.status_code == 200
            data = resp.json()
            assert "chinese" in data
            assert "audioUrl" in data

    @pytest.mark.asyncio
    async def test_chat_stream_empty_prompt_validation(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/chat/stream", json={"prompt": "  "})
            assert resp.status_code == 422

            resp2 = await c.post("/api/chat", json={"prompt": ""})
            assert resp2.status_code == 422

    @pytest.mark.asyncio
    async def test_static_ui_files_served(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # Root index.html
            r_root = await c.get("/")
            assert r_root.status_code == 200
            assert "galgame2voice" in r_root.text

            # CSS
            r_css = await c.get("/static/css/style.css")
            assert r_css.status_code == 200
            assert "chat-container" in r_css.text or "app-container" in r_css.text

            # Audio Player JS
            r_player = await c.get("/static/js/audio_player.js")
            assert r_player.status_code == 200
            assert "StreamingAudioPlayer" in r_player.text

            # Chat Client JS
            r_client = await c.get("/static/js/chat_client.js")
            assert r_client.status_code == 200
            assert "/api/chat/stream" in r_client.text
