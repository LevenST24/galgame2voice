"""
Comprehensive tests for Milestone 4 (Streaming Pipeline & Web Audio Player).
Covers:
 - galgame2voice.utils.text_splitter.split_japanese_sentences
 - galgame2voice.services.chat_service.StreamingBilingualParser
 - Fallback parsing for non-JSON/broken LLM outputs
 - ChatService stream_chat end-to-end pipeline
 - /api/chat/stream SSE endpoint (events: text, audio_chunk, done, error)
 - Static UI assets and Web Audio Player file serving
"""

import asyncio
import json
import os
import tempfile
import time
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

    def test_split_agile_whitespace(self):
        """Blank agile input is empty after strip; tabs are empty on both paths."""
        assert split_japanese_sentences("  \r\n  \t  ", is_first_chunk=True, min_chars=6) == []
        assert split_japanese_sentences("\t\t", is_first_chunk=False) == []
        assert split_japanese_sentences("\t\t", is_first_chunk=True, min_chars=6) == []

    @pytest.mark.parametrize("punct_only", [
        ",", "、", "，", "。", "！", "？", "!", "?",
        ",,,,,,,,", "、、、、、、", "，，，，，，",
        "。。。。。。", "！？！？！？", "!?!?!?",
        "、、，，,,。。。！？!?",
        "   ，，、、。。！？   ",
    ])
    def test_chunking_pure_punctuation(self, punct_only):
        """Punctuation-only text must never crash, never hang, and never lose content."""
        expected_stripped = punct_only.replace(" ", "").replace("\n", "")
        # Test is_first_chunk=False
        res_default = split_japanese_sentences(punct_only, is_first_chunk=False)
        assert isinstance(res_default, list)
        if expected_stripped:
            assert len(res_default) >= 1
            combined_default = "".join(res_default).replace(" ", "").replace("\n", "")
            assert combined_default == expected_stripped
        else:
            assert res_default == []

        # Test is_first_chunk=True
        res_agile = split_japanese_sentences(punct_only, is_first_chunk=True, min_chars=6)
        assert isinstance(res_agile, list)
        if expected_stripped:
            assert len(res_agile) >= 1
            combined_agile = "".join(res_agile).replace(" ", "").replace("\n", "")
            assert combined_agile == expected_stripped
        else:
            assert res_agile == []

    def test_chunking_emojis_and_unicode_symbols(self):
        """Emojis, math symbols, and mixed scripts must be preserved safely."""
        emoji_text = "🐱こんにちは！🐶先生、今日はいい天気ですね🎉一緒に出かけましょう？🚀"
        res = split_japanese_sentences(emoji_text, is_first_chunk=True, min_chars=6)
        assert len(res) >= 2
        # Verify first sentence contains initial emoji
        assert "🐱こんにちは！" in res[0]
        # Verify total text preservation
        assert "".join(res) == emoji_text

        # Pure emojis with commas
        emoji_clauses = "🍎、🍌、🍇、🍓、🍉、🍒、🍑"
        res_emoji = split_japanese_sentences(emoji_clauses, is_first_chunk=True, min_chars=6)
        assert len(res_emoji) >= 1
        assert "".join(res_emoji) == emoji_clauses

    # Asserts <150ms/<300ms wall-clock bounds on 30K-51K char chunking; sensitive to multi-worker CPU contention.
    @pytest.mark.serial
    @pytest.mark.wall_clock
    def test_chunking_huge_paragraph_no_catastrophic_backtracking(self):
        """
        Stress-tests 50,000 characters to assert zero ReDoS, stack overflow,
        or unbounded execution time (< 150ms).
        """
        # 1. 30,000 characters without ANY punctuation
        huge_plain = "あいうえおかきくけこさしすせそたちつてとなにぬねの" * 1200  # 30,000 chars
        t0 = time.perf_counter()
        res_plain = split_japanese_sentences(huge_plain, is_first_chunk=True, min_chars=6)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert len(res_plain) == 1
        assert len(res_plain[0]) == 30000
        assert elapsed_ms < 150.0, f"Huge plain text chunking took too long: {elapsed_ms:.2f}ms"

        # 2. 50,000 characters with mixed commas and terminal punctuation
        pattern_block = "初めまして、今日はいい天気ですね。一緒に出かけませんか！そうですね？"  # 34 chars
        huge_mixed = pattern_block * 1500  # 51,000 chars
        t0 = time.perf_counter()
        res_mixed = split_japanese_sentences(huge_mixed, is_first_chunk=True, min_chars=6)
        elapsed_mixed_ms = (time.perf_counter() - t0) * 1000.0
        assert len(res_mixed) > 1000
        assert elapsed_mixed_ms < 300.0, f"Huge mixed text chunking took too long: {elapsed_mixed_ms:.2f}ms"
        # Zero dropped characters
        assert sum(len(s) for s in res_mixed) == len(huge_mixed)

    def test_streaming_bilingual_parser_erratic_feed(self):
        """Feeds raw JSON 1 character at a time; asserts proper incremental decoding."""
        raw_json = (
            '{"tts": {"speed": 1.15, "temp": 0.85, "emotion": "happy"}, '
            '"chinese": "你好啊，好久不见！", '
            '"japanese": "お久しぶりですね、元気にしていましたか？"}'
        )
        parser = StreamingBilingualParser()
        emitted_deltas = []
        emitted_ja = []

        for char in raw_json:
            delta_ch, ja_sents = parser.feed_chunk(char)
            if delta_ch:
                emitted_deltas.append(delta_ch)
            if ja_sents:
                emitted_ja.extend(ja_sents)

        full_ch, full_ja, remaining_ja = parser.finalize()
        if remaining_ja:
            emitted_ja.extend(remaining_ja)

        assert "".join(emitted_deltas) == "你好啊，好久不见！"
        assert full_ch == "你好啊，好久不见！"
        assert full_ja == "お久しぶりですね、元気にしていましたか？"
        assert parser.tts_speed == 1.15
        assert parser.tts_temperature == 0.85
        assert parser.tts_emotion == "happy"
        # Verify agile chunk 0 split on first clause pause: "お久しぶりですね、"
        assert emitted_ja[0] == "お久しぶりですね、"


# ============================================================================
# ChatService End-to-End Tests
# ============================================================================

class TestChatServiceM4:
    """Tests the ChatService streaming pipeline."""

    @pytest.mark.asyncio
    async def test_chat_service_stream_chat(self, temp_db_path, mock_gpt_sovits, tmp_path, configured_voice):
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
    """Tests FastAPI router endpoint /api/chat/stream and static assets."""

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
    async def test_chat_stream_empty_prompt_validation(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/chat/stream", json={"prompt": "  "})
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_static_ui_files_served(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # Root index.html
            r_root = await c.get("/")
            assert r_root.status_code == 200
            assert "Gal2Voice" in r_root.text

            # Built assets exist and are fingerprint-hashed
            import re as _re
            asset_names = _re.findall(r'/static/assets/(index-[\w-]+\.(?:js|css))', r_root.text)
            assert asset_names, "index.html must reference built /static/assets files"
            for name in asset_names:
                r_asset = await c.get(f"/static/assets/{name}")
                assert r_asset.status_code == 200
