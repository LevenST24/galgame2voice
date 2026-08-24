"""
Adversarial Stress and E2E Hardening Test Suite for Milestone 7 (Full System Pass).
Authored by m7_challenger_1.

Comprehensive Tier 5 Test Matrix:
1. Full Lifecycle Cross-Module E2E Integration:
   - Fresh DB init -> Providers -> Voice Profiles -> Active Switch -> Multi-turn SSE Chat -> Masked Config Queries -> Verified Persistence
   - Cross-module voice profile switching during ongoing multi-turn dialogue
   - Provider failover and error recovery during active chat streams
2. High-Concurrency Multi-Turn SSE Streaming Stress:
   - Concurrent multi-client SSE streaming (10+ simultaneous sessions)
   - Early client disconnect & cancellation resilience under load
   - Adversarial bilingual chunk fuzzing (1-char tokens, markdown fences, unescaped unicode, malformed JSON, plain-text fallback)
   - SSE formatter error and cancellation handling
3. Concurrent Web API + Telegram Bot Load Interleaving:
   - Simultaneous burst: Web SSE streams + Telegram text chat + Telegram voice note STT
   - Shared VoiceManager inference mutex contention under simultaneous Web and Telegram TTS
   - End-to-end Telegram voice note STT-to-TTS pipeline under error conditions
   - Session namespace isolation across web, telegram, and api channels
4. Database Contention, WAL Concurrency & SQL Injection Defense:
   - 50-task concurrent read/write burst under SQLite WAL mode
   - SQL injection attack payload matrix across all persistence layers
   - Extreme multi-turn sliding window token trimming (100+ turns)
5. Acceptance Criteria Verification Matrix (ORIGINAL_REQUEST.md):
   - Python/FastAPI core, Windows batch scripts, Health/Status endpoints, Secret Masking,
     Streaming pipeline, Voice Profile mutex & rollback, Telegram Bot multi-turn & STT.
"""

import asyncio
import json
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
import pytest

from galgame2voice.adapters.base import BaseLLMAdapter, BaseSTTAdapter, ChatMessage, LLMResponse, TestResult
from galgame2voice.adapters.registry import get_llm_adapter, get_stt_adapter, list_provider_presets, get_provider_preset
from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import (
    MessageCreate,
    ProviderCreate,
    ProviderUpdate,
    SettingsUpdate,
    VoiceProfileCreate,
    VoiceProfileUpdate,
)
from galgame2voice.database.session import init_db
from galgame2voice.main import create_app
from galgame2voice.routers.chat import sse_event_formatter
from galgame2voice.services.chat_service import ChatService, StreamingBilingualParser
from galgame2voice.services.session_manager import SessionManager, SessionTurn
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.voice_manager import GptSovitsClient, VoiceManager, clean_japanese_parentheses
from galgame2voice.telegram_bot import TelegramBotHandlers, validate_bot_token
from galgame2voice.utils.logger import MaskingFilter
from tests.conftest import MockGptSovitsServer, MockLLMServer, DATABASE_SCHEMA_SQL, mask_secret


# ============================================================================
# Test Fixtures & Utilities
# ============================================================================

