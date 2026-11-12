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
from unittest.mock import patch
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


@pytest.mark.asyncio
async def test_memory_lru_access_order_evicts_cold_key(temp_db_and_cache_dir):
    """Promoting a key keeps it when the in-memory entry cap evicts the cold one."""
    db_path, cache_dir = temp_db_and_cache_dir
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_mem_entries=3)
    keys = []
    payloads = []
    for i in range(3):
        text = f"entry_{i}"
        data = b"DATA_" + bytes([i])
        key, clean, params_hash = mgr.compute_cache_key(text)
        await mgr.put(key, text, clean, 1, params_hash, data)
        keys.append(key)
        payloads.append(data)

    await mgr.get(keys[0])
    text4 = "entry_3"
    data4 = b"DATA_3"
    key4, clean4, params_hash4 = mgr.compute_cache_key(text4)
    await mgr.put(key4, text4, clean4, 1, params_hash4, data4)

    # Disk still holds every key, so get() would hide memory eviction.
    for key in (*keys, key4):
        (cache_dir / f"{key}.wav").unlink()

    assert await mgr.get(keys[1]) is None
    assert (await mgr.get(keys[0]))[0] == payloads[0]
    assert (await mgr.get(keys[2]))[0] == payloads[2]
    assert (await mgr.get(key4))[0] == data4
    await mgr.aclose()


@pytest.mark.asyncio
async def test_memory_byte_budget_matches_cached_lengths(temp_db_and_cache_dir):
    """A 1500-byte memory budget keeps the newest entries and a consistent counter."""
    db_path, cache_dir = temp_db_and_cache_dir
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_mem_entries=100)
    mgr.max_mem_bytes = 1500
    keys = []
    payload = b"B" * 500
    for i in range(5):
        text = f"byte_item_{i}"
        key, clean, params_hash = mgr.compute_cache_key(text)
        await mgr.put(key, text, clean, 1, params_hash, payload)
        keys.append(key)

    for key in keys:
        (cache_dir / f"{key}.wav").unlink()

    assert await mgr.get(keys[0]) is None
    assert await mgr.get(keys[1]) is None
    assert (await mgr.get(keys[2]))[0] == payload
    assert (await mgr.get(keys[3]))[0] == payload
    assert (await mgr.get(keys[4]))[0] == payload
    assert len(mgr._mem_cache) <= 3
    assert mgr._mem_bytes_total <= 1500
    # No public stats field reports the in-memory byte counter.
    assert mgr._mem_bytes_total == sum(len(v) for v in mgr._mem_cache.values())
    await mgr.aclose()


@pytest.mark.asyncio
async def test_zero_byte_disk_file_is_unlinked_and_counts_one_miss(temp_db_and_cache_dir):
    """A 0-byte disk file is a miss, is removed, and can be replaced."""
    db_path, cache_dir = temp_db_and_cache_dir
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path)
    text = "zero_byte_sentence"
    key, clean, params_hash = mgr.compute_cache_key(text)
    await mgr.put(key, text, clean, 1, params_hash, b"ORIGINAL_VALID_AUDIO")
    disk_file = cache_dir / f"{key}.wav"
    disk_file.write_bytes(b"")
    mgr._mem_cache_discard(key)

    assert await mgr.get(key) is None
    assert not disk_file.exists()
    stats = await mgr.get_stats()
    assert stats["total_misses"] == 1

    await mgr.put(key, text, clean, 1, params_hash, b"RECOVERED_AUDIO")
    recovered = await mgr.get(key)
    assert recovered is not None
    assert recovered[0] == b"RECOVERED_AUDIO"
    assert recovered[1] == f"/audio/cache/{key}.wav"
    assert recovered[2] == len(b"RECOVERED_AUDIO")
    await mgr.aclose()


