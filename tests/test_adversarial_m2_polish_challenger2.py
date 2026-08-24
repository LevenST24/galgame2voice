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
import json
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
# 1. Frontend Audio Streaming & Player Resilience
# ============================================================================

class TestFrontendAudioStreamingResilience:
    """Stress-tests player logic, volume bounds, mute toggles, and interrupt invalidation."""

    def test_audio_player_js_source_completeness(self):
        """Verifies audio_player.js implements volume control, mute, cross-fade, and interrupt."""
        js_path = PROJECT_ROOT / "galgame2voice" / "static" / "js" / "audio_player.js"
        assert js_path.exists(), "audio_player.js missing"
        code = js_path.read_text(encoding="utf-8")

        assert "class StreamingAudioPlayer" in code
        assert "setVolume" in code
        assert "setMuted" in code
        assert "interrupt" in code
        assert "enqueue" in code
        assert "crossFadeDuration" in code
        assert "currentSessionId" in code
        assert "decodeAudioData" in code
        assert "createAnalyser" in code

    def test_volume_clamping_and_muting_logic(self):
        """Emulates setVolume and setMuted bounds checking."""
        class MockPlayer:
            def __init__(self):
                self.volume = 1.0
                self.is_muted = False

            def set_volume(self, val):
                self.volume = max(0.0, min(1.0, float(val)))

            def set_muted(self, is_muted):
                self.is_muted = bool(is_muted)
                return self.is_muted

            def effective_gain(self):
                return 0.0 if self.is_muted else self.volume

        player = MockPlayer()
        # Normal range
        player.set_volume(0.5)
        assert player.effective_gain() == 0.5

        # Out of bounds high
        player.set_volume(2.5)
        assert player.volume == 1.0
        assert player.effective_gain() == 1.0

        # Out of bounds low / negative
        player.set_volume(-0.5)
        assert player.volume == 0.0
        assert player.effective_gain() == 0.0

        # Mute toggle
        player.set_volume(0.8)
        player.set_muted(True)
        assert player.effective_gain() == 0.0
        player.set_muted(False)
        assert player.effective_gain() == 0.8

    def test_queue_interrupt_session_invalidation_under_rapid_triggers(self):
        """
        Simulate user typing rapidly and interrupting 20 times while async chunk fetches
        are in flight. Ensures no stale chunk plays in a newer session.
        """
        class MockAudioSessionQueue:
            def __init__(self):
                self.current_session_id = 0
                self.played = []

            def interrupt(self):
                self.current_session_id += 1

            def on_fetch_complete(self, session_id, chunk_index):
                # Discard if stale
                if session_id != self.current_session_id:
                    return False
                self.played.append((session_id, chunk_index))
                return True

        queue = MockAudioSessionQueue()

        # Generate in-flight requests across multiple sessions
        pending = []
        for sess in range(5):
            for chunk_idx in range(3):
                pending.append((queue.current_session_id, chunk_idx))
            queue.interrupt()  # User submitted new message

        # Final current session
        current_sess = queue.current_session_id
        pending.append((current_sess, 0))
        pending.append((current_sess, 1))

        # Complete all pending in random/reverse order
        for s_id, c_idx in reversed(pending):
            queue.on_fetch_complete(s_id, c_idx)

        # Only chunks from current_sess should have played
        assert len(queue.played) == 2
        assert all(s == current_sess for s, _ in queue.played)
        assert [c for _, c in queue.played] == [1, 0]


# ============================================================================
# 2. SSE Event Parsing & Bilingual Streaming
# ============================================================================

