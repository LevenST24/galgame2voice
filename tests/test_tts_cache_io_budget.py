"""Exercise public cache consumers without full-file reads or unbounded retention."""

import asyncio
import builtins
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.tts_cache_manager import TtsCacheManager
from galgame2voice.services.tts_scheduler import TtsScheduler
from galgame2voice.services.tts_service import TtsService


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    cache = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=db_path, max_mem_mb=1)
    scheduler = TtsScheduler()
    monkeypatch.setattr("galgame2voice.services.tts_service.get_tts_scheduler", lambda: scheduler)
    client = MagicMock()
    client.synthesize = AsyncMock(return_value=b"fresh audio")
    service = TtsService(client=client, cache_manager=cache, audio_dir=tmp_path / "audio")
    try:
        yield service, cache, client
    finally:
        await scheduler.aclose()
        await cache.aclose()


async def seed(service, cache, audio, text="こんにちは。"):
    opts = service._sanitize_dynamic_voice_options({"_pre_resolved": True}, text=text)
    key, clean, params = cache.compute_cache_key(text, options=opts)
    entry = await cache.put(key, text, clean, 1, params, audio)
    return key, entry


@pytest.mark.parametrize("warm", [False, True])
async def test_file_hit_reads_no_audio_bytes(stack, monkeypatch, warm):
    service, cache, client = stack
    key, entry = await seed(service, cache, b"X" * 65536)
    if not warm:
        cache._mem_cache_discard(key)
    read_bytes = MagicMock(side_effect=AssertionError("file lookup read the entire audio"))
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert await service.synthesize_to_file("こんにちは。", options={"_pre_resolved": True}) == entry
    client.synthesize.assert_not_awaited()
    read_bytes.assert_not_called()
    assert cache._hits == 1
    assert cache._misses == 0
    if not warm:
        assert key not in cache._mem_cache


@pytest.mark.parametrize("warm", [False, True])
async def test_stream_hit_counts_one_playback(stack, warm):
    service, cache, client = stack
    audio = b"ABCD" * 4096
    key, _ = await seed(service, cache, audio)
    if not warm:
        cache._mem_cache_discard(key)
    chunks = [chunk async for chunk in service.stream_tts(
        "こんにちは。", options={"_pre_resolved": True}, chunk_size=2048,
    )]
    assert b"".join(chunks) == audio
    assert all(0 < len(chunk) <= 2048 for chunk in chunks)
    client.synthesize.assert_not_awaited()
    assert cache._hits == 1, "one cached playback was counted more than once"
    await cache.flush_dirty_touches()
    async with get_db(cache.db_path) as conn:
        assert (await crud.get_tts_cache_entry(conn, key)).hit_count == 1


async def test_large_stream_delivers_first_chunk_without_full_read(stack, monkeypatch):
    service, cache, client = stack
    audio = b"large audio" * 200000
    key, _ = await seed(service, cache, audio)
    cache._mem_cache_discard(key)
    read_bytes = MagicMock(side_effect=AssertionError("stream read the entire audio"))
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    stream = service.stream_tts("こんにちは。", options={"_pre_resolved": True}, chunk_size=8192)
    try:
        assert await anext(stream) == audio[:8192]
        read_bytes.assert_not_called()
        assert key not in cache._mem_cache
        assert cache._mem_bytes_total <= cache.max_mem_bytes
    finally:
        await stream.aclose()
    client.stream_tts.assert_not_called()


async def test_service_closes_cached_file_on_early_exit(stack, monkeypatch):
    service, cache, client = stack
    key, _ = await seed(service, cache, b"X" * 65536)
    cache._mem_cache_discard(key)
    real_open = builtins.open
    handles = []

    def track_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if Path(path) == cache.cache_dir / f"{key}.wav":
            handles.append(handle)
        return handle

    monkeypatch.setattr(builtins, "open", track_open)
    stream = service.stream_tts("こんにちは。", options={"_pre_resolved": True}, chunk_size=1024)
    try:
        assert await anext(stream) == b"X" * 1024
    finally:
        await stream.aclose()
    assert handles, "cold playback should use the disk stream"
    assert all(handle.closed for handle in handles), "service left its cached file open"
    assert key not in cache._mem_cache, "an interrupted read populated partial cached audio"


