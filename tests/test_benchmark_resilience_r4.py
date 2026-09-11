"""
Quantitative Benchmark & Resilience Test Suite (Milestone 7 / R4).
Verifies:
1. R1 Agile First-Sentence Chunking & Latency Benchmark:
   - split_japanese_sentences agile clause pause splitting (、，,) with min_chars=6 on chunk 0.
   - StreamingBilingualParser immediate chunk 0 emission upon clause pause.
   - Backwards compatibility for single sentences and subsequent chunks.
   - Quantitative latency / token reduction comparison benchmark.
2. R1 GPT-SoVITS Asynchronous Warm-up:
   - VoiceManager.warmup_current_profile() execution and probe synthesis.
   - Graceful offline and network timeout resilience.
3. R2 Frontend Audio Player Static Integrity:
   - frontend/src/audio_player.js Web Audio API AudioContext, sample-accurate scheduling,
     12ms micro-fade envelope, 40ms linear fade-out interrupt.
   - galgame2voice/static/js/audio_player.js static preservation.
4. R2 Interrupt & Cancellation Timing:
   - stream_chat cancel event responsiveness and queue/lock release latency < 100ms.
5. R3 50+ Turn Conversation Memory Stability & VRAM Watermark Guard:
   - 50 continuous turns bilingual dialogue memory stability (RSS drift < 35MB).
   - VoiceManager._check_vram_guard() floor threshold (0.45GB) & InsufficientMemoryError.
6. R3 SSE Keep-Alive Heartbeat:
   - stream_chat() and stream_chat_events() W3C keep-alive comment (: keep-alive\\n\\n)
     emission on >= 5.0s queue silence.
7. R3 Frontend Bounded Blob URL LRU Store:
   - frontend/src/voice.js BoundedAudioStore with MAX_AUDIO_STORE_ENTRIES = 30 and
     automatic URL.revokeObjectURL on eviction/deletion/clear.
8. R4 SQLite WAL Concurrency:
   - Concurrent read/write burst transactions in WAL mode with zero database locked errors.
"""

import asyncio
import gc
import json
import os
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest
import psutil

from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate, VoiceProfileCreate
from galgame2voice.database.session import get_db, immediate_transaction
from galgame2voice.services.chat_service import ChatService, SseKeepAlive
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.voice_manager import VoiceManager, InsufficientMemoryError
from galgame2voice.utils.text_splitter import split_japanese_sentences
from tests.conftest import MockGptSovitsServer


# ============================================================================
# Helpers & Mock Adapters
# ============================================================================

class MockFastBilingualLLMAdapter(BaseLLMAdapter):
    """Fast mock LLM adapter that yields complete bilingual JSON tokens without delay."""

    def __init__(self, chinese: str = "你好，很高兴见到你！", japanese: str = "こんにちは、お会いできて嬉しいです！"):
        super().__init__(api_key="sk-mock-fast", base_url="http://mock-fast")
        self.chinese = chinese
        self.japanese = japanese

    async def chat(self, messages: List[ChatMessage], model: str, **kwargs: Any) -> LLMResponse:
        content = json.dumps({"chinese": self.chinese, "japanese": self.japanese}, ensure_ascii=False)
        return LLMResponse(content=content)

    async def stream_chat(self, messages: List[ChatMessage], model: str, **kwargs: Any) -> AsyncIterator[str]:
        tokens = [
            f'{{"chinese": "{self.chinese}"',
            f', "japanese": "{self.japanese}"}}'
        ]
        for t in tokens:
            yield t

    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        return TestResult(success=True, message="OK", latency_ms=1.0)

    async def list_models(self) -> List[str]:
        return ["mock-fast-model"]


