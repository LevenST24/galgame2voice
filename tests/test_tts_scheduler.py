"""
Tests for TtsScheduler streaming scheduling, priority ordering,
cancellation support, bounded buffer, and telemetry metrics.
"""

import asyncio
import io
from pathlib import Path
import struct
from typing import AsyncGenerator, List
from unittest.mock import AsyncMock, MagicMock, patch
import wave
import pytest

from galgame2voice.services.gpt_sovits_client import GptSovitsClient, _safe_resolve_path
from galgame2voice.services.tts_scheduler import (
    SingleFlightCoordinator,
    TtsScheduler,
    TtsPriority,
    get_tts_scheduler,
)


@pytest.fixture
async def scheduler():
    sched = TtsScheduler()
    sched.reset_metrics()
    yield sched
    await sched.aclose()


@pytest.mark.asyncio
async def test_scheduler_priority_ordering(scheduler):
    """Verifies that high priority tasks execute before normal and low priority tasks."""
    execution_order: List[str] = []

    # Pause worker temporarily by inserting a blocking task
    gate = asyncio.Event()

    async def blocker():
        await gate.wait()
        return "blocker"

    # Block worker
    block_task = asyncio.create_task(scheduler.schedule(blocker, priority=TtsPriority.NORMAL))
    await asyncio.sleep(0.02)  # Allow worker to pick up blocker

    # Enqueue tasks with different priorities while worker is blocked
    async def make_task(name: str):
        execution_order.append(name)
        return name

    fut_low = asyncio.create_task(
        scheduler.schedule(lambda: make_task("low"), priority=TtsPriority.LOW)
    )
    fut_normal = asyncio.create_task(
        scheduler.schedule(lambda: make_task("normal"), priority=TtsPriority.NORMAL)
    )
    fut_high = asyncio.create_task(
        scheduler.schedule(lambda: make_task("high"), priority=TtsPriority.HIGH)
    )

    await asyncio.sleep(0.02)
    # Release blocker
    gate.set()
    await block_task
    await asyncio.gather(fut_low, fut_normal, fut_high)

    assert execution_order == ["high", "normal", "low"]


@pytest.mark.asyncio
async def test_schedule_stream_metrics_tracking(scheduler):
    """Verifies active_streams and queue_wait_time metrics are tracked correctly."""
    gate = asyncio.Event()

    async def slow_stream() -> AsyncGenerator[bytes, None]:
        yield b"first"
        await gate.wait()
        yield b"second"

    stream_gen = scheduler.schedule_stream(slow_stream, priority=TtsPriority.NORMAL)
    # Get first chunk
    first_chunk = await anext(stream_gen)
    assert first_chunk == b"first"

    # While inside generator, active_streams must be 1
    assert scheduler.active_streams == 1
    assert scheduler.active_stream_counts == 1

    # Let stream complete
    gate.set()
    second_chunk = await anext(stream_gen)
    assert second_chunk == b"second"

    with pytest.raises(StopAsyncIteration):
        await anext(stream_gen)

    # After complete, active_streams back to 0
    assert scheduler.active_streams == 0
    metrics = scheduler.get_metrics()
    assert metrics["active_streams"] == 0
    assert metrics["cancelled_streams"] == 0
    assert scheduler.avg_queue_wait_time >= 0.0


@pytest.mark.asyncio
async def test_schedule_stream_cancel_queued_generation(scheduler):
    """Verifies cancel_generation cancels a queued stream task before execution."""
    blocker_gate = asyncio.Event()

    async def blocker():
        await blocker_gate.wait()

    # Block worker
    asyncio.create_task(scheduler.schedule(blocker, priority=TtsPriority.NORMAL))
    await asyncio.sleep(0.02)

    gen_id = "test_gen_123"
    received = []

    async def stream_fn() -> AsyncGenerator[bytes, None]:
        yield b"chunk"

    async def run_consumer():
        async for c in scheduler.schedule_stream(stream_fn, generation_id=gen_id):
            received.append(c)

    consumer_task = asyncio.create_task(run_consumer())
    await asyncio.sleep(0.02)

    # Cancel generation before blocker finishes
    cancelled_count = scheduler.cancel_generation(gen_id)
    assert cancelled_count >= 1

    # Unblock worker
    blocker_gate.set()
    await consumer_task

    assert received == []
    assert scheduler.cancelled_streams >= 1


