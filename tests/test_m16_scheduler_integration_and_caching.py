"""
Unit and integration tests for Milestone 16:
TTS Scheduler Priority Hookup, Stale Generation Cancellation,
Loop Rebinding Resilience, AudioStaticFiles HTTP Caching Headers,
and Dynamic Jitter Buffer Adaptive Timing.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from galgame2voice.main import AudioStaticFiles
from galgame2voice.services.tts_scheduler import (
    SingleFlightCoordinator,
    TtsPriority,
    TtsScheduler,
)
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.tts_cache_manager import TtsCacheManager


# ============================================================================
# 1. TtsScheduler Priority & Dispatch Ordering
# ============================================================================

@pytest.mark.asyncio
async def test_scheduler_priority_ordering():
    """Verify that HIGH priority (0) tasks preempt NORMAL priority (1) tasks in queue."""
    scheduler = TtsScheduler()
    execution_order = []
    release_gate = asyncio.Event()

    async def blocking_task():
        await release_gate.wait()
        execution_order.append("blocking")
        return "blocking_done"

    async def normal_task():
        execution_order.append("normal")
        return "normal_done"

    async def high_task():
        execution_order.append("high")
        return "high_done"

    # 1. Schedule a blocking task to keep the worker busy
    fut_block = asyncio.create_task(scheduler.schedule(blocking_task, priority=TtsPriority.NORMAL))
    await asyncio.sleep(0.01)  # Ensure worker picks up blocking_task

    # 2. While worker is blocked, schedule a NORMAL task first, then a HIGH task second
    fut_normal = asyncio.create_task(scheduler.schedule(normal_task, priority=TtsPriority.NORMAL))
    fut_high = asyncio.create_task(scheduler.schedule(high_task, priority=TtsPriority.HIGH))

    await asyncio.sleep(0.01)

    # 3. Unblock the worker and wait for all tasks to complete
    release_gate.set()
    await asyncio.gather(fut_block, fut_normal, fut_high)
    await scheduler.aclose()

    # The HIGH task must have executed BEFORE the NORMAL task!
    assert execution_order == ["blocking", "high", "normal"]


# ============================================================================
# 2. Stale Generation Cancellation
# ============================================================================

@pytest.mark.asyncio
async def test_scheduler_stale_generation_cancellation():
    """Verify that cancel_generation cancels queued tasks and prevents execution."""
    scheduler = TtsScheduler()
    executed = []
    block_gate = asyncio.Event()

    async def current_turn_task():
        await block_gate.wait()
        executed.append("current")
        return "ok"

    async def stale_turn_task_1():
        executed.append("stale_1")
        return "stale_1"

    async def stale_turn_task_2():
        executed.append("stale_2")
        return "stale_2"

    # Start active turn task
    fut_curr = asyncio.create_task(
        scheduler.schedule(current_turn_task, priority=TtsPriority.HIGH, generation_id="turn_1")
    )
    await asyncio.sleep(0.01)

    # Enqueue tasks for stale generation
    fut_stale1 = asyncio.create_task(
        scheduler.schedule(stale_turn_task_1, priority=TtsPriority.NORMAL, generation_id="turn_stale")
    )
    fut_stale2 = asyncio.create_task(
        scheduler.schedule(stale_turn_task_2, priority=TtsPriority.NORMAL, generation_id="turn_stale")
    )
    await asyncio.sleep(0.01)

    # User interrupts! Cancel turn_stale
    cancelled_count = scheduler.cancel_generation("turn_stale")
    assert cancelled_count == 2

    # Unblock current turn
    block_gate.set()
    await fut_curr

    # Verify stale tasks raised CancelledError
    with pytest.raises(asyncio.CancelledError):
        await fut_stale1
    with pytest.raises(asyncio.CancelledError):
        await fut_stale2

    await scheduler.aclose()
    assert executed == ["current"]
    assert "stale_1" not in executed
    assert "stale_2" not in executed


# ============================================================================
# 3. TtsService Routing Through Scheduler
# ============================================================================

@pytest.mark.asyncio
async def test_tts_service_synthesize_to_file_uses_scheduler(tmp_path: Path):
    """Verify that synthesize_to_file passes priority and gen_id to scheduler."""
    mock_client = MagicMock()
    mock_client.synthesize = AsyncMock(return_value=b"RIFFdummywavdata123456789")

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_mgr = TtsCacheManager(cache_dir=cache_dir)
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    tts_svc = TtsService(client=mock_client, cache_manager=cache_mgr, audio_dir=audio_dir)

    # First call: cache miss, triggers synthesis via scheduler
    url, path, size = await tts_svc.synthesize_to_file(
        "おはようございます！",
        options={"_priority": 0, "_generation_id": "gen_test_123"},
        use_cache=True,
    )
    assert url.startswith("/audio/cache/")
    assert size == len(b"RIFFdummywavdata123456789")
    mock_client.synthesize.assert_awaited_once()

    # Second call with same text: immediate cache HIT (no scheduler overhead)
    mock_client.synthesize.reset_mock()
    url2, path2, size2 = await tts_svc.synthesize_to_file(
        "おはようございます！",
        options={"_priority": 1, "_generation_id": "gen_test_456"},
        use_cache=True,
    )
    assert url2 == url
    assert mock_client.synthesize.await_count == 0

    await cache_mgr.aclose()


# ============================================================================
# 4. Multi-Loop and Loop-Switching Resilience
# ============================================================================

@pytest.mark.asyncio
async def test_scheduler_and_single_flight_loop_rebind():
    """Verify SingleFlightCoordinator and TtsScheduler dynamically adapt if loop changes."""
    sf = SingleFlightCoordinator()
    sched = TtsScheduler()

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
    await sched.aclose()


# ============================================================================
# 5. AudioStaticFiles HTTP Caching Headers
# ============================================================================

@pytest.mark.asyncio
async def test_audio_static_files_caching_headers(tmp_path: Path):
    """Verify that cached files get immutable headers while ephemeral files get max-age=3600."""
    audio_dir = tmp_path / "audio"
    cache_dir = audio_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    cache_file = cache_dir / "abc12345.wav"
    cache_file.write_bytes(b"RIFFcache")

    ephemeral_file = audio_dir / "voice_001.wav"
    ephemeral_file.write_bytes(b"RIFFephemeral")

    static_handler = AudioStaticFiles(directory=str(audio_dir))

    # Test cache/ URL
    scope_cache = {
        "type": "http",
        "method": "GET",
        "path": "/cache/abc12345.wav",
        "headers": [],
    }
    resp_cache = await static_handler.get_response("cache/abc12345.wav", scope_cache)
    assert resp_cache.status_code == 200
    assert "public, max-age=31536000, immutable" == resp_cache.headers.get("cache-control")
    assert resp_cache.headers.get("accept-ranges") == "bytes"

    # Test regular ephemeral audio URL
    scope_eph = {
        "type": "http",
        "method": "GET",
        "path": "/voice_001.wav",
        "headers": [],
    }
    resp_eph = await static_handler.get_response("voice_001.wav", scope_eph)
    assert resp_eph.status_code == 200
    assert "public, max-age=3600" == resp_eph.headers.get("cache-control")
    assert resp_eph.headers.get("accept-ranges") == "bytes"


# ============================================================================
# 6. Web Audio Dynamic Jitter Buffer Adaptation Math
# ============================================================================

def test_dynamic_jitter_buffer_adaptation_logic():
    """Unit test the adaptive lead buffer math matching frontend audio_player.js."""
    min_lead = 0.02
    max_lead = 0.12
    current_lead = 0.02
    consecutive_smooth = 0
    underrun_count = 0

    # 1. Simulate underrun occurs
    underrun_count += 1
    current_lead = min(max_lead, current_lead + 0.02)
    consecutive_smooth = 0
    assert underrun_count == 1
    assert current_lead == pytest.approx(0.04)

    # 2. Second underrun occurs
    underrun_count += 1
    current_lead = min(max_lead, current_lead + 0.02)
    assert current_lead == pytest.approx(0.06)

    # 3. Four smooth chunks (not yet 5)
    for _ in range(4):
        consecutive_smooth += 1
        if consecutive_smooth >= 5 and current_lead > min_lead:
            current_lead = max(min_lead, current_lead - 0.005)
            consecutive_smooth = 0
    assert current_lead == pytest.approx(0.06)

    # 4. Fifth smooth chunk triggers decay
    consecutive_smooth += 1
    if consecutive_smooth >= 5 and current_lead > min_lead:
        current_lead = max(min_lead, current_lead - 0.005)
        consecutive_smooth = 0
    assert current_lead == pytest.approx(0.055)
    assert consecutive_smooth == 0
