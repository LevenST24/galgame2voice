# -*- coding: utf-8 -*-
"""
Tests for Milestone M14: Real-Time Streaming & TTS Architecture Optimization.
Verifies:
1. True upstream-to-downstream streaming pipelining with early lock release.
2. Silent audio guard enforcement in streaming TTS.
3. AudioSpecCache cache hit and mtime_ns / size invalidation.
4. VoiceProfileResolver in-memory caching and invalidation.
5. SingleFlightCoordinator concurrency coalescing (cache stampede prevention).
6. TtsScheduler priority ordering (HIGH > NORMAL > LOW) and stale task cancellation.
7. TTS Cache Key reference audio identity hardening (mtime_ns + size).
8. TtsCacheManager stream_cached chunked delivery.
"""

import asyncio
import io
import os
import struct
import tempfile
import time
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from galgame2voice.services.gpt_sovits_client import (
    AudioSpec,
    AudioSpecCache,
    GptSovitsClient,
    SILENT_AUDIO_ERROR,
    probe_audio_spec,
)
from galgame2voice.services.voice_resolver import (
    ResolvedVoiceContext,
    VoiceProfileResolver,
    get_voice_resolver,
)
from galgame2voice.services.tts_scheduler import (
    SingleFlightCoordinator,
    TtsPriority,
    TtsScheduler,
    get_tts_scheduler,
)
from galgame2voice.services.tts_cache_manager import TtsCacheManager
from galgame2voice.services.tts_service import TtsService