@pytest.mark.asyncio
async def test_schedule_stream_cancel_active_stream(scheduler):
    """Verifies cancel_generation stops an actively running stream."""
    stream_running = asyncio.Event()
    cleaned_up = asyncio.Event()
    gen_id = "test_gen_active"

    async def endless_stream() -> AsyncGenerator[bytes, None]:
        try:
            stream_running.set()
            while True:
                await asyncio.sleep(0.01)
                yield b"tick"
        finally:
            cleaned_up.set()

    received = []

    async def consume():
        async for c in scheduler.schedule_stream(endless_stream, generation_id=gen_id):
            received.append(c)

    consume_task = asyncio.create_task(consume())
    await stream_running.wait()

    # Stream is running, cancel it
    cancelled = scheduler.cancel_generation(gen_id)
    assert cancelled >= 1

    await consume_task
    try:
        await asyncio.wait_for(cleaned_up.wait(), timeout=0.2)
    except asyncio.TimeoutError:
        pass
    assert cleaned_up.is_set()
    assert scheduler.cancelled_streams >= 1
    assert scheduler.active_streams == 0


@pytest.mark.asyncio
async def test_schedule_stream_caller_abort(scheduler):
    """Verifies caller breaking early terminates the worker generator and updates metrics."""
    cleaned_up = asyncio.Event()

    async def infinite_stream() -> AsyncGenerator[bytes, None]:
        try:
            while True:
                await asyncio.sleep(0.01)
                yield b"data"
        finally:
            cleaned_up.set()

    count = 0
    async for chunk in scheduler.schedule_stream(infinite_stream):
        count += 1
        if count >= 3:
            break

    # Allow worker to detect stop_event and cleanup
    await asyncio.sleep(0.05)
    assert cleaned_up.is_set()
    assert scheduler.active_streams == 0
    assert scheduler.cancelled_streams >= 1


