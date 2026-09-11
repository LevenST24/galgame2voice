"""
Adversarial Stress Test Suite — Challenger R4-1
Exhaustive empirical validation for Galgame2Voice Phase 4 / Milestone 7 optimizations.

Tests:
1. Adversarial Agile Chunking:
   - Empty text, whitespace only.
   - Pure punctuation (commas only, terminals only, mixed CJK/ASCII punctuation).
   - Emojis and zero-width/unicode characters.
   - Ultra-short clauses (< 6 chars) with clause pause vs terminal punctuation.
   - Massive paragraphs (10,000 to 50,000 chars) testing for ReDoS and memory growth.
   - StreamingBilingualParser single-char feeding and malformed JSON recovery.
2. Rapid Interrupt Stress:
   - 50+ cancellation signals fired at varying phase offsets during active stream_chat.
   - Strict assertions:
     - Zero unhandled exceptions.
     - Release latency strictly < 100ms per cancellation.
     - Zero leaked background tasks (_bg_tasks).
     - GPU inference lock immediately unlocked.
3. 50-Turn Continuous Dialogue Memory Stability:
   - 50 continuous turns with memory tracking.
   - Strict assertion: RSS drift < 35MB.
"""

import asyncio
import gc
import json
import os
import random
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import psutil
import pytest

from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult
from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.services.tts_service import TtsService
from galgame2voice.utils.text_splitter import split_japanese_sentences


# ============================================================================
# Mock Adapters for Adversarial Stress
# ============================================================================

class MockStreamingLLMAdapter(BaseLLMAdapter):
    """Configurable streaming LLM adapter for stress and cancellation testing."""

    def __init__(self, token_delay: float = 0.005, num_tokens: int = 25):
        super().__init__(api_key="sk-mock-stream", base_url="http://mock-stream")
        self.token_delay = token_delay
        self.num_tokens = num_tokens
        self.started_event = asyncio.Event()

    async def chat(self, messages: List[ChatMessage], model: str, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content='{"chinese": "你好", "japanese": "こんにちは"}')

    async def stream_chat(self, messages: List[ChatMessage], model: str, **kwargs: Any) -> AsyncIterator[str]:
        self.started_event.set()
        yield '{"chinese": "你好，指挥官！今天也是元气满满的一天呢。"'
        yield ', "japanese": "こんにちは、'
        for i in range(self.num_tokens):
            if self.token_delay > 0:
                await asyncio.sleep(self.token_delay)
            yield f'セリフ切片{i}、'
        yield '今日も一日頑張りましょう！"}'

    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        return TestResult(success=True, message="OK", latency_ms=1.0)

    async def list_models(self) -> List[str]:
        return ["mock-stream-model"]


class MockFastBilingualLLM(BaseLLMAdapter):
    """Fast adapter yielding complete bilingual response in 2 chunks."""

    def __init__(self, turn_id: int = 0):
        super().__init__(api_key="sk-mock-fast", base_url="http://mock-fast")
        self.turn_id = turn_id

    async def chat(self, messages: List[ChatMessage], model: str, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content=f'{{"chinese": "回复{self.turn_id}", "japanese": "返事{self.turn_id}。"}}')

    async def stream_chat(self, messages: List[ChatMessage], model: str, **kwargs: Any) -> AsyncIterator[str]:
        yield f'{{"chinese": "回复{self.turn_id}，你好！"'
        yield f', "japanese": "こんにちは、返事{self.turn_id}。今日もよろしくね！"}}'

    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        return TestResult(success=True, message="OK", latency_ms=1.0)

    async def list_models(self) -> List[str]:
        return ["mock-fast-model"]


# ============================================================================
# 1. Adversarial Agile Chunking Tests
# ============================================================================

