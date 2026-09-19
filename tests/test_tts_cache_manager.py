"""
Unit and integration tests for TtsCacheManager:
1. stream_cached() memory spike fix & chunked streaming.
2. batch_touch_tts_cache_entries & in-memory _dirty_touches batching.
"""

import asyncio
import os
import tempfile
import time
from pathlib import Path
import pytest
import aiosqlite

from galgame2voice.database.session import get_db, init_db
from galgame2voice.database import crud
from galgame2voice.services.tts_cache_manager import TtsCacheManager


@pytest.fixture
async def temp_db_and_cache_dir(tmp_path):
    """Provides an isolated SQLite database and cache directory for testing."""
    db_path = str(tmp_path / "test_tts.db")
    cache_dir = tmp_path / "audio_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    await init_db(db_path)
    return db_path, cache_dir


@pytest.mark.asyncio
async def test_batch_touch_tts_cache_entries_crud(temp_db_and_cache_dir):
    """Verifies that batch_touch_tts_cache_entries properly updates hit counts and last_accessed_at in SQLite."""
    db_path, _ = temp_db_and_cache_dir
    async with get_db(db_path) as conn:
        # Insert initial entries
        await crud.upsert_tts_cache_entry(conn, "key1", "hello", "hello", None, "h1", "/audio/cache/key1.wav", 1024)
        await crud.upsert_tts_cache_entry(conn, "key2", "world", "world", None, "h2", "/audio/cache/key2.wav", 2048)

        entry1 = await crud.get_tts_cache_entry(conn, "key1")
        entry2 = await crud.get_tts_cache_entry(conn, "key2")
        assert entry1.hit_count == 0
        assert entry2.hit_count == 0

        # Batch touch with various counts
        touches = {"key1": 3, "key2": 5, "nonexistent": 2}
        await crud.batch_touch_tts_cache_entries(conn, touches)

        entry1_after = await crud.get_tts_cache_entry(conn, "key1")
        entry2_after = await crud.get_tts_cache_entry(conn, "key2")
        assert entry1_after.hit_count == 3
        assert entry2_after.hit_count == 5

        # Additional batch touch
        await crud.batch_touch_tts_cache_entries(conn, {"key1": 2})
        entry1_final = await crud.get_tts_cache_entry(conn, "key1")
        assert entry1_final.hit_count == 5

        # Empty and non-positive touches should be no-ops
        await crud.batch_touch_tts_cache_entries(conn, {})
        await crud.batch_touch_tts_cache_entries(conn, {"key1": 0, "key2": -1})
        entry1_noop = await crud.get_tts_cache_entry(conn, "key1")
        assert entry1_noop.hit_count == 5


@pytest.mark.asyncio
async def test_stream_cached_bounded_chunks_and_memory_caching(temp_db_and_cache_dir):
    """
    Verifies stream_cached() yields data chunk-by-chunk without loading the entire
    file into memory beforehand, and caches to memory LRU only if file_size <= max_mem_bytes.
    """
    db_path, cache_dir = temp_db_and_cache_dir

    # Create manager with max_mem_mb=1 (1 MB limit)
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_mem_mb=1)

    # 1. Test file <= max_mem_bytes (e.g. 32 KB audio)
    audio_data_small = b"X" * 32768  # 32 KB
    await mgr.put("key_small", "text", "text", None, "h_small", audio_data_small)

    # Evict from in-memory cache to ensure we test disk streaming path
    async with mgr._lock:
        mgr._mem_cache.clear()
        mgr._mem_bytes_total = 0
        assert "key_small" not in mgr._mem_cache

    # Stream with bounded chunk size 4096
    chunks_yielded = []
    async for chunk in mgr.stream_cached("key_small", chunk_size=4096):
        assert len(chunk) <= 4096
        chunks_yielded.append(chunk)

    reconstructed = b"".join(chunks_yielded)
    assert reconstructed == audio_data_small
    assert len(chunks_yielded) == 8  # 32768 / 4096 = 8 chunks

    # Since file <= max_mem_bytes, it should now be loaded into memory LRU
    assert "key_small" in mgr._mem_cache
    assert mgr._mem_cache["key_small"] == audio_data_small

    # Streaming again should serve directly from in-memory cache
    mem_chunks = []
    async for chunk in mgr.stream_cached("key_small", chunk_size=4096):
        mem_chunks.append(chunk)
    assert b"".join(mem_chunks) == audio_data_small

    await mgr.aclose()


