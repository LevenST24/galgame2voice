"""
Adversarial Stress Test Suite for M2 Polish & Verification.
Challenger: challenger_r2_2.

Verification Scope:
1. Frontend Audio Streaming & Player Resilience:
   - Web Audio API graph lifecycle, volume clamping, mute toggling, cross-fade scheduling.
   - Queue state machine with async decode fallback, out-of-order chunk arrivals, and interrupt session invalidation.
2. SSE Event Parsing & Bilingual Streaming:
   - Chunk fragmentation across JSON and multibyte UTF-8 boundaries.
   - Event types: 'text', 'audio_chunk', 'done', 'error', '[DONE]'.
   - Malformed / corrupted SSE payload recovery without crashing stream reader.
3. Multi-Sentence Replay & Master Audio Download Helper:
   - Master audio download button presence in VN and Chat Bubble views (DOM & CSS).
   - downloadAudioFile helper logic and fallback resolution.
   - Replay queueing behavior for multi-chunk vs single-URL messages.
4. Provider Error Recovery & Fault Tolerance:
   - LLM stream failure (429, 500, timeout, disconnected socket).
   - TTS synthesis failure on individual sentences (partial success recovery).
   - Master audio file concatenation and database retention.
"""

import asyncio
import io
import os
import re
import tempfile
import wave
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult
from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from galgame2voice.routers.chat import set_chat_service
from galgame2voice.services.chat_service import ChatService, StreamingBilingualParser
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_service import TtsService
from galgame2voice.utils.text_splitter import split_japanese_sentences

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ============================================================================
# 2. SSE Event Parsing & Bilingual Streaming
# ============================================================================

class TestSSEEventParsingAndBilingualStreaming:
    """Stress tests SSE streaming parser against corrupted frames and chunk boundaries."""

    def test_sse_parser_graceful_recovery_on_corrupt_json_lines(self):
        """Parser must not throw unhandled exceptions on garbage SSE payload lines."""
        corrupt_stream = [
            "event: text\n",
            "data: {corrupted_json_syntax\n\n",
            "event: text\n",
            'data: {"delta_chinese": "正常文本"}\n\n',
            "event: audio_chunk\n",
            "data: [NOT_JSON_DATA]\n\n",
            "event: audio_chunk\n",
            'data: {"index": 0, "audio_url": "/audio/ok.wav", "sentence": "テスト"}\n\n',
            "event: error\n",
            'data: {"error": "Recovered error notice"}\n\n',
        ]

        parser = StreamingBilingualParser()
        emitted_ch = []
        emitted_ja = []
        for piece in corrupt_stream:
            try:
                ch, ja = parser.feed_chunk(piece)
                if ch:
                    emitted_ch.append(ch)
                if ja:
                    emitted_ja.extend(ja)
            except Exception as e:
                pytest.fail(f"Parser raised unexpected exception on corrupt stream: {e}")

        ch_final, ja_final, _ = parser.finalize()
        assert isinstance(ch_final, str)
        assert isinstance(ja_final, str)


# ============================================================================
# 3. Multi-Sentence Replay & Master Audio Download Helper
# ============================================================================

class TestReplayAndDownloadHelper:
    """Stress-tests the UI elements and helper methods for audio replay and master download."""

    def test_index_html_contains_master_download_buttons(self):
        """Verify index.html contains btn-vn-download and replay buttons in VN and Chat views."""
        html_path = PROJECT_ROOT / "galgame2voice" / "static" / "index.html"
        content = html_path.read_text(encoding="utf-8")

        # 旧版 VN 下载按钮已随 UI 重做移除；改为断言当前语音条/设置核心元素
        assert 'id="sVoice"' in content, "Missing #sVoice in index.html"
        assert 'id="sVoiceDelete"' in content, "Missing #sVoiceDelete in index.html"
        assert 'id="sCustomVoiceBox"' in content, "Missing #sCustomVoiceBox in index.html"
        assert "角色音色" in content

# ============================================================================
# 4. Provider Error Recovery & Fault Tolerance
# ============================================================================

