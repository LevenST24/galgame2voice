"""A turn's telemetry must not report a milestone that never happened as a timing."""
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from galgame2voice.utils.profiler import ChatTurnProfiler


def test_turn_without_audio_reports_no_first_audio_time():
    profiler = ChatTurnProfiler(turn_id="synthetic", enabled=False)
    profiler.record_llm_first_token()
    profiler.record_first_sentence()

    data = profiler.to_dict()

    assert data["t5_app_ttfa_ms"] is None
    assert data["passed_target"] is None
    for key in ("t3_tts_dispatch_ms", "t4_upstream_first_byte_ms", "t6_frontend_delivered_ms",
                "t7_playback_ms", "tts_infer_done_ms"):
        assert data[key] is None, key
    assert data["t1_ttft_ms"] is not None and data["t1_ttft_ms"] >= 0
    assert data["t2_first_sentence_ms"] >= data["t1_ttft_ms"]


def test_turn_without_any_token_reports_no_first_token_time():
    data = ChatTurnProfiler(turn_id="synthetic", enabled=False).to_dict()

    assert data["t1_ttft_ms"] is None
    assert data["t2_first_sentence_ms"] is None


def test_turn_with_audio_keeps_its_first_audio_time_and_target_verdict():
    profiler = ChatTurnProfiler(turn_id="synthetic", enabled=False)
    profiler.t_first_audio = profiler.t_start + 0.25

    data = profiler.to_dict()

    assert data["t5_app_ttfa_ms"] == 250.0
    assert data["passed_target"] is True


def test_waterfall_without_audio_does_not_claim_a_first_audio_time():
    profiler = ChatTurnProfiler(turn_id="synthetic", enabled=True)
    profiler.record_llm_first_token()

    report = profiler.render_ascii()

    assert "TTFA:" in report
    assert "no audio" in report
    assert "PASS" not in report and "WARN" not in report


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
    svc.memory_service = MagicMock()
    svc.memory_service.get_relevant_memories = AsyncMock(return_value=[])
    svc.affection_service = MagicMock()
    svc.affection_service.get_affection = AsyncMock(return_value=50)

    events = []
    try:
        async for evt in svc.stream_chat(
            prompt="hello",
            session_id="test_m15_session",
        ):
            events.append(evt)
    finally:
        await svc.aclose()

    done_events = [e for e in events if e.get("event") == "done"]
    assert len(done_events) == 1
    done_data = done_events[0]["data"]
    assert "telemetry" in done_data
    telemetry = done_data["telemetry"]
    assert "t1_ttft_ms" in telemetry
    assert "t5_app_ttfa_ms" in telemetry
    assert "chunk_count" in telemetry


def test_profiler_waterfall_generation():
    profiler = ChatTurnProfiler(turn_id="test-turn-1", enabled=True)
    profiler.record_llm_first_token()
    profiler.record_first_sentence()
    profiler.record_tts_dispatch(chunk_index=0)
    profiler.record_tts_inference(chunk_index=0, cached=False)
    profiler.record_tts_inference(chunk_index=1, cached=True)
    profiler.record_first_audio()

    report = profiler.generate_report()
    assert "Chat Turn #test-turn-1" in report
    assert "LLM TTFT:" in report
    assert "First Sentence:" in report
    assert "TTS Dispatch:" in report
    assert "TTS Inference:" in report
    assert "TTFA:" in report
    assert "Cache Hit:" in report
