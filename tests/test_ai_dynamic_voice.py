"""
Tests for AI-Driven Dynamic Voice Inference Settings.
Covers:
1. Dynamic parameter clamping helpers (clamp_dynamic_speed, clamp_dynamic_temperature)
2. StreamingBilingualParser dynamic tts parameter extraction, safety clamping, and backward compatibility
3. TtsService dynamic voice option sanitization and slider enforcement in fixed parameter mode
4. ChatService stream_chat and chat_sync dynamic parameter resolution and tts_params event emission
5. Chat router endpoints (/api/chat/stream, /api/chat) forwarding ai_adaptive_voice
"""

import asyncio
import json
import math
from typing import Any, AsyncIterator, Dict, List, Optional
import pytest
from httpx import AsyncClient, ASGITransport

from galgame2voice.services.gpt_sovits_client import (
    DYNAMIC_SPEED_MIN,
    DYNAMIC_SPEED_MAX,
    DYNAMIC_TEMP_MIN,
    DYNAMIC_TEMP_MAX,
    clamp_dynamic_speed,
    clamp_dynamic_temperature,
)
from galgame2voice.services.chat_service import StreamingBilingualParser, ChatService
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, TestResult
from galgame2voice.routers.chat import set_chat_service, router as chat_router, ChatRequest
from galgame2voice.main import create_app


# ============================================================================
# Mock Adapter for Dynamic Voice Testing
# ============================================================================

class MockDynamicLLMAdapter(BaseLLMAdapter):
    def __init__(self, stream_chunks: Optional[List[str]] = None, sync_response: Optional[str] = None):
        super().__init__(api_key="sk-test-dynamic", base_url="http://mock-dynamic-llm")
        self.stream_chunks = stream_chunks or [
            '{"tts": {"speed": 1.25, "temp": 0.85, "emotion": "tsundere"}, ',
            '"chinese": "哼！才不是为了你特意过来的呢！", ',
            '"japanese": "べ、別にアンタのために来たんじゃないんだからね！"}'
        ]
        self.sync_response = sync_response or json.dumps({
            "tts": {"speed": 1.15, "temp": 0.90, "emotion": "shy"},
            "chinese": "那个……能一直留在我身边吗？",
            "japanese": "あの……ずっとそばにいてくれますか？"
        }, ensure_ascii=False)

    async def chat(self, messages: List[ChatMessage], model: str, temperature: float = 1.0, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content=self.sync_response)

    async def stream_chat(self, messages: List[ChatMessage], model: str, temperature: float = 1.0, **kwargs: Any) -> AsyncIterator[str]:
        for chunk in self.stream_chunks:
            await asyncio.sleep(0.002)
            yield chunk

    async def test_connection(self, model: Optional[str] = None) -> TestResult:
        return TestResult(success=True, message="Connected", latency_ms=5.0)

    async def list_models(self) -> List[str]:
        return ["mock-chat"]


# ============================================================================
# 1. Clamping Function Unit Tests
# ============================================================================

class TestDynamicClampingFunctions:
    """Validates boundary rules and graceful fallbacks for clamp helpers."""

    def test_clamp_speed_valid_ranges(self):
        assert clamp_dynamic_speed(1.0) == 1.0
        assert clamp_dynamic_speed(0.70) == 0.70
        assert clamp_dynamic_speed(1.35) == 1.35
        assert clamp_dynamic_speed(1.154) == 1.154

    def test_clamp_speed_out_of_bounds(self):
        assert clamp_dynamic_speed(0.1) == DYNAMIC_SPEED_MIN
        assert clamp_dynamic_speed(-5.0) == DYNAMIC_SPEED_MIN
        assert clamp_dynamic_speed(2.0) == DYNAMIC_SPEED_MAX
        assert clamp_dynamic_speed(999.0) == DYNAMIC_SPEED_MAX

    def test_clamp_speed_invalid_types_and_nan(self):
        assert clamp_dynamic_speed(None, fallback=1.0) == 1.0
        assert clamp_dynamic_speed("invalid", fallback=0.9) == 0.9
        assert clamp_dynamic_speed(float("nan"), fallback=1.0) == 1.0
        assert clamp_dynamic_speed(float("inf"), fallback=1.0) == 1.0
        assert clamp_dynamic_speed({}, fallback=1.1) == 1.1

    def test_clamp_temperature_valid_ranges(self):
        assert clamp_dynamic_temperature(1.0) == 1.0
        assert clamp_dynamic_temperature(0.60) == 0.60
        assert clamp_dynamic_temperature(1.20) == 1.20
        assert clamp_dynamic_temperature(0.85) == 0.85

    def test_clamp_temperature_out_of_bounds(self):
        assert clamp_dynamic_temperature(0.0) == DYNAMIC_TEMP_MIN
        assert clamp_dynamic_temperature(-1.0) == DYNAMIC_TEMP_MIN
        assert clamp_dynamic_temperature(1.8) == DYNAMIC_TEMP_MAX
        assert clamp_dynamic_temperature(10.0) == DYNAMIC_TEMP_MAX

    def test_clamp_temperature_invalid_types_and_nan(self):
        assert clamp_dynamic_temperature(None, fallback=1.0) == 1.0
        assert clamp_dynamic_temperature("bad", fallback=0.75) == 0.75
        assert clamp_dynamic_temperature(float("nan"), fallback=1.0) == 1.0
        assert clamp_dynamic_temperature(float("inf"), fallback=1.0) == 1.0


