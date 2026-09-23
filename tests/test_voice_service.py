"""
Unit and Integration Tests for VoiceManager streaming synthesis and unified scheduling.
Verifies:
1. stream_tts cache hit yields cached audio directly with zero GPU overhead.
2. stream_tts cache miss routes through tts_scheduler.schedule_stream and asynchronously populates cache.
3. stream_tts respects use_cache=False.
4. stream_tts mid-stream cancellation does not cache partial audio.
"""

import asyncio
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch
import pytest

from galgame2voice.services.voice_manager import VoiceManager
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_scheduler import get_tts_scheduler, TtsPriority
from galgame2voice.services.tts_cache_manager import get_tts_cache_manager


@pytest.fixture
def mock_client():
    """Mock GptSovitsClient with controllable stream_tts."""
    client = AsyncMock(spec=GptSovitsClient)
    client.current_refer_audio = "ref/test.wav"
    client.is_healthy = AsyncMock(return_value=True)

    stream_call_count = 0

    async def mock_stream_tts(text, options=None, chunk_size=4096) -> AsyncGenerator[bytes, None]:
        nonlocal stream_call_count
        stream_call_count += 1
        # Yield fake wav chunks
        yield b"RIFF" + b"\x00" * 40
        yield b"DATA" + b"\x01\x02\x03\x04"
        yield b"EXTRA" + b"\x05\x06\x07\x08"

    client.stream_tts = mock_stream_tts
    client.get_stream_calls = lambda: stream_call_count
    return client


@pytest.mark.asyncio
async def test_stream_tts_cache_miss_routes_scheduler_and_populates_cache(temp_db_path, mock_client, tmp_path):
    """Verifies that cache miss routes through tts_scheduler and asynchronously caches audio."""
    cache_mgr = get_tts_cache_manager(cache_dir=str(tmp_path / "cache"), db_path=temp_db_path)
    await cache_mgr.clear()

    vm = VoiceManager(gpt_sovits_client_or_server=mock_client, db_path=temp_db_path)
    vm.tts_service.cache_manager = cache_mgr

    text = "テスト音声ストリーミングです。"
    options = {"voice_profile_id": 1, "prompt_lang": "ja", "text_lang": "ja"}

    # First call: Cache miss
    chunks_miss = []
    async for chunk in vm.stream_tts(text, options=options):
        chunks_miss.append(chunk)

    full_miss_audio = b"".join(chunks_miss)
    assert full_miss_audio.startswith(b"RIFF")
    assert mock_client.get_stream_calls() == 1

    # Wait for async background cache task to complete
    await asyncio.sleep(0.1)

    # Verify cache key exists in cache_mgr
    cache_key, _, _ = cache_mgr.compute_cache_key(text, options=options)
    cached_entry = await cache_mgr.get(cache_key)
    assert cached_entry is not None
    assert cached_entry[0] == full_miss_audio

    # Second call: Cache HIT (zero GPU calls!)
    chunks_hit = []
    async for chunk in vm.stream_tts(text, options=options):
        chunks_hit.append(chunk)

    full_hit_audio = b"".join(chunks_hit)
    assert full_hit_audio == full_miss_audio
    # Crucial assertion: client.stream_tts call count must remain 1 (ZERO additional GPU calls!)
    assert mock_client.get_stream_calls() == 1


@pytest.mark.asyncio
async def test_stream_tts_cache_disabled(temp_db_path, mock_client, tmp_path):
    """Verifies use_cache=False bypasses cache check and does not store audio."""
    cache_mgr = get_tts_cache_manager(cache_dir=str(tmp_path / "cache_disabled"), db_path=temp_db_path)
    await cache_mgr.clear()

    vm = VoiceManager(gpt_sovits_client_or_server=mock_client, db_path=temp_db_path)
    vm.tts_service.cache_manager = cache_mgr

    text = "キャッシュ無効化テスト。"
    options = {"voice_profile_id": 1}

    chunks = []
    async for chunk in vm.stream_tts(text, options=options, use_cache=False):
        chunks.append(chunk)

    assert b"".join(chunks).startswith(b"RIFF")
    assert mock_client.get_stream_calls() == 1

    await asyncio.sleep(0.05)
    cache_key, _, _ = cache_mgr.compute_cache_key(text, options=options)
    assert await cache_mgr.get(cache_key) is None


@pytest.mark.asyncio
async def test_stream_tts_aborted_not_cached(temp_db_path, mock_client, tmp_path):
    """Verifies that aborting stream midway does NOT cache incomplete audio."""
    cache_mgr = get_tts_cache_manager(cache_dir=str(tmp_path / "cache_abort"), db_path=temp_db_path)
    await cache_mgr.clear()

    vm = VoiceManager(gpt_sovits_client_or_server=mock_client, db_path=temp_db_path)
    vm.tts_service.cache_manager = cache_mgr

    text = "中断テスト文章。"
    options = {"voice_profile_id": 1}

    # Caller consumes only 1 chunk and breaks early
    async for _ in vm.stream_tts(text, options=options):
        break

    # Wait for any potential background task
    await asyncio.sleep(0.08)

    cache_key, _, _ = cache_mgr.compute_cache_key(text, options=options)
    # The cache should be empty because stream didn't finish normally!
    assert await cache_mgr.get(cache_key) is None


@pytest.mark.asyncio
async def test_stream_tts_routes_through_scheduler_priority(temp_db_path, mock_client, tmp_path):
    """Verifies that stream_tts invokes schedule_stream on the unified scheduler with specified priority."""
    scheduler = get_tts_scheduler()
    scheduler.reset_metrics()

    vm = VoiceManager(gpt_sovits_client_or_server=mock_client, db_path=temp_db_path)
    options = {"_priority": int(TtsPriority.HIGH), "voice_profile_id": 1}

    with patch.object(scheduler, "schedule_stream", wraps=scheduler.schedule_stream) as mock_sched:
        chunks = []
        async for chunk in vm.stream_tts("プライオリティテスト", options=options, use_cache=False):
            chunks.append(chunk)

        assert mock_sched.called
        call_kwargs = mock_sched.call_args[1]
        assert call_kwargs.get("priority") == TtsPriority.HIGH