@pytest.mark.asyncio
async def test_stream_cached_large_file_not_stored_in_memory(temp_db_and_cache_dir):
    """
    Verifies that files larger than max_mem_bytes are streamed in bounded chunks
    and are NOT stored into the in-memory LRU cache upon completion.
    """
    db_path, cache_dir = temp_db_and_cache_dir

    # Configure small memory cap: 1 MB
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_mem_mb=1)

    # Put a 1.5 MB file directly on disk & DB to simulate large audio
    large_size = int(1.5 * 1024 * 1024)
    large_data = b"L" * large_size
    file_path = cache_dir / "key_large.wav"
    file_path.write_bytes(large_data)

    async with get_db(db_path) as conn:
        await crud.upsert_tts_cache_entry(conn, "key_large", "large", "large", None, "h_large", "/audio/cache/key_large.wav", large_size)

    # Ensure not in memory cache
    assert "key_large" not in mgr._mem_cache

    # Stream cached with 8192 chunk size
    streamed_bytes_count = 0
    chunk_count = 0
    async for chunk in mgr.stream_cached("key_large", chunk_size=8192):
        assert len(chunk) <= 8192
        streamed_bytes_count += len(chunk)
        chunk_count += 1

    assert streamed_bytes_count == large_size
    assert chunk_count == (large_size + 8191) // 8192

    # Verification: Must NOT be stored in memory LRU cache because size > max_mem_bytes
    assert "key_large" not in mgr._mem_cache
    assert mgr._mem_bytes_total == 0

    await mgr.aclose()


@pytest.mark.asyncio
async def test_stream_cached_early_break_closes_fd(temp_db_and_cache_dir):
    """Verifies that early generator termination cleanly closes file descriptor and does not store partial data."""
    db_path, cache_dir = temp_db_and_cache_dir
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_mem_mb=1)

    audio_data = b"E" * 65536
    await mgr.put("key_early", "early", "early", None, "h_early", audio_data)

    async with mgr._lock:
        mgr._mem_cache.clear()
        mgr._mem_bytes_total = 0

    # Read only 1 chunk and break
    async for chunk in mgr.stream_cached("key_early", chunk_size=4096):
        assert len(chunk) == 4096
        break

    # Since stream was interrupted early, partial data should NOT be in memory cache
    assert "key_early" not in mgr._mem_cache

    await mgr.aclose()


@pytest.mark.asyncio
async def test_dirty_touches_buffering_and_batch_flushing(temp_db_and_cache_dir):
    """
    Verifies that cache hits buffer into _dirty_touches without immediate DB queries,
    and are flushed to SQLite via batch_touch_tts_cache_entries either explicitly,
    periodically, or via aclose().
    """
    db_path, cache_dir = temp_db_and_cache_dir
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_mem_mb=5)

    audio = b"AUDIO_DATA"
    await mgr.put("key_a", "txta", "txta", None, "ha", audio)
    await mgr.put("key_b", "txtb", "txtb", None, "hb", audio)

    # Initial check: hit_count is 0 in DB
    async with get_db(db_path) as conn:
        e_a = await crud.get_tts_cache_entry(conn, "key_a")
        e_b = await crud.get_tts_cache_entry(conn, "key_b")
        assert e_a.hit_count == 0
        assert e_b.hit_count == 0

    # Multiple gets on key_a and key_b
    for _ in range(5):
        await mgr.get("key_a")
    for _ in range(3):
        await mgr.get("key_b")

    # In-memory buffer check: touches are buffered in _dirty_touches
    async with mgr._lock:
        assert mgr._dirty_touches.get("key_a") == 5
        assert mgr._dirty_touches.get("key_b") == 3

    # Before flushing, verify SQLite entries haven't had per-hit individual queries spamming updates
    async with get_db(db_path) as conn:
        e_a_before = await crud.get_tts_cache_entry(conn, "key_a")
        assert e_a_before.hit_count == 0

    # Explicit flush
    await mgr.flush_dirty_touches()

    # Verify buffer is cleared
    async with mgr._lock:
        assert len(mgr._dirty_touches) == 0

    # Verify SQLite updated with the batched hit counts
    async with get_db(db_path) as conn:
        e_a_after = await crud.get_tts_cache_entry(conn, "key_a")
        e_b_after = await crud.get_tts_cache_entry(conn, "key_b")
        assert e_a_after.hit_count == 5
        assert e_b_after.hit_count == 3

    # Now test streaming hits buffering
    async for _ in mgr.stream_cached("key_a", chunk_size=1024):
        pass
    async for _ in mgr.stream_cached("key_a", chunk_size=1024):
        pass

    async with mgr._lock:
        assert mgr._dirty_touches.get("key_a") == 2

    # aclose() flushes remaining dirty touches
    await mgr.aclose()

    async with get_db(db_path) as conn:
        e_a_final = await crud.get_tts_cache_entry(conn, "key_a")
        assert e_a_final.hit_count == 7


@pytest.mark.asyncio
async def test_get_stats_flushes_dirty_touches(temp_db_and_cache_dir):
    """Verifies that calling get_stats() flushes dirty touches so reported stats are fresh."""
    db_path, cache_dir = temp_db_and_cache_dir
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path)

    await mgr.put("key_stat", "stat", "stat", None, "h_stat", b"STAT_DATA")
    await mgr.get("key_stat")
    await mgr.get("key_stat")

    # get_stats() should flush dirty touches to DB
    stats = await mgr.get_stats()
    assert stats["db_hits"] == 2
    assert stats["memory_hits"] == 2

    await mgr.aclose()