# ============================================================================
# 2. StreamingBilingualParser Dynamic Voice Tests
# ============================================================================

class TestStreamingBilingualParserDynamicVoice:
    """Verifies incremental parsing of dynamic TTS inference parameters."""

    def test_parser_extracts_dynamic_tts_fields_fully(self):
        parser = StreamingBilingualParser()
        raw = json.dumps({
            "tts": {"speed": 1.2, "temp": 0.8, "emotion": "tsundere"},
            "chinese": "不理你了！",
            "japanese": "もう知らない！"
        }, ensure_ascii=False)

        parser.feed_chunk(raw)
        zh, ja, rem = parser.finalize()

        assert zh == "不理你了！"
        assert ja == "もう知らない！"
        assert parser.tts_speed == 1.2
        assert parser.tts_temperature == 0.8
        assert parser.tts_emotion == "tsundere"
        assert parser.get_emotion() == "tsundere"

    def test_parser_early_extraction_before_japanese_completes(self):
        """Dynamic parameters in chunk 0 must be immediately available before sentence 0 is completed."""
        parser = StreamingBilingualParser()

        # Chunk 0: tts parameters are present
        delta_ch, sentences = parser.feed_chunk('{"tts": {"speed": 1.1, "temp": 0.9, "emotion": "happy"}, "chinese": "你好')
        assert parser.tts_speed == 1.1
        assert parser.tts_temperature == 0.9
        assert parser.tts_emotion == "happy"

        # Check dynamic options retrieval while stream is ongoing
        base_opts = {"speed": 0.8, "temperature": 0.7, "top_k": 15}
        merged = parser.get_dynamic_tts_options(base_opts, adaptive_enabled=True)
        assert merged["speed"] == 1.1
        assert merged["temperature"] == 0.9
        assert merged["top_k"] == 15
        assert merged["emotion"] == "happy"

        # Chunk 1: completes chinese and japanese sentence
        delta_ch, sentences = parser.feed_chunk('！", "japanese": "こんにちは！"}')
        assert len(sentences) == 1
        assert sentences[0] == "こんにちは！"

    def test_parser_clamps_extreme_llm_dynamic_parameters(self):
        parser = StreamingBilingualParser()
        raw = '{"tts": {"speed": 3.5, "temp": 0.1, "emotion": "cool"}, "chinese": "...", "japanese": "..."}'
        parser.feed_chunk(raw)
        parser.finalize()

        # speed clamped to DYNAMIC_SPEED_MAX (1.35)
        assert parser.tts_speed == DYNAMIC_SPEED_MAX
        # temp clamped to DYNAMIC_TEMP_MIN (0.60)
        assert parser.tts_temperature == DYNAMIC_TEMP_MIN
        assert parser.tts_emotion == "cool"

    def test_parser_adaptive_disabled_preserves_base_options(self):
        parser = StreamingBilingualParser()
        raw = '{"tts": {"speed": 1.3, "temp": 0.65, "emotion": "sad"}, "chinese": "呜...", "japanese": "うう..."}'
        parser.feed_chunk(raw)
        parser.finalize()

        base_opts = {"speed": 0.95, "temperature": 1.0, "top_p": 0.9}
        # When adaptive_enabled is False, base options MUST remain untouched
        fixed_opts = parser.get_dynamic_tts_options(base_opts, adaptive_enabled=False)
        assert fixed_opts["speed"] == 0.95
        assert fixed_opts["temperature"] == 1.0
        assert "emotion" not in fixed_opts

    def test_parser_backward_compatible_without_tts_field(self):
        """Standard legacy format without tts field should parse cleanly and leave dynamic fields None."""
        parser = StreamingBilingualParser()
        raw = '{"chinese": "早安。", "japanese": "おはよう。"}'
        parser.feed_chunk(raw)
        zh, ja, rem = parser.finalize()

        assert zh == "早安。"
        assert ja == "おはよう。"
        assert parser.tts_speed is None
        assert parser.tts_temperature is None
        assert parser.tts_emotion is None

        base_opts = {"speed": 1.0, "temperature": 1.0}
        opts = parser.get_dynamic_tts_options(base_opts, adaptive_enabled=True)
        assert opts == base_opts

    def test_parser_markdown_code_block_and_aliases(self):
        """Supports markdown codeblocks and aliases: speed_factor and temperature."""
        parser = StreamingBilingualParser()
        raw = (
            "```json\n"
            "{\n"
            '  "tts": {"speed_factor": 1.22, "temperature": 1.12, "emotion": "gentle"},\n'
            '  "chinese": "请慢用。\\n",\n'
            '  "japanese": "召し上がれ。\\n"\n'
            "}\n"
            "```"
        )
        parser.feed_chunk(raw)
        zh, ja, _ = parser.finalize()

        assert "请慢用" in zh
        assert "召し上がれ" in ja
        assert parser.tts_speed == 1.22
        assert parser.tts_temperature == 1.12
        assert parser.tts_emotion == "gentle"


