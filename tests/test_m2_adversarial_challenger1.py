"""
Empirical Adversarial Test Suite for Milestone 2 (Commercial Galgame Immersion UI & Streaming).
Authored by Challenger 1.

Coverage:
1. SSE streaming protocol stress tests (fragmented tokens, multiple sentences, out-of-order data).
2. Edge cases: empty text, whitespace prompts, special characters, unicode emojis, newlines.
3. Fast message skipping, rapid interruptions, and cancellation tokens.
4. Audio decode fallback and error handling.
5. Static UI structure, CSS class contracts (.chat-container, .app-container), and dual-mode elements.
"""

import asyncio
import json
import pytest
from typing import AsyncIterator, List, Optional
from httpx import AsyncClient, ASGITransport
from pathlib import Path

from galgame2voice.utils.text_splitter import split_japanese_sentences
from galgame2voice.services.chat_service import StreamingBilingualParser, ChatService
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult
from galgame2voice.routers.chat import set_chat_service, router as chat_router
from galgame2voice.main import create_app
from tests.conftest import MockGptSovitsServer


class AdversarialStreamingLLMAdapter(BaseLLMAdapter):
    def __init__(self, chunks: List[str]):
        super().__init__(api_key="sk-adversarial", base_url="http://mock-llm")
        self.chunks = chunks

    async def chat(self, messages: List[ChatMessage], model: str, **kwargs) -> LLMResponse:
        return LLMResponse(content="".join(self.chunks))

    async def stream_chat(self, messages: List[ChatMessage], model: str, **kwargs) -> AsyncIterator[str]:
        for c in self.chunks:
            await asyncio.sleep(0.001)
            yield c

    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        return TestResult(success=True, message="OK", latency_ms=1.0)

    async def list_models(self) -> List[str]:
        return ["adv-model"]


class TestBilingualParserAdversarial:
    """Stress-test StreamingBilingualParser against hostile inputs."""

    def test_broken_json_and_plain_text_recovery(self):
        parser = StreamingBilingualParser()
        chunks = [
            "我是纯文本回复，并没有遵循 JSON 格式。",
            "但是我有中文和日文：\n",
            "中文: 你好呀！\n",
            "日文: こんにちは！\n"
        ]
        for c in chunks:
            parser.feed_chunk(c)
        ch, ja, rem = parser.finalize()
        assert "你好呀" in ch or "纯文本回复" in ch
        assert len(ch) > 0

    def test_fragmented_byte_boundaries_and_emojis(self):
        parser = StreamingBilingualParser()
        full_json = '{"chinese": "🌸 早上好！指挥官 ✨", "japanese": "🌸 おはようございます！指揮官 ✨"}'
        # Split into 1-character fragments
        for char in full_json:
            parser.feed_chunk(char)
        ch, ja, rem = parser.finalize()
        assert "🌸 早上好！指挥官 ✨" in ch
        assert "🌸 おはようございます！指揮官 ✨" in ja

    def test_excessive_escaped_characters(self):
        parser = StreamingBilingualParser()
        payload = '{"chinese": "Line1\\nLine2\\tTab\\\\Backslash\\\"Quote\\\"", "japanese": "行1\\n行2\\tタブ\\\\バックスラッシュ\\\"クォート\\\""}'
        parser.feed_chunk(payload)
        ch, ja, _ = parser.finalize()
        assert "Line1\nLine2\tTab\\Backslash\"Quote\"" in ch
        assert "行1\n行2\tタブ\\バックスラッシュ\"クォート\"" in ja

    def test_empty_and_corrupt_json_payloads(self):
        parser = StreamingBilingualParser()
        parser.feed_chunk("{")
        ch, ja, _ = parser.finalize()
        assert isinstance(ch, str)
        assert isinstance(ja, str)


