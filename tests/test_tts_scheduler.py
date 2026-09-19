"""
Tests for TtsScheduler streaming scheduling, priority ordering,
cancellation support, bounded buffer, and telemetry metrics.
"""

import asyncio
from typing import AsyncGenerator, List
import pytest

from galgame2voice.services.tts_scheduler import (
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
async def test_schedule_stream_normal_flow(scheduler):
    """Verifies schedule_stream yields all chunks sequentially."""
    chunks = [b"chunk1", b"chunk2", b"chunk3"]

    async def sample_stream() -> AsyncGenerator[bytes, None]:
        for c in chunks:
            await asyncio.sleep(0.01)
            yield c

    received = []
    async for chunk in scheduler.schedule_stream(sample_stream, priority=TtsPriority.HIGH):
        received.append(chunk)

    assert received == chunks
    assert scheduler.active_streams == 0
    assert scheduler.cancelled_streams == 0


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