# ============================================================================
# 3. TtsService Sanitization Tests
# ============================================================================

class TestTtsServiceSanitization:
    """Verifies that TtsService safely clamps dynamic options only when adaptive is enabled."""

    def test_sanitize_dynamic_options_when_enabled(self, tmp_path):
        service = TtsService(client=None, audio_dir=tmp_path)
        opts = {
            "ai_adaptive_voice": True,
            "speed": 2.5,
            "temperature": 0.2,
        }
        sanitized = service._sanitize_dynamic_voice_options(dict(opts))
        assert sanitized["speed"] == DYNAMIC_SPEED_MAX
        assert sanitized["speed_factor"] == DYNAMIC_SPEED_MAX
        assert sanitized["temperature"] == DYNAMIC_TEMP_MIN

    def test_sanitize_dynamic_options_when_disabled(self, tmp_path):
        service = TtsService(client=None, audio_dir=tmp_path)
        opts = {
            "ai_adaptive_voice": False,
            "speed": 2.0,
            "temperature": 0.3,
        }
        sanitized = service._sanitize_dynamic_voice_options(dict(opts))
        # Fixed parameter mode preserves user settings exactly
        assert sanitized["speed"] == 2.0
        assert sanitized["temperature"] == 0.3


# ============================================================================
# 4. ChatService End-to-End Pipeline Tests
# ============================================================================