async def test_disk_stream_stays_bounded_if_file_grows_after_stat(stack, monkeypatch):
    service, cache, client = stack
    key, (_, path, _) = await seed(service, cache, b"small")
    cache._mem_cache_discard(key)
    expanded_audio = b"X" * (cache.max_mem_bytes + 8192)
    real_open = builtins.open

    def expand_on_open(open_path, *args, **kwargs):
        if Path(open_path) == path:
            path.write_bytes(expanded_audio)
        return real_open(open_path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", expand_on_open)
    total = 0
    async for chunk in service.stream_tts("こんにちは。", options={"_pre_resolved": True}, chunk_size=8192):
        assert len(chunk) <= 8192
        total += len(chunk)
    assert total == len(expanded_audio)
    assert key not in cache._mem_cache
    assert cache._mem_bytes_total <= cache.max_mem_bytes


@pytest.mark.parametrize("damage", ["missing", "truncate"])
async def test_stream_cache_miss_falls_back_and_records_one_miss(stack, damage):
    service, cache, client = stack
    if damage == "truncate":
        key, (_, path, _) = await seed(service, cache, b"old")
        path.write_bytes(b"")
        cache._mem_cache_discard(key)

    async def upstream(*args, **kwargs):
        yield b"fresh"
        yield b" audio"

    client.stream_tts = upstream
    assert b"".join([chunk async for chunk in service.stream_tts(
        "こんにちは。", options={"_pre_resolved": True},
    )]) == b"fresh audio"
    assert cache._misses == 1
    assert cache._hits == 0
    opts = service._sanitize_dynamic_voice_options({"_pre_resolved": True}, text="こんにちは。")
    key, _, _ = cache.compute_cache_key("こんにちは。", options=opts)
    assert (await cache.get(key))[0] == b"fresh audio"


@pytest.mark.parametrize("damage", ["delete", "truncate"])
async def test_file_hit_revalidates_disk_even_with_warm_memory(stack, damage):
    service, cache, client = stack
    key, (_, path, _) = await seed(service, cache, b"old audio")
    assert key in cache._mem_cache
    if damage == "delete":
        path.unlink()
    else:
        path.write_bytes(b"")
    _, returned_path, size = await service.synthesize_to_file("こんにちは。", options={"_pre_resolved": True})
    client.synthesize.assert_awaited_once()
    assert returned_path.read_bytes() == b"fresh audio"
    assert size == len(b"fresh audio")
    assert cache._hits == 0
    assert cache._misses == 1


@pytest.mark.parametrize("operation", ["put", "get"])
async def test_oversized_entry_preserves_small_hot_entries(stack, operation):
    service, cache, client = stack
    small_key, _ = await seed(service, cache, b"small", text="small")
    large = b"X" * (cache.max_mem_bytes + 1)
    if operation == "put":
        large_key, _ = await seed(service, cache, large, text="large")
    else:
        large_key = "disk-only-large"
        (cache.cache_dir / f"{large_key}.wav").write_bytes(large)
        assert (await cache.get(large_key))[0] == large
    assert small_key in cache._mem_cache, "large entry evicted useful small entries"
    assert large_key not in cache._mem_cache
    assert cache._mem_bytes_total <= cache.max_mem_bytes


async def test_concurrent_overwrites_use_serialized_size_deltas(stack, monkeypatch):
    service, cache, client = stack
    key, _ = await seed(service, cache, b"old")
    await cache.aclose()
    # Start both calls together while write admission is held. Old-row reads
    # must happen after admission, so the second writer sees the first size.
    cache._disk_bytes_total = 3
    cache._disk_files_total = 1
    cache._stats_initialized = True
    # Observe actual admission, so both calls have reached the write gate
    # before it is opened, without a timing-dependent scheduling sleep.
    real_lock = cache._write_lock
    both_waiting = asyncio.Event()
    class AdmissionGate:
        waiting = 0

        async def __aenter__(self):
            self.waiting += 1
            if self.waiting == 2:
                both_waiting.set()
            await real_lock.acquire()

        async def __aexit__(self, *args):
            real_lock.release()

    await cache._write_lock.acquire()
    monkeypatch.setattr(cache, "_write_lock", AdmissionGate())
    calls = [asyncio.create_task(cache.put(key, "text", "text", 1, "params", data))
             for data in (b"first", b"second-longer")]
    try:
        await asyncio.wait_for(both_waiting.wait(), timeout=3)
    finally:
        real_lock.release()
    await asyncio.gather(*calls)
    async with get_db(cache.db_path) as conn:
        actual = await crud.get_tts_cache_stats(conn)
    assert cache._disk_files_total == actual["total_files"] == 1
    assert cache._disk_bytes_total == actual["total_size_bytes"]