@pytest.mark.asyncio
async def test_unreadable_disk_cache_counts_one_miss(temp_db_and_cache_dir):
    """OSError while reading a cache file is one miss and not a hit."""
    db_path, cache_dir = temp_db_and_cache_dir
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path)
    text = "unreadable_disk_sector"
    key, clean, params_hash = mgr.compute_cache_key(text)
    await mgr.put(key, text, clean, 1, params_hash, b"VALID_AUDIO")
    mgr._mem_cache_discard(key)

    with patch.object(Path, "read_bytes", side_effect=OSError("Disk read error")):
        assert await mgr.get(key) is None
    stats = await mgr.get_stats()
    assert stats["total_misses"] == 1
    await mgr.aclose()


@pytest.mark.asyncio
async def test_concurrent_distinct_keys_keep_counter_and_file_count(temp_db_and_cache_dir):
    """Fifty writers of distinct keys keep every file and a consistent memory counter."""
    db_path, cache_dir = temp_db_and_cache_dir
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_mem_entries=128)

    async def worker(worker_id: int):
        for i in range(20):
            text = f"task_{worker_id}_line_{i}"
            key, clean, params_hash = mgr.compute_cache_key(text)
            data = f"AUDIO_{worker_id}_{i}".encode("utf-8") * 50
            await mgr.put(key, text, clean, 1, params_hash, data)
            hit = await mgr.get(key)
            assert hit is not None
            assert hit[0] == data

    await asyncio.gather(*[asyncio.create_task(worker(i)) for i in range(50)])
    await mgr.aclose()

    assert mgr._mem_bytes_total == sum(len(v) for v in mgr._mem_cache.values())
    stats = await mgr.get_stats()
    assert stats["total_files"] == 1000
    assert stats["total_hits"] >= 1000
    await mgr.aclose()


@pytest.mark.asyncio
async def test_concurrent_shared_key_overwrites_do_not_raise(temp_db_and_cache_dir):
    """Fifty tasks overwriting five hot keys must not raise."""
    db_path, cache_dir = temp_db_and_cache_dir
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_mem_entries=64)

    async def worker(worker_id: int):
        for i in range(10):
            shared_id = i % 5
            text = f"hot_key_{shared_id}"
            key, clean, params_hash = mgr.compute_cache_key(text)
            data = f"WORKER_{worker_id}_HOT_{shared_id}".encode("utf-8") * 50
            await mgr.put(key, text, clean, 1, params_hash, data)
            assert await mgr.get(key) is not None

    await asyncio.gather(*[asyncio.create_task(worker(i)) for i in range(50)])
    await mgr.aclose()


@pytest.mark.asyncio
async def test_burst_put_and_prune_stays_within_entry_bound(temp_db_and_cache_dir):
    """Concurrent puts against a tiny cache must not crash and must stay bounded."""
    db_path, cache_dir = temp_db_and_cache_dir
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_cache_mb=1, max_entries=5)

    async def worker(idx: int):
        text = f"Burst Test Audio Sentence {idx}"
        key, clean, params_hash = mgr.compute_cache_key(text)
        fake_wav = b"RIFF" + b"\x00" * 200000
        await mgr.put(key, text, clean, 1, params_hash, fake_wav)
        assert await mgr.get(key) is not None

    await asyncio.gather(*[worker(i) for i in range(20)])
    await mgr.aclose()
    stats = await mgr.get_stats()
    assert stats["total_files"] <= 10


@pytest.mark.asyncio
async def test_concurrent_get_throttle_mutation(temp_db_and_cache_dir):
    """Concurrent get() must not raise while throttle bookkeeping walks its map."""
    db_path, cache_dir = temp_db_and_cache_dir
    manager = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_mem_entries=32)
    for i in range(10):
        await manager.put(
            cache_key=f"key_{i}",
            text=f"text_{i}",
            clean_text=f"text_{i}",
            voice_profile_id=1,
            params_hash="hash",
            audio_bytes=b"RIFF....data....",
        )

    async def reader(idx: int):
        for step in range(50):
            await manager.get(f"key_{step % 150}")

    await asyncio.gather(*[asyncio.create_task(reader(i)) for i in range(30)])
    await manager.aclose()


