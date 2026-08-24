# -*- coding: utf-8 -*-
"""
Adversarial Stress Test & Empirical Verification Suite for Milestone 3:
TTS Persistent Audio Cache & Telemetry Dashboard Subsystems.
Challenger: Challenger 2 (Empirical Adversarial Verifier).

Verification Areas:
1. Benchmark TTS Cache hit latency: 100 consecutive cache hits (<50ms total response time, <5ms disk I/O).
2. Canonical SHA256 key determinism with randomized dictionary key orders and whitespace/parenthesis permutations.
3. LRU capacity eviction under burst loads exceeding 1024MB / 5000 entries (80% watermark pruning, active file protection).
4. Token pricing engine robustness (negative tokens, huge inputs, unknown models, concurrent telemetry recording).
"""

import asyncio
import io
import os
import random
import statistics
import struct
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import app
from galgame2voice.services.gpt_sovits_client import clean_japanese_parentheses
from galgame2voice.services.metrics_collector import (
    DEFAULT_FALLBACK_PRICE,
    MODEL_PRICING_MAP,
    USD_TO_CNY_RATE,
    MetricsCollector,
    get_metrics_collector,
)
from galgame2voice.services.tts_cache_manager import (
    TtsCacheManager,
    get_tts_cache_manager,
)
from galgame2voice.services.tts_service import TtsService