class MockSlowCancellableLLMAdapter(BaseLLMAdapter):
    """Slow streaming LLM adapter designed to test cancellation responsiveness."""

    def __init__(self):
        super().__init__(api_key="sk-mock-slow", base_url="http://mock-slow")
        self.started_event = asyncio.Event()

    async def chat(self, messages: List[ChatMessage], model: str, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content='{"chinese": "你好", "japanese": "こんにちは"}')

    async def stream_chat(self, messages: List[ChatMessage], model: str, **kwargs: Any) -> AsyncIterator[str]:
        yield '{"chinese": "你好，指挥官！", "japanese": "こんにちは、'
        self.started_event.set()
        for i in range(100):
            await asyncio.sleep(0.02)
            yield f'トークン{i}、'
        yield 'これで終わりです。"}'

    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        return TestResult(success=True, message="OK", latency_ms=1.0)

    async def list_models(self) -> List[str]:
        return ["mock-slow-model"]


class MockPausingLLMAdapter(BaseLLMAdapter):
    """Adapter that pauses mid-stream to trigger SSE keep-alive heartbeats."""

    def __init__(self, pause_seconds: float = 0.6):
        super().__init__(api_key="sk-mock-pause", base_url="http://mock-pause")
        self.pause_seconds = pause_seconds
        self.paused_event = asyncio.Event()

    async def chat(self, messages: List[ChatMessage], model: str, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content='{"chinese": "你好", "japanese": "こんにちは"}')

    async def stream_chat(self, messages: List[ChatMessage], model: str, **kwargs: Any) -> AsyncIterator[str]:
        yield '{"chinese": "你好", "japanese": "'
        self.paused_event.set()
        await asyncio.sleep(self.pause_seconds)
        yield 'こんにちは！"}'

    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        return TestResult(success=True, message="OK", latency_ms=1.0)

    async def list_models(self) -> List[str]:
        return ["mock-pause-model"]


# ============================================================================
# 1. R1 Agile First-Sentence Chunking & Latency Benchmark
# ============================================================================