def _generate_synthetic_wav_bytes(
    duration_s: float = 3.5, sample_rate: int = 32000, amplitude: int = 12000
) -> bytes:
    """Generates synthetic 16-bit mono PCM WAV bytes with audible sine wave."""
    num_samples = int(duration_s * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        samples = []
        for i in range(num_samples):
            val = int(amplitude * 0.5 * (1.0 if (i // 72) % 2 == 0 else -1.0))
            samples.append(val)
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_true_streaming_pipelining_and_early_lock_release():
    """
    Verifies that GptSovitsClient.stream_tts yields Chunk 0 immediately upon arrival,
    before upstream finishes emitting, and releases the inference lock once upstream HTTP completes.
    """
    client = GptSovitsClient(server="http://127.0.0.1:9880")
    client.is_healthy = True
    client.current_refer_audio = "dummy_ref.wav"
    client.current_refer_text = "プロンプト"

    audible_wav = _generate_synthetic_wav_bytes(duration_s=3.5)
    chunk_size = 4096
    upstream_chunks = [audible_wav[i:i + chunk_size] for i in range(0, len(audible_wav), chunk_size)]
    assert len(upstream_chunks) >= 5

    upstream_finished = False

    class MockAsyncResponse:
        status_code = 200
        headers = {"content-type": "audio/wav"}

        def raise_for_status(self):
            pass

        async def aiter_bytes(self, *args, **kwargs):
            nonlocal upstream_finished
            for chunk in upstream_chunks:
                await asyncio.sleep(0.02)  # Simulate network delivery delay
                yield chunk
            upstream_finished = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    def mock_stream(*args, **kwargs):
        return MockAsyncResponse()

    client._http_client = MagicMock()
    client._http_client.stream = mock_stream

    # Consume stream
    chunks_received = []

    gen = client.stream_tts(
        "こんにちは",
        options={"_pre_resolved": True, "ref_audio_path": "dummy_ref.wav", "prompt_text": "テスト"},
        chunk_size=chunk_size,
    )
    async for chunk in gen:
        chunks_received.append(chunk)
        if len(chunks_received) == 1:
            # Chunk 0 MUST arrive while upstream is still actively delivering!
            assert not upstream_finished, "Chunk 0 should be delivered BEFORE upstream finished all chunks"

    assert upstream_finished, "Upstream should have finished"
    assert len(chunks_received) == len(upstream_chunks)
    assert b"".join(chunks_received) == audible_wav

    # Lock must be released after stream completion
    assert not client.lock.locked(), "Inference lock must be released after stream completion"


@pytest.mark.asyncio
async def test_single_flight_coordinator_coalescing():
    """
    Verifies that concurrent requests for identical synthesis keys coalesce into
    a single GPU inference call, eliminating duplicate GPU work and cache stampedes.
    """
    coordinator = SingleFlightCoordinator()
    call_count = 0

    async def expensive_synthesis():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)  # Simulate GPU synthesis latency
        return b"SYNTHESIZED_WAV_BYTES"

    # Launch 10 simultaneous tasks requesting the same key
    tasks = [
        coordinator.execute("shared_cache_key_xyz", expensive_synthesis)
        for _ in range(10)
    ]
    results = await asyncio.gather(*tasks)

    # All 10 callers must receive the exact result
    assert len(results) == 10
    for res in results:
        assert res == b"SYNTHESIZED_WAV_BYTES"

    # Crucial: expensive_synthesis must only have been invoked ONCE!
    assert call_count == 1, f"Expected 1 GPU invocation but got {call_count}"


def test_safe_resolve_path():
    # Normal existing/valid path
    p = _safe_resolve_path("test_audio.wav")
    assert isinstance(p, Path)

    # Windows permission / access simulated failure fallback
    with patch.object(Path, "resolve", side_effect=PermissionError("Access denied")):
        fallback_p = _safe_resolve_path("forbidden_audio.wav")
        assert isinstance(fallback_p, Path)
        assert str(fallback_p).endswith("forbidden_audio.wav")


@pytest.mark.asyncio
async def test_voice_manager_warmup_uses_low_priority():
    from galgame2voice.services.voice_manager import VoiceManager

    mock_client = MagicMock(spec=GptSovitsClient)
    mock_client.check_health = AsyncMock(return_value={"connected": True})
    mock_client.switch_voice_profile = AsyncMock(return_value=True)
    mock_client.synthesize = AsyncMock(return_value=b"RIFF...")
    mock_client.current_refer_audio = "dummy.wav"
    mock_client.current_gpt_weights = None
    mock_client.current_sovits_weights = None

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
    try:
        with patch.object(scheduler, "schedule", wraps=scheduler.schedule) as spy_schedule:
            success = await vm.warmup_current_profile()
            assert success is True
            assert spy_schedule.called
            _, kwargs = spy_schedule.call_args
            assert kwargs.get("priority") == TtsPriority.LOW
            assert "warmup_99" in kwargs.get("task_id", "")
    finally:
        await vm.aclose()


@pytest.mark.asyncio
async def test_scheduler_and_single_flight_loop_rebind():
    """Verify SingleFlightCoordinator and TtsScheduler dynamically adapt if loop changes."""
    sf = SingleFlightCoordinator()
    sched = TtsScheduler()
    try:
        # Normal execution on current loop
        res = await sf.execute("test_key", AsyncMock(return_value="res_1"))
        assert res == "res_1"

        # Simulate loop change by pointing internal _loop to None
        sf._loop = None
        sf._lock = None
        res2 = await sf.execute("test_key_2", AsyncMock(return_value="res_2"))
        assert res2 == "res_2"

        sched._loop = None
        sched_res = await sched.schedule(AsyncMock(return_value="sched_ok"))
        assert sched_res == "sched_ok"
    finally:
        await sched.aclose()