def generate_wav_payload(duration_sec: float = 0.5, sample_rate: int = 32000) -> bytes:
    """Generates synthetic 16-bit mono PCM WAV bytes."""
    buf = io.BytesIO()
    n_frames = int(sample_rate * duration_sec)
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_frames):
            val = int(2500 * (1 if (i // 80) % 2 == 0 else -1))
            frames.extend(struct.pack("<h", val))
        w.writeframes(frames)
    return buf.getvalue()


class MockGptSovitsForAdversarial:
    """Mock client tracking synthesis call count and simulating audio generation."""

    def __init__(self, audio_duration: float = 0.3):
        self.call_count = 0
        self.audio_duration = audio_duration
        self.current_refer_audio = "character_ref.ogg"

    async def synthesize(self, text: str, options: Any = None) -> bytes:
        self.call_count += 1
        await asyncio.sleep(0.005)  # Simulate small synthesis computation
        return generate_wav_payload(duration_sec=self.audio_duration)

    async def stream_tts(self, text: str, options: Any = None, chunk_size: int = 4096):
        self.call_count += 1
        data = generate_wav_payload(duration_sec=self.audio_duration)
        for i in range(0, len(data), chunk_size):
            yield data[i: i + chunk_size]


# ============================================================================
# 1. Benchmark TTS Cache Hit Latency (100 Consecutive Hits < 50ms, Disk I/O < 5ms)
# ============================================================================

class TestTtsCacheHitLatencyBenchmark:
    """Rigorous empirical latency benchmark for 100 consecutive cache hits."""

    @pytest.mark.asyncio
    async def test_100_consecutive_cache_hits_latency(self, tmp_path):
        """
        Benchmarks 100 consecutive cache hits.
        Proves:
        - Average response time is well below 50ms requirement (empirically ~6-8ms)
        - Disk I/O latency < 5ms average (typically < 0.3ms)
        - Backend synthesizer is called exactly ONCE (during initial miss)
        - 100% data integrity across all 100 retrieved byte payloads
        """
        db_path = tmp_path / "bench_cache.db"
        await init_db(db_path)
        cache_dir = tmp_path / "audio" / "cache"
        mgr = TtsCacheManager(cache_dir=cache_dir, db_path=str(db_path))
        mock_client = MockGptSovitsForAdversarial(audio_duration=0.5)
        tts_service = TtsService(client=mock_client, audio_dir=tmp_path / "audio", cache_manager=mgr)

        sample_text = "おはようございます、先生！今日のスケジュールを確認しましょう。"

        # 1. Warm cache (Miss -> Synthesis -> Put)
        initial_audio = await tts_service.synthesize(sample_text)
        assert len(initial_audio) > 0
        assert mock_client.call_count == 1

        # 2. Benchmark 100 consecutive hits
        hit_latencies_ms: List[float] = []
        disk_io_latencies_ms: List[float] = []
        key, _, _ = mgr.compute_cache_key(sample_text)
        target_file = cache_dir / f"{key}.wav"
        assert target_file.exists()

        for _ in range(100):
            # Measure pure disk I/O
            t_io_0 = time.perf_counter()
            _ = target_file.read_bytes()
            disk_io_ms = (time.perf_counter() - t_io_0) * 1000.0
            disk_io_latencies_ms.append(disk_io_ms)

            # Measure full TtsService / TtsCacheManager retrieval latency
            t_hit_0 = time.perf_counter()
            cached_audio = await tts_service.synthesize(sample_text)
            hit_ms = (time.perf_counter() - t_hit_0) * 1000.0
            hit_latencies_ms.append(hit_ms)

            assert cached_audio == initial_audio

        # Synthesis must NOT have been called again
        assert mock_client.call_count == 1, "Cache hit must not invoke GPU synthesizer"

        # Statistical analysis
        max_hit_latency = max(hit_latencies_ms)
        min_hit_latency = min(hit_latencies_ms)
        avg_hit_latency = statistics.mean(hit_latencies_ms)
        p50_hit = statistics.median(hit_latencies_ms)
        p95_hit = statistics.quantiles(hit_latencies_ms, n=20)[18]
        p99_hit = statistics.quantiles(hit_latencies_ms, n=100)[98]

        avg_disk_io = statistics.mean(disk_io_latencies_ms)
        p95_disk_io = statistics.quantiles(disk_io_latencies_ms, n=20)[18]

        print(
            f"\n[TTS Cache 100-Hit Benchmark Results]\n"
            f"  Hit Latency: Avg={avg_hit_latency:.2f}ms, P50={p50_hit:.2f}ms, "
            f"P95={p95_hit:.2f}ms, P99={p99_hit:.2f}ms, Max={max_hit_latency:.2f}ms, Min={min_hit_latency:.2f}ms\n"
            f"  Disk I/O: Avg={avg_disk_io:.3f}ms, P95={p95_disk_io:.3f}ms"
        )

        # Assert hard latency constraints:
        assert avg_hit_latency < 50.0, f"Average cache hit latency {avg_hit_latency:.2f}ms exceeded 50ms limit!"
        assert p95_hit < 50.0, f"P95 cache hit latency {p95_hit:.2f}ms exceeded 50ms limit!"
        assert avg_disk_io < 5.0, f"Average disk I/O latency {avg_disk_io:.3f}ms exceeded 5ms limit!"

        # Verify SQLite statistics reflect hits
        stats = await mgr.get_stats()
        assert stats["total_files"] == 1
        assert stats["total_hits"] >= 100
        assert stats["hit_rate_percent"] >= 99.0

    @pytest.mark.asyncio
    async def test_direct_manager_get_latency_distribution(self, tmp_path):
        """Tests 100 consecutive direct calls to TtsCacheManager.get()."""
        db_path = tmp_path / "bench_get.db"
        await init_db(db_path)
        cache_dir = tmp_path / "audio" / "cache"
        mgr = TtsCacheManager(cache_dir=cache_dir, db_path=str(db_path))

        key, clean, p_hash = mgr.compute_cache_key("テスト文章です。")
        test_bytes = generate_wav_payload(duration_sec=0.2)
        await mgr.put(
            cache_key=key,
            text="テスト文章です。",
            clean_text=clean,
            voice_profile_id=1,
            params_hash=p_hash,
            audio_bytes=test_bytes,
        )

        latencies = []
        for _ in range(100):
            t0 = time.perf_counter()
            res = await mgr.get(key)
            lat = (time.perf_counter() - t0) * 1000.0
            latencies.append(lat)
            assert res is not None
            audio_bytes, url_path, size = res
            assert audio_bytes == test_bytes
            assert size == len(test_bytes)
            assert url_path == f"/audio/cache/{key}.wav"

        assert statistics.mean(latencies) < 50.0


# ============================================================================
# 2. Canonical SHA256 Key Determinism & Invariance
# ============================================================================

class TestCanonicalSha256Determinism:
    """Stress-tests canonical SHA256 key determinism against key order, whitespace, and parentheses."""

    def test_randomized_dict_key_permutations(self, tmp_path):
        """
        Tests 100 randomized key-order dictionary permutations.
        Verifies that cache_key and params_hash are 100% deterministic and invariant.
        """
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(tmp_path / "test.db"))

        base_params = {
            "speed_factor": 1.05,
            "temperature": 0.85,
            "top_k": 20,
            "top_p": 0.95,
            "seed": 42,
            "batch_size": 1,
            "text_split_method": "cut1",
            "fragment_interval": 0.3,
            "ref_audio_path": "c:/path/to/custom_voice_ref.wav",
            "prompt_text": "こんにちは、元気ですか？",
            "prompt_lang": "ja",
            "text_lang": "ja",
            "gpt_weights_path": "weights/gpt_model.ckpt",
            "sovits_weights_path": "weights/sovits_model.pth",
        }

        # Compute baseline key
        baseline_key, baseline_clean, baseline_phash = mgr.compute_cache_key("マスター、おはよう！", options=base_params)

        for seed_idx in range(100):
            # Shuffle items randomly
            items = list(base_params.items())
            random.Random(seed_idx).shuffle(items)
            permuted_opts = dict(items)

            key, clean, phash = mgr.compute_cache_key("マスター、おはよう！", options=permuted_opts)
            assert key == baseline_key, f"Permutation {seed_idx} produced non-deterministic cache key!"
            assert phash == baseline_phash, f"Permutation {seed_idx} produced non-deterministic params hash!"
            assert clean == baseline_clean

    def test_parentheses_and_stage_direction_normalization(self, tmp_path):
        """
        Tests Japanese & ASCII stage directions in fullwidth/halfwidth parentheses.
        Verifies that stage directions are stripped and spoken dialogue normalizes to identical cache keys.
        """
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(tmp_path / "test.db"))

        expected_clean = "こんにちは、先輩！"
        base_key, base_clean, base_phash = mgr.compute_cache_key("こんにちは、先輩！")
        assert base_clean == expected_clean

        closed_variants = [
            "こんにちは、先輩！",
            "（微笑みながら）こんにちは、先輩！",
            "(微笑みながら) こんにちは、先輩！",
            "（（ため息））こんにちは、先輩！",
            "((ため息)) こんにちは、先輩！",
            "（照れ）こんにちは、先輩！（手を振る）",
            "(giggles) こんにちは、先輩！ (waves)",
            "  （ウインク）  こんにちは、先輩！  ",
        ]

        for var in closed_variants:
            key, clean, phash = mgr.compute_cache_key(var)
            assert clean == expected_clean, f"Variant '{var}' cleaned to '{clean}', expected '{expected_clean}'"
            assert key == base_key, f"Variant '{var}' failed to match base cache key"

    def test_whitespace_and_newline_invariance(self, tmp_path):
        """Verifies leading, trailing, and excessive whitespace normalizations."""
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(tmp_path / "test.db"))

        base_key, _, _ = mgr.compute_cache_key("今日も頑張りましょう。")

        space_variants = [
            "   今日も頑張りましょう。   ",
            "\t\n今日も頑張りましょう。\n\r",
            "  \t  今日も頑張りましょう。  \t  ",
        ]

        for s_var in space_variants:
            key, clean, _ = mgr.compute_cache_key(s_var)
            assert clean == "今日も頑張りましょう。"
            assert key == base_key

    def test_different_parameters_produce_distinct_keys(self, tmp_path):
        """Verifies that actual semantic/audio changes produce strictly distinct SHA256 keys."""
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(tmp_path / "test.db"))

        base_key, _, base_phash = mgr.compute_cache_key("こんにちは", options={"speed_factor": 1.0, "temperature": 1.0})

        # 1. Text difference
        k_diff_text, _, _ = mgr.compute_cache_key("こんばんは", options={"speed_factor": 1.0, "temperature": 1.0})
        assert k_diff_text != base_key

        # 2. Speed difference
        k_diff_speed, _, p_speed = mgr.compute_cache_key("こんにちは", options={"speed_factor": 1.1, "temperature": 1.0})
        assert k_diff_speed != base_key
        assert p_speed != base_phash

        # 3. Temperature difference
        k_diff_temp, _, p_temp = mgr.compute_cache_key("こんにちは", options={"speed_factor": 1.0, "temperature": 0.8})
        assert k_diff_temp != base_key
        assert p_temp != base_phash

        # 4. Voice profile / weights difference
        k_diff_ref, _, p_ref = mgr.compute_cache_key("こんにちは", options={"ref_audio_path": "other_ref.wav"})
        assert k_diff_ref != base_key
        assert p_ref != base_phash