class TestAdversarialAgileChunking:
    """Stress tests split_japanese_sentences and StreamingBilingualParser on pathological inputs."""

    @pytest.mark.parametrize("empty_input", ["", "   ", "\t\t", "\n\n", "  \r\n  \t  "])
    def test_chunking_empty_and_whitespace(self, empty_input):
        """Empty and whitespace-only inputs must return empty list without error."""
        res_default = split_japanese_sentences(empty_input, is_first_chunk=False)
        assert res_default == [], f"Expected [] for {repr(empty_input)}, got {res_default}"

        res_agile = split_japanese_sentences(empty_input, is_first_chunk=True, min_chars=6)
        assert res_agile == [], f"Expected [] for agile {repr(empty_input)}, got {res_agile}"

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

    def test_chunking_mixed_cjk_ascii_punctuation(self):
        """Mixed Chinese, Japanese, and ASCII commas/periods/exclamations."""
        mixed = "初めまして，今日はいい天気ですね、一緒に行こう,楽しみだね！本当に？すごい!!"
        # Agile split
        res = split_japanese_sentences(mixed, is_first_chunk=True, min_chars=6)
        assert len(res) >= 3
        # First chunk split on Chinese comma '，' since len("初めまして，") >= 6
        assert res[0] == "初めまして，"
        # Subsequent split should only split on terminal punct, preserving '、' and ',' inside sentences
        assert res[1] == "今日はいい天気ですね、一緒に行こう,楽しみだね！"
        assert "".join(res) == mixed

    def test_chunking_ultra_short_clauses(self):
        """Ultra-short clauses (< 6 chars) must NOT split prematurely when is_first_chunk=True."""
        # 1. 2-character clauses: "あ、い、う、え、お、か、き。"
        # "あ、" len 2 < 6
        # "あ、い、" len 4 < 6
        # "あ、い、う、" len 6 >= 6 -> splits here!
        short_text = "あ、い、う、え、お、か、き。"
        res = split_japanese_sentences(short_text, is_first_chunk=True, min_chars=6)
        assert res[0] == "あ、い、う、"
        # Subsequent chunks require terminal punctuation
        assert res[1] == "え、お、か、き。"

        # 2. Short clause followed immediately by terminal punctuation:
        # Terminal punctuation overrides min_chars!
        short_terminal = "はい！先生、おはようございます。"
        res_term = split_japanese_sentences(short_terminal, is_first_chunk=True, min_chars=6)
        assert res_term[0] == "はい！"
        assert res_term[1] == "先生、おはようございます。"

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
# 2. Rapid Interrupt Stress Tests (50+ Cancellation Signals)
# ============================================================================

class TestRapidInterruptStress:
    """Stress tests stream_chat under 50+ rapid cancellations across varying lifecycles."""

    @pytest.mark.asyncio
    async def test_rapid_50_interrupt_signals_during_streaming(self, temp_db_path, mock_gpt_sovits):
        """
        Rapidly fires 50 cancellation signals at randomized intervals during stream_chat.
        Asserts:
        - Latency from cancel_event.set() to stream termination is strictly < 100ms.
        - Client inference lock is cleanly released (lock.locked() is False).
        - Zero unhandled exceptions.
        - Zero leaked background tasks (_bg_tasks).
        """
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, db_path=temp_db_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        adapter = MockStreamingLLMAdapter(token_delay=0.005, num_tokens=30)

        async def _mock_adapter_getter(conn=None, provider_id=None):
            return adapter, "mock-stream-model", "mock-prov"

        chat_service._get_active_llm_adapter = _mock_adapter_getter

        cancel_latencies: List[float] = []

        for turn in range(50):
            cancel_event = asyncio.Event()
            session_id = f"cancel_stress_sess_{turn}"

            gen = chat_service.stream_chat(
                prompt=f"Stress cancellation turn {turn}",
                session_id=session_id,
                cancel_event=cancel_event,
            )

            # Random cancel timing:
            # 0: immediately before starting
            # 1: after first event received
            # 2: after delay 1ms .. 20ms
            timing_mode = turn % 3
            collected_events = []

            if timing_mode == 0:
                # Cancel immediately before consuming
                t_cancel = time.perf_counter()
                cancel_event.set()
                async for ev in gen:
                    collected_events.append(ev)
                t_done = time.perf_counter()
            elif timing_mode == 1:
                # Cancel immediately upon receiving first event
                try:
                    first_ev = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
                    collected_events.append(first_ev)
                except (StopAsyncIteration, asyncio.TimeoutError):
                    pass
                t_cancel = time.perf_counter()
                cancel_event.set()
                async for ev in gen:
                    collected_events.append(ev)
                t_done = time.perf_counter()
            else:
                # Cancel after arbitrary short delay (2ms .. 15ms)
                delay_sec = (turn % 10 + 1) * 0.0015
                async def _delayed_cancel():
                    await asyncio.sleep(delay_sec)
                    cancel_event.set()

                cancel_task = asyncio.create_task(_delayed_cancel())
                t_cancel = None
                try:
                    async for ev in gen:
                        collected_events.append(ev)
                        if cancel_event.is_set() and t_cancel is None:
                            t_cancel = time.perf_counter()
                finally:
                    if not cancel_task.done():
                        cancel_task.cancel()
                    if t_cancel is None:
                        t_cancel = time.perf_counter()
                t_done = time.perf_counter()

            latency_ms = (t_done - t_cancel) * 1000.0
            cancel_latencies.append(latency_ms)

            # Assert lock is released immediately
            assert not client.lock.locked(), f"Inference lock remained locked on turn {turn}"

            # Assert latency is strictly < 100ms
            assert latency_ms < 100.0, f"Turn {turn} cancel latency exceeded 100ms: {latency_ms:.2f}ms"

        # Reaping and background task check
        await chat_service.aclose()
        leaked_tasks = [t for t in chat_service._bg_tasks if not t.done()]
        assert len(leaked_tasks) == 0, f"Found {len(leaked_tasks)} leaked background tasks!"

        # Summary telemetry
        avg_latency = sum(cancel_latencies) / len(cancel_latencies)
        max_latency = max(cancel_latencies)
        assert max_latency < 100.0
        assert avg_latency < 30.0, f"Average cancel latency was high: {avg_latency:.2f}ms"