class TestR1AgileFirstSentenceChunkingAndLatency:
    """Verifies agile clause pauses (、，,) splitting on chunk 0 and latency reduction."""

    def test_r1_split_japanese_sentences_clause_pauses(self):
        """Verify split_japanese_sentences splits on clause pauses for chunk 0 when len >= 6."""
        # Japanese comma '、' with len >= 6
        text_ja = "こんにちは、先生、今日はいい天気ですね。"
        sents = split_japanese_sentences(text_ja, is_first_chunk=True, min_chars=6)
        assert len(sents) == 2
        assert sents[0] == "こんにちは、"
        assert sents[1] == "先生、今日はいい天気ですね。"

        # Fullwidth Chinese comma '，' with len >= 6
        text_cn = "初めまして，よろしくお願いします。"
        sents_cn = split_japanese_sentences(text_cn, is_first_chunk=True, min_chars=6)
        assert len(sents_cn) == 2
        assert sents_cn[0] == "初めまして，"
        assert sents_cn[1] == "よろしくお願いします。"

        # Standard ASCII comma ',' with len >= 6
        text_ascii = "Hello world,今日も一日頑張りましょう。"
        sents_ascii = split_japanese_sentences(text_ascii, is_first_chunk=True, min_chars=6)
        assert len(sents_ascii) == 2
        assert sents_ascii[0] == "Hello world,"
        assert sents_ascii[1] == "今日も一日頑張りましょう。"

    def test_r1_split_japanese_sentences_backwards_compatibility(self):
        """Verify is_first_chunk=False strictly preserves clauses and only terminal punctuation splits."""
        text = "こんにちは、先生、今日はいい天気ですね。一緒に出かけましょう？"
        # Strict mode (default / is_first_chunk=False)
        strict_sents = split_japanese_sentences(text, is_first_chunk=False)
        assert len(strict_sents) == 2
        assert strict_sents[0] == "こんにちは、先生、今日はいい天気ですね。"
        assert strict_sents[1] == "一緒に出かけましょう？"

        # Subsequent chunks under is_first_chunk=True preserve terminal punctuation
        sents_all = split_japanese_sentences(text, is_first_chunk=True, min_chars=6)
        assert len(sents_all) == 3
        assert sents_all[0] == "こんにちは、"
        assert sents_all[1] == "先生、今日はいい天気ですね。"
        assert sents_all[2] == "一緒に出かけましょう？"

    def test_r1_split_short_clause_no_premature_split(self):
        """Clause pauses with length < min_chars must NOT split prematurely."""
        # 'はい、' length is 3 < 6 -> must not split on 'はい、'
        short_clause = "はい、先生、今日はいい天気ですね。"
        sents = split_japanese_sentences(short_clause, is_first_chunk=True, min_chars=6)
        # 'はい、先生、' has length 6 -> splits at second comma!
        assert sents[0] == "はい、先生、"
        assert sents[1] == "今日はいい天気ですね。"

        # Terminal punctuation overrides min_chars
        terminal_short = "はい！先生、おはようございます。"
        sents_term = split_japanese_sentences(terminal_short, is_first_chunk=True, min_chars=6)
        assert sents_term[0] == "はい！"

    def test_r1_streaming_bilingual_parser_agile_first_chunk(self):
        """Verify StreamingBilingualParser emits chunk 0 immediately upon clause pause."""
        parser = StreamingBilingualParser()
        # Feed chunk 0 containing clause pause with length >= 6
        ch_delta1, ja_sents1 = parser.feed_chunk('{"chinese": "你好", "japanese": "こんにちは、')
        assert ja_sents1 == ["こんにちは、"]
        assert parser.first_sentence_emitted is True

        # Feed chunk 1 with intermediate clause pause: should NOT split on clause pause because chunk 0 already emitted
        ch_delta2, ja_sents2 = parser.feed_chunk('先生、今日はいい天気ですね。')
        assert ja_sents2 == ["先生、今日はいい天気ですね。"]

        ch_full, ja_full, rem = parser.finalize()
        assert ch_full == "你好"
        assert ja_full == "こんにちは、先生、今日はいい天気ですね。"
        assert rem == []

    def test_r1_agile_first_sentence_latency_benchmark(self):
        """
        Quantitative Latency Benchmark:
        Compares character and token wait distance to first audio chunk emission
        between Agile Chunking and Rigid Terminal Chunking.
        """
        token_stream = [
            '{"chinese": "',
            '你好，',
            '今天的天气',
            '真不错。',
            '", "japanese": "',
            'こんにちは、',
            '先生、',
            '今日はいい天気ですね。',
            '"}'
        ]

        # 1. Agile Parser
        agile_parser = StreamingBilingualParser()
        agile_emitted_token_idx = -1
        agile_emitted_chars = -1
        accumulated_ja_len = 0

        for idx, token in enumerate(token_stream):
            _, completed = agile_parser.feed_chunk(token)
            if 'japanese": "' in token:
                accumulated_ja_len += len(token.split('japanese": "')[-1])
            elif agile_parser.japanese_extracted:
                accumulated_ja_len = len(agile_parser.japanese_extracted)

            if completed and agile_emitted_token_idx == -1:
                agile_emitted_token_idx = idx
                agile_emitted_chars = len(completed[0])

        # 2. Strict / Legacy Parser Simulation (waits for rigid terminal punctuation [。！？!?\\n])
        strict_emitted_token_idx = -1
        strict_emitted_chars = -1
        buffer_ja = ""
        ja_started = False
        for idx, token in enumerate(token_stream):
            if 'japanese": "' in token:
                ja_started = True
                buffer_ja += token.split('japanese": "')[-1]
            elif ja_started:
                clean_tok = token.replace('"}', '')
                buffer_ja += clean_tok

            matches = split_japanese_sentences(buffer_ja, is_first_chunk=False)
            if matches and any(re.search(r'[。！？!?\n]$', m) for m in matches):
                strict_emitted_token_idx = idx
                strict_emitted_chars = len(matches[0])
                break

        assert agile_emitted_token_idx != -1
        assert strict_emitted_token_idx != -1
        assert agile_emitted_token_idx < strict_emitted_token_idx

        # Quantitative verification: Agile chunk emits with >= 50% fewer characters
        char_reduction_pct = (1.0 - (agile_emitted_chars / strict_emitted_chars)) * 100.0
        assert char_reduction_pct >= 50.0, (
            f"Expected >= 50% character latency reduction, got {char_reduction_pct:.1f}% "
            f"({agile_emitted_chars} chars vs {strict_emitted_chars} chars)"
        )


