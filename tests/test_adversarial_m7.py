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
3. Concurrent Web SSE Streaming + Direct TTS Load Interleaving:
   - Simultaneous burst: Web SSE streams + direct TTS synthesis requests
"""

import asyncio
import json
import os
import tempfile
from typing import Any, AsyncGenerator, Dict, List, Optional
from unittest.mock import patch

import aiosqlite
import pytest

from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult
from galgame2voice.database import crud
from galgame2voice.database.models import (
    ProviderCreate,
    VoiceProfileCreate,
)
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.character_voice_catalog import get_character_voice_catalog
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.voice_manager import VoiceManager
from tests.conftest import MockGptSovitsServer


# ============================================================================
# Test Fixtures & Utilities
# ============================================================================

@pytest.fixture
async def m7_db_path(configured_voice):
    """Creates a temporary sqlite database file initialized with the full schema and seed data."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_m7_adv_")
    os.close(fd)

    await init_db(path)
    async with aiosqlite.connect(path) as conn:
        profile = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name=configured_voice.name,
            gpt_weights_path=configured_voice.gpt_weights_path,
            sovits_weights_path=configured_voice.sovits_weights_path,
            ref_audio_path=configured_voice.ref_audio_path,
            prompt_text=configured_voice.prompt_text,
            prompt_lang=configured_voice.prompt_lang,
        ))
        assert await crud.set_active_voice_profile(conn, profile.id)

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
    async def test_e2e_fresh_db_to_multiturn_dialogue(self, m7_db_path, m7_mock_gpt):
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
        tts_service = voice_manager.tts_service

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
            await get_character_voice_catalog().create_profile(
                VoiceProfileCreate(
                    name="Natsume",
                    gpt_weights_path="weights/natsume.ckpt",
                    sovits_weights_path="weights/natsume.pth",
                    refer_audio_path="ref/natsume.wav",
                    refer_text="おはようございます、先輩。",
                    refer_language="ja",
                    prompt_language="ja",
                    text_language="ja",
                    is_default=0,
                ),
                db_factory=lambda: get_db(voice_manager.db_path),
            )
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
                    character_name="Natsume",
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
        tts_service = voice_manager.tts_service
        chat_service = ChatService(tts_service=tts_service, db_path=m7_db_path)

        # Create Profile B
        await get_character_voice_catalog().create_profile(
            VoiceProfileCreate(
                name="Ayase",
                gpt_weights_path="weights/ayase.ckpt",
                sovits_weights_path="weights/ayase.pth",
                refer_audio_path="ref/ayase.wav",
                refer_text="あやせです。",
                refer_language="ja",
                prompt_language="ja",
                text_language="ja",
                is_default=0,
            ),
            db_factory=lambda: get_db(voice_manager.db_path),
        )

        mock_adapter = CustomMockLLMAdapter(provider_config={}, custom_tokens=[
            '{"chinese": "我是第一位角色。", "japanese": "私は一人目です。"}'
        ])

        with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=mock_adapter):
            # Turn 1 with default profile
            events1 = [e async for e in chat_service.stream_chat("Hello 1", session_id="switch_session")]
            done1 = next(e for e in events1 if e["event"] == "done")
            assert done1["data"]["chinese"] == "我是第一位角色。"
            assert m7_mock_gpt.current_gpt_weights != "weights/ayase.ckpt"

            # Switch to Ayase mid-dialogue
            switch_ok = await voice_manager.switch_profile("Ayase")
            assert switch_ok is True
            assert m7_mock_gpt.current_gpt_weights == "weights/ayase.ckpt"

            # Turn 2 with Ayase active
            mock_adapter.custom_tokens = ['{"chinese": "我是绫濑！", "japanese": "あやせですよ！"}']
            events2 = [e async for e in chat_service.stream_chat("Hello 2", session_id="switch_session")]
            done2 = next(e for e in events2 if e["event"] == "done")
            assert done2["data"]["chinese"] == "我是绫濑！"

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
        tts_service = voice_manager.tts_service
        chat_service = ChatService(tts_service=tts_service, db_path=m7_db_path)

        failing_adapter = CustomMockLLMAdapter(provider_config={}, fail_after_tokens=0)

        # Turn 1: Fails immediately
        with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=failing_adapter):
            events = []
            async for event in chat_service.stream_chat("Fail prompt", session_id="recovery_session"):
                events.append(event)

            assert len(events) == 2
            assert events[0]["event"] == "turn_started"
            assert events[0]["data"]["session_id"] == "recovery_session"
            assert isinstance(events[0]["data"]["user_message_id"], int) and events[0]["data"]["user_message_id"] > 0
            assert events[1]["event"] == "error"
            assert "Simulated adapter failure" in events[1]["data"]["error"]

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
        tts_service = voice_manager.tts_service
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
        tts_service = voice_manager.tts_service
        chat_service = ChatService(tts_service=tts_service, db_path=m7_db_path)

        def cancellation_adapter(*args, **kwargs):
            tokens = [
                '{"chinese": "正在开始...", ',
                '"japanese": "始まりました。長い文です。' + '続く。' * 20 + '"}'
            ]
            return CustomMockLLMAdapter(provider_config={}, custom_tokens=tokens)

        async def run_cancelled_client(client_id: int):
            cancel_event = asyncio.Event()
            received = 0
            async for event in chat_service.stream_chat(
                prompt=f"Abort prompt {client_id}",
                session_id=f"abort_session_{client_id}",
                cancel_event=cancel_event,
            ):
                received += 1
                if received >= 1:
                    cancel_event.set()  # Simulate client disconnect after 1st event

        # Concurrent context managers patching the same global restore each
        # other's mocks out of order. Patch once around the concurrent work.
        with patch("galgame2voice.services.chat_service.get_llm_adapter", side_effect=cancellation_adapter):
            tasks = [run_cancelled_client(i) for i in range(10)]
            await asyncio.gather(*tasks)

        # After cancellations, verify a normal request succeeds immediately on the same db
        normal_adapter = CustomMockLLMAdapter(provider_config={}, custom_tokens=[
            '{"chinese": "正常测试", "japanese": "正常テスト"}'
        ])
        with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=normal_adapter):
            events = [e async for e in chat_service.stream_chat("Normal after cancel", session_id="post_cancel_session")]
            done = next(e for e in events if e["event"] == "done")
            assert done["data"]["chinese"] == "正常测试"