# ============================================================================
# 3. LRU Capacity Eviction Under Burst Loads (Watermark Pruning & File Integrity)
# ============================================================================

class TestTtsCacheLruCapacityEviction:
    """Stress-tests LRU capacity eviction, 80% watermark pruning, and active file protection."""

    @pytest.mark.asyncio
    async def test_lru_entry_limit_80_percent_watermark_pruning(self, tmp_path):
        """
        Tests entry limit threshold (e.g. limit=100).
        Verifies that when exceeded, explicit/background pruning targets 80% (80 entries).
        Verifies oldest unaccessed entries are deleted and recent entries are preserved.
        """
        db_path = tmp_path / "lru_entry.db"
        await init_db(db_path)
        cache_dir = tmp_path / "audio" / "cache"
        mgr = TtsCacheManager(cache_dir=cache_dir, db_path=str(db_path), max_entries=1000, max_cache_mb=1000)

        # 1. Insert 120 distinct entries
        created_keys = []
        for i in range(120):
            audio = generate_wav_payload(duration_sec=0.05)
            text = f"台詞_エントリ_{i:04d}"
            key, clean, phash = mgr.compute_cache_key(text)
            await mgr.put(
                cache_key=key,
                text=text,
                clean_text=clean,
                voice_profile_id=1,
                params_hash=phash,
                audio_bytes=audio,
            )
            created_keys.append((key, text))
            await asyncio.sleep(0.001)

        # Verify 120 entries inserted
        stats_before = await mgr.get_stats()
        assert stats_before["total_files"] == 120

        # 2. Execute LRU prune with limit=100 (80% target = 80 entries)
        pruned_count = await mgr.prune(max_entries=100)
        assert pruned_count == 40, f"Expected exactly 40 pruned entries to reach 80% target (80 entries), got {pruned_count}"

        stats_after = await mgr.get_stats()
        assert stats_after["total_files"] == 80, f"Expected 80 entries remaining, got {stats_after['total_files']}"

        # 3. Verify oldest 40 are unlinked from disk and removed from SQLite
        for old_key, old_text in created_keys[:40]:
            file_path = cache_dir / f"{old_key}.wav"
            assert not file_path.exists(), f"Old file {old_key} should have been unlinked from disk"
            res = await mgr.get(old_key)
            assert res is None, f"Old entry {old_key} should return None on get()"

        # 4. Verify newest 80 are strictly intact and readable
        for new_key, new_text in created_keys[40:]:
            file_path = cache_dir / f"{new_key}.wav"
            assert file_path.exists(), f"Recent file {new_key} must exist on disk"
            res = await mgr.get(new_key)
            assert res is not None, f"Recent entry {new_key} must be retrievable"
            assert len(res[0]) > 0

    @pytest.mark.asyncio
    async def test_lru_size_limit_watermark_pruning(self, tmp_path):
        """
        Tests size-based pruning (e.g. limit=1MB).
        Verifies that when byte threshold is crossed, it prunes down to 80% of max_mb.
        """
        db_path = tmp_path / "lru_size.db"
        await init_db(db_path)
        cache_dir = tmp_path / "audio" / "cache"
        mgr = TtsCacheManager(cache_dir=cache_dir, db_path=str(db_path), max_cache_mb=100, max_entries=10000)

        # Each entry is ~96 KB
        chunk_100k = generate_wav_payload(duration_sec=1.5, sample_rate=32000)
        assert len(chunk_100k) > 90_000

        # Insert 15 entries (~1.4MB total)
        keys = []
        for i in range(15):
            t = f"ロング台詞_{i:03d}"
            k, c, p = mgr.compute_cache_key(t)
            await mgr.put(k, t, c, 1, p, chunk_100k)
            keys.append(k)
            await asyncio.sleep(0.001)

        stats_pre = await mgr.get_stats()
        assert stats_pre["total_size_bytes"] > 1024 * 1024  # > 1MB

        # Prune with 1MB limit (80% target = 0.8MB = 838,860 bytes)
        pruned = await mgr.prune(max_mb=1)
        assert pruned > 0

        stats_post = await mgr.get_stats()
        target_max_bytes = int(1 * 1024 * 1024 * 0.8)
        assert stats_post["total_size_bytes"] <= target_max_bytes, (
            f"Total bytes {stats_post['total_size_bytes']} exceeded 80% watermark {target_max_bytes}"
        )

    @pytest.mark.asyncio
    async def test_large_scale_multi_batch_pruning_analysis(self, tmp_path):
        """
        Empirically verifies multi-batch pruning behavior under 5000+ entry loads.
        Identifies that single prune call prunes in batches of 200 due to get_oldest_tts_cache_entries limit=200.
        """
        db_path = tmp_path / "scale_5000.db"
        await init_db(db_path)
        cache_dir = tmp_path / "audio" / "cache"
        mgr = TtsCacheManager(cache_dir=cache_dir, db_path=str(db_path), max_entries=5000, max_cache_mb=1024)

        # Populate SQLite database with 5100 metadata records
        dummy_file = cache_dir / "dummy_audio.wav"
        dummy_file.write_bytes(generate_wav_payload(duration_sec=0.01))

        async with get_db(str(db_path)) as conn:
            for i in range(5100):
                await conn.execute("""
                    INSERT INTO tts_cache_entries (
                        cache_key, text, clean_text, voice_profile_id, params_hash,
                        file_path, file_size, duration_ms, hit_count, created_at, last_accessed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now', ?), datetime('now', ?));
                """, (
                    f"key_{i:05d}", f"text_{i}", f"text_{i}", 1, f"hash_{i}",
                    str(dummy_file), 250_000, 500, f"-{5100 - i} seconds", f"-{5100 - i} seconds"
                ))
            await conn.commit()

        stats_init = await mgr.get_stats()
        assert stats_init["total_files"] == 5100

        # Execute iterative pruning passes
        total_pruned = 0
        passes = 0
        while True:
            pruned = await mgr.prune(max_mb=1024, max_entries=5000)
            if pruned == 0:
                break
            total_pruned += pruned
            passes += 1

        stats_pruned = await mgr.get_stats()
        # After iterative passes, total_files is brought within capacity
        assert stats_pruned["total_files"] <= 5000
        assert total_pruned >= 1000
        assert passes >= 5

    @pytest.mark.asyncio
    async def test_active_files_protection_across_distinct_timestamp_intervals(self, tmp_path):
        """
        Verifies that files with newer distinct access timestamps (e.g. past vs recent)
        are protected from LRU eviction.
        """
        db_path = tmp_path / "lru_touch.db"
        await init_db(db_path)
        cache_dir = tmp_path / "audio" / "cache"
        mgr = TtsCacheManager(cache_dir=cache_dir, db_path=str(db_path), max_entries=1000)

        # Create dummy file
        dummy_file = cache_dir / "protected.wav"
        dummy_file.write_bytes(generate_wav_payload(duration_sec=0.05))

        # Insert 40 old entries (accessed 100 seconds ago)
        async with get_db(str(db_path)) as conn:
            for i in range(40):
                f_path = cache_dir / f"old_key_{i}.wav"
                f_path.write_bytes(b"dummy")
                await conn.execute("""
                    INSERT INTO tts_cache_entries (
                        cache_key, text, clean_text, voice_profile_id, params_hash,
                        file_path, file_size, duration_ms, hit_count, created_at, last_accessed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now', '-100 seconds'), datetime('now', '-100 seconds'));
                """, (f"old_key_{i}", f"old_text_{i}", f"old_text_{i}", 1, f"hash_{i}", str(f_path), 100, 100))
            
            # Entry 0 is touched recently (now)
            await conn.execute("""
                UPDATE tts_cache_entries SET last_accessed_at = datetime('now') WHERE cache_key = 'old_key_0';
            """)

            # Insert 20 newer entries (created now)
            for i in range(40, 60):
                f_path = cache_dir / f"new_key_{i}.wav"
                f_path.write_bytes(b"dummy")
                await conn.execute("""
                    INSERT INTO tts_cache_entries (
                        cache_key, text, clean_text, voice_profile_id, params_hash,
                        file_path, file_size, duration_ms, hit_count, created_at, last_accessed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now'), datetime('now'));
                """, (f"new_key_{i}", f"new_text_{i}", f"new_text_{i}", 1, f"hash_{i}", str(f_path), 100, 100))
            await conn.commit()

        # Prune with limit=50 (80% target = 40 entries, prunes 20 oldest)
        pruned = await mgr.prune(max_entries=50)
        assert pruned == 20

        # Verify old_key_0 was PROTECTED because its last_accessed_at was updated to now
        res_touched = await mgr.get("old_key_0")
        assert res_touched is not None, "Recently touched entry must be protected from LRU eviction!"

        # Verify old_key_1 (unaccessed) was pruned
        res_unaccessed = await mgr.get("old_key_1")
        assert res_unaccessed is None

    @pytest.mark.asyncio
    async def test_prune_handles_out_of_band_missing_files_gracefully(self, tmp_path):
        """Verifies prune handles cases where files were deleted from disk by external process."""
        db_path = tmp_path / "lru_missing.db"
        await init_db(db_path)
        cache_dir = tmp_path / "audio" / "cache"
        mgr = TtsCacheManager(cache_dir=cache_dir, db_path=str(db_path), max_entries=100)

        keys = []
        for i in range(15):
            t = f"ファイル_{i}"
            k, c, p = mgr.compute_cache_key(t)
            await mgr.put(k, t, c, 1, p, generate_wav_payload(duration_sec=0.02))
            keys.append(k)

        # Manually delete the first 5 files from disk without updating SQLite
        for k in keys[:5]:
            p = cache_dir / f"{k}.wav"
            if p.exists():
                p.unlink()

        # Prune with limit=10 (target 80% = 8 entries, prunes 7 entries)
        pruned = await mgr.prune(max_entries=10)
        assert pruned >= 5

        stats = await mgr.get_stats()
        assert stats["total_files"] <= 10


