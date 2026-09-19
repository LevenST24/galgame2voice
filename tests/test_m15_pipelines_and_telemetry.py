"""
Unit and integration tests for Milestone 15:
Full-System Comprehensive Optimization, Pipeline Modularization,
T0-T7 Latency Telemetry, and Asynchronous Warm-up Preheating.
"""

import asyncio
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from galgame2voice.utils.profiler import ChatTurnProfiler
from galgame2voice.services.chat_pipelines import (
    LlmStreamPipeline,
    TextSegmentationPipeline,
    TtsStreamPipeline,
)
from galgame2voice.services.gpt_sovits_client import _safe_resolve_path
from galgame2voice.services.tts_scheduler import TtsPriority, get_tts_scheduler


# ============================================================================
# 1. ChatTurnProfiler T0-T7 Telemetry & Waterfall Tests
# ============================================================================

def test_chat_turn_profiler_telemetry_checkpoints():
    profiler = ChatTurnProfiler(turn_id="test_m15_turn")
    time.sleep(0.005)
    profiler.record_llm_first_token()
    time.sleep(0.005)
    profiler.record_first_sentence()
    time.sleep(0.005)
    profiler.record_tts_dispatch(0)
    time.sleep(0.005)
    profiler.record_upstream_first_byte()
    time.sleep(0.005)
    profiler.record_app_first_chunk()
    time.sleep(0.005)
    profiler.record_first_audio()
    profiler.record_tts_inference(0, cached=False)
    time.sleep(0.005)
    profiler.record_frontend_delivered()
    time.sleep(0.005)
    profiler.record_playback_started()

    data = profiler.to_dict()
    assert data["turn_id"] == "test_m15_turn"
    assert data["t1_ttft_ms"] > 0
    assert data["t2_first_sentence_ms"] >= data["t1_ttft_ms"]
    assert data["t3_tts_dispatch_ms"] >= data["t2_first_sentence_ms"]
    assert data["t4_upstream_first_byte_ms"] >= data["t3_tts_dispatch_ms"]
    assert data["t5_app_ttfa_ms"] >= data["t4_upstream_first_byte_ms"]
    assert data["t6_frontend_delivered_ms"] >= data["t5_app_ttfa_ms"]
    assert data["t7_playback_ms"] >= data["t6_frontend_delivered_ms"]
    assert data["chunk_count"] == 1
    assert data["cached_chunks"] == 0

    waterfall = profiler.render_ascii()
    assert "T0 Request Sent" in waterfall
    assert "T1 LLM First Token" in waterfall
    assert "T4 Upstream First Byte" in waterfall
    assert "T5 App Chunk 0 Emitted" in waterfall
    assert "T6 SSE Delivered" in waterfall
    assert "T7 Playback Start" in waterfall


def test_profiler_with_cached_inference():
    profiler = ChatTurnProfiler(turn_id="turn_cached")
    profiler.record_tts_dispatch(0)
    profiler.record_tts_inference(0, cached=True)
    profiler.record_tts_dispatch(1)
    profiler.record_tts_inference(1, cached=False)

    data = profiler.to_dict()
    assert data["chunk_count"] == 2
    assert data["cached_chunks"] == 1
    assert data["generated_chunks"] == 1
    assert data["cache_hit_rate"] == 0.5


# ============================================================================
# 2. Pipeline Modularization Tests
# ============================================================================

@pytest.mark.asyncio
async def test_llm_stream_pipeline_streaming_and_cancellation():
    class MockAdapter:
        async def stream_chat(self, messages, **kwargs):
            for tok in ["【", "喜", "悦", "】", "你好", "啊", "！", "こんにちは"]:
                yield tok

    adapter = MockAdapter()
    pipe = LlmStreamPipeline(adapter, [{"role": "user", "content": "hi"}], "mock-model")

    tokens = []
    async for tok in pipe.stream_tokens():
        tokens.append(tok)

    assert "".join(tokens) == "【喜悦】你好啊！こんにちは"