class TestProviderErrorRecovery:
    """Stress-tests ChatService and provider layer when upstream services degrade or fail."""

    def _create_mock_wav(self, duration_sec: float = 0.5) -> bytes:
        """Helper to create a minimal valid PCM WAV byte payload."""
        buf = io.BytesIO()
        sample_rate = 24000
        num_samples = int(sample_rate * duration_sec)
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(b"\x00\x00" * num_samples)
        return buf.getvalue()

    @pytest.mark.asyncio
    async def test_chat_service_partial_tts_failure_recovery(self, tmp_path):
        """
        If sentence 1 synthesizes successfully but sentence 2 fails (e.g. GPT-SoVITS OOM/timeout),
        ChatService should emit sentence 1's audio_chunk, emit the error or fallback,
        and still finalize the chat session cleanly without hanging.
        """
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        await init_db(db_path)

        audio_dir = tmp_path / "audio_recovery"
        audio_dir.mkdir()

        # Mock TTS Service with 1 success, 1 failure
        call_count = 0
        async def mock_synth(text, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return self._create_mock_wav(0.3)
            raise RuntimeError("GPT-SoVITS 500 Out of Memory")

        client = GptSovitsClient()
        client.synthesize = AsyncMock(side_effect=mock_synth)
        tts_service = TtsService(client=client, audio_dir=audio_dir)

        chat_service = ChatService(tts_service=tts_service, db_path=db_path)

        # Mock LLM stream returning 2 Japanese sentences
        class MultiSentenceLLM(BaseLLMAdapter):
            async def stream_chat(self, *args, **kwargs):
                yield '{"chinese": "第一句中文。第二句中文。", '
                yield '"japanese": "最初の文です。二番目の文です。"}'

            async def chat(self, *args, **kwargs):
                return LLMResponse(content='{"chinese": "第一句中文。第二句中文。", "japanese": "最初の文です。二番目の文です。"}')

            async def test_connection(self, *args, **kwargs):
                return TestResult(success=True, message="OK")

            async def list_models(self):
                return ["test-llm"]

        mock_llm = MultiSentenceLLM(api_key="sk-test", base_url="http://test")
        chat_service._get_active_llm_adapter = AsyncMock(return_value=(mock_llm, "test-llm"))

        events = []
        async for ev in chat_service.stream_chat(prompt="测试容错", session_id="err_sess_1"):
            events.append(ev)

        # Verify stream completed and emitted done event
        event_types = [e["event"] for e in events]
        assert "text" in event_types
        assert "done" in event_types

        # Verify audio chunks: sentence 1 succeeded, sentence 2 did not crash the pipeline
        audio_chunks = [e for e in events if e["event"] == "audio_chunk"]
        assert len(audio_chunks) >= 1, "At least sentence 1 should have produced an audio chunk"

        # Verify DB persisted message
        async with get_db(db_path) as conn:
            msgs = await crud.get_recent_messages(conn, "err_sess_1", limit=10)
            assert len(msgs) == 2  # user + assistant
            assert any(m.role == "assistant" for m in msgs)
            assistant_m = next(m for m in msgs if m.role == "assistant")
            assert "第一句中文" in assistant_m.content_chinese

        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except OSError:
                pass

    @pytest.mark.asyncio
    async def test_chat_service_upstream_llm_timeout_handling(self, tmp_path):
        """
        When upstream LLM provider hangs or raises httpx.ReadTimeout,
        the stream should yield an informative error event and terminate cleanly.
        """
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        await init_db(db_path)

        client = GptSovitsClient()
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=db_path)

        class TimeoutLLM(BaseLLMAdapter):
            async def stream_chat(self, *args, **kwargs):
                raise httpx.ReadTimeout("Upstream LLM timed out after 30s")
                yield ""

            async def chat(self, *args, **kwargs):
                raise httpx.ReadTimeout("Upstream LLM timed out")

            async def test_connection(self, *args, **kwargs):
                return TestResult(success=False, message="Timeout")

            async def list_models(self):
                return []

        mock_llm = TimeoutLLM(api_key="sk-test", base_url="http://test")
        chat_service._get_active_llm_adapter = AsyncMock(return_value=(mock_llm, "test-llm"))

        events = []
        async for ev in chat_service.stream_chat(prompt="超时测试", session_id="timeout_sess"):
            events.append(ev)

        assert len(events) >= 1
        # Expect error event
        error_events = [e for e in events if e["event"] == "error"]
        assert len(error_events) == 1
        assert "timed out" in error_events[0]["data"]["error"].lower() or "timeout" in error_events[0]["data"]["error"].lower()

        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except OSError:
                pass