# ============================================================================
# 2. R1 GPT-SoVITS Asynchronous Warm-up & Cache Pre-seeding
# ============================================================================

class TestR1GptSovitsWarmup:
    """Verifies VoiceManager.warmup_current_profile() execution and resilience."""

    @pytest.mark.asyncio
    async def test_r1_warmup_current_profile_success(self, temp_db_path, mock_gpt_sovits):
        """Verifies successful asynchronous warm-up calls weight switch and lightweight probe."""
        # Insert active profile in test db
        async with get_db(temp_db_path) as conn:
            profile = await crud.create_voice_profile(conn, VoiceProfileCreate(
                name="WarmupChar",
                gpt_weights_path="weights/warmup.ckpt",
                sovits_weights_path="weights/warmup.pth",
                ref_audio_path="ref/warmup.wav",
                prompt_text="テストプロンプトです。",
                prompt_lang="ja",
                text_lang="ja",
                is_default=True,
            ))

        client = GptSovitsClient(server=mock_gpt_sovits)
        vm = VoiceManager(gpt_sovits_client_or_server=client, db_path=temp_db_path)
        vm.active_profile = profile

        success = await vm.warmup_current_profile()
        assert success is True

        # Verify that weights endpoints and tts probe endpoint were invoked
        endpoints_called = [c["endpoint"] for c in mock_gpt_sovits.call_history]
        assert "/set_gpt_weights" in endpoints_called
        assert "/set_sovits_weights" in endpoints_called
        assert "/set_refer_audio" in endpoints_called
        assert "/tts" in endpoints_called

        # Verify probe text was lightweight punctuation
        tts_calls = [c for c in mock_gpt_sovits.call_history if c["endpoint"] == "/tts"]
        assert len(tts_calls) >= 1
        assert tts_calls[-1]["payload"].get("text") == "。"

    @pytest.mark.asyncio
    async def test_r1_warmup_current_profile_offline_resilience(self, temp_db_path, mock_gpt_sovits):
        """Verifies warm-up fails gracefully without crashing when engine is offline."""
        async with get_db(temp_db_path) as conn:
            profile = await crud.create_voice_profile(conn, VoiceProfileCreate(
                name="OfflineWarmupChar",
                gpt_weights_path="weights/offline.ckpt",
                sovits_weights_path="weights/offline.pth",
                ref_audio_path="ref/offline.wav",
                is_default=True,
            ))

        mock_gpt_sovits.set_online(False)
        client = GptSovitsClient(server=mock_gpt_sovits)
        vm = VoiceManager(gpt_sovits_client_or_server=client, db_path=temp_db_path)
        vm.active_profile = profile

        # Must not raise unhandled exception
        success = await vm.warmup_current_profile()
        assert success is False

    @pytest.mark.asyncio
    async def test_r1_warmup_current_profile_timeout_and_no_profile(self, temp_db_path, mock_gpt_sovits):
        """Verifies timeout resilience and graceful exit when no profile exists."""
        client = GptSovitsClient(server=mock_gpt_sovits)
        vm = VoiceManager(gpt_sovits_client_or_server=client, db_path=temp_db_path)
        vm.active_profile = None

        # 1. No profile found in empty DB
        success_no_prof = await vm.warmup_current_profile()
        assert success_no_prof is False

        # 2. Probe synthesis timeout
        async with get_db(temp_db_path) as conn:
            profile = await crud.create_voice_profile(conn, VoiceProfileCreate(
                name="TimeoutWarmupChar",
                gpt_weights_path="weights/timeout.ckpt",
                sovits_weights_path="weights/timeout.pth",
                ref_audio_path="ref/timeout.wav",
                is_default=True,
            ))
        vm.active_profile = profile

        with patch.object(client, "synthesize", side_effect=asyncio.TimeoutError("Probe timeout")):
            success_timeout = await vm.warmup_current_profile()
            assert success_timeout is False


