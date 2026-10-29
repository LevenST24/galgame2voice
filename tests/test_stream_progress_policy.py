"""Voice waiting has a bounded owner and an advertised browser policy."""

import asyncio
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from galgame2voice.routers import chat
from galgame2voice.services.chat_pipelines import tts_pipeline
from galgame2voice.services.chat_pipelines.stream_coordinator import StreamCoordinator


def coordinator_for(tmp_path, synthesize):
    return StreamCoordinator(adapter=SimpleNamespace(), messages=[], model_name="synthetic",
        actual_provider_id="synthetic", session_id="synthetic", prompt="synthetic",
        tts_service=SimpleNamespace(synthesize_to_file=synthesize), db_path=str(tmp_path / "unused.db"),
        metrics_collector=None, affection_service=None, ai_adaptive_voice=False)


def test_browser_accepts_the_advertised_backend_budget():
    source = (Path(__file__).resolve().parents[1] / "frontend/src/ai.js").read_text(encoding="utf-8")
    limit = re.search(r"MAX_VOICE_WAIT_MS\s*=\s*(\d+)", source)
    assert limit is not None
    assert round(tts_pipeline.TTS_JOB_TIMEOUT_SECONDS * 1000) <= int(limit[1])


async def test_http_stream_advertises_the_voice_budget(monkeypatch):
    async def source(**kwargs):
        yield {"event": "done", "data": {"chinese": "synthetic"}}
    monkeypatch.setattr(chat, "get_chat_service", lambda: SimpleNamespace(stream_chat=source))
    response = await chat.chat_stream_endpoint(chat.ChatRequest(prompt="synthetic"), None)
    assert response.headers.get("X-G2V-Progress-Policy") == "1"
    budget = int(response.headers["X-G2V-Voice-Job-Timeout-Ms"])
    assert budget == round(tts_pipeline.TTS_JOB_TIMEOUT_SECONDS * 1000)
    assert budget >= 300_000, "cold synthesis must retain its supported read window"
    await response.body_iterator.aclose()


async def test_hung_voice_job_returns_a_line_failure_and_finishes_cleanup(monkeypatch):
    monkeypatch.setattr(tts_pipeline, "TTS_JOB_TIMEOUT_SECONDS", 0.02, raising=False)
    closed = asyncio.Event()
    async def synthesize(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    pipeline = tts_pipeline.TtsStreamPipeline(SimpleNamespace(synthesize_to_file=synthesize), "synthetic")
    result, error = await asyncio.wait_for(pipeline.synthesize_chunk("こんにちは", 0, {}), 0.5)
    assert result is None and "超时" in error
    assert closed.is_set()


async def test_repeated_cancel_cannot_interrupt_voice_job_cleanup():
    entered, cleaning, release, closed = (asyncio.Event() for _ in range(4))
    async def synthesize(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            closed.set()
    pipeline = tts_pipeline.TtsStreamPipeline(SimpleNamespace(synthesize_to_file=synthesize), "synthetic")
    task = asyncio.create_task(pipeline.synthesize_chunk("こんにちは", 0, {}))
    try:
        await entered.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "the second cancellation must wait for owned cleanup"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_successful_voice_job_keeps_its_audio_metadata():
    async def synthesize(*args, **kwargs):
        return "/audio/cache/synthetic.wav", Path("synthetic.wav"), 32
    pipeline = tts_pipeline.TtsStreamPipeline(SimpleNamespace(synthesize_to_file=synthesize), "synthetic")
    result, error = await pipeline.synthesize_chunk("こんにちは", 2, {})
    assert error is None and result["is_cached"] is True
    assert result["index"] == 2 and result["audio_url"] == "/audio/cache/synthetic.wav"


async def test_worker_announces_voice_work_before_waiting_for_inference(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def synthesize(*args, **kwargs):
        entered.set()
        await release.wait()
        return "/audio/synthetic.wav", tmp_path / "synthetic.wav", 32
    coordinator = coordinator_for(tmp_path, synthesize)
    run = coordinator._init_stream_run()
    run.chunk_s_map[0] = 0
    run.chunk_results[0] = {"settled": False, "chunk": None, "error": None}
    await run.tts_queue.put(("こんにちは。", 0))
    await run.tts_queue.put(None)
    task = asyncio.create_task(coordinator._tts_worker(run))
    try:
        await entered.wait()
        event = await asyncio.wait_for(run.event_queue.get(), 0.1)
        assert event == {"event": "voice_job", "data": {"index": 0, "status": "started"}}
        release.set()
        await asyncio.wait_for(task, 1)
        events = []
        while not run.event_queue.empty():
            events.append(run.event_queue.get_nowait())
        finished = {"event": "voice_job", "data": {"index": 0, "status": "finished"}}
        assert finished in events
        assert any(isinstance(item, dict) and item["event"] == "audio_chunk" for item in events)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_expired_voice_job_settles_the_line_instead_of_aborting_the_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_pipeline, "TTS_JOB_TIMEOUT_SECONDS", 0.02)
    async def synthesize(*args, **kwargs):
        await asyncio.Event().wait()
    coordinator = coordinator_for(tmp_path, synthesize)
    run = coordinator._init_stream_run()
    line = {"index": 0, "zh": "文字保留", "ja": "こんにちは。", "emotion": "neutral", "act": {}}
    run.lines.append(line)
    run.chunk_line_map[0] = 0
    run.chunk_results[0] = {"settled": False, "chunk": None, "error": None}
    coordinator._line_audio_state(run, 0).update(sealed=True, pending=1)
    await run.tts_queue.put(line)
    await run.tts_queue.put(None)
    await asyncio.wait_for(coordinator._tts_worker(run), 1)
    events = []
    while not run.event_queue.empty():
        event = run.event_queue.get_nowait()
        if isinstance(event, dict):
            events.append(event)
    assert not any(event["event"] == "error" for event in events)
    result = next(event["data"] for event in events if event["event"] == "line_audio")
    assert result["line"] == 0 and result["status"] == "failed"
    assert "超时" in result["error"] and run.lines[0]["zh"] == "文字保留"
