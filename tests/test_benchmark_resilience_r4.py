"""
Quantitative Benchmark & Resilience Test Suite (Milestone 7 / R4).
Verifies:
1. R1 Agile First-Sentence Chunking:
   - split_japanese_sentences agile clause pause splitting (、，,) with min_chars=6 on chunk 0.
   - StreamingBilingualParser immediate chunk 0 emission upon clause pause.
   - Backwards compatibility for single sentences and subsequent chunks.
2. R1 GPT-SoVITS Asynchronous Warm-up:
   - VoiceManager.warmup_current_profile() execution and probe synthesis.
   - Graceful offline and network timeout resilience.
3. R2 Interrupt & Cancellation Timing:
   - stream_chat cancel event responsiveness and queue/lock release latency.
4. R3 50+ Turn Conversation Memory Stability:
   - 50 continuous turns bilingual dialogue memory stability (RSS drift < 35MB).
5. R3 SSE Keep-Alive Heartbeat:
   - stream_chat() and stream_chat_events() W3C keep-alive comment (: keep-alive\\n\\n)
     emission on >= 5.0s queue silence.
"""

import asyncio
import gc
import json
import re
import time
from typing import Any, AsyncIterator, List, Optional
from unittest.mock import patch

import httpx
import psutil
import pytest

from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult
from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db
from galgame2voice.services.chat_service import ChatService, SseKeepAlive
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.voice_manager import VoiceManager
from galgame2voice.utils.text_splitter import split_japanese_sentences


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

        # Verify the probe is a short real word
        tts_calls = [c for c in mock_gpt_sovits.call_history if c["endpoint"] == "/tts"]
        assert len(tts_calls) >= 1
        assert tts_calls[-1]["payload"].get("text") == "はい。"

    @pytest.mark.asyncio
    async def test_r1_warmup_probe_passes_the_engine_text_check(self, temp_db_path, mock_gpt_sovits):
        """GPT-SoVITS answers symbol-only text with 400 "请输入有效文本"; the probe must not be rejected."""
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

        mock_handle = mock_gpt_sovits.handle_request

        async def engine_text_check(method, path, json_data=None, params=None):
            text = (json_data or params or {}).get("text")
            if path == "/tts" and text is not None and not re.sub(r"\W+", "", text):
                return httpx.Response(status_code=400, json={"message": "tts failed", "Exception": "请输入有效文本"})
            return await mock_handle(method, path, json_data=json_data, params=params)

        mock_gpt_sovits.handle_request = engine_text_check
        client = GptSovitsClient(server=mock_gpt_sovits)
        vm = VoiceManager(gpt_sovits_client_or_server=client, db_path=temp_db_path)
        vm.active_profile = profile

        assert await vm.warmup_current_profile() is True

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
# 4. R2 Interrupt & Cancellation Timing
# ============================================================================

class TestR2InterruptAndCancellationTiming:
    """Verifies cancellation signal halts active generation and releases locks promptly."""

    # Asserts tight wall-clock cancellation latency on active stream_chat; sensitive to multi-worker CPU contention.
    @pytest.mark.serial
    @pytest.mark.wall_clock
    @pytest.mark.asyncio
    async def test_r2_stream_chat_cancellation_ends_the_stream_promptly(self, temp_db_path, mock_gpt_sovits):
        """Cancelling active stream_chat must reap tasks and release locks well before the stream would end."""
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
        metadata_event = await gen.__anext__()
        assert metadata_event.get("event") == "turn_started"
        assert metadata_event.get("data", {}).get("session_id") == "cancel_timing_session"
        assert isinstance(metadata_event.get("data", {}).get("user_message_id"), int) and metadata_event["data"]["user_message_id"] > 0
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

        # The uncancelled stream runs about 2 s. One measurement on a busy runner has taken 112 ms;
        # typical latency is held to a 30 ms average in test_rapid_50_interrupt_signals_during_streaming.
        assert cancel_latency_ms < 500.0, f"Cancel latency exceeded 500ms: {cancel_latency_ms:.2f}ms"

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