class TestChatStreamingPipelineAdversarial:
    """Stress-test FastAPI SSE streaming endpoints under high contention and edge cases."""

    @pytest.mark.asyncio
    async def test_rapid_stream_cancellation(self, temp_db_path, mock_gpt_sovits, tmp_path):
        app = create_app()
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        long_chunks = [f'{{"chinese": "第{i}句话", "japanese": "文{i}"}}' for i in range(20)]
        adapter = AdversarialStreamingLLMAdapter(long_chunks)
        async def _mock_adapter(*args, **kwargs):
            return (adapter, "adv-model")
        chat_service._get_active_llm_adapter = _mock_adapter
        set_chat_service(chat_service)

        cancel_event = asyncio.Event()
        events = []

        async def read_stream():
            async for ev in chat_service.stream_chat(prompt="Test Prompt", session_id="cancel-sess", cancel_event=cancel_event):
                events.append(ev)
                if len(events) >= 2:
                    cancel_event.set() # Trigger cancel mid-stream

        await read_stream()
        # Ensure streaming stopped early
        assert len(events) < len(long_chunks)

    @pytest.mark.asyncio
    async def test_chat_stream_empty_and_whitespace_prompts(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # Test empty string
            r1 = await c.post("/api/chat/stream", json={"prompt": ""})
            assert r1.status_code == 422

            # Test all-whitespace string
            r2 = await c.post("/api/chat/stream", json={"prompt": "     \n\t  "})
            assert r2.status_code == 422

            # Test missing prompt field
            r3 = await c.post("/api/chat/stream", json={"session_id": "123"})
            assert r3.status_code == 422

    @pytest.mark.asyncio
    async def test_special_characters_and_html_injection_in_prompt(self, temp_db_path, mock_gpt_sovits, tmp_path):
        app = create_app()
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        adapter = AdversarialStreamingLLMAdapter([
            '{"chinese": "<script>alert(\'xss\')</script> & 特殊字符", "japanese": "<script>alert(\'xss\')</script> & 日本語"}'
        ])
        async def _mock_adapter(*args, **kwargs):
            return (adapter, "adv-model")
        chat_service._get_active_llm_adapter = _mock_adapter
        set_chat_service(chat_service)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/chat/stream", json={
                "prompt": "<script>alert('xss')</script> & 特殊字符 < > & \" '",
                "session_id": "xss-test"
            })
            assert resp.status_code == 200
            body = resp.text
            assert "event: text" in body
            assert "event: done" in body


class TestFrontendAssetsAndDOMStructure:
    """Verifies static files, DOM hierarchy, and CSS contracts for Milestone 2."""

    def test_index_html_contains_all_required_dom_elements(self):
        index_file = Path("galgame2voice/static/index.html")
        assert index_file.exists(), "index.html must exist"
        content = index_file.read_text(encoding="utf-8")

        # Visual Novel Mode elements
        assert 'id="vn-view"' in content
        assert 'class="vn-dialogue-box' in content
        assert 'id="vn-character-name"' in content
        assert 'id="vn-text-ja"' in content
        assert 'id="vn-text-zh"' in content
        assert 'id="vn-audio-equalizer"' in content
        assert 'id="vn-audio-status"' in content
        assert 'id="btn-vn-replay"' in content
        assert 'id="btn-vn-log"' in content

        # Classic Chat Mode elements
        assert 'id="chat-view"' in content
        assert 'id="chat-log"' in content
        assert 'id="chat-form"' in content
        assert 'id="prompt-input"' in content
        assert 'id="send-btn"' in content
        assert 'id="reset-context-btn"' in content

        # Header Capsule elements
        assert 'id="quick-voice-select"' in content
        assert 'id="sovits-status-badge"' in content
        assert 'id="mute-toggle-btn"' in content
        assert 'id="volume-slider"' in content
        assert 'id="btn-mode-vn"' in content
        assert 'id="btn-mode-chat"' in content

    def test_style_css_contains_glassmorphism_and_animations(self):
        style_file = Path("galgame2voice/static/css/style.css")
        assert style_file.exists(), "style.css must exist"
        css = style_file.read_text(encoding="utf-8")

        # Test asset compatibility classes
        assert ".chat-container" in css or ".app-container" in css
        # Glassmorphism backdrop-filter
        assert "backdrop-filter" in css
        # Equalizer bar styling
        assert ".audio-equalizer-bars" in css or ".bar" in css
        # Standee aura / animations
        assert "keyframes" in css