@pytest.mark.asyncio
async def test_llm_stream_pipeline_early_cancel():
    cancel_evt = asyncio.Event()

    class MockLongAdapter:
        async def stream_chat(self, messages, **kwargs):
            for i in range(100):
                yield f"tok_{i}"

    adapter = MockLongAdapter()
    pipe = LlmStreamPipeline(
        adapter, [{"role": "user", "content": "hi"}], "mock-model", cancel_event=cancel_evt
    )

    tokens = []
    async for tok in pipe.stream_tokens():
        tokens.append(tok)
        if len(tokens) == 3:
            cancel_evt.set()

    assert len(tokens) == 3


def test_text_segmentation_pipeline_bilingual_parsing():
    pipe = TextSegmentationPipeline()

    # Feed structured streaming JSON tokens incrementally
    delta1, sents1, emo1 = pipe.feed_token('{"emotion": "happy", "chinese": "今天天气真好！", "japanese": "今日はとてもいい天気ですね。')
    assert delta1 == "今天天气真好！"
    assert emo1 == "happy"
    assert len(sents1) == 1
    assert "今日はとてもいい天気ですね。" in sents1[0]

    delta2, sents2, emo2 = pipe.feed_token('明日も晴れるといいな。"}\n')
    assert delta2 == ""
    assert emo2 == "happy"

    full_ch, full_ja, rem_ch, rem_sents, final_emo = pipe.finalize()
    assert "今天天气真好！" in full_ch
    assert "明日も晴れるといいな。" in full_ja
    assert final_emo == "happy"


def test_tts_stream_pipeline_helpers():
    mock_tts_service = MagicMock()
    pipe = TtsStreamPipeline(mock_tts_service, generation_id="test_gen_123")

    # Vocal vs non-vocal
    assert pipe.is_vocal_sentence("こんにちは。") is True
    assert pipe.is_vocal_sentence("   ") is False
    assert pipe.is_vocal_sentence("（ため息）") is False
    assert pipe.is_vocal_sentence("...") is False

    # Options preparation
    mock_prof = MagicMock()
    mock_prof.id = 42
    mock_prof.name = "TestChar"
    mock_prof.prompt_lang = "ja"
    mock_prof.text_lang = "ja"
    mock_prof.ref_audio_path = "path/to/ref.wav"
    mock_prof.prompt_text = "Prompt text"

    base_opts = {"speed": 1.1}
    prepared_opts = pipe.prepare_chunk_options(base_opts, mock_prof, chunk_index=0, sentence="おはよう")

    assert prepared_opts["_priority"] == 0  # Chunk 0 has high priority
    assert prepared_opts["_generation_id"] == "test_gen_123"
    assert prepared_opts["voice_profile_id"] == 42
    assert prepared_opts["character_name"] == "TestChar"
    assert prepared_opts["text_split_method"] == "cut0"  # short sentence <= 80


@pytest.mark.asyncio
async def test_tts_stream_pipeline_synthesize_chunk():
    mock_tts_service = MagicMock()
    mock_tts_service.synthesize_to_file = AsyncMock(
        return_value=("/audio/cache/chunk_0.wav", Path("chunk_0.wav"), 1.5)
    )
    pipe = TtsStreamPipeline(mock_tts_service, generation_id="test_gen_456")
    profiler = ChatTurnProfiler(turn_id="test_chunk")

    chunk_data, err = await pipe.synthesize_chunk(
        sentence="元気ですか？",
        chunk_index=0,
        options={},
        profiler=profiler,
    )

    assert err is None
    assert chunk_data is not None
    assert chunk_data["index"] == 0
    assert chunk_data["audio_url"] == "/audio/cache/chunk_0.wav"
    assert chunk_data["is_cached"] is True
    assert profiler.app_first_chunk_ts is not None


# ============================================================================
# 3. Defensive Path Resolution Tests
# ============================================================================