# ============================================================================
# 3. R2 Frontend Audio Player Static Integrity
# ============================================================================

class TestR2FrontendAudioPlayerStaticIntegrity:
    """Verifies static implementation of Web Audio API, micro-fade, and interrupt."""

    def test_r2_frontend_audio_player_static_integrity(self):
        """Verifies frontend/src/audio_player.js contains required Web Audio API constructs."""
        player_path = Path("frontend/src/audio_player.js")
        assert player_path.exists(), "frontend/src/audio_player.js must exist"
        code = player_path.read_text(encoding="utf-8")

        # 1. Web Audio API AudioContext lazy initialization
        assert "window.AudioContext || window.webkitAudioContext" in code
        assert "export class StreamAudioController" in code
        assert "createBufferSource()" in code
        assert "decodeAudioData(" in code

        # 2. Sample-accurate timeline scheduling
        assert "source.start(startTime)" in code
        assert "this.nextStartTime = startTime + duration - (fadeDur / 2)" in code

        # 3. 12ms micro-fade envelope (attack & decay)
        assert "this.crossFadeMs = options.crossFadeMs || 0.012" in code
        assert "linearRampToValueAtTime(1.0, startTime + fadeDur)" in code
        assert "linearRampToValueAtTime(0.0001, startTime + duration)" in code

        # 4. 40ms smooth linear interrupt fade-out
        assert "interrupt(fadeMs = 40)" in code
        assert "linearRampToValueAtTime(0.0001, now + fadeSec)" in code
        assert "this.abortController.abort()" in code
        assert "this.queue = []" in code
        assert "source.stop()" in code

    def test_r2_static_audio_player_untouched_and_intact(self):
        """Verifies galgame2voice/static/js/audio_player.js remains intact for backward compatibility."""
        static_path = Path("galgame2voice/static/js/audio_player.js")
        assert static_path.exists(), "galgame2voice/static/js/audio_player.js must remain intact"
        code = static_path.read_text(encoding="utf-8")

        assert "class StreamingAudioPlayer" in code
        assert "crossFadeDuration = options.crossFadeDuration || 0.025" in code
        assert "initContext()" in code
        assert "enqueue(" in code


# ============================================================================
# 4. R2 Interrupt & Cancellation Timing
# ============================================================================

class TestR2InterruptAndCancellationTiming:
    """Verifies cancellation signal halts active generation and releases locks < 100ms."""

    @pytest.mark.asyncio
    async def test_r2_stream_chat_cancellation_latency_under_100ms(self, temp_db_path, mock_gpt_sovits):
        """Cancelling active stream_chat must reap tasks and release locks in < 100ms."""
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, db_path=temp_db_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        adapter = MockSlowCancellableLLMAdapter()

        async def _mock_adapter_getter(conn=None, provider_id=None):
            return adapter, "mock-slow-model", "mock-prov"

        chat_service._get_active_llm_adapter = _mock_adapter_getter

        cancel_event = asyncio.Event()
        gen = chat_service.stream_chat(
            prompt="测试打断时序",
            session_id="cancel_timing_session",
            cancel_event=cancel_event,
        )

        # Wait until producer starts and yields first token
        first_event = await gen.__anext__()
        assert first_event is not None
        assert adapter.started_event.is_set()

        # Trigger cancel signal and measure latency
        t_cancel = time.perf_counter()
        cancel_event.set()

        remaining_events = []
        async for ev in gen:
            remaining_events.append(ev)

        t_done = time.perf_counter()
        cancel_latency_ms = (t_done - t_cancel) * 1000.0

        # Assert cancel response is well under 100ms
        assert cancel_latency_ms < 100.0, f"Cancel latency exceeded 100ms: {cancel_latency_ms:.2f}ms"

        # Assert final event is truncated done
        done_events = [ev for ev in remaining_events if ev.get("event") == "done"]
        assert len(done_events) == 1
        assert done_events[0].get("data", {}).get("truncated") is True

        # Assert inference lock is not leaked
        assert not client.lock.locked(), "Inference mutex lock must be released after cancellation"

        # Assert background tasks cleaned up
        await chat_service.aclose()
        assert len([t for t in chat_service._bg_tasks if not t.done()]) == 0


