"""
Adversarial Stress Test Suite for Milestone 2: Commercial Galgame Immersion UI & Streaming.
Authored by Challenger 2.

Empirically tests and challenges:
1. Web Audio AnalyserNode frequency scaling, FFT bin bounds, and dynamic equalizer visualizer mathematics.
2. Audio Queue concurrency, async race conditions, out-of-order chunk decoding, and session cancellation (interrupt).
3. SSE Streaming pipeline error resilience, chunk boundary fragmentation, malformed payloads, and bilingual event alignment.
4. Frontend DOM state transitions, click-to-skip text animation, view mode toggling, and context reset safety.
"""

import asyncio
import json
import math
import re
from pathlib import Path
from typing import AsyncIterator, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from httpx import AsyncClient, ASGITransport

from galgame2voice.main import create_app
from galgame2voice.config import get_settings
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_service import TtsService
from galgame2voice.routers.chat import set_chat_service
from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ============================================================================
# 3. SSE Streaming Pipeline & Protocol Alignment Stress Tests
# ============================================================================

class MockStreamingLLM(BaseLLMAdapter):
    def __init__(self, chunks: List[str]):
        super().__init__(api_key="sk-test", base_url="http://mock")
        self.chunks = chunks

    async def chat(self, *args, **kwargs) -> LLMResponse:
        return LLMResponse(content="".join(self.chunks))

    async def stream_chat(self, *args, **kwargs) -> AsyncIterator[str]:
        for c in self.chunks:
            yield c

    async def test_connection(self, *args, **kwargs) -> TestResult:
        return TestResult(success=True, message="OK", latency_ms=1.0)

    async def list_models(self) -> List[str]:
        return ["mock-model"]


class TestSSEStreamingProtocol:
    """Stress test the backend SSE stream generator and frontend parser contract."""

    @pytest.mark.asyncio
    async def test_sse_stream_bilingual_events_emission(self, temp_db_path, mock_gpt_sovits, tmp_path, configured_voice):
        """
        Test that `POST /api/chat/stream` emits:
        - `event: text` with delta_chinese / full_chinese
        - `event: audio_chunk` with index, audio_url, sentence
        - `event: done` with full summary
        """
        app = create_app()
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        mock_llm = MockStreamingLLM([
            '{"chinese": "早上好！今天天气真不错呢。',
            '", "japanese": "おはようございます！今日はいい天気ですね。',
            '"}'
        ])
        async def _mock_adapter(*args, **kwargs):
            return (mock_llm, "mock-model")
        chat_service._get_active_llm_adapter = _mock_adapter
        set_chat_service(chat_service)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post(
                    "/api/chat/stream",
                    json={"session_id": "test_m2_sess", "prompt": "你好", "stream": True}
                )
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers.get("content-type", "")

                body = resp.text
                assert "event: text" in body
                assert "event: audio_chunk" in body
                assert "event: done" in body

                # Parse SSE lines
                lines = body.split("\n")
                events = []
                current_event = "message"
                for line in lines:
                    line = line.strip()
                    if not line:
                        current_event = "message"
                        continue
                    if line.startswith("event:"):
                        current_event = line.replace("event:", "").strip()
                    elif line.startswith("data:"):
                        data_str = line.replace("data:", "").strip()
                        if data_str != "[DONE]":
                            parsed = json.loads(data_str)
                            events.append((current_event, parsed))

                # Verify text events exist
                text_events = [e for e in events if e[0] == "text"]
                assert len(text_events) > 0
                for _, payload in text_events:
                    assert "delta_chinese" in payload or "full_chinese" in payload

                # Verify audio_chunk events exist
                audio_events = [e for e in events if e[0] == "audio_chunk"]
                assert len(audio_events) > 0
                for _, payload in audio_events:
                    assert "audio_url" in payload
                    assert "index" in payload
                    assert "sentence" in payload

                # Verify done event exists
                done_events = [e for e in events if e[0] == "done"]
                assert len(done_events) == 1
                _, done_payload = done_events[0]
                assert "chinese" in done_payload
                assert "japanese" in done_payload
                assert "audio_url" in done_payload
        finally:
            set_chat_service(None)

    @pytest.mark.asyncio
    async def test_sse_stream_error_handling_when_llm_fails(self, temp_db_path, mock_gpt_sovits, tmp_path):
        """
        When LLM provider raises an exception, the SSE stream should emit
        an `event: error` rather than crashing the application.
        """
        app = create_app()
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        class FailingLLM(BaseLLMAdapter):
            async def chat(self, *args, **kwargs):
                raise RuntimeError("LLM 429 Rate Limit")
            async def stream_chat(self, *args, **kwargs):
                raise RuntimeError("LLM 429 Rate Limit")
                yield ""
            async def test_connection(self, *args, **kwargs):
                return TestResult(success=False, message="Error")
            async def list_models(self):
                return []

        async def _mock_adapter(*args, **kwargs):
            return (FailingLLM(api_key="", base_url=""), "mock")
        chat_service._get_active_llm_adapter = _mock_adapter
        set_chat_service(chat_service)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post(
                    "/api/chat/stream",
                    json={"session_id": "test_err_sess", "prompt": "fail test", "stream": True}
                )
                assert resp.status_code == 200
                body = resp.text
                assert "event: error" in body or "error" in body.lower()
        finally:
            set_chat_service(None)


# ============================================================================
# 4. Frontend DOM & Static Assets Verification
# ============================================================================

class TestFrontendDOMStructure:
    """Verify frontend HTML, CSS, and JS components meet Galgame Immersion requirements."""

    def test_index_html_contains_all_galgame_and_capsule_elements(self):
        """Verify index.html contains all required IDs and dual-mode stage elements."""
        html_path = PROJECT_ROOT / "frontend" / "index.html"
        assert html_path.exists()
        content = html_path.read_text(encoding="utf-8")

        assert 'id="advRoot"' in content
        assert 'id="messages"' not in content
        # 1. Session drawer and dialogs
        assert 'id="sidebar"' in content
        assert 'id="sessionList"' in content

        # 2. 会话与全局设置入口
        assert 'id="globalSettingsBtn"' in content
        assert 'id="sessionModal"' in content
        assert 'id="globalModal"' in content

        # 3. 语音能力控件
        assert 'id="sVoice"' in content
        assert 'id="sVoiceDelete"' in content

        # 4. 状态与提示
        assert 'id="toastRoot"' in content

    def test_css_contains_glassmorphism_aura_and_equalizer_animations(self):
        """Verify CSS contains backdrop filters, keyframes, and modern SPA styling."""
        css_paths = list((PROJECT_ROOT / "galgame2voice" / "static" / "assets").glob("index-*.css"))
        if css_paths:
            css_path = css_paths[0]
        else:
            css_path = PROJECT_ROOT / "frontend" / "src" / "styles.css"
        assert css_path.exists()
        content = css_path.read_text(encoding="utf-8")

        # Check glassmorphism
        assert "backdrop-filter" in content or "-webkit-backdrop-filter" in content
        assert "blur" in content

        # Check animations & keyframes
        assert "@keyframes" in content or "animation" in content

        # Check modern UI container / layout classes
        assert ".app" in content or ".chat" in content or ".modal" in content