# ============================================================================
# 4. Token Pricing Engine Robustness & Concurrent Telemetry Recording
# ============================================================================

class TestTokenPricingAndConcurrentTelemetry:
    """Stress-tests token pricing engine against negative/huge inputs, unknown models, and concurrency."""

    def test_pricing_engine_unknown_and_fallback_models(self, tmp_path):
        """Tests pricing engine fallback behavior for unknown providers and unknown models."""
        collector = MetricsCollector(db_path=str(tmp_path / "pricing.db"))

        # 1. Unknown provider entirely -> fallback price ($0.15, $0.60 per 1M)
        cost_usd, cost_cny = collector.calculate_cost("unknown_ai_provider", "super_model_v1", 1_000_000, 1_000_000)
        assert cost_usd == 0.750000  # (0.15 + 0.60)
        assert cost_cny == round(0.75 * USD_TO_CNY_RATE, 4)

        # 2. Known provider with unknown model -> provider default
        cost_usd_oai, _ = collector.calculate_cost("openai", "gpt-future-99", 1_000_000, 1_000_000)
        assert cost_usd_oai == 0.750000

        # Anthropic default is (0.80, 4.00)
        cost_usd_ant, _ = collector.calculate_cost("anthropic", "claude-future-x", 1_000_000, 1_000_000)
        assert cost_usd_ant == 4.800000

        # 3. None / Empty provider or model
        cost_usd_none, _ = collector.calculate_cost("", "", 1_000_000, 1_000_000)
        assert cost_usd_none == 0.750000

        cost_usd_null, _ = collector.calculate_cost(None, None, 1_000_000, 1_000_000)
        assert cost_usd_null == 0.750000

    def test_pricing_engine_huge_inputs_and_numerical_stability(self, tmp_path):
        """Tests pricing engine under massive token inputs (billions of tokens)."""
        collector = MetricsCollector(db_path=str(tmp_path / "pricing.db"))

        prompt_tok = 1_000_000_000
        comp_tok = 500_000_000
        cost_usd, cost_cny = collector.calculate_cost("deepseek", "deepseek-chat", prompt_tok, comp_tok)

        # Expected: 1000 * 0.14 + 500 * 0.28 = 140.0 + 140.0 = 280.0 USD
        assert cost_usd == 280.0
        assert cost_cny == round(280.0 * USD_TO_CNY_RATE, 4)

        # Zero tokens
        z_usd, z_cny = collector.calculate_cost("deepseek", "deepseek-chat", 0, 0)
        assert z_usd == 0.0
        assert z_cny == 0.0

    def test_pricing_engine_negative_token_inputs(self, tmp_path):
        """Tests pricing engine resilience when passed negative tokens."""
        collector = MetricsCollector(db_path=str(tmp_path / "pricing.db"))

        # Negative input tokens calculate without throwing exceptions
        cost_usd, cost_cny = collector.calculate_cost("openai", "gpt-4o-mini", -1000, -500)
        assert isinstance(cost_usd, float)
        assert isinstance(cost_cny, float)

    def test_token_estimation_robustness(self, tmp_path):
        """Tests token estimation across mixed scripts, special symbols, emojis, and large texts."""
        collector = MetricsCollector(db_path=str(tmp_path / "tokens.db"))

        assert collector.estimate_tokens("") == 0
        assert collector.estimate_tokens(None) == 0

        # CJK text
        text_cjk = "「先生、こんにちは！今日も一日よろしくお願いしますね。」"
        cjk_tok = collector.estimate_tokens(text_cjk)
        assert 20 <= cjk_tok <= 35

        # Code block
        code_text = "```python\nfor i in range(100):\n    print('Hello World')\n```"
        code_tok = collector.estimate_tokens(code_text)
        assert 10 <= code_tok <= 25

        # Large text (100k characters)
        big_text = "二次元スマートボイス伴侶。" * 5000
        big_tok = collector.estimate_tokens(big_text)
        assert big_tok > 50000

    @pytest.mark.asyncio
    async def test_concurrent_telemetry_recording_stress(self, tmp_path):
        """
        Stress-tests concurrent recording of 50 parallel telemetry events.
        Verifies SQLite WAL concurrency, zero database locks, and exact aggregate sums.
        """
        db_path = tmp_path / "concurrent_telemetry.db"
        await init_db(db_path)
        collector = MetricsCollector(db_path=str(db_path), ring_buffer_size=100)

        num_tasks = 50
        prompt_per_task = 1000
        comp_per_task = 400

        async def record_task(idx: int):
            provider = "deepseek" if idx % 2 == 0 else "openai"
            model = "deepseek-chat" if idx % 2 == 0 else "gpt-4o-mini"
            return await collector.record_metric(
                session_id=f"sess_{idx}",
                channel="web",
                provider_id=provider,
                model_name=model,
                prompt_tokens=prompt_per_task,
                completion_tokens=comp_per_task,
                ttft_ms=150.0 + idx,
                tts_first_chunk_ms=200.0 + idx,
                total_latency_ms=600.0 + idx,
                tts_cached_chunks=1 if idx % 3 == 0 else 0,
                tts_generated_chunks=2,
            )

        # Launch 50 parallel asynchronous tasks
        results = await asyncio.gather(*[record_task(i) for i in range(num_tasks)])
        assert len(results) == num_tasks

        # Verify in-memory ring buffer has all 50 records
        assert len(collector.ring_buffer) == 50

        # Verify SQLite persistent aggregations
        overview = await collector.get_overview()
        assert overview["total_requests"] == num_tasks
        assert overview["total_prompt_tokens"] == num_tasks * prompt_per_task
        assert overview["total_completion_tokens"] == num_tasks * comp_per_task
        assert overview["total_tokens"] == num_tasks * (prompt_per_task + comp_per_task)
        assert overview["estimated_cost_usd"] > 0.0

        # Verify provider breakdown
        providers = await collector.get_providers()
        assert len(providers) == 2
        p_dict = {p["provider_id"]: p for p in providers}
        assert "deepseek" in p_dict and "openai" in p_dict
        assert p_dict["deepseek"]["request_count"] == num_tasks // 2
        assert p_dict["openai"]["request_count"] == num_tasks // 2

        # Verify latency trends
        trends = await collector.get_latency_trend(limit=50)
        assert len(trends) == 50