# ============================================================================
# 5. R3 50+ Turn Conversation Memory Stability & VRAM Watermark Guard
# ============================================================================

class TestR3MemoryStabilityAndVramGuard:
    """Verifies long dialogue memory stability (50+ turns) and VRAM safety guard."""

    @pytest.mark.asyncio
    async def test_r3_50_turn_continuous_dialogue_memory_stability(self, temp_db_path, mock_gpt_sovits):
        """Simulates 50 continuous turns; asserts process RSS memory drift < 35MB."""
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, db_path=temp_db_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        adapter = MockFastBilingualLLMAdapter()

        async def _mock_adapter_getter(conn=None, provider_id=None):
            return adapter, "mock-fast-model", "mock-prov"

        chat_service._get_active_llm_adapter = _mock_adapter_getter

        # Warm up 2 turns before baseline measurement to stabilize Python interpreter caches
        for i in range(2):
            async for _ in chat_service.stream_chat(prompt=f"Warmup {i}", session_id="bench_session"):
                pass

        gc.collect()
        process = psutil.Process()
        rss_before = process.memory_info().rss

        # Execute 50 continuous conversation turns
        for i in range(50):
            async for _ in chat_service.stream_chat(prompt=f"Turn message {i}", session_id="bench_session"):
                pass

        await chat_service.aclose()
        gc.collect()
        rss_after = process.memory_info().rss

        drift_mb = (rss_after - rss_before) / (1024 * 1024)

        # Assert RSS drift < 35.0 MB
        assert drift_mb < 35.0, f"50-turn RSS memory drift exceeded 35MB: {drift_mb:.2f} MB"
        assert len([t for t in chat_service._bg_tasks if not t.done()]) == 0

    def test_r3_vram_watermark_guard(self):
        """Verifies VoiceManager._check_vram_guard() behavior across VRAM levels."""
        vm = VoiceManager(gpt_sovits_client_or_server="http://127.0.0.1:9880")

        # 1. Low free VRAM (< 0.45GB) -> InsufficientMemoryError
        with patch("galgame2voice.services.voice_manager.get_gpu_vram_status", return_value=(8.0, 0.25)):
            with patch("galgame2voice.services.voice_manager.release_system_memory") as mock_rel:
                with pytest.raises(InsufficientMemoryError) as exc_info:
                    vm._check_vram_guard(min_free_vram_gb=0.45)
                assert "显卡可用显存不足" in str(exc_info.value)
                mock_rel.assert_called_once()

        # 2. Healthy free VRAM (>= 0.45GB) -> Clean execution
        with patch("galgame2voice.services.voice_manager.get_gpu_vram_status", return_value=(8.0, 3.5)):
            vm._check_vram_guard(min_free_vram_gb=0.45)

        # 3. CPU / CI mode without discrete CUDA GPU -> Clean skip
        with patch("galgame2voice.services.voice_manager.get_gpu_vram_status", return_value=(None, None)):
            vm._check_vram_guard(min_free_vram_gb=0.45)

        # 4. Custom threshold parameter
        with patch("galgame2voice.services.voice_manager.get_gpu_vram_status", return_value=(8.0, 0.8)):
            with pytest.raises(InsufficientMemoryError):
                vm._check_vram_guard(min_free_vram_gb=1.0)


# ============================================================================
# 6. R3 SSE Keep-Alive Heartbeat
# ============================================================================

