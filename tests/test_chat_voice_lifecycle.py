"""Cross-module lifecycle tests spanning DB, providers, voice profiles, SSE chat, and direct TTS."""

import asyncio
from typing import Any, AsyncGenerator, Dict, List, Optional
from unittest.mock import patch

import aiosqlite
import pytest

from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult
from galgame2voice.database import crud
from galgame2voice.database.models import ProviderCreate, VoiceProfileCreate
from galgame2voice.database.session import get_db
from galgame2voice.services.character_voice_catalog import get_character_voice_catalog
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.voice_manager import VoiceManager


class _CustomMockLLMAdapter(BaseLLMAdapter):
    """Configurable mock adapter for controlled streaming."""

    def __init__(
        self,
        provider_config: Optional[Dict[str, Any]] = None,
        custom_tokens: Optional[List[str]] = None,
    ):
        cfg = provider_config or {}
        super().__init__(
            api_key=cfg.get("api_key", "sk-mock-key"),
            base_url=cfg.get("base_url", "https://api.openai.com/v1"),
        )
        self.custom_tokens = custom_tokens or [
            '{"chinese": "你好，指挥官！", "japanese": "こんにちは、指揮官！今日も頑張りましょう。'
            '（微笑みながら）よろしくね。"}'
        ]

    async def chat(self, messages: List[ChatMessage], model: str, temperature: float = 1.0, **kwargs) -> LLMResponse:
        return LLMResponse(
            content="".join(self.custom_tokens),
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )

    async def stream_chat(
        self, messages: List[ChatMessage], model: str, temperature: float = 1.0, **kwargs
    ) -> AsyncGenerator[str, None]:
        for token in self.custom_tokens:
            await asyncio.sleep(0.001)
            yield token

    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        return TestResult(success=True, message="Custom mock adapter connection verified", latency_ms=12.5)

    async def list_models(self) -> List[str]:
        return ["custom-model-v1", "custom-model-v2"]


@pytest.mark.asyncio
async def test_e2e_fresh_db_to_multiturn_dialogue(isolate_test_database, configured_voice, mock_gpt_sovits):
    """Lifecycle: create provider & voice profile (legacy aliases), switch voice, run 3-turn SSE chat, verify DB & masking."""
    voice_manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=isolate_test_database)
    tts_service = voice_manager.tts_service

    async with aiosqlite.connect(isolate_test_database) as conn:
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

    mock_adapter = _CustomMockLLMAdapter(
        provider_config={"api_key": "sk-siliconflow-secret-key-1234567890"},
        custom_tokens=[
            '{"chinese": "第一句话。第二句话！", ',
            '"japanese": "最初の文です。（微笑み）二番目の文です！"}',
        ],
    )

    chat_service = ChatService(tts_service=tts_service, db_path=isolate_test_database)

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

    async with aiosqlite.connect(isolate_test_database) as conn:
        history = await crud.get_recent_messages(conn, "lifecycle_session_001")
        assert len(history) == 6  # 3 user messages + 3 assistant messages

        providers = await crud.list_providers(conn)
        sf_prov = next(p for p in providers if p.id == "custom_cloud")
        assert sf_prov.api_key.startswith("sk-****")
        assert "7890" in sf_prov.api_key
        assert "secret-key" not in sf_prov.api_key


@pytest.mark.asyncio
async def test_cross_module_profile_switch_interleaved_in_active_session(
    isolate_test_database, configured_voice, mock_gpt_sovits,
):
    """Switching voice profile mid-session updates TTS weights without breaking session history."""
    voice_manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=isolate_test_database)
    tts_service = voice_manager.tts_service
    chat_service = ChatService(tts_service=tts_service, db_path=isolate_test_database)

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

    mock_adapter = _CustomMockLLMAdapter(
        provider_config={},
        custom_tokens=['{"chinese": "我是第一位角色。", "japanese": "私は一人目です。"}'],
    )

    with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=mock_adapter):
        events1 = [e async for e in chat_service.stream_chat("Hello 1", session_id="switch_session")]
        done1 = next(e for e in events1 if e["event"] == "done")
        assert done1["data"]["chinese"] == "我是第一位角色。"
        assert mock_gpt_sovits.current_gpt_weights != "weights/ayase.ckpt"

        switch_ok = await voice_manager.switch_profile("Ayase")
        assert switch_ok is True
        assert mock_gpt_sovits.current_gpt_weights == "weights/ayase.ckpt"

        mock_adapter.custom_tokens = ['{"chinese": "我是绫濑！", "japanese": "あやせですよ！"}']
        events2 = [e async for e in chat_service.stream_chat("Hello 2", session_id="switch_session")]
        done2 = next(e for e in events2 if e["event"] == "done")
        assert done2["data"]["chinese"] == "我是绫濑！"

    history = await chat_service.session_manager.get_history("switch_session")
    assert len(history) == 4


@pytest.mark.asyncio
async def test_simultaneous_web_sse_and_direct_tts_load(
    isolate_test_database, configured_voice, mock_gpt_sovits,
):
    """Concurrent web SSE streaming and direct TtsService.synthesize sharing backend resources."""
    voice_manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=isolate_test_database)
    tts_service = voice_manager.tts_service
    chat_service = ChatService(tts_service=tts_service, db_path=isolate_test_database)

    mock_adapter = _CustomMockLLMAdapter(
        provider_config={},
        custom_tokens=['{"chinese": "并发测试通过！", "japanese": "並行テスト完了！"}'],
    )

    async def web_client_task(i: int):
        events = []
        async for event in chat_service.stream_chat(
            prompt=f"Web user {i}",
            session_id=f"web_load_{i}",
        ):
            events.append(event)
        return ("web", i, len(events))

    async def direct_tts_task(i: int):
        audio_bytes = await tts_service.synthesize(
            text=f"並行合成テスト{i}番です。",
            options={"speed": 1.0, "top_k": 15},
        )
        return ("direct_tts", i, len(audio_bytes))

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
        assert count >= 2
    for _, _, byte_len in tts_results:
        assert byte_len > 100