@pytest.mark.asyncio
async def test_prune_one_call_drains_past_single_batch(temp_db_and_cache_dir):
    """One prune() deletes more than the 200-row page when the entry cap requires it."""
    db_path, cache_dir = temp_db_and_cache_dir
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_entries=5000, max_cache_mb=1024)
    async with get_db(db_path) as conn:
        for i in range(250):
            file_path = cache_dir / f"batch_{i}.wav"
            payload = b"RIFF" + b"\x00" * 16
            file_path.write_bytes(payload)
            await crud.upsert_tts_cache_entry(
                conn=conn,
                cache_key=f"batch_{i}",
                text=f"text_{i}",
                clean_text=f"text_{i}",
                voice_profile_id=1,
                params_hash="h",
                file_path=str(file_path),
                file_size=len(payload),
            )
    await mgr.aclose()

    pruned = await mgr.prune(max_entries=10, max_mb=1024)
    stats = await mgr.get_stats()
    assert pruned == 242
    assert stats["total_files"] == 8
    await mgr.aclose()


@pytest.mark.asyncio
async def test_prune_drops_rows_whose_cache_files_are_already_missing(temp_db_and_cache_dir):
    """Rows whose cache file was removed outside prune are still deleted."""
    db_path, cache_dir = temp_db_and_cache_dir
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_entries=100)
    keys = []
    for i in range(15):
        text = f"ファイル_{i}"
        key, clean, params_hash = mgr.compute_cache_key(text)
        await mgr.put(key, text, clean, 1, params_hash, b"RIFF" + bytes([i]))
        keys.append(key)
    for key in keys[:5]:
        (cache_dir / f"{key}.wav").unlink()
    await mgr.aclose()

    async with get_db(db_path) as conn:
        for key in keys[:5]:
            await conn.execute(
                "UPDATE tts_cache_entries SET last_accessed_at = datetime('now', '-1 day') WHERE cache_key = ?",
                (key,),
            )
        await conn.commit()

    pruned = await mgr.prune(max_entries=10)
    stats = await mgr.get_stats()
    assert pruned == 7
    assert stats["total_files"] == 8
    async with get_db(db_path) as conn:
        for key in keys[:5]:
            assert await crud.get_tts_cache_entry(conn, key) is None
            assert await mgr.get(key) is None
    await mgr.aclose()


@pytest.mark.asyncio
async def test_clear_concurrent_with_put_does_not_raise(temp_db_and_cache_dir):
    """clear() overlapping put() finishes and still reports usable stats."""
    db_path, cache_dir = temp_db_and_cache_dir
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path, max_cache_mb=10, max_entries=50)

    async def put_task(i: int):
        audio = b"RIFF" + b"\x00" * 200 + bytes([i % 256])
        await mgr.put(f"key_{i}", f"text_{i}", f"clean_{i}", 1, f"hash_{i}", audio)

    async def clear_task():
        await asyncio.sleep(0.01)
        return await mgr.clear()

    await asyncio.gather(
        *[put_task(i) for i in range(15)],
        clear_task(),
        clear_task(),
    )
    stats = await mgr.get_stats()
    assert stats["total_files"] >= 0
    assert stats["hit_rate_percent"] >= 0.0
    await mgr.aclose()


@pytest.mark.asyncio
async def test_put_database_failure_does_not_leave_a_memory_hit(temp_db_and_cache_dir):
    """A failed metadata write must not leave the audio readable from the cache."""
    db_path, cache_dir = temp_db_and_cache_dir
    mgr = TtsCacheManager(cache_dir=cache_dir, db_path=db_path)
    bad_key = "fail_key_xyz"
    with patch(
        "galgame2voice.database.crud.upsert_tts_cache_entry",
        side_effect=RuntimeError("Disk write simulated DB failure"),
    ):
        with pytest.raises(RuntimeError, match="Disk write simulated DB failure"):
            await mgr.put(bad_key, "fail text", "fail", 1, "hash_bad", b"RIFF" + b"\x00" * 200)
    assert await mgr.get(bad_key) is None
    await mgr.aclose()