class TestR3SseKeepAlive:
    """Verifies W3C SSE keep-alive comment frames (: keep-alive\\n\\n) on silence."""

    @pytest.mark.asyncio
    async def test_r3_sse_keep_alive_on_queue_silence(self, temp_db_path, mock_gpt_sovits, monkeypatch):
        """Verifies stream_chat and stream_chat_events emit ': keep-alive\\n\\n' on silence."""
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, db_path=temp_db_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        adapter = MockPausingLLMAdapter(pause_seconds=0.8)

        async def _mock_adapter_getter(conn=None, provider_id=None):
            return adapter, "mock-pause-model", "mock-prov"

        chat_service._get_active_llm_adapter = _mock_adapter_getter

        # Accelerate monotonic time during pause so queue timeout (0.5s) triggers >= 5.0s jump
        real_monotonic = time.monotonic
        time_offset = 0.0

        def accelerated_monotonic():
            return real_monotonic() + time_offset

        monkeypatch.setattr(time, "monotonic", accelerated_monotonic)

        # Task to shift virtual clock during queue silence after the first token is consumed
        async def _shift_clock():
            await adapter.paused_event.wait()
            await asyncio.sleep(0.05)
            nonlocal time_offset
            time_offset = 6.0

        asyncio.create_task(_shift_clock())

        # 1. Test stream_chat yields SseKeepAlive frame
        events = []
        async for ev in chat_service.stream_chat(prompt="测试保活心跳", session_id="ka_sess_1"):
            events.append(ev)

        keep_alive_events = [ev for ev in events if isinstance(ev, SseKeepAlive) or ev == ": keep-alive\n\n"]
        assert len(keep_alive_events) >= 1, "stream_chat must yield SseKeepAlive frame during silence"
        assert keep_alive_events[0].get("comment") == ": keep-alive\n\n"

        # 2. Test stream_chat_events formats keep-alive as W3C comment string
        time_offset = 0.0
        adapter.paused_event.clear()
        asyncio.create_task(_shift_clock())

        raw_events = []
        async for ev_str in chat_service.stream_chat_events(prompt="测试保活心跳格式", session_id="ka_sess_2"):
            raw_events.append(ev_str)

        assert any(e == ": keep-alive\n\n" for e in raw_events), (
            "stream_chat_events must yield verbatim ': keep-alive\\n\\n' string"
        )


# ============================================================================
# 7. R3 Frontend Bounded Blob URL LRU Store
# ============================================================================

class TestR3FrontendBoundedBlobStore:
    """Verifies BoundedAudioStore static specification and behavioral contracts."""

    def test_r3_frontend_bounded_blob_store_static_integrity(self):
        """Verifies frontend/src/voice.js contains BoundedAudioStore with 30-item LRU cap."""
        voice_js_path = Path("frontend/src/voice.js")
        assert voice_js_path.exists(), "frontend/src/voice.js must exist"
        code = voice_js_path.read_text(encoding="utf-8")

        assert "export const MAX_AUDIO_STORE_ENTRIES = 30;" in code
        assert "export class BoundedAudioStore extends Map" in code
        assert "URL.revokeObjectURL(url)" in code
        assert "this.size > MAX_AUDIO_STORE_ENTRIES" in code
        assert "safeRevokeUrl(evicted.url)" in code
        assert "export const audioStore = new BoundedAudioStore();" in code

    def test_r3_bounded_audio_store_lru_behavior_simulation(self):
        """Simulates BoundedAudioStore contract: 50 insertions caps at 30 and revokes oldest 20."""
        MAX_ENTRIES = 30
        revoked_urls: List[str] = []

        class PyBoundedAudioStore(dict):
            def set(self, key, value):
                if key in self:
                    old = self[key]
                    if old and old.get("url") and old.get("url") != value.get("url"):
                        revoked_urls.append(old["url"])
                    del self[key]
                self[key] = value
                if len(self) > MAX_ENTRIES:
                    oldest_k = next(iter(self))
                    evicted = self.pop(oldest_k)
                    if evicted and evicted.get("url"):
                        revoked_urls.append(evicted["url"])

            def delete(self, key):
                if key in self:
                    item = self.pop(key)
                    if item and item.get("url"):
                        revoked_urls.append(item["url"])

            def clear(self):
                for item in self.values():
                    if item and item.get("url"):
                        revoked_urls.append(item["url"])
                super().clear()

        store = PyBoundedAudioStore()
        # Insert 50 blob items
        for i in range(50):
            store.set(f"msg_{i}", {"url": f"blob:http://localhost/audio_{i}.webm"})

        assert len(store) == 30
        # 20 earliest items (0..19) must be revoked in order
        assert len(revoked_urls) == 20
        assert revoked_urls[0] == "blob:http://localhost/audio_0.webm"
        assert revoked_urls[19] == "blob:http://localhost/audio_19.webm"
        assert "msg_0" not in store
        assert "msg_49" in store

        # Overwrite item
        store.set("msg_49", {"url": "blob:http://localhost/audio_49_v2.webm"})
        assert revoked_urls[-1] == "blob:http://localhost/audio_49.webm"

        # Explicit delete
        store.delete("msg_48")
        assert revoked_urls[-1] == "blob:http://localhost/audio_48.webm"
        assert len(store) == 29

        # Explicit clear
        store.clear()
        assert len(store) == 0
        assert len(revoked_urls) == 20 + 1 + 1 + 29  # 51 total entries accounted for