def _generate_synthetic_wav_bytes(duration_s: float = 3.5, sample_rate: int = 32000, amplitude: int = 12000) -> bytes:
    """Generates synthetic 16-bit mono PCM WAV bytes with audible sine wave."""
    num_samples = int(duration_s * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        # Create non-zero amplitude data
        samples = []
        for i in range(num_samples):
            # 440Hz tone
            val = int(amplitude * 0.5 * (1.0 if (i // 72) % 2 == 0 else -1.0))
            samples.append(val)
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buf.getvalue()


def _generate_silent_wav_bytes(duration_s: float = 3.5, sample_rate: int = 32000) -> bytes:
    """Generates synthetic 16-bit mono PCM WAV bytes with all zeros (silent)."""
    return _generate_synthetic_wav_bytes(duration_s, sample_rate, amplitude=0)


# ============================================================================
# 1. True Upstream-to-Downstream Streaming Pipelining & Early Lock Release
# ============================================================================

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
async def test_silent_audio_guard_in_streaming():
    """
    Verifies that all-zero silent audio triggers SILENT_AUDIO_ERROR before yielding chunks.
    """
    client = GptSovitsClient(server="http://127.0.0.1:9880")
    client.is_healthy = True
    client.current_refer_audio = "dummy_ref.wav"
    client.current_refer_text = "プロンプト"

    silent_wav = _generate_silent_wav_bytes(duration_s=3.0)
    chunk_size = 4096
    upstream_chunks = [silent_wav[i:i + chunk_size] for i in range(0, len(silent_wav), chunk_size)]

    class MockSilentResponse:
        status_code = 200
        headers = {"content-type": "audio/wav"}

        def raise_for_status(self):
            pass

        async def aiter_bytes(self, *args, **kwargs):
            for chunk in upstream_chunks:
                yield chunk

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    def mock_stream(*args, **kwargs):
        return MockSilentResponse()

    client._http_client = MagicMock()
    client._http_client.stream = mock_stream

    with pytest.raises(RuntimeError) as exc_info:
        async for _ in client.stream_tts(
            "テスト",
            options={"_pre_resolved": True, "ref_audio_path": "dummy_ref.wav", "prompt_text": "テスト"},
        ):
            pass

    assert SILENT_AUDIO_ERROR in str(exc_info.value)
    # Lock must also be cleanly released
    assert not client.lock.locked()


# ============================================================================
# 2. AudioSpecCache & Invalidation on File Modification
# ============================================================================

def test_audio_spec_cache_invalidation_on_file_change():
    """
    Verifies that modifying an audio file's size or mtime invalidates the AudioSpecCache entry.
    """
    cache = AudioSpecCache(maxsize=10)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        tf_path = Path(tf.name)
        try:
            # Write 2-second synthetic audio
            data_2s = _generate_synthetic_wav_bytes(duration_s=2.0, sample_rate=32000)
            tf.write(data_2s)
            tf.flush()
            tf.close()

            spec1 = cache.get_spec(tf_path)
            assert spec1 is not None
            assert 1.9 <= spec1.duration_s <= 2.1
            assert spec1.sample_rate == 32000

            # Second read should hit cache
            spec2 = cache.get_spec(tf_path)
            assert spec2 is spec1

            # Modify the file: write 4-second audio
            time.sleep(0.01)  # Ensure distinct mtime
            data_4s = _generate_synthetic_wav_bytes(duration_s=4.0, sample_rate=32000)
            tf_path.write_bytes(data_4s)

            spec3 = cache.get_spec(tf_path)
            assert spec3 is not None
            assert 3.9 <= spec3.duration_s <= 4.1
            assert spec3 is not spec1, "Cache must invalidate when file size / mtime changes"
        finally:
            tf_path.unlink(missing_ok=True)


# ============================================================================
# 3. VoiceProfileResolver In-Memory Caching & Invalidation
# ============================================================================

@pytest.mark.asyncio
async def test_voice_profile_resolver_caching_and_invalidation():
    """
    Verifies VoiceProfileResolver caches resolved context in RAM and invalidates on demand.
    """
    resolver = VoiceProfileResolver(ttl_seconds=60.0)

    # Mock DB profile
    mock_profile = MagicMock()
    mock_profile.id = 42
    mock_profile.name = "四季夏目"
    mock_profile.prompt_lang = "ja"
    mock_profile.text_lang = "ja"
    mock_profile.ref_audio_path = "nonexistent.wav"
    mock_profile.prompt_text = "デフォルトプロンプト"

    with patch("galgame2voice.database.crud.get_voice_profile", new_callable=AsyncMock) as mock_get_db:
        mock_get_db.return_value = mock_profile

        ctx1 = await resolver.resolve_context("test.db", profile_id=42)
        assert ctx1 is not None
        assert ctx1.profile_id == 42
        assert ctx1.name == "四季夏目"
        assert mock_get_db.call_count == 1

        # Second call should hit in-memory cache without hitting DB
        ctx2 = await resolver.resolve_context("test.db", profile_id=42)
        assert ctx2 is ctx1
        assert mock_get_db.call_count == 1

        # Invalidate specific profile ID
        resolver.invalidate(profile_id=42)
        ctx3 = await resolver.resolve_context("test.db", profile_id=42)
        assert ctx3 is not None
        assert mock_get_db.call_count == 2


# ============================================================================
# 4. SingleFlightCoordinator Concurrency Coalescing
# ============================================================================

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


# ============================================================================
# 5. TtsScheduler Priority Ordering & Stale Task Cancellation
# ============================================================================

@pytest.mark.asyncio
async def test_tts_scheduler_priority_and_cancellation():
    """
    Verifies priority scheduling (HIGH > NORMAL > LOW) and generation cancellation.
    """
    scheduler = TtsScheduler()
    execution_order = []

    async def make_task(label, delay=0.01):
        async def _work():
            await asyncio.sleep(delay)
            execution_order.append(label)
            return label
        return _work

    # Enqueue LOW, then HIGH, then NORMAL while holding scheduler busy
    busy_gate = asyncio.Event()

    async def blocking_task():
        await busy_gate.wait()
        execution_order.append("initial_busy")

    t_init = asyncio.create_task(scheduler.schedule(blocking_task, priority=TtsPriority.HIGH))

    # Give worker loop a tick to pick up t_init and block on busy_gate
    await asyncio.sleep(0.01)

    # Enqueue other tasks while worker is blocked
    t_low = asyncio.create_task(scheduler.schedule(await make_task("low"), priority=TtsPriority.LOW))
    t_norm = asyncio.create_task(scheduler.schedule(await make_task("norm"), priority=TtsPriority.NORMAL))
    t_high = asyncio.create_task(scheduler.schedule(await make_task("high"), priority=TtsPriority.HIGH))

    # Give all three tasks a tick to finish enqueueing before worker starts
    await asyncio.sleep(0.01)

    # Release worker
    busy_gate.set()
    await asyncio.gather(t_init, t_high, t_norm, t_low)

    # HIGH must run before NORMAL, which must run before LOW
    idx_high = execution_order.index("high")
    idx_norm = execution_order.index("norm")
    idx_low = execution_order.index("low")
    assert idx_high < idx_norm < idx_low, f"Priority violation: {execution_order}"

    # Test stale generation cancellation
    cancelled_ran = False

    async def should_be_cancelled():
        nonlocal cancelled_ran
        cancelled_ran = True
        return "ran"

    # Block scheduler again
    blocker_gate = asyncio.Event()

    async def blocker():
        await blocker_gate.wait()

    t_block = asyncio.create_task(scheduler.schedule(blocker, priority=TtsPriority.HIGH))
    await asyncio.sleep(0.01)

    t_stale = asyncio.create_task(
        scheduler.schedule(
            should_be_cancelled,
            priority=TtsPriority.LOW,
            generation_id="gen_chat_999",
        )
    )
    await asyncio.sleep(0.01)

    try:
        # Cancel while sitting behind t_block
        cancelled_count = scheduler.cancel_generation("gen_chat_999")
        assert cancelled_count == 1
    finally:
        blocker_gate.set()
        await t_block

    with pytest.raises(asyncio.CancelledError):
        await t_stale

    assert not cancelled_ran, "Cancelled task should never have executed!"
    await scheduler.aclose()


# ============================================================================
# 6. Hardened Cache Key Identity (mtime_ns + size)
# ============================================================================

@pytest.mark.requires_writable_temp_dir
def test_hardened_cache_key_identity_with_file_mtime():
    """
    Verifies that changing a reference audio's mtime or content changes its cache key,
    preventing stale audio cache collisions.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        ref_file = tmp_path / "reference.wav"
        ref_file.write_bytes(b"INITIAL_AUDIO_CONTENT")

        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=tmp_path / "test.db")

        opts = {"ref_audio_path": str(ref_file)}
        key1, _, _ = mgr.compute_cache_key("こんにちは", options=opts)

        # Modify the audio content and update mtime
        time.sleep(0.01)
        ref_file.write_bytes(b"MODIFIED_AUDIO_CONTENT_WITH_DIFFERENT_LENGTH")

        key2, _, _ = mgr.compute_cache_key("こんにちは", options=opts)

        assert key1 != key2, "Cache key must change when reference audio file content/mtime is updated!"


# ============================================================================
# 7. TtsCacheManager stream_cached Chunk Delivery
# ============================================================================

@pytest.mark.requires_writable_temp_dir
@pytest.mark.asyncio
async def test_tts_cache_manager_stream_cached():
    """
    Verifies stream_cached delivers audio in chunks without loading everything into monolithic memory.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=tmp_path / "test.db")

        test_data = _generate_synthetic_wav_bytes(duration_s=1.0)
        await mgr.put(
            cache_key="test_chunk_key",
            text="テスト",
            clean_text="テスト",
            voice_profile_id=1,
            params_hash="dummy_hash",
            audio_bytes=test_data,
        )

        chunks = []
        async for chunk in mgr.stream_cached("test_chunk_key", chunk_size=1024):
            chunks.append(chunk)

        assert len(chunks) > 1
        assert b"".join(chunks) == test_data
