"""
Adversarial Stress & Hardening Test Suite for Milestone M2_PERF_STABILITY.
Focuses on:
1. TTS Cache Latency Benchmark (10,000 in-memory requests < 0.05ms and < 0.005ms target)
2. High-concurrency burst reads/writes across 50 concurrent async tasks
3. Cache LRU eviction (entry-based and byte-based)
4. Zero-byte corrupt file detection and auto-recovery
5. Corrupted / unreadable disk cache fallback
"""

import asyncio
import os
import random
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from galgame2voice.database.session import init_db
from galgame2voice.services.tts_cache_manager import TtsCacheManager


@pytest.mark.asyncio
class TestTtsCacheAdversarialM2:
    """Empirical verification harness for TTS Cache latency, concurrency, and stability."""

    async def test_in_memory_latency_benchmark_10k(self, tmp_path):
        """
        Empirically measures in-memory get() latency across 10,000 requests.
        Verifies average latency is strictly < 0.05ms (target < 0.005ms).
        """
        db_path = tmp_path / "latency.db"
        await init_db(db_path)
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(db_path))

        text = "先生、おはようございます！"
        key, clean, params_hash = mgr.compute_cache_key(text)
        sample_audio = b"RIFF" + b"\x00" * 4096  # 4KB synthetic audio
        await mgr.put(key, text, clean, 1, params_hash, sample_audio)

        # Warm up cache
        for _ in range(100):
            res = await mgr.get(key)
            assert res is not None

        latencies_ms = []
        t_bench_start = time.perf_counter()
        for _ in range(10000):
            t0 = time.perf_counter()
            hit = await mgr.get(key)
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)

        total_elapsed_ms = (time.perf_counter() - t_bench_start) * 1000.0
        avg_latency_ms = sum(latencies_ms) / len(latencies_ms)
        latencies_ms.sort()
        p50_ms = latencies_ms[5000]
        p95_ms = latencies_ms[9500]
        p99_ms = latencies_ms[9900]
        max_latency_ms = latencies_ms[-1]

        print(f"\n[10k Single-Key Benchmark]")
        print(f"  Total time for 10,000 gets: {total_elapsed_ms:.2f} ms")
        print(f"  Average latency: {avg_latency_ms:.6f} ms ({avg_latency_ms * 1000.0:.2f} us)")
        print(f"  p50 latency:     {p50_ms:.6f} ms")
        print(f"  p95 latency:     {p95_ms:.6f} ms")
        print(f"  p99 latency:     {p99_ms:.6f} ms")
        print(f"  Max latency:     {max_latency_ms:.6f} ms")

        # Contract requirements
        assert avg_latency_ms < 0.05, f"Average latency {avg_latency_ms:.4f}ms exceeds 0.05ms threshold"
        assert avg_latency_ms < 0.005, f"Average latency {avg_latency_ms:.4f}ms exceeds target 0.005ms"

    async def test_in_memory_multi_key_latency_benchmark_10k(self, tmp_path):
        """
        Empirically measures in-memory get() latency across 10,000 requests
        distributed randomly over 50 distinct cached items.
        """
        db_path = tmp_path / "multi_latency.db"
        await init_db(db_path)
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(db_path), max_mem_entries=100)

        keys = []
        for i in range(50):
            txt = f"台詞テスト番号_{i}"
            k, c, p = mgr.compute_cache_key(txt)
            await mgr.put(k, txt, c, 1, p, b"WAV_DATA_" + bytes([i % 256]) * 1000)
            keys.append(k)

        # Warm up
        for k in keys:
            await mgr.get(k)

        rng = random.Random(1337)
        workload = [rng.choice(keys) for _ in range(10000)]

        latencies_ms = []
        t_bench_start = time.perf_counter()
        for k in workload:
            t0 = time.perf_counter()
            hit = await mgr.get(k)
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)

        total_elapsed_ms = (time.perf_counter() - t_bench_start) * 1000.0
        avg_latency_ms = sum(latencies_ms) / len(latencies_ms)

        print(f"\n[10k Multi-Key (50 keys) Benchmark]")
        print(f"  Total time for 10,000 gets: {total_elapsed_ms:.2f} ms")
        print(f"  Average latency: {avg_latency_ms:.6f} ms ({avg_latency_ms * 1000.0:.2f} us)")

        assert avg_latency_ms < 0.05, f"Average multi-key latency {avg_latency_ms:.4f}ms exceeds 0.05ms"
        assert avg_latency_ms < 0.005, f"Average multi-key latency {avg_latency_ms:.4f}ms exceeds 0.005ms target"

    async def test_lru_entry_count_and_access_ordering(self, tmp_path):
        """
        Tests in-memory LRU entry limit eviction and access-order preservation.
        """
        db_path = tmp_path / "lru_entry.db"
        await init_db(db_path)
        # Cap memory cache at 3 items
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(db_path), max_mem_entries=3)

        keys = []
        for i in range(3):
            txt = f"entry_{i}"
            k, c, p = mgr.compute_cache_key(txt)
            await mgr.put(k, txt, c, 1, p, b"DATA_" + bytes([i]))
            keys.append(k)

        assert len(mgr._mem_cache) == 3

        # Access entry 0 to promote it to most-recently used
        await mgr.get(keys[0])

        # Add a 4th entry; entry 1 (least recently used) should be evicted from memory
        txt4 = "entry_3"
        k4, c4, p4 = mgr.compute_cache_key(txt4)
        await mgr.put(k4, txt4, c4, 1, p4, b"DATA_3")

        assert len(mgr._mem_cache) == 3
        assert keys[1] not in mgr._mem_cache, "Oldest unaccessed entry_1 must be evicted"
        assert keys[0] in mgr._mem_cache, "Recently accessed entry_0 must be preserved"
        assert keys[2] in mgr._mem_cache
        assert k4 in mgr._mem_cache

    async def test_lru_byte_limit_eviction(self, tmp_path):
        """
        Tests in-memory LRU byte-limit eviction when entries exceed byte threshold.
        """
        db_path = tmp_path / "lru_bytes.db"
        await init_db(db_path)
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(db_path), max_mem_entries=100)
        # Set manual tight byte budget: 1500 bytes
        mgr.max_mem_bytes = 1500

        for i in range(5):
            txt = f"byte_item_{i}"
            k, c, p = mgr.compute_cache_key(txt)
            # Each entry is 500 bytes
            await mgr.put(k, txt, c, 1, p, b"B" * 500)

        # Total memory bytes must not exceed 1500 bytes
        assert mgr._mem_bytes_total <= 1500
        assert len(mgr._mem_cache) <= 3
        # Ensure internal byte counter exactly matches cache content lengths
        assert mgr._mem_bytes_total == sum(len(v) for v in mgr._mem_cache.values())

    async def test_zero_byte_disk_cache_recovery(self, tmp_path):
        """
        Tests that zero-byte corrupt disk files are detected, unlinked, and handled cleanly.
        """
        db_path = tmp_path / "zero_byte.db"
        await init_db(db_path)
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(db_path))

        txt = "zero_byte_sentence"
        k, c, p = mgr.compute_cache_key(txt)
        await mgr.put(k, txt, c, 1, p, b"ORIGINAL_VALID_AUDIO")

        disk_file = mgr.cache_dir / f"{k}.wav"
        assert disk_file.exists()

        # Truncate disk file to 0 bytes
        disk_file.write_bytes(b"")

        # Evict memory cache to force fallback to disk inspection
        mgr._mem_cache_discard(k)

        # get() must detect 0-byte file, unlink it, and return None
        res = await mgr.get(k)
        assert res is None
        assert not disk_file.exists(), "Corrupt 0-byte file must be unlinked"
        assert mgr._misses == 1

        # Re-putting valid audio succeeds
        await mgr.put(k, txt, c, 1, p, b"RECOVERED_AUDIO")
        res2 = await mgr.get(k)
        assert res2 is not None
        assert res2[0] == b"RECOVERED_AUDIO"

    async def test_corrupted_disk_cache_read_fallback(self, tmp_path):
        """
        Tests fallback when disk file exists but throws OSError on read.
        """
        db_path = tmp_path / "corrupt_read.db"
        await init_db(db_path)
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(db_path))

        txt = "unreadable_disk_sector"
        k, c, p = mgr.compute_cache_key(txt)
        await mgr.put(k, txt, c, 1, p, b"VALID_AUDIO")
        mgr._mem_cache_discard(k)

        with patch.object(Path, "read_bytes", side_effect=OSError("Disk read error")):
            res = await mgr.get(k)
            assert res is None
            assert mgr._misses == 1

    async def test_50_concurrent_tasks_distinct_keys(self, tmp_path):
        """
        Stress tests 50 concurrent async tasks doing burst reads and writes on distinct keys.
        """
        db_path = tmp_path / "concurrent_distinct.db"
        await init_db(db_path)
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(db_path), max_mem_entries=128)

        async def worker(worker_id: int):
            for i in range(20):
                txt = f"task_{worker_id}_line_{i}"
                k, c, p = mgr.compute_cache_key(txt)
                data = f"AUDIO_{worker_id}_{i}".encode("utf-8") * 50
                # Put
                await mgr.put(k, txt, c, 1, p, data)
                # Immediate Get (in-memory hit)
                hit = await mgr.get(k)
                assert hit is not None
                assert hit[0] == data

        tasks = [asyncio.create_task(worker(i)) for i in range(50)]
        await asyncio.gather(*tasks)

        # Drain background tasks
        if mgr._bg_tasks:
            await asyncio.gather(*list(mgr._bg_tasks), return_exceptions=True)

        assert mgr._mem_bytes_total == sum(len(v) for v in mgr._mem_cache.values())
        stats = await mgr.get_stats()
        assert stats["total_files"] == 1000
        assert stats["total_hits"] >= 1000

    async def test_50_concurrent_tasks_shared_burst_overwrite(self, tmp_path):
        """
        Adversarial test: 50 concurrent async tasks simultaneously writing
        to a shared set of identical keys (hot key contention).
        Reveals any race conditions or Windows file sharing lock collisions.
        """
        db_path = tmp_path / "concurrent_shared.db"
        await init_db(db_path)
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(db_path), max_mem_entries=64)

        errors = []

        async def worker(worker_id: int):
            try:
                for i in range(10):
                    shared_id = i % 5  # Contention on 5 shared hot keys
                    txt = f"hot_key_{shared_id}"
                    k, c, p = mgr.compute_cache_key(txt)
                    data = f"WORKER_{worker_id}_HOT_{shared_id}".encode("utf-8") * 50
                    await mgr.put(k, txt, c, 1, p, data)
                    res = await mgr.get(k)
                    assert res is not None
            except Exception as exc:
                errors.append((worker_id, exc))

        tasks = [asyncio.create_task(worker(i)) for i in range(50)]
        await asyncio.gather(*tasks)

        if mgr._bg_tasks:
            await asyncio.gather(*list(mgr._bg_tasks), return_exceptions=True)

        if errors:
            pytest.fail(
                f"Concurrent burst write failed with {len(errors)} errors: "
                f"{type(errors[0][1]).__name__}: {errors[0][1]}"
            )