# ============================================================================
# 3. 50+ Turn Long Dialogue Memory Stress Tests
# ============================================================================

class TestLongDialogueMemoryStress:
    """Stress tests continuous 50-turn conversation for memory RSS drift."""

    @pytest.mark.asyncio
    async def test_50_turn_dialogue_rss_drift_under_35mb(self, temp_db_path, mock_gpt_sovits):
        """
        Executes 50 complete dialogue turns in sequence.
        Measures process RSS memory before and after.
        Asserts RSS drift < 35MB and zero uncollected background tasks.
        """
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, db_path=temp_db_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        async def _mock_adapter_getter(conn=None, provider_id=None):
            return MockFastBilingualLLM(turn_id=0), "mock-fast-model", "mock-prov"

        chat_service._get_active_llm_adapter = _mock_adapter_getter

        # Warm up 3 turns to stabilize module imports, memory allocations, and SQLite cache
        for w in range(3):
            async for _ in chat_service.stream_chat(prompt=f"Warmup turn {w}", session_id="mem_stress_session"):
                pass

        gc.collect()
        process = psutil.Process()
        rss_before = process.memory_info().rss

        # Execute 50 continuous conversation turns
        for turn in range(50):
            events = []
            async for ev in chat_service.stream_chat(
                prompt=f"Continuous dialogue turn {turn}: 探讨Galgame剧情与角色心理",
                session_id="mem_stress_session",
            ):
                events.append(ev)

            assert len(events) >= 2, f"Turn {turn} did not emit expected events"
            done_ev = next((e for e in events if e.get("event") == "done"), None)
            assert done_ev is not None, f"Turn {turn} missing done event"

        # Complete pending memory extraction tasks
        await chat_service.aclose()
        gc.collect()
        rss_after = process.memory_info().rss

        drift_mb = (rss_after - rss_before) / (1024 * 1024)

        # Assert RSS drift < 35.0 MB
        assert drift_mb < 35.0, f"50-turn dialogue RSS memory drift exceeded 35MB: {drift_mb:.2f} MB"

        # Verify zero background tasks retained
        assert len([t for t in chat_service._bg_tasks if not t.done()]) == 0

        # Verify all 50 user + 50 assistant messages persisted cleanly in DB
        async with get_db(temp_db_path) as conn:
            user_count = (await (await conn.execute("SELECT count(*) FROM messages WHERE role='user' AND session_id='mem_stress_session';")).fetchone())[0]
            asst_count = (await (await conn.execute("SELECT count(*) FROM messages WHERE role='assistant' AND session_id='mem_stress_session';")).fetchone())[0]
            assert user_count == 53  # 3 warmup + 50 turns
            assert asst_count == 53