def test_safe_resolve_path():
    # Normal existing/valid path
    p = _safe_resolve_path("test_audio.wav")
    assert isinstance(p, Path)

    # Windows permission / access simulated failure fallback
    with patch.object(Path, "resolve", side_effect=PermissionError("Access denied")):
        fallback_p = _safe_resolve_path("forbidden_audio.wav")
        assert isinstance(fallback_p, Path)
        assert str(fallback_p).endswith("forbidden_audio.wav")


# ============================================================================
# 4. Asynchronous Warm-up via TtsScheduler Priority Tests
# ============================================================================

@pytest.mark.asyncio
async def test_voice_manager_warmup_uses_low_priority():
    from galgame2voice.services.voice_manager import VoiceManager
    from galgame2voice.services.gpt_sovits_client import GptSovitsClient

    mock_client = MagicMock(spec=GptSovitsClient)
    mock_client.check_health = AsyncMock(return_value={"connected": True})
    mock_client.switch_voice_profile = AsyncMock(return_value=True)
    mock_client.synthesize = AsyncMock(return_value=b"RIFF...")
    mock_client.current_refer_audio = "dummy.wav"

    mock_profile = MagicMock()
    mock_profile.id = 99
    mock_profile.name = "WarmupChar"
    mock_profile.prompt_lang = "ja"
    mock_profile.text_lang = "ja"
    mock_profile.ref_audio_path = "ref.wav"
    mock_profile.prompt_text = "prompt"

    vm = VoiceManager(gpt_sovits_client_or_server=mock_client)
    vm.active_profile = mock_profile

    scheduler = get_tts_scheduler()
    with patch.object(scheduler, "schedule", wraps=scheduler.schedule) as spy_schedule:
        success = await vm.warmup_current_profile()
        assert success is True
        assert spy_schedule.called
        _, kwargs = spy_schedule.call_args
        assert kwargs.get("priority") == TtsPriority.LOW
        assert "warmup_99" in kwargs.get("task_id", "")


@pytest.mark.asyncio
async def test_chat_service_stream_chat_includes_telemetry(tmp_path):
    from galgame2voice.services.chat_service import ChatService

    mock_llm_adapter = MagicMock()
    async def mock_stream_chat(*args, **kwargs):
        yield '{"emotion": "happy", "chinese": "测试", "japanese": "テストです。"}'
    mock_llm_adapter.stream_chat = mock_stream_chat

    mock_tts_service = MagicMock()
    mock_tts_service.synthesize_to_file = AsyncMock(
        return_value=("/audio/cache/chunk_0.wav", Path("chunk_0.wav"), 1.0)
    )

    db_file = tmp_path / "test_chat.db"
    svc = ChatService(
        tts_service=mock_tts_service,
        db_path=db_file,
    )
    svc._get_active_llm_adapter = AsyncMock(return_value=(mock_llm_adapter, "mock-model", "mock-prov"))
    svc._resolve_adapter_triple = AsyncMock(return_value=(mock_llm_adapter, "mock-model", "mock-prov"))
    svc._extract_memory_safe = AsyncMock(return_value=None)
    svc.voice_manager = MagicMock()
    svc.voice_manager.get_active_profile = AsyncMock(return_value=None)
    svc.voice_manager.get_profile = AsyncMock(return_value=None)
    svc.memory_service = MagicMock()
    svc.memory_service.get_relevant_memories = AsyncMock(return_value=[])
    svc.affection_service = MagicMock()
    svc.affection_service.get_affection = AsyncMock(return_value=50)

    events = []
    async for evt in svc.stream_chat(
        prompt="hello",
        session_id="test_m15_session",
    ):
        events.append(evt)

    done_events = [e for e in events if e.get("event") == "done"]
    assert len(done_events) == 1
    done_data = done_events[0]["data"]
    assert "telemetry" in done_data
    telemetry = done_data["telemetry"]
    assert "t1_ttft_ms" in telemetry
    assert "t5_app_ttfa_ms" in telemetry
    assert "chunk_count" in telemetry