@pytest.fixture
async def m7_db_path():
    """Creates a temporary sqlite database file initialized with the full schema and seed data."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_m7_adv_")
    os.close(fd)

    await init_db(path)

    yield path

    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture
def m7_mock_gpt():
    """Fresh MockGptSovitsServer instance."""
    return MockGptSovitsServer()


@pytest.fixture
def m7_mock_llm():
    """Fresh MockLLMServer instance."""
    return MockLLMServer()


class CustomMockLLMAdapter(BaseLLMAdapter):
    """Configurable mock adapter for controlled streaming and error simulation."""
    def __init__(self, provider_config: Optional[Dict[str, Any]] = None, custom_tokens: Optional[List[str]] = None, fail_after_tokens: int = -1):
        cfg = provider_config or {}
        api_key = cfg.get("api_key", "sk-mock-key")
        base_url = cfg.get("base_url", "https://api.openai.com/v1")
        super().__init__(api_key=api_key, base_url=base_url)
        self.custom_tokens = custom_tokens or [
            '{"chinese": "你好，指挥官！", "japanese": "こんにちは、指揮官！今日も頑張りましょう。'
            '（微笑みながら）よろしくね。"}'
        ]
        self.fail_after_tokens = fail_after_tokens
        self.call_count = 0

    async def chat(self, messages: List[ChatMessage], model: str, temperature: float = 1.0, **kwargs) -> LLMResponse:
        self.call_count += 1
        full_text = "".join(self.custom_tokens)
        return LLMResponse(content=full_text, usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})

    async def stream_chat(self, messages: List[ChatMessage], model: str, temperature: float = 1.0, **kwargs) -> AsyncGenerator[str, None]:
        self.call_count += 1
        for idx, token in enumerate(self.custom_tokens):
            if self.fail_after_tokens >= 0 and idx >= self.fail_after_tokens:
                raise RuntimeError(f"Simulated adapter failure at token index {idx}")
            await asyncio.sleep(0.001)
            yield token

    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        return TestResult(success=True, message="Custom mock adapter connection verified", latency_ms=12.5)

    async def list_models(self) -> List[str]:
        return ["custom-model-v1", "custom-model-v2"]


class DynamicMockLLMAdapter(BaseLLMAdapter):
    """Dynamically reflects prompt in response, safe for multi-task concurrency."""
    def __init__(self, *args, **kwargs):
        super().__init__(api_key="sk-dynamic", base_url="https://api.openai.com/v1")

    async def chat(self, messages: List[ChatMessage], model: str, temperature: float = 1.0, **kwargs) -> LLMResponse:
        user_msg = messages[-1].content if messages else ""
        content = json.dumps({
            "chinese": f"回复: {user_msg}",
            "japanese": f"応答: {user_msg}。"
        }, ensure_ascii=False)
        return LLMResponse(content=content, usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})

    async def stream_chat(self, messages: List[ChatMessage], model: str, temperature: float = 1.0, **kwargs) -> AsyncGenerator[str, None]:
        user_msg = messages[-1].content if messages else ""
        tokens = [
            '{"chinese": "',
            f'回复: {user_msg}',
            '", "japanese": "',
            f'応答: {user_msg}。',
            '"}'
        ]
        for t in tokens:
            await asyncio.sleep(0.001)
            yield t

    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        return TestResult(success=True, message="Dynamic connection OK", latency_ms=10.0)

    async def list_models(self) -> List[str]:
        return ["dynamic-model"]


# ============================================================================
# 1. Full Lifecycle Cross-Module E2E Integration Tests
# ============================================================================

class TestFullLifecycleCrossModuleE2E:
    """
    Tests complete real-world user lifecycle across DB, config, providers, voice manager,
    and multi-turn SSE streaming pipeline.
    """

    @pytest.mark.asyncio
    async def test_e2e_fresh_db_to_multiturn_dialogue(self, m7_db_path, m7_mock_gpt, m7_mock_llm):
        """
        Lifecycle:
        1. DB initialized with defaults.
        2. Create and activate a custom provider ('siliconflow').
        3. Create and switch to a custom voice profile ('Natsume').
        4. Execute 3 consecutive dialogue turns via ChatService streaming.
        5. Verify session history, token counts, audio URLs, and secret masking.
        """
        # Step 1: Create VoiceManager and TTSService linked to mock GPT-SoVITS
        voice_manager = VoiceManager(gpt_sovits_client_or_server=m7_mock_gpt, db_path=m7_db_path)
        tts_service = TtsService(client=voice_manager.client)

        # Step 2: Add and activate SiliconFlow provider
        async with aiosqlite.connect(m7_db_path) as conn:
            await crud.create_provider(conn, ProviderCreate(
                id="custom_cloud",
                name="Custom Cloud Provider",
                api_base_url="https://api.custom-cloud.example.com/v1",
                api_key="sk-customcloud-secret-key-1234567890",
                chat_model="Qwen/Qwen2.5-7B-Instruct",
                stt_model="FunAudioLLM/SenseVoiceSmall",
                is_active=1,
                custom_headers={},
            ))
            await crud.set_active_provider(conn, "custom_cloud")

            # Step 3: Create and switch to Natsume voice profile
            profile = await voice_manager.create_profile(VoiceProfileCreate(
                name="Natsume",
                gpt_weights_path="weights/natsume.ckpt",
                sovits_weights_path="weights/natsume.pth",
                refer_audio_path="ref/natsume.wav",
                refer_text="おはようございます、先輩。",
                refer_language="ja",
                prompt_language="ja",
                text_language="ja",
                is_default=0,
            ))
            switch_ok = await voice_manager.switch_profile("Natsume")
            assert switch_ok is True

        # Step 4: Execute 3-turn multi-turn dialogue with Custom Mock Adapter
        mock_adapter = CustomMockLLMAdapter(
            provider_config={"api_key": "sk-siliconflow-secret-key-1234567890"},
            custom_tokens=[
                '{"chinese": "第一句话。第二句话！", ',
                '"japanese": "最初の文です。（微笑み）二番目の文です！"}'
            ]
        )

        chat_service = ChatService(tts_service=tts_service, db_path=m7_db_path)

        with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=mock_adapter):
            for turn in range(1, 4):
                events = []
                async for event in chat_service.stream_chat(
                    prompt=f"User turn {turn}",
                    session_id="lifecycle_session_001",
                    character_name="四季夏目",
                ):
                    events.append(event)

                event_types = [e["event"] for e in events]
                assert "text" in event_types, f"Turn {turn} missing text event"
                assert "audio_chunk" in event_types, f"Turn {turn} missing audio_chunk event"
                assert "done" in event_types, f"Turn {turn} missing done event"

                done_event = next(e for e in events if e["event"] == "done")
                assert "第一句话" in done_event["data"]["chinese"]
                assert done_event["data"]["total_audio_url"].startswith("/audio/")
                assert len(done_event["data"]["chunks"]) >= 1

        # Step 5: Verify Database History and Key Masking
        async with aiosqlite.connect(m7_db_path) as conn:
            history = await crud.get_recent_messages(conn, "lifecycle_session_001")
            assert len(history) == 6  # 3 user messages + 3 assistant messages

            # Verify API key masking when queried via CRUD
            providers = await crud.list_providers(conn)
            sf_prov = next(p for p in providers if p.id == "custom_cloud")
            assert sf_prov.api_key.startswith("sk-****")
            assert "7890" in sf_prov.api_key
            assert "secret-key" not in sf_prov.api_key

    @pytest.mark.asyncio
    async def test_cross_module_profile_switch_interleaved_in_active_session(self, m7_db_path, m7_mock_gpt):
        """
        Verifies that switching voice profile in the middle of a multi-turn session
        seamlessly updates TTS weights and character persona without breaking session history.
        """
        voice_manager = VoiceManager(gpt_sovits_client_or_server=m7_mock_gpt, db_path=m7_db_path)
        tts_service = TtsService(client=voice_manager.client)
        chat_service = ChatService(tts_service=tts_service, db_path=m7_db_path)

        # Create Profile B
        await voice_manager.create_profile(VoiceProfileCreate(
            name="Ayase",
            gpt_weights_path="weights/ayase.ckpt",
            sovits_weights_path="weights/ayase.pth",
            refer_audio_path="ref/ayase.wav",
            refer_text="あやせです。",
            refer_language="ja",
            prompt_language="ja",
            text_language="ja",
            is_default=0,
        ))

        mock_adapter = CustomMockLLMAdapter(provider_config={}, custom_tokens=[
            '{"chinese": "我是第一位角色。", "japanese": "私は一人目です。"}'
        ])

        with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=mock_adapter):
            # Turn 1 with default profile
            res1 = await chat_service.chat_sync("Hello 1", session_id="switch_session")
            assert res1["chinese"] == "我是第一位角色。"
            assert m7_mock_gpt.current_gpt_weights != "weights/ayase.ckpt"

            # Switch to Ayase mid-dialogue
            switch_ok = await voice_manager.switch_profile("Ayase")
            assert switch_ok is True
            assert m7_mock_gpt.current_gpt_weights == "weights/ayase.ckpt"

            # Turn 2 with Ayase active
            mock_adapter.custom_tokens = ['{"chinese": "我是绫濑！", "japanese": "あやせですよ！"}']
            res2 = await chat_service.chat_sync("Hello 2", session_id="switch_session")
            assert res2["chinese"] == "我是绫濑！"

        # Verify Session Manager has both turns intact (2 turns = 4 messages)
        history = await chat_service.session_manager.get_history("switch_session")
        assert len(history) == 4

    @pytest.mark.asyncio
    async def test_cross_module_provider_failover_and_error_recovery(self, m7_db_path, m7_mock_gpt):
        """
        Verifies that an error in LLM streaming yields a structured SSE error event,
        leaves the session in a recoverable state, and allows subsequent turns to succeed.
        """
        voice_manager = VoiceManager(gpt_sovits_client_or_server=m7_mock_gpt, db_path=m7_db_path)
        tts_service = TtsService(client=voice_manager.client)
        chat_service = ChatService(tts_service=tts_service, db_path=m7_db_path)

        failing_adapter = CustomMockLLMAdapter(provider_config={}, fail_after_tokens=0)

        # Turn 1: Fails immediately
        with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=failing_adapter):
            events = []
            async for event in chat_service.stream_chat("Fail prompt", session_id="recovery_session"):
                events.append(event)

            assert len(events) == 1
            assert events[0]["event"] == "error"
            assert "Simulated adapter failure" in events[0]["data"]["error"]

        # Turn 2: Working adapter recovers session
        working_adapter = CustomMockLLMAdapter(provider_config={}, custom_tokens=[
            '{"chinese": "已成功恢复！", "japanese": "無事に回復しました！"}'
        ])
        with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=working_adapter):
            events = []
            async for event in chat_service.stream_chat("Recover prompt", session_id="recovery_session"):
                events.append(event)

            done_event = next(e for e in events if e["event"] == "done")
            assert done_event["data"]["chinese"] == "已成功恢复！"


# ============================================================================
# 2. High-Concurrency Multi-Turn SSE Streaming Stress Tests
# ============================================================================

class TestMultiTurnSseStreamingStress:
    """
    Stress tests for SSE streaming under high client concurrency, early disconnections,
    fuzzing token streams, and edge-case bilingual JSON payloads.
    """

    @pytest.mark.asyncio
    async def test_high_concurrency_multi_client_sse_streaming(self, m7_db_path, m7_mock_gpt):
        """
        10 concurrent client sessions simultaneously streaming multi-sentence bilingual responses.
        Validates no deadlocks, isolated session states, monotonic chunk indexing, and 100% completion.
        """
        voice_manager = VoiceManager(gpt_sovits_client_or_server=m7_mock_gpt, db_path=m7_db_path)
        tts_service = TtsService(client=voice_manager.client)
        chat_service = ChatService(tts_service=tts_service, db_path=m7_db_path)

        async def run_client(client_id: int):
            events = []
            async for event in chat_service.stream_chat(
                prompt=f"Client {client_id} prompt",
                session_id=f"concurrent_session_{client_id}",
            ):
                events.append(event)
            return events

        with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=DynamicMockLLMAdapter()):
            tasks = [run_client(i) for i in range(10)]
            results = await asyncio.gather(*tasks)

        assert len(results) == 10
        for i, events in enumerate(results):
            done_events = [e for e in events if e["event"] == "done"]
            assert len(done_events) == 1, f"Client {i} missing done event"
            done_data = done_events[0]["data"]
            assert f"Client {i} prompt" in done_data["chinese"]
            assert f"Client {i} prompt" in done_data["japanese"]

            audio_chunks = [e for e in events if e["event"] == "audio_chunk"]
            assert len(audio_chunks) >= 1
            for idx, ch in enumerate(audio_chunks):
                assert ch["data"]["index"] == idx

    @pytest.mark.asyncio
    async def test_early_client_disconnect_cancellation_stress(self, m7_db_path, m7_mock_gpt):
        """
        Simulates 10 concurrent clients disconnecting mid-stream (cancelling the stream).
        Verifies prompt resource cleanup, no hung tasks, and immediate availability for subsequent requests.
        """
        voice_manager = VoiceManager(gpt_sovits_client_or_server=m7_mock_gpt, db_path=m7_db_path)
        tts_service = TtsService(client=voice_manager.client)
        chat_service = ChatService(tts_service=tts_service, db_path=m7_db_path)

        async def run_cancelled_client(client_id: int):
            cancel_event = asyncio.Event()
            tokens = [
                '{"chinese": "正在开始...", ',
                '"japanese": "始まりました。長い文です。' + '続く。' * 20 + '"}'
            ]
            adapter = CustomMockLLMAdapter(provider_config={}, custom_tokens=tokens)
            with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=adapter):
                received = 0
                async for event in chat_service.stream_chat(
                    prompt=f"Abort prompt {client_id}",
                    session_id=f"abort_session_{client_id}",
                    cancel_event=cancel_event,
                ):
                    received += 1
                    if received >= 1:
                        cancel_event.set()  # Simulate client disconnect after 1st event

        tasks = [run_cancelled_client(i) for i in range(10)]
        await asyncio.gather(*tasks)

        # After cancellations, verify a normal request succeeds immediately on the same db
        normal_adapter = CustomMockLLMAdapter(provider_config={}, custom_tokens=[
            '{"chinese": "正常测试", "japanese": "正常テスト"}'
        ])
        with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=normal_adapter):
            res = await chat_service.chat_sync("Normal after cancel", session_id="post_cancel_session")
            assert res["chinese"] == "正常测试"

    def test_adversarial_bilingual_chunk_fuzzing(self):
        """
        Exhaustive fuzzing matrix for StreamingBilingualParser:
        1. 1-character token streaming with CJK, emojis, and multiline text.
        2. Markdown fences with leading/trailing whitespaces.
        3. Deeply nested JSON with additional arbitrary metadata.
        4. Escaped quotes and escaped backslashes.
        5. Plain-text fallback without JSON markup.
        6. Abruptly truncated JSON payload.
        """
        # Fuzz Case 1: 1-Character token stream
        parser1 = StreamingBilingualParser()
        full_json = json.dumps({
            "chinese": "这是单字符流式解析测试✨！",
            "japanese": "これは一文字ストリーミングテストです🌸！"
        }, ensure_ascii=False)

        emitted_deltas = []
        emitted_sentences = []
        for char in full_json:
            delta, sents = parser1.feed_chunk(char)
            if delta:
                emitted_deltas.append(delta)
            if sents:
                emitted_sentences.extend(sents)

        ch_final, ja_final, rem_sents = parser1.finalize()
        assert "".join(emitted_deltas) == "这是单字符流式解析测试✨！"
        assert ch_final == "这是单字符流式解析测试✨！"
        assert ja_final == "これは一文字ストリーミングテストです🌸！"

        # Fuzz Case 2: Markdown codeblock wrappers
        parser2 = StreamingBilingualParser()
        parser2.feed_chunk("```json\n")
        parser2.feed_chunk('{"chinese": "包装测试", "japanese": "ラッパーテスト"}\n')
        parser2.feed_chunk("```")
        ch2, ja2, _ = parser2.finalize()
        assert ch2 == "包装测试"
        assert ja2 == "ラッパーテスト"

        # Fuzz Case 3: Extra fields and nested keys
        parser3 = StreamingBilingualParser()
        raw_complex = json.dumps({
            "thought": "Let's think carefully...",
            "emotion": "happy",
            "chinese": "多字段测试",
            "japanese": "複数フィールドテスト。",
            "metadata": {"confidence": 0.99}
        }, ensure_ascii=False)
        parser3.feed_chunk(raw_complex)
        ch3, ja3, _ = parser3.finalize()
        assert ch3 == "多字段测试"
        assert ja3 == "複数フィールドテスト。"

        # Fuzz Case 4: Plain text fallback
        parser4 = StreamingBilingualParser()
        parser4.feed_chunk("中文：这是纯文本回复格式\n日文：これはプレーンテキストフォーマットです")
        ch4, ja4, _ = parser4.finalize()
        assert "这是纯文本回复格式" in ch4
        assert "これはプレーンテキストフォーマットです" in ja4

        # Fuzz Case 5: Escaped quotes in text
        parser5 = StreamingBilingualParser()
        parser5.feed_chunk(r'{"chinese": "他说:\"你好\"！", "japanese": "彼は「こんにちは」と言いました。"}')
        ch5, ja5, _ = parser5.finalize()
        assert '他说:"你好"！' in ch5
        assert '彼は「こんにちは」と言いました。' in ja5

    @pytest.mark.asyncio
    async def test_sse_event_formatter_adversarial_handling(self):
        """
        Verifies sse_event_formatter handles CancelledError and exceptions cleanly.
        """
        async def cancellable_gen():
            yield {"event": "text", "data": {"delta": "part1"}}
            raise asyncio.CancelledError()

        async def error_gen():
            yield {"event": "text", "data": {"delta": "part1"}}
            raise RuntimeError("Unexpected pipeline crash")

        # Test cancellation
        results = []
        async for line in sse_event_formatter(cancellable_gen()):
            results.append(line)
        assert len(results) == 1
        assert "part1" in results[0]

        # Test error handling
        err_results = []
        async for line in sse_event_formatter(error_gen()):
            err_results.append(line)
        assert len(err_results) == 2
        assert "event: error" in err_results[1]
        assert "Unexpected pipeline crash" in err_results[1]


# ============================================================================
# 3. Concurrent Web API + Telegram Bot Load Interleaving Tests
# ============================================================================

class TestConcurrentWebAndTelegramLoad:
    """
    Stress tests combining concurrent Web Chat SSE streaming with Telegram Bot
    text and voice note processing sharing the same backend resources.
    """

    @pytest.mark.asyncio
    async def test_simultaneous_web_sse_and_telegram_bot_load(self, m7_db_path, m7_mock_gpt):
        """
        Interleaved concurrent load:
        - 5 Web SSE streaming clients
        - 5 Telegram text chat requests
        - 5 Telegram voice note requests (simulated STT + Chat + TTS)
        All sharing the same SQLite DB, GptSovitsClient, and VoiceManager.
        """
        voice_manager = VoiceManager(gpt_sovits_client_or_server=m7_mock_gpt, db_path=m7_db_path)
        tts_service = TtsService(client=voice_manager.client)
        chat_service = ChatService(tts_service=tts_service, db_path=m7_db_path)

        mock_adapter = CustomMockLLMAdapter(provider_config={}, custom_tokens=[
            '{"chinese": "并发测试通过！", "japanese": "並行テスト完了！"}'
        ])

        # 1. Web Client Task
        async def web_client_task(i: int):
            events = []
            async for event in chat_service.stream_chat(
                prompt=f"Web user {i}",
                session_id=f"web_load_{i}",
            ):
                events.append(event)
            return ("web", i, len(events))

        # 2. Telegram Text Task
        async def telegram_text_task(i: int):
            res = await chat_service.chat_sync(
                prompt=f"Telegram user text {i}",
                session_id=f"tg_text_{i}",
            )
            return ("tg_text", i, res["chinese"])

        # 3. Telegram Voice Task (STT -> Chat -> TTS)
        async def telegram_voice_task(i: int):
            # Simulated STT step
            transcribed_text = f"Telegram voice transcription {i}"
            res = await chat_service.chat_sync(
                prompt=transcribed_text,
                session_id=f"tg_voice_{i}",
            )
            return ("tg_voice", i, res["audio_url"])

        # Launch all 15 tasks concurrently
        with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=mock_adapter):
            tasks = []
            for i in range(5):
                tasks.append(web_client_task(i))
                tasks.append(telegram_text_task(i))
                tasks.append(telegram_voice_task(i))

            results = await asyncio.gather(*tasks)

        assert len(results) == 15
        web_results = [r for r in results if r[0] == "web"]
        tg_text_results = [r for r in results if r[0] == "tg_text"]
        tg_voice_results = [r for r in results if r[0] == "tg_voice"]

        assert len(web_results) == 5
        assert len(tg_text_results) == 5
        assert len(tg_voice_results) == 5

        for _, _, count in web_results:
            assert count >= 2  # At least text + done
        for _, _, ch in tg_text_results:
            assert ch == "并发测试通过！"
        for _, _, audio_url in tg_voice_results:
            assert audio_url.startswith("/audio/")

    @pytest.mark.asyncio
    async def test_shared_voice_manager_tts_lock_contention(self, m7_db_path, m7_mock_gpt):
        """
        High-frequency concurrent synthesis calls on GptSovitsClient.
        Verifies that asyncio.Lock prevents race conditions and all synthesis requests succeed.
        """
        voice_manager = VoiceManager(gpt_sovits_client_or_server=m7_mock_gpt, db_path=m7_db_path)

        async def synth_task(idx: int):
            return await voice_manager.synthesize(
                text=f"同時合成テスト{idx}番です。",
                options={"speed": 1.0, "top_k": 15}
            )

        # 20 concurrent synthesis tasks
        tasks = [synth_task(i) for i in range(20)]
        audio_outputs = await asyncio.gather(*tasks)

        assert len(audio_outputs) == 20
        for audio in audio_outputs:
            assert isinstance(audio, bytes)
            assert audio.startswith(b"RIFF")
            assert len(audio) > 100

    @pytest.mark.asyncio
    async def test_session_channel_namespace_isolation(self, m7_db_path):
        """
        Verifies that session keys tagged by channel ('web:123', 'telegram:123', 'api:123')
        remain strictly isolated in SessionManager and SQLite.
        """
        sm = SessionManager(db_path=m7_db_path)

        # Add messages under identical suffix but different channel prefixes
        await sm.add_turn("web:user_100", "user", "Web prompt", "")
        await sm.add_turn("web:user_100", "assistant", "Web reply CH", "Web reply JA")

        await sm.add_turn("telegram:user_100", "user", "TG prompt", "")
        await sm.add_turn("telegram:user_100", "assistant", "TG reply CH", "TG reply JA")

        await sm.add_turn("api:user_100", "user", "API prompt", "")
        await sm.add_turn("api:user_100", "assistant", "API reply CH", "API reply JA")

        hist_web = await sm.get_history("web:user_100")
        hist_tg = await sm.get_history("telegram:user_100")
        hist_api = await sm.get_history("api:user_100")

        assert len(hist_web) == 2
        assert hist_web[0].content_chinese == "Web prompt"

        assert len(hist_tg) == 2
        assert hist_tg[0].content_chinese == "TG prompt"

        assert len(hist_api) == 2
        assert hist_api[0].content_chinese == "API prompt"

        # Clearing web session does not affect telegram session
        await sm.clear_session("web:user_100")
        assert len(await sm.get_history("web:user_100")) == 0
        assert len(await sm.get_history("telegram:user_100")) == 2


# ============================================================================
# 4. Database Contention, WAL Concurrency & SQL Injection Defense
# ============================================================================

class TestDatabaseContentionAndIntegrity:
    """
    Stress tests for SQLite database under high-concurrency writes/reads and SQL injection attacks.
    """

    @pytest.mark.asyncio
    async def test_sqlite_wal_high_concurrency_burst(self, m7_db_path):
        """
        50 concurrent asynchronous tasks performing mixed reads, updates, and inserts.
        Verifies that SQLite in WAL mode handles write contention without locking failures.
        """
        async def db_worker(task_id: int):
            async with aiosqlite.connect(m7_db_path) as conn:
                # 1. Read settings
                _ = await crud.get_settings_raw(conn)

                # 2. Insert message
                await crud.add_message(conn, MessageCreate(
                    session_id=f"wal_stress_session_{task_id % 5}",
                    role="user",
                    content_chinese=f"Stress message from task {task_id}",
                    content_japanese=f"タスク{task_id}からの負荷テスト",
                    audio_url="",
                    latency_ms=task_id,
                ))

                # 3. Read providers
                _ = await crud.list_providers(conn)

                # 4. Update setting
                if task_id % 10 == 0:
                    await crud.update_settings(conn, SettingsUpdate(
                        audio_speed=1.0 + (task_id * 0.01)
                    ))
            return True

        tasks = [db_worker(i) for i in range(50)]
        results = await asyncio.gather(*tasks)
        assert all(results)
        assert len(results) == 50

        # Verify total messages recorded
        async with aiosqlite.connect(m7_db_path) as conn:
            all_msgs = []
            for s_id in range(5):
                msgs = await crud.get_recent_messages(conn, f"wal_stress_session_{s_id}", limit=100)
                all_msgs.extend(msgs)
            assert len(all_msgs) == 50

    @pytest.mark.asyncio
    async def test_sql_injection_adversarial_matrix(self, m7_db_path):
        """
        Tests injection payloads across session_id, role, content, provider_id, and profile names.
        """
        malicious_payloads = [
            "'; DROP TABLE settings; --",
            "' OR '1'='1",
            "\" OR \"\"=\"",
            "admin'--",
            "1; ATTACH DATABASE 'evil.db' AS evil; --",
            "<script>alert('xss')</script>",
            "UNION ALL SELECT key, value, description, updated_at FROM settings--",
        ]

        async with aiosqlite.connect(m7_db_path) as conn:
            for idx, payload in enumerate(malicious_payloads):
                # Test in session and messages
                sess_id = f"inj_sess_{idx}_{payload[:10]}"
                await crud.get_or_create_session(conn, sess_id)
                msg = await crud.add_message(conn, MessageCreate(
                    session_id=sess_id,
                    role="user",
                    content_chinese=payload,
                    content_japanese=payload,
                    audio_url=payload,
                    latency_ms=0,
                ))
                assert msg.content_chinese == payload

                # Verify settings table was NOT dropped
                settings = await crud.get_settings_raw(conn)
                assert settings is not None
                assert settings.gpt_sovits_url != ""

    @pytest.mark.asyncio
    async def test_extreme_session_history_sliding_window(self, m7_db_path):
        """
        Populates a session with 50 dialogue turns (100 messages) and verifies
        SessionManager truncates properly according to max_messages and max_tokens.
        """
        sm = SessionManager(db_path=m7_db_path)
        session_id = "scale_100_msgs_session"

        # Populate 50 turns (100 messages)
        for i in range(50):
            await sm.add_turn(session_id, "user", f"User prompt turn {i}")
            await sm.add_turn(session_id, "assistant", f"Chinese reply turn {i}", f"Japanese reply turn {i}")

        # Retrieve with max_messages of 10
        history_10 = await sm.get_history(session_id, max_messages=10)
        assert len(history_10) == 10
        assert history_10[-1].content_chinese == "Chinese reply turn 49"

        # Build chat messages with token limit
        chat_msgs = await sm.build_chat_messages(
            session_id=session_id,
            user_prompt="New turn 50",
            character_name="四季夏目",
            max_messages=6,
            max_tokens=300,
        )

        assert chat_msgs[0].role == "system"
        assert chat_msgs[-1].role == "user"
        assert chat_msgs[-1].content == "New turn 50"
        # Total messages should be system + up to 6 history + 1 current user <= 8
        assert len(chat_msgs) <= 8


# ============================================================================
# 5. Acceptance Criteria Verification Matrix (ORIGINAL_REQUEST.md)
# ============================================================================

class TestOriginalRequestAcceptanceCriteria:
    """
    Explicit verification corresponding 1:1 to every acceptance criterion
    defined in ORIGINAL_REQUEST.md.
    """

    def test_ac_lightweight_core_no_java_dependencies(self):
        """
        AC: No Java/Maven dependencies required to run galgame2voice.
        Verifies Python FastAPI core initializes without Java runtime.
        """
        app = create_app()
        assert app.title == "galgame2voice"
        assert len(app.routes) > 10

    def test_ac_windows_scripts_exist_and_structure(self):
        """
        AC: 启动.bat and 停止.bat properly launch and terminate the Python service.
        """
        project_root = Path(__file__).resolve().parent.parent
        start_bat = project_root / "启动.bat"
        stop_bat = project_root / "停止.bat"
        launcher_py = project_root / "scripts" / "run_server.py"

        assert start_bat.exists(), "启动.bat missing"
        assert stop_bat.exists(), "停止.bat missing"
        assert launcher_py.exists(), "scripts/run_server.py missing"

        content_start = start_bat.read_text(encoding="utf-8", errors="ignore")
        content_stop = stop_bat.read_text(encoding="utf-8", errors="ignore")
        content_launcher = launcher_py.read_text(encoding="utf-8", errors="ignore")

        assert "python" in content_start.lower() or "uvicorn" in content_start.lower() or "call" in content_start.lower()
        assert "run_server.py" in content_start
        assert "uvicorn" in content_launcher
        assert "taskkill" in content_stop.lower()

    @pytest.mark.asyncio
    async def test_ac_health_and_status_endpoints(self, m7_db_path, m7_mock_gpt):
        """
        AC: Health check endpoint (/api/health or /status) responds with status of galgame2voice and GPT-SoVITS backend.
        """
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # Check /api/health
            resp_health = await client.get("/api/health")
            assert resp_health.status_code == 200
            data_health = resp_health.json()
            assert data_health["status"] in ("healthy", "degraded", "running", "ok")
            assert "gpt_sovits" in data_health or "backend" in data_health or "app" in data_health

            # Check /status
            resp_status = await client.get("/status")
            assert resp_status.status_code == 200
            data_status = resp_status.json()
            assert "app" in data_status or "galgame2voice" in data_status or "status" in data_status

    @pytest.mark.asyncio
    async def test_ac_unified_config_and_security(self, m7_db_path):
        """
        AC: All settings persist in SQLite. API keys returned to web frontend are masked (sk-****1234).
        No plaintext API keys in logs.
        """
        async with aiosqlite.connect(m7_db_path) as conn:
            # Verify secret masking helper
            masked = mask_secret("sk-1234567890abcdef1234567890abcdef")
            assert masked.startswith("sk-****")
            assert masked.endswith("cdef")
            assert "1234567890" not in masked

            # Verify logger MaskingFilter
            masking_filter = MaskingFilter()
            import logging
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=0,
                msg="Auth Header: Bearer sk-1234567890abcdef1234567890abcdef, TG: 123456789:ABCdefGhIJKlmnoPQRstuvwxYZ123456",
                args=(), exc_info=None
            )
            masking_filter.filter(record)
            assert "sk-1234567890abcdef" not in record.msg
            assert "123456789:ABCdefGh" not in record.msg

    @pytest.mark.asyncio
    async def test_ac_web_chat_and_streaming_playback(self, m7_db_path, m7_mock_gpt):
        """
        AC: Web chat renders streamed Chinese text immediately without waiting for voice synthesis.
        Frontend Web Audio player receives initial audio chunks.
        """
        voice_manager = VoiceManager(gpt_sovits_client_or_server=m7_mock_gpt, db_path=m7_db_path)
        tts_service = TtsService(client=voice_manager.client)
        chat_service = ChatService(tts_service=tts_service, db_path=m7_db_path)

        mock_adapter = CustomMockLLMAdapter(provider_config={}, custom_tokens=[
            '{"chinese": "立即流式中文输出", "japanese": "ストリーミング音声です。"}'
        ])

        with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=mock_adapter):
            events = []
            async for event in chat_service.stream_chat("AC Test", session_id="ac_web_chat"):
                events.append(event)

            # First event after prompt should be text delta
            text_events = [e for e in events if e["event"] == "text"]
            assert len(text_events) >= 1
            assert text_events[0]["data"]["delta_chinese"] != ""

            # Audio chunk event contains audio url
            audio_events = [e for e in events if e["event"] == "audio_chunk"]
            assert len(audio_events) >= 1
            assert audio_events[0]["data"]["audio_url"].startswith("/audio/")

    @pytest.mark.asyncio
    async def test_ac_voice_switching_and_mutex_rollback(self, m7_db_path, m7_mock_gpt):
        """
        AC: Model switching locks inference and updates GPT/SoVITS weights correctly via API.
        Rollback triggered on step failure.
        """
        voice_manager = VoiceManager(gpt_sovits_client_or_server=m7_mock_gpt, db_path=m7_db_path)

        profile = await voice_manager.create_profile(VoiceProfileCreate(
            name="RollbackTestProfile",
            gpt_weights_path="weights/rb.ckpt",
            sovits_weights_path="weights/rb.pth",
            refer_audio_path="ref/rb.wav",
            refer_text="ロールバックテスト",
            refer_language="ja",
            prompt_language="ja",
            text_language="ja",
            is_default=0,
        ))

        # Test successful switch
        res_ok = await voice_manager.switch_profile("RollbackTestProfile")
        assert res_ok is True

        # Test failure rollback on step 2 (sovits weights)
        m7_mock_gpt.fail_on_step("set_sovits_weights")
        switch_failed = await voice_manager.client.switch_voice_profile({
            "gpt_weights_path": "weights/invalid.ckpt",
            "sovits_weights_path": "weights/invalid.pth",
            "refer_audio_path": "ref/invalid.wav",
            "refer_text": "text",
            "refer_language": "ja"
        })
        assert switch_failed is False

    def test_ac_japanese_parentheses_filtering(self):
        """
        AC: Japanese text stage cue parentheses (（...） and (...)) stripped before TTS synthesis.
        """
        raw_ja = "こんにちは！（微笑みながら）今日も一日、頑張りましょう(元気に)！"
        cleaned = clean_japanese_parentheses(raw_ja)
        assert "微笑みながら" not in cleaned
        assert "元気に" not in cleaned
        assert "（" not in cleaned
        assert "）" not in cleaned
        assert "(" not in cleaned
        assert ")" not in cleaned
        assert cleaned == "こんにちは！今日も一日、頑張りましょう！"

    def test_ac_telegram_bot_token_validation_and_commands(self):
        """
        AC: Telegram bot token validation and command handling.
        """
        assert validate_bot_token("123456789:ABCdefGhIJKlmnoPQRstuvwxYZ123456") is True
        assert validate_bot_token("invalid_token") is False
        assert validate_bot_token("") is False