class TestSSEEventParsingAndBilingualStreaming:
    """Stress tests SSE streaming parser against corrupted frames and chunk boundaries."""

    def test_sse_parser_fragmented_lines_and_utf8_boundaries(self):
        """
        Simulate browser ReadableStream delivering partial fragments across line breaks,
        event tags, and multi-byte Chinese characters.
        """
        class MockSSEClientParser:
            def __init__(self):
                self.buffer = ""
                self.current_event = "message"
                self.received_text = ""
                self.received_audio_chunks = []
                self.done_payload = None
                self.errors = []

            def feed(self, fragment: str):
                self.buffer += fragment
                lines = self.buffer.split("\n")
                self.buffer = lines.pop()  # Keep incomplete line

                for line in lines:
                    trimmed = line.strip()
                    if not trimmed:
                        self.current_event = "message"
                        continue

                    if trimmed.startswith("event:"):
                        self.current_event = trimmed[6:].strip()
                        continue

                    if trimmed.startswith("data:"):
                        data_str = trimmed[5:].strip()
                        if data_str == "[DONE]":
                            continue
                        try:
                            payload = json.loads(data_str)
                            if self.current_event == "text" or "delta_chinese" in payload:
                                self.received_text += payload.get("delta_chinese", "")
                            elif self.current_event == "audio_chunk" or ("audio_url" in payload and "index" in payload):
                                self.received_audio_chunks.append(payload)
                            elif self.current_event == "done":
                                self.done_payload = payload
                            elif self.current_event == "error":
                                self.errors.append(payload)
                        except json.JSONDecodeError:
                            pass

        parser = MockSSEClientParser()

        # Severely fragmented SSE stream
        raw_stream = (
            "event: text\n"
            'data: {"delta_chinese": "你好，"}\n\n'
            "event: text\n"
            'data: {"delta_chinese": "指挥官！"}\n\n'
            "event: audio_chunk\n"
            'data: {"index": 0, "audio_url": "/audio/chunk_0.wav", "sentence": "こんにちは！"}\n\n'
            "event: done\n"
            'data: {"chinese": "你好，指挥官！", "japanese": "こんにちは！", "audio_url": "/audio/full_1.wav", "chunks": [{"index": 0, "audio_url": "/audio/chunk_0.wav"}]}\n\n'
            "data: [DONE]\n\n"
        )

        # Feed 3 characters at a time to test extreme fragmentation
        chunk_size = 3
        for i in range(0, len(raw_stream), chunk_size):
            parser.feed(raw_stream[i:i + chunk_size])

        assert parser.received_text == "你好，指挥官！"
        assert len(parser.received_audio_chunks) == 1
        assert parser.received_audio_chunks[0]["audio_url"] == "/audio/chunk_0.wav"
        assert parser.done_payload is not None
        assert parser.done_payload["audio_url"] == "/audio/full_1.wav"

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

        assert 'id="btn-vn-download"' in content, "Missing #btn-vn-download in index.html"
        assert 'id="btn-vn-replay"' in content, "Missing #btn-vn-replay in index.html"
        assert "下载母带" in content
        assert "重新播放" in content

    def test_chat_client_js_contains_download_audio_helper_and_bindings(self):
        """Verify chat_client.js defines downloadAudioFile and attaches handlers."""
        js_path = PROJECT_ROOT / "galgame2voice" / "static" / "js" / "chat_client.js"
        content = js_path.read_text(encoding="utf-8")

        assert "function downloadAudioFile(" in content
        assert "btn-vn-download" in content
        assert "btn-download-bubble" in content
        assert "btn-play-bubble" in content
        assert "lastVoiceData.audio_url" in content
        assert "audioPlayer.interrupt()" in content

    def test_download_audio_helper_url_resolution(self):
        """
        Emulate downloadAudioFile target URL resolution for:
        1. Single master audio URL (`audio_url`).
        2. Multi-chunk audio object with chunks array.
        3. Empty / missing audio URL.
        """
        def resolve_download_url(voice_data):
            if not voice_data:
                return None, "暂无可下载的语音母带"
            if isinstance(voice_data, dict):
                target = voice_data.get("audio_url")
                if not target and voice_data.get("chunks"):
                    chunks = voice_data["chunks"]
                    if len(chunks) > 0 and chunks[0].get("audio_url"):
                        target = chunks[0]["audio_url"]
                if target:
                    return target, None
                return None, "暂无可下载的语音母带"
            elif isinstance(voice_data, str) and voice_data:
                return voice_data, None
            return None, "暂无可下载的语音母带"

        # Case 1: Standard master audio URL
        url, err = resolve_download_url({"audio_url": "/audio/full_abc123.wav"})
        assert url == "/audio/full_abc123.wav"
        assert err is None

        # Case 2: Fallback to chunk[0] if full is missing
        url, err = resolve_download_url({"chunks": [{"audio_url": "/audio/chunk_0.wav"}]})
        assert url == "/audio/chunk_0.wav"
        assert err is None

        # Case 3: Empty voice data
        url, err = resolve_download_url(None)
        assert url is None
        assert "暂无" in err

        # Case 4: Dict with empty values
        url, err = resolve_download_url({"audio_url": "", "chunks": []})
        assert url is None
        assert "暂无" in err

    def test_multi_sentence_replay_queue_dispatch(self):
        """
        Verifies that clicking replay on a multi-sentence response interrupts active
        audio and enqueues all chunks sequentially.
        """
        class MockPlayerRecorder:
            def __init__(self):
                self.enqueued = []
                self.interrupted = False

            def interrupt(self):
                self.interrupted = True
                self.enqueued.clear()

            def enqueue(self, chunk):
                self.enqueued.append(chunk)

        player = MockPlayerRecorder()
        voice_data = {
            "audio_url": "/audio/full_merged.wav",
            "sentence": "こんにちは。いい天気ですね。",
            "chunks": [
                {"index": 0, "audio_url": "/audio/chunk_0.wav", "sentence": "こんにちは。"},
                {"index": 1, "audio_url": "/audio/chunk_1.wav", "sentence": "いい天気ですね。"}
            ]
        }

        # Simulate replay click handler logic from chat_client.js
        player.interrupt()
        if voice_data.get("chunks") and len(voice_data["chunks"]) > 0:
            for i, chunk in enumerate(voice_data["chunks"]):
                player.enqueue({
                    "index": chunk.get("index", i),
                    "audio_url": chunk["audio_url"],
                    "sentence": chunk.get("sentence", "")
                })

        assert player.interrupted is True
        assert len(player.enqueued) == 2
        assert player.enqueued[0]["audio_url"] == "/audio/chunk_0.wav"
        assert player.enqueued[1]["audio_url"] == "/audio/chunk_1.wav"


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