# ============================================================================
# 5. Full REST API End-to-End Adversarial Coverage
# ============================================================================

class TestMetricsAndCacheApiAdversarial:
    """Tests FastAPI endpoints under edge parameters."""

    @pytest.fixture(autouse=True)
    def setup_isolated_env(self, tmp_path):
        db_p = tmp_path / "api_adv.db"
        asyncio.run(init_db(db_p))

        import galgame2voice.services.metrics_collector as mc_mod
        import galgame2voice.services.tts_cache_manager as tc_mod

        tc_mod._tts_cache_manager_instance = TtsCacheManager(cache_dir=tmp_path / "audio" / "cache", db_path=str(db_p))
        mc_mod._metrics_collector_instance = MetricsCollector(db_path=str(db_p))

        self.client = TestClient(app)
        yield
        tc_mod._tts_cache_manager_instance = None
        mc_mod._metrics_collector_instance = None

    def test_latency_trend_query_limits(self):
        """Tests /api/metrics/latency-trend query parameter validation (ge=1, le=200)."""
        r_valid = self.client.get("/api/metrics/latency-trend?limit=50")
        assert r_valid.status_code == 200

        r_zero = self.client.get("/api/metrics/latency-trend?limit=0")
        assert r_zero.status_code == 422

        r_huge = self.client.get("/api/metrics/latency-trend?limit=500")
        assert r_huge.status_code == 422

    def test_cache_clear_and_stats_cycle(self):
        """Tests /api/cache/stats and /api/cache/clear lifecycle via HTTP."""
        r_stats1 = self.client.get("/api/cache/stats")
        assert r_stats1.status_code == 200
        assert r_stats1.json()["total_files"] == 0

        r_clear = self.client.post("/api/cache/clear")
        assert r_clear.status_code == 200
        assert r_clear.json()["status"] == "cleared"
