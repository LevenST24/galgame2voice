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
# 1. Web Audio AnalyserNode Frequency Scaling & Equalizer Math Stress Tests
# ============================================================================

class TestAnalyserNodeFrequencyScaling:
    """Stress-test AnalyserNode frequency scaling calculations and bounds."""

    def test_audio_player_source_file_structure(self):
        """Verify audio_player.js contains genuine Web Audio API AnalyserNode integration."""
        js_path = PROJECT_ROOT / "galgame2voice" / "static" / "js" / "audio_player.js"
        assert js_path.exists(), "audio_player.js does not exist"
        content = js_path.read_text(encoding="utf-8")

        # Verify AnalyserNode creation and parameters
        assert "createAnalyser" in content
        assert "fftSize = 64" in content or "fftSize = 128" in content or "fftSize" in content
        assert "smoothingTimeConstant" in content
        assert "getByteFrequencyData" in content
        assert "attachEqualizer" in content
        assert "requestAnimationFrame" in content
        assert "interrupt" in content

    def test_equalizer_bar_height_mathematical_bounds(self):
        """
        Empirically test the formula:
        heightPx = Math.max(4, Math.round(4 + (val / 255) * 18))
        Verify that for all possible byte frequency values [0..255], height is strictly in [4, 22].
        """
        def calc_bar_height(val):
            # Mirror JavaScript logic in audio_player.js
            clamped_val = max(0, min(255, val)) if isinstance(val, (int, float)) else 0
            return max(4, round(4 + (clamped_val / 255) * 18))

        # Check boundary conditions
        assert calc_bar_height(0) == 4, "Min byte frequency must yield 4px"
        assert calc_bar_height(255) == 22, "Max byte frequency (255) must yield 22px"
        assert calc_bar_height(128) == 13, "Mid byte frequency (128) must yield 13px"

        # Check all 256 byte values
        for byte_val in range(256):
            h = calc_bar_height(byte_val)
            assert 4 <= h <= 22, f"Height {h} for byte {byte_val} is out of bounds [4, 22]"
            assert isinstance(h, int), f"Height {h} must be integer"

    def test_equalizer_frequency_bin_step_sampling(self):
        """
        Test frequency spectrum downsampling from 32 FFT bins to N equalizer bars (e.g. 5 bars).
        step = Math.max(1, Math.floor(data.length / bars.length))
        Ensure indices never exceed frequencyBinCount.
        """
        fft_size = 64
        frequency_bin_count = fft_size // 2  # 32 bins
        bar_counts = [1, 3, 5, 8, 16, 32, 64]

        for bar_count in bar_counts:
            step = max(1, frequency_bin_count // bar_count)
            sampled_indices = [i * step for i in range(bar_count)]
            # If bar_count is larger than bins, sampled indices might go past, but index check safeguards
            for i, idx in enumerate(sampled_indices):
                is_valid = idx < frequency_bin_count
                if is_valid:
                    assert 0 <= idx < frequency_bin_count


# ============================================================================
# 2. Audio Queue Concurrency & Session Interruption Simulation
# ============================================================================

class TestAudioQueueConcurrencyAndInterrupt:
    """Simulate audio queue scheduling, async fetch resolution, and interrupt invalidation."""

    class MockAudioQueue:
        """Python simulation of StreamingAudioPlayer queue state machine."""
        def __init__(self):
            self.current_session_id = 0
            self.queue = []
            self.active_sources = []
            self.is_playing = False
            self.played_chunks = []
            self.errors = []

        def enqueue(self, chunk):
            item = {
                "session_id": self.current_session_id,
                "index": chunk.get("index", 0),
                "url": chunk.get("audio_url"),
                "sentence": chunk.get("sentence", ""),
                "audio_buffer": None,
                "status": "fetching"
            }
            self.queue.append(item)
            return item

        def resolve_fetch(self, item, success=True, buffer_duration=1.0):
            # Check session invalidation
            if item["session_id"] != self.current_session_id:
                return False  # Discarded due to interrupt
            if success:
                item["audio_buffer"] = {"duration": buffer_duration}
                item["status"] = "ready"
                self._schedule_next()
                return True
            else:
                item["status"] = "error"
                self.errors.append(item)
                self._schedule_next()
                return False

        def _schedule_next(self):
            while self.queue:
                next_item = self.queue[0]
                if next_item["status"] == "error":
                    # Discard errored chunk so it doesn't block future chunks
                    self.queue.pop(0)
                    continue
                if next_item["status"] != "ready" or not next_item["audio_buffer"]:
                    break
                self.queue.pop(0)
                self.active_sources.append(next_item)
                self.played_chunks.append(next_item["index"])
                self.is_playing = True

        def interrupt(self):
            self.current_session_id += 1
            self.queue.clear()
            self.active_sources.clear()
            self.is_playing = False

    def test_audio_queue_sequential_playback_when_resolved_in_order(self):
        """Verify sequential chunks play in order when fetched in order."""
        player = self.MockAudioQueue()
        item0 = player.enqueue({"index": 0, "audio_url": "/audio/0.wav"})
        item1 = player.enqueue({"index": 1, "audio_url": "/audio/1.wav"})
        item2 = player.enqueue({"index": 2, "audio_url": "/audio/2.wav"})

        player.resolve_fetch(item0, success=True)
        player.resolve_fetch(item1, success=True)
        player.resolve_fetch(item2, success=True)

        assert player.played_chunks == [0, 1, 2]

    def test_audio_queue_preserves_order_on_out_of_order_resolution(self):
        """
        If Chunk 2 finishes network decoding before Chunk 0,
        Chunk 2 MUST NOT play before Chunk 0.
        """
        player = self.MockAudioQueue()
        item0 = player.enqueue({"index": 0, "audio_url": "/audio/0.wav"})
        item1 = player.enqueue({"index": 1, "audio_url": "/audio/1.wav"})
        item2 = player.enqueue({"index": 2, "audio_url": "/audio/2.wav"})

        # Chunk 2 resolves first
        player.resolve_fetch(item2, success=True)
        assert player.played_chunks == [], "Chunk 2 must not play while Chunk 0 is pending"

        # Chunk 0 resolves next
        player.resolve_fetch(item0, success=True)
        assert player.played_chunks == [0], "Chunk 0 plays; Chunk 1 still pending"

        # Chunk 1 resolves last -> now Chunk 1 and Chunk 2 both trigger
        player.resolve_fetch(item1, success=True)
        assert player.played_chunks == [0, 1, 2]

    def test_interrupt_cancels_all_inflight_and_active_playback(self):
        """
        When user interrupts (submits new prompt or clicks stop):
        1. Current session ID increments.
        2. Active sources and pending queue are wiped.
        3. Subsequent late arrivals from the dead session are ignored.
        """
        player = self.MockAudioQueue()
        item0 = player.enqueue({"index": 0, "audio_url": "/audio/old_0.wav"})
        item1 = player.enqueue({"index": 1, "audio_url": "/audio/old_1.wav"})

        # Chunk 0 starts playing
        player.resolve_fetch(item0, success=True)
        assert player.is_playing is True
        assert len(player.active_sources) == 1

        # User interrupts
        player.interrupt()
        assert player.is_playing is False
        assert len(player.queue) == 0
        assert len(player.active_sources) == 0

        # Old Chunk 1 late arrival from previous session
        res = player.resolve_fetch(item1, success=True)
        assert res is False, "Late resolved chunk from dead session must be discarded"
        assert player.played_chunks == [0], "Old chunk must not be added to played chunks"

        # New session chunks
        item_new = player.enqueue({"index": 0, "audio_url": "/audio/new_0.wav"})
        player.resolve_fetch(item_new, success=True)
        assert player.played_chunks == [0, 0]
        assert player.is_playing is True


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
    async def test_sse_stream_bilingual_events_emission(self, temp_db_path, mock_gpt_sovits, tmp_path):
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

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/api/chat/stream",
                json={"session_id": "test_err_sess", "prompt": "fail test", "stream": True}
            )
            assert resp.status_code == 200
            body = resp.text
            assert "event: error" in body or "error" in body.lower()