# ============================================================================
# 4. Concurrent Cancellation Storm & Parser Chaos
# ============================================================================

class TestConcurrentCancellationStorm:
    """Stress tests concurrent cancellations firing simultaneously across multiple sessions."""

    @pytest.mark.asyncio
    async def test_concurrent_streams_simultaneous_cancellation(self, temp_db_path, mock_gpt_sovits):
        """Spawns 4 concurrent streams and fires cancellations simultaneously; asserts release < 100ms."""
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, db_path=temp_db_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        adapter = MockStreamingLLMAdapter(token_delay=0.01, num_tokens=50)

        async def _mock_adapter_getter(conn=None, provider_id=None):
            return adapter, "mock-stream-model", "mock-prov"

        chat_service._get_active_llm_adapter = _mock_adapter_getter

        num_concurrent = 4
        cancel_events = [asyncio.Event() for _ in range(num_concurrent)]
        started_events = [asyncio.Event() for _ in range(num_concurrent)]

        async def run_one(idx: int):
            gen = chat_service.stream_chat(
                prompt=f"Concurrent storm prompt {idx}",
                session_id=f"storm_sess_{idx}",
                cancel_event=cancel_events[idx],
            )
            # Wait for first event
            first = await gen.__anext__()
            started_events[idx].set()
            # Wait for cancel
            events = [first]
            async for ev in gen:
                events.append(ev)
            return events

        tasks = [asyncio.create_task(run_one(i)) for i in range(num_concurrent)]
        await asyncio.gather(*(se.wait() for se in started_events))

        # Trigger all cancellations at once
        t0 = time.perf_counter()
        for ce in cancel_events:
            ce.set()

        results = await asyncio.gather(*tasks, return_exceptions=True)
        t_done = time.perf_counter()
        total_latency_ms = (t_done - t0) * 1000.0

        for idx, res in enumerate(results):
            assert not isinstance(res, Exception), f"Stream {idx} raised exception: {res}"
            assert any(e.get("event") == "done" and e.get("data", {}).get("truncated") is True for e in res)

        assert total_latency_ms < 100.0, f"Simultaneous {num_concurrent}-cancellation latency was {total_latency_ms:.2f}ms >= 100ms"
        assert not client.lock.locked()

        await chat_service.aclose()
        assert len([t for t in chat_service._bg_tasks if not t.done()]) == 0


class TestParserChaosAndRecovery:
    """Stress tests StreamingBilingualParser on corrupted, truncated, and malformed inputs."""

    def test_parser_markdown_chaos(self):
        """Markdown codeblocks with backticks, json keywords, and irregular spacing."""
        chaotic = "```json   \n  \t ````json\n{\"chinese\": \"你好\", \"japanese\": \"こんにちは！\"}```"
        parser = StreamingBilingualParser()
        parser.feed_chunk(chaotic)
        ch, ja, sents = parser.finalize()
        assert ch == "你好"
        assert ja == "こんにちは！"

    def test_parser_unclosed_trailing_backslash(self):
        """Trailing backslashes and partial escape sequences must not crash parser."""
        parser = StreamingBilingualParser()
        parser.feed_chunk('{"chinese": "测试\\')
        ch, _ = parser.feed_chunk('u4f60好", "japanese": "テスト\\')
        _, _ = parser.feed_chunk('"}\\\\')
        ch_full, ja_full, _ = parser.finalize()
        assert "测试" in ch_full
        assert "テスト" in ja_full

    def test_parser_non_json_fallback_extraction(self):
        """Plaintext output where LLM forgot JSON format entirely."""
        parser = StreamingBilingualParser()
        tokens = [
            "这是纯文本输出。\n",
            "Chinese: 很高兴认识你。\n",
            "Japanese: はじめまして、よろしくお願いします！\n"
        ]
        for t in tokens:
            parser.feed_chunk(t)
        ch, ja, sents = parser.finalize()
        assert "很高兴认识你" in ch
        assert "はじめまして" in ja