class TestChatServiceDynamicVoiceIntegration:
    """Verifies ChatService stream_chat and chat_sync pipelines with dynamic voice settings."""

    @pytest.mark.asyncio
    async def test_stream_chat_dynamic_voice_enabled(self, temp_db_path, mock_gpt_sovits, tmp_path):
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        adapter = MockDynamicLLMAdapter()
        async def _mock_adapter(*args, **kwargs):
            return (adapter, "mock-dynamic-model")
        chat_service._get_active_llm_adapter = _mock_adapter

        events = []
        async for ev in chat_service.stream_chat(
            prompt="今天去哪里玩？",
            session_id="sess-dynamic-1",
            ai_adaptive_voice=True,
            tts_options={"speed": 1.0, "temperature": 1.0},
        ):
            events.append(ev)

        # Check audio chunk events were generated
        audio_events = [e for e in events if e["event"] == "audio_chunk"]
        assert len(audio_events) >= 1

        # Check done event contains dynamic tts_params
        done_events = [e for e in events if e["event"] == "done"]
        assert len(done_events) == 1
        done_data = done_events[0]["data"]
        assert "tts_params" in done_data
        params = done_data["tts_params"]
        assert params is not None
        assert params["speed"] == 1.25
        assert params["temperature"] == 0.85
        assert params["emotion"] == "tsundere"
        assert params["adaptive_enabled"] is True

    @pytest.mark.asyncio
    async def test_stream_chat_dynamic_voice_disabled(self, temp_db_path, mock_gpt_sovits, tmp_path):
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        adapter = MockDynamicLLMAdapter()
        async def _mock_adapter(*args, **kwargs):
            return (adapter, "mock-dynamic-model")
        chat_service._get_active_llm_adapter = _mock_adapter

        captured_chunk_options = []
        original_synth = tts_service.synthesize_to_file

        async def _spy_synth(text, options=None, **kwargs):
            captured_chunk_options.append(dict(options or {}))
            return await original_synth(text, options=options, **kwargs)

        tts_service.synthesize_to_file = _spy_synth

        user_fixed_speed = 0.85
        user_fixed_temp = 1.05
        events = []
        async for ev in chat_service.stream_chat(
            prompt="早安",
            session_id="sess-fixed-mode",
            ai_adaptive_voice=False,
            tts_options={"speed": user_fixed_speed, "temperature": user_fixed_temp},
        ):
            events.append(ev)

        done_ev = [e for e in events if e["event"] == "done"][0]
        params = done_ev["data"]["tts_params"]
        assert params["adaptive_enabled"] is False

        # In fixed parameter mode, TTS synthesis must use user sliders, NOT LLM's speed=1.25
        assert len(captured_chunk_options) >= 1
        for opt in captured_chunk_options:
            assert opt.get("speed") == user_fixed_speed
            assert opt.get("temperature") == user_fixed_temp

    @pytest.mark.asyncio
    async def test_chat_sync_returns_tts_params(self, temp_db_path, mock_gpt_sovits, tmp_path):
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        adapter = MockDynamicLLMAdapter()
        async def _mock_adapter(*args, **kwargs):
            return (adapter, "mock-dynamic-model")
        chat_service._get_active_llm_adapter = _mock_adapter

        res = await chat_service.chat_sync(
            prompt="你好呀",
            session_id="sess-sync-dynamic",
            ai_adaptive_voice=True,
        )
        assert "tts_params" in res
        assert res["tts_params"]["speed"] == 1.15
        assert res["tts_params"]["temperature"] == 0.90
        assert res["tts_params"]["emotion"] == "shy"
        assert res["tts_params"]["adaptive_enabled"] is True


# ============================================================================
# 5. FastAPI Chat Router API Tests
# ============================================================================