# ============================================================================
# 8. R4 SQLite WAL Concurrency
# ============================================================================

class TestR4SqliteWalConcurrency:
    """Verifies SQLite WAL burst concurrency and zero database is locked errors."""

    @pytest.mark.asyncio
    async def test_r4_sqlite_wal_pragmas(self, temp_db_path):
        """Verifies database initializes with WAL mode, foreign keys, and busy timeout."""
        async with get_db(temp_db_path) as conn:
            mode = (await (await conn.execute("PRAGMA journal_mode;")).fetchone())[0]
            assert str(mode).lower() == "wal", f"Expected WAL mode, got {mode}"

            busy_timeout = (await (await conn.execute("PRAGMA busy_timeout;")).fetchone())[0]
            assert busy_timeout >= 5000, f"Expected busy_timeout >= 5000, got {busy_timeout}"

            fk = (await (await conn.execute("PRAGMA foreign_keys;")).fetchone())[0]
            assert fk == 1, "Foreign keys must be ON"

    @pytest.mark.asyncio
    async def test_r4_sqlite_wal_burst_concurrency_no_locks(self, temp_db_path):
        """
        Executes 15 concurrent writer tasks and 15 concurrent reader tasks.
        Asserts zero database is locked / OperationalError failures and exact record count.
        """
        num_writers = 15
        num_readers = 15
        writes_per_task = 4

        async def writer(writer_id: int):
            for step in range(writes_per_task):
                async with get_db(temp_db_path) as conn:
                    async with immediate_transaction(conn):
                        await conn.execute(
                            "INSERT INTO session_messages (session_id, role, content_chinese, content_japanese) "
                            "VALUES (?, ?, ?, ?);",
                            (f"wal_worker_{writer_id}", "user", f"W_{writer_id}_S_{step}", f"日文_{writer_id}_{step}")
                        )
                await asyncio.sleep(0.001)
            return True

        async def reader(reader_id: int):
            seen_counts = []
            for _ in range(writes_per_task):
                async with get_db(temp_db_path) as conn:
                    row = await (await conn.execute("SELECT count(*) FROM session_messages;")).fetchone()
                    seen_counts.append(row[0])
                    # Query recent rows
                    rows = await (await conn.execute("SELECT * FROM session_messages ORDER BY id DESC LIMIT 5;")).fetchall()
                    assert isinstance(rows, list)
                await asyncio.sleep(0.001)
            return seen_counts

        tasks = [writer(w) for w in range(num_writers)] + [reader(r) for r in range(num_readers)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, res in enumerate(results):
            assert not isinstance(res, Exception), f"Concurrent WAL task {i} encountered error: {res}"

        # Verify exact final count of written records
        async with get_db(temp_db_path) as conn:
            final_count = (await (await conn.execute("SELECT count(*) FROM session_messages;")).fetchone())[0]
            assert final_count == num_writers * writes_per_task