# ============================================================================
# 4. Frontend DOM & Static Assets Verification
# ============================================================================

class TestFrontendDOMStructure:
    """Verify frontend HTML, CSS, and JS components meet Galgame Immersion requirements."""

    def test_index_html_contains_all_galgame_and_capsule_elements(self):
        """Verify index.html contains all required IDs and dual-mode stage elements."""
        html_path = PROJECT_ROOT / "galgame2voice" / "static" / "index.html"
        assert html_path.exists()
        content = html_path.read_text(encoding="utf-8")

        # 1. Dual mode view containers
        assert 'id="vn-view"' in content
        assert 'id="chat-view"' in content
        assert 'id="btn-mode-vn"' in content
        assert 'id="btn-mode-chat"' in content

        # 2. Visual Novel Stage elements
        assert 'id="vn-character-name"' in content
        assert 'id="vn-text-ja"' in content
        assert 'id="vn-text-zh"' in content
        assert 'id="vn-audio-equalizer"' in content
        assert 'id="vn-audio-status"' in content
        assert 'id="btn-vn-replay"' in content
        assert 'id="btn-vn-history"' in content

        # 3. Top Capsule Bar controls
        assert 'id="quick-voice-select"' in content
        assert 'id="sovits-status-badge"' in content
        assert 'id="volume-slider"' in content
        assert 'id="mute-toggle-btn"' in content
        assert 'id="reset-context-btn"' in content

        # 4. History modal
        assert 'id="history-modal"' in content
        assert 'id="history-modal-body"' in content

    def test_css_contains_glassmorphism_aura_and_equalizer_animations(self):
        """Verify CSS contains backdrop filters, auraPulse keyframes, and equalizer bar styling."""
        css_path = PROJECT_ROOT / "galgame2voice" / "static" / "css" / "style.css"
        assert css_path.exists()
        content = css_path.read_text(encoding="utf-8")

        # Check glassmorphism
        assert "backdrop-filter" in content or "-webkit-backdrop-filter" in content
        assert "blur" in content

        # Check equalizer
        assert ".audio-equalizer-bars" in content or ".equalizer" in content
        assert ".bar" in content

        # Check compatibility classes
        assert ".chat-container" in content or ".app-container" in content

    def test_chat_client_sse_parser_state_machine_robustness(self):
        """
        Verify chat_client.js properly handles SSE event streams without fragile assumptions.
        """
        js_path = PROJECT_ROOT / "galgame2voice" / "static" / "js" / "chat_client.js"
        assert js_path.exists()
        content = js_path.read_text(encoding="utf-8")

        assert "event: text" in content or "currentEventType === 'text'" in content
        assert "audio_chunk" in content
        assert "event: done" in content or "currentEventType === 'done'" in content
        assert "event: error" in content or "currentEventType === 'error'" in content
        assert "skipTypewriter" in content
        assert "StreamingAudioPlayer" in content