class TestChatRouterDynamicVoiceAPI:
    """Verifies ChatRequest schema validation and HTTP endpoint handling for ai_adaptive_voice."""

    def test_chat_request_default_ai_adaptive_voice(self):
        req = ChatRequest(prompt="测试")
        assert req.ai_adaptive_voice is True

    def test_chat_request_explicit_ai_adaptive_voice_false(self):
        req = ChatRequest(prompt="测试", ai_adaptive_voice=False)
        assert req.ai_adaptive_voice is False

    @pytest.mark.asyncio
    async def test_api_chat_stream_forwards_ai_adaptive_voice(self, temp_db_path, mock_gpt_sovits, tmp_path):
        app = create_app()
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        adapter = MockDynamicLLMAdapter()
        async def _mock_adapter(*args, **kwargs):
            return (adapter, "mock-dynamic-model")
        chat_service._get_active_llm_adapter = _mock_adapter
        set_chat_service(chat_service)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/chat/stream", json={
                "prompt": "你好",
                "session_id": "api-test-adaptive",
                "ai_adaptive_voice": True,
            })
            assert resp.status_code == 200
            body = resp.text
            assert "event: done" in body
            assert '"adaptive_enabled": true' in body

    @pytest.mark.asyncio
    async def test_api_chat_sync_forwards_ai_adaptive_voice(self, temp_db_path, mock_gpt_sovits, tmp_path):
        app = create_app()
        client = GptSovitsClient(server=mock_gpt_sovits)
        tts_service = TtsService(client=client, audio_dir=tmp_path)
        chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

        adapter = MockDynamicLLMAdapter()
        async def _mock_adapter(*args, **kwargs):
            return (adapter, "mock-dynamic-model")
        chat_service._get_active_llm_adapter = _mock_adapter
        set_chat_service(chat_service)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/chat", json={
                "prompt": "今天心情怎么样？",
                "session_id": "api-sync-adaptive",
                "ai_adaptive_voice": False,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert "tts_params" in data
            assert data["tts_params"]["adaptive_enabled"] is False

    def test_parser_extracts_string_numbers_and_negative_numbers(self):
        """Validates that quoted numeric strings and negative values are properly extracted and clamped."""
        parser = StreamingBilingualParser()
        raw = '{"tts": {"speed": "-0.5", "temp": "1.8", "emotion": "happy"}, "chinese": "测试", "japanese": "テスト"}'
        parser.feed_chunk(raw)
        zh, ja, _ = parser.finalize()

        assert zh == "测试"
        assert ja == "テスト"
        # Negative speed clamped to DYNAMIC_SPEED_MIN (0.70)
        assert parser.tts_speed == DYNAMIC_SPEED_MIN
        # 1.8 clamped to DYNAMIC_TEMP_MAX (1.20)
        assert parser.tts_temperature == DYNAMIC_TEMP_MAX
        assert parser.tts_emotion == "happy"

    def test_parser_extracts_chinese_emotion_names(self):
        """Validates that Chinese emotion names are mapped to valid archetype emotions."""
        parser = StreamingBilingualParser()
        raw = '{"tts": {"speed": 1.1, "temp": 0.9, "emotion": "傲娇"}, "chinese": "哼！", "japanese": "フン！"}'
        parser.feed_chunk(raw)
        zh, ja, _ = parser.finalize()

        assert parser.tts_emotion == "tsundere"
        assert parser.get_emotion() == "tsundere"

    def test_resolve_tts_options_dynamic_clamping(self):
        """Verifies resolve_tts_options clamps speed and temperature when ai_adaptive_voice is True."""
        from galgame2voice.services.gpt_sovits_client import resolve_tts_options

        # When adaptive is True: extreme values are clamped
        opts = resolve_tts_options({"speed": 2.5, "temperature": 0.1, "ai_adaptive_voice": True})
        assert opts["speed"] == DYNAMIC_SPEED_MAX
        assert opts["temperature"] == DYNAMIC_TEMP_MIN

        # When adaptive is False: user values are preserved
        fixed = resolve_tts_options({"speed": 2.5, "temperature": 0.1, "ai_adaptive_voice": False})
        assert fixed["speed"] == 2.5
        assert fixed["temperature"] == 0.1

    @pytest.mark.asyncio
    async def test_prepare_messages_augments_legacy_prompt(self, temp_db_path, tmp_path):
        """Verifies ChatService automatically augments legacy prompt without tts into dynamic format."""
        from galgame2voice.database.session import get_db
        from galgame2voice.database import crud
        from galgame2voice.database.models import VoiceProfileCreate

        chat_service = ChatService(db_path=temp_db_path)
        async with get_db(temp_db_path) as conn:
            # Create a profile with legacy prompt
            prof = await crud.create_voice_profile(conn, VoiceProfileCreate(
                name="LegacyTestChar",
                system_prompt='重要：你必须严格输出如下 JSON 格式，不要输出任何多余文字、不要加代码块标记：\n{"chinese": "显示给玩家的中文台词", "japanese": "对应的口语化日文台词"}\n要求：\n1. 角色口吻回复。',
                gpt_weights_path="dummy.ckpt",
                sovits_weights_path="dummy.pth",
                ref_audio_path="ref.wav",
                prompt_text="hello",
                is_default=True,
            ))
            await crud.set_active_voice_profile(conn, prof.id)

            messages = await chat_service._prepare_messages(
                conn, session_id="legacy-test-sess", user_prompt="你好"
            )
            sys_msg = messages[0].content
            assert '"tts":' in sys_msg
            assert "0.5~1.5" in sys_msg

    @pytest.mark.asyncio
    async def test_voice_synthesize_api_accepts_ai_adaptive_voice(self, temp_db_path, mock_gpt_sovits, tmp_path):
        """Verifies /api/voice/synthesize accepts ai_adaptive_voice in request body."""
        from galgame2voice.services.voice_manager import get_voice_manager
        vm = get_voice_manager()
        vm.client.server = mock_gpt_sovits
        vm.tts_service.client.server = mock_gpt_sovits

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/voice/synthesize", json={
                "text": "こんにちは",
                "speed": 1.2,
                "ai_adaptive_voice": True,
            })
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "audio/wav"