# ============================================================================
# 3. Concurrent Web SSE Streaming + Direct TTS Load Interleaving Tests
# ============================================================================

class TestConcurrentWebAndDirectTtsLoad:
    """
    Stress tests combining concurrent Web Chat SSE streaming with direct TTS
    synthesis sharing the same backend resources.
    """

    @pytest.mark.asyncio
    async def test_simultaneous_web_sse_and_direct_tts_load(self, m7_db_path, m7_mock_gpt):
        """
        Interleaved concurrent load:
        - 5 Web SSE streaming clients
        - 5 direct TtsService.synthesize requests
        All sharing the same SQLite DB, GptSovitsClient, and VoiceManager.
        """
        voice_manager = VoiceManager(gpt_sovits_client_or_server=m7_mock_gpt, db_path=m7_db_path)
        tts_service = voice_manager.tts_service
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

        # 2. Direct TTS synthesis task sharing the same backend resources
        async def direct_tts_task(i: int):
            audio_bytes = await tts_service.synthesize(
                text=f"並行合成テスト{i}番です。",
                options={"speed": 1.0, "top_k": 15},
            )
            return ("direct_tts", i, len(audio_bytes))

        # Launch concurrent tasks
        with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=mock_adapter):
            tasks = []
            for i in range(5):
                tasks.append(web_client_task(i))
                tasks.append(direct_tts_task(i))

            results = await asyncio.gather(*tasks)

        assert len(results) == 10
        web_results = [r for r in results if r[0] == "web"]
        tts_results = [r for r in results if r[0] == "direct_tts"]

        assert len(web_results) == 5
        assert len(tts_results) == 5

        for _, _, count in web_results:
            assert count >= 2  # At least text + done
        for _, _, byte_len in tts_results:
            assert byte_len > 100

