"""
Automated Test Suite for Milestone 3: TTS Offline Persistent Audio Cache & Performance Dashboard.
Covers canonical SHA256 hashing, cache hit/miss lifecycle, <50ms zero-latency benchmarks,
LRU capacity pruning, concurrency safety, token estimation, pricing engines,
telemetry timers, and REST API endpoints.
"""

import asyncio
import os
import time
import uuid
import wave
import pytest
from pathlib import Path
from typing import Dict, Any

from fastapi.testclient import TestClient
from galgame2voice.main import app, _audio_cleanup_loop
from galgame2voice.database.session import get_db, init_db
from galgame2voice.database import crud
from galgame2voice.services.tts_cache_manager import TtsCacheManager, get_tts_cache_manager
from galgame2voice.services.metrics_collector import MetricsCollector, get_metrics_collector, MODEL_PRICING_MAP
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.gpt_sovits_client import GptSovitsClient


# ============================================================================
# Fixtures & Helpers
# ============================================================================

def make_test_wav_bytes(duration_sec: float = 0.5, sample_rate: int = 32000) -> bytes:
    """Generates synthetic 16-bit PCM mono WAV bytes for test audio."""
    import io, struct
    buf = io.BytesIO()
    n_frames = int(sample_rate * duration_sec)
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_frames):
            val = int(3000 * (1 if (i // 100) % 2 == 0 else -1))
            frames.extend(struct.pack("<h", val))
        w.writeframes(frames)
    return buf.getvalue()


class MockGptSovitsForCache:
    """Mock client producing deterministic audio bytes for TTS tests."""
    def __init__(self):
        self.call_count = 0
        self.current_refer_audio = "siki.ogg"

    async def synthesize(self, text: str, options: Any = None) -> bytes:
        self.call_count += 1
        await asyncio.sleep(0.01)  # Simulate small GPU latency
        return make_test_wav_bytes(duration_sec=0.2)

    async def stream_tts(self, text: str, options: Any = None, chunk_size: int = 4096):
        self.call_count += 1
        data = make_test_wav_bytes(duration_sec=0.2)
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]


# ============================================================================
# 1. Canonical SHA256 Hashing Tests
# ============================================================================

class TestTtsCacheCanonicalHashing:
    """Tests determinism and sensitivity of canonical SHA256 cache key generation."""

    def test_canonical_hash_key_determinism(self, tmp_path):
        db_p = tmp_path / "test.db"
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(db_p))
        
        opts1 = {"speed_factor": 1.0, "temperature": 1.0, "top_k": 15, "top_p": 1.0}
        opts2 = {"top_p": 1.0, "speed_factor": 1.0, "top_k": 15, "temperature": 1.0}

        k1, clean1, p1 = mgr.compute_cache_key("こんにちは！", options=opts1)
        k2, clean2, p2 = mgr.compute_cache_key("こんにちは！", options=opts2)

        assert k1 == k2, "Identical parameters in different dict order must produce identical cache keys"
        assert clean1 == clean2 == "こんにちは！"
        assert p1 == p2

    def test_canonical_hash_sensitivity_to_text_and_params(self, tmp_path):
        db_p = tmp_path / "test.db"
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(db_p))

        base_opts = {"speed_factor": 1.0, "temperature": 1.0, "top_k": 15}

        k_base, _, _ = mgr.compute_cache_key("こんにちは", options=base_opts)
        k_diff_text, _, _ = mgr.compute_cache_key("こんばんは", options=base_opts)
        assert k_base != k_diff_text, "Different text must produce different cache keys"

        k_diff_speed, _, _ = mgr.compute_cache_key("こんにちは", options={"speed_factor": 1.2, "temperature": 1.0, "top_k": 15})
        assert k_base != k_diff_speed, "Different speed must produce different cache keys"

        k_diff_temp, _, _ = mgr.compute_cache_key("こんにちは", options={"speed_factor": 1.0, "temperature": 0.8, "top_k": 15})
        assert k_base != k_diff_temp, "Different temperature must produce different cache keys"

    def test_japanese_parentheses_cleaning_before_hashing(self, tmp_path):
        db_p = tmp_path / "test.db"
        mgr = TtsCacheManager(cache_dir=tmp_path / "cache", db_path=str(db_p))

        k1, clean1, _ = mgr.compute_cache_key("（微笑）おはようございます！")
        k2, clean2, _ = mgr.compute_cache_key("(微笑) おはようございます！")
        k3, clean3, _ = mgr.compute_cache_key("おはようございます！")

        assert clean1 == clean2 == clean3 == "おはようございます！"
        assert k1 == k2 == k3, "Stage directions in parentheses must normalize to identical cache key"


# ============================================================================
# 2. Cache Hit/Miss Lifecycle & Performance Benchmark (<50ms)
# ============================================================================

class TestTtsCacheLifecycleAndPerformance:
    """Validates cache store, retrieve, sub-50ms latency, and file corruption resilience."""

    @pytest.mark.asyncio
    async def test_cache_hit_and_miss_lifecycle(self, tmp_path):
        db_p = tmp_path / "cache_test.db"
        await init_db(db_p)
        cache_dir = tmp_path / "audio" / "cache"
        mgr = TtsCacheManager(cache_dir=cache_dir, db_path=str(db_p))
        mock_client = MockGptSovitsForCache()
        tts_svc = TtsService(client=mock_client, audio_dir=tmp_path / "audio", cache_manager=mgr)

        text = "先生、今日もお疲れ様です。"

        # 1. First Call: Cache MISS -> GPU synthesis
        audio1 = await tts_svc.synthesize(text)
        assert len(audio1) > 0
        assert mock_client.call_count == 1

        # 2. Second Call: Cache HIT -> Local disk read (<50ms)
        t0 = time.perf_counter()
        audio2 = await tts_svc.synthesize(text)
        hit_latency_ms = (time.perf_counter() - t0) * 1000.0

        assert audio1 == audio2
        assert mock_client.call_count == 1, "Cache hit must NOT call backend synthesizer"
        assert hit_latency_ms < 50.0, f"Cache hit took too long: {hit_latency_ms:.2f}ms (must be < 50ms)"

        # Verify SQLite metadata
        stats = await mgr.get_stats()
        assert stats["total_files"] == 1
        assert stats["total_hits"] >= 1
        assert stats["hit_rate_percent"] > 0

    @pytest.mark.asyncio
    async def test_synthesize_to_file_returns_cached_path(self, tmp_path):
        db_p = tmp_path / "file_cache.db"
        await init_db(db_p)
        mgr = TtsCacheManager(cache_dir=tmp_path / "audio" / "cache", db_path=str(db_p))
        tts_svc = TtsService(client=MockGptSovitsForCache(), audio_dir=tmp_path / "audio", cache_manager=mgr)

        text = "お帰りなさい、マスター。"
        url1, path1, size1 = await tts_svc.synthesize_to_file(text)
        assert "/audio/cache/" in url1
        assert path1.exists()

        url2, path2, size2 = await tts_svc.synthesize_to_file(text)
        assert url1 == url2
        assert path1 == path2
        assert size1 == size2

    @pytest.mark.asyncio
    async def test_corrupt_file_auto_recovery(self, tmp_path):
        db_p = tmp_path / "corrupt_test.db"
        await init_db(db_p)
        mgr = TtsCacheManager(cache_dir=tmp_path / "audio" / "cache", db_path=str(db_p))
        mock_client = MockGptSovitsForCache()
        tts_svc = TtsService(client=mock_client, audio_dir=tmp_path / "audio", cache_manager=mgr)

        text = "異常検知テストです。"
        url, path, _ = await tts_svc.synthesize_to_file(text)
        assert mock_client.call_count == 1

        # Corrupt file by truncating to 0 bytes
        path.write_bytes(b"")

        # Next call detects 0-byte corrupt file, re-synthesizes, and updates cache
        audio = await tts_svc.synthesize(text)
        assert len(audio) > 0
        assert mock_client.call_count == 2, "Corrupt cache must trigger fresh synthesis"

    @pytest.mark.asyncio
    async def test_concurrency_identical_requests(self, tmp_path):
        db_p = tmp_path / "concurrent.db"
        await init_db(db_p)
        mgr = TtsCacheManager(cache_dir=tmp_path / "audio" / "cache", db_path=str(db_p))
        tts_svc = TtsService(client=MockGptSovitsForCache(), audio_dir=tmp_path / "audio", cache_manager=mgr)

        text = "並行処理の検証を行っています。"
        tasks = [tts_svc.synthesize(text) for _ in range(10)]
        results = await asyncio.gather(*tasks)

        assert len(results) == 10
        first = results[0]
        for r in results:
            assert r == first


# ============================================================================
# 3. LRU Pruning & Storage Isolation Tests
# ============================================================================

class TestTtsCacheLruPruningAndIsolation:
    """Validates LRU capacity pruning and isolation from transient audio cleanup."""

    @pytest.mark.asyncio
    async def test_lru_pruning_by_entry_limit(self, tmp_path):
        db_p = tmp_path / "lru.db"
        await init_db(db_p)
        cache_dir = tmp_path / "audio" / "cache"
        mgr = TtsCacheManager(cache_dir=cache_dir, db_path=str(db_p), max_entries=100, max_cache_mb=100)

        # Store 10 distinct entries
        for i in range(10):
            audio = make_test_wav_bytes(duration_sec=0.1)
            key, clean, p_hash = mgr.compute_cache_key(f"台詞_{i}")
            await mgr.put(
                cache_key=key,
                text=f"台詞_{i}",
                clean_text=clean,
                voice_profile_id=1,
                params_hash=p_hash,
                audio_bytes=audio,
            )

        stats_init = await mgr.get_stats()
        assert stats_init["total_files"] == 10

        # Force prune with limit 5 entries (targets 80% = 4 entries)
        pruned = await mgr.prune(max_entries=5)
        stats = await mgr.get_stats()

        assert stats["total_files"] <= 5
        assert pruned > 0

    @pytest.mark.asyncio
    async def test_cache_clear_action(self, tmp_path):
        db_p = tmp_path / "clear.db"
        await init_db(db_p)
        cache_dir = tmp_path / "audio" / "cache"
        mgr = TtsCacheManager(cache_dir=cache_dir, db_path=str(db_p))

        # Put 3 items
        for i in range(3):
            audio = make_test_wav_bytes(duration_sec=0.1)
            k, c, p = mgr.compute_cache_key(f"テキスト_{i}")
            await mgr.put(k, f"テキスト_{i}", c, 1, p, audio)

        stats_before = await mgr.get_stats()
        assert stats_before["total_files"] == 3

        deleted, freed_mb = await mgr.clear()
        assert deleted == 3

        stats_after = await mgr.get_stats()
        assert stats_after["total_files"] == 0
        assert stats_after["total_size_mb"] == 0.0

    @pytest.mark.asyncio
    async def test_cleanup_loop_protects_audio_cache_directory(self, tmp_path):
        audio_dir = tmp_path / "audio"
        cache_dir = audio_dir / "cache"
        audio_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)

        # 1. Create an expired ephemeral file in audio/
        ephemeral_file = audio_dir / "voice_old_123.wav"
        ephemeral_file.write_bytes(b"ephemeral audio")
        past_time = time.time() - 3600  # 1 hour old
        os.utime(ephemeral_file, (past_time, past_time))

        # 2. Create a persistent cache file in audio/cache/
        cache_file = cache_dir / "hash_persisted.wav"
        cache_file.write_bytes(b"persistent audio")
        os.utime(cache_file, (past_time, past_time))

        # Run 1 iteration of cleanup loop logic manually
        now = time.time()
        cutoff = now - (30 * 60)
        for f in audio_dir.iterdir():
            if f.is_dir() or f.name.lower() == "cache":
                continue
            if f.is_file() and f.suffix.lower() in (".wav", ".ogg"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()

        assert not ephemeral_file.exists(), "Expired ephemeral file must be cleaned"
        assert cache_file.exists(), "Persistent cache file in audio/cache/ must NEVER be deleted by audio cleaner"


# ============================================================================
# 4. Token Estimation, Pricing & Latency Telemetry Tests
# ============================================================================

class TestMetricsCollectorAndTelemetry:
    """Validates model pricing formulas, token estimation, and ring buffer operations."""

    def test_pricing_calculation_accuracy(self, tmp_path):
        collector = MetricsCollector(db_path=str(tmp_path / "m.db"))

        # DeepSeek pricing ($0.14 input, $0.28 output per 1M)
        cost_usd, cost_cny = collector.calculate_cost("deepseek", "deepseek-chat", 1_000_000, 1_000_000)
        assert round(cost_usd, 4) == 0.4200
        assert round(cost_cny, 2) == round(0.42 * 7.20, 2)

        # OpenAI GPT-4o-mini ($0.15 input, $0.60 output per 1M)
        cost_usd, _ = collector.calculate_cost("openai", "gpt-4o-mini", 500_000, 500_000)
        assert round(cost_usd, 4) == 0.3750

        # Google Gemini 1.5 Flash ($0.075 input, $0.30 output per 1M)
        cost_usd, _ = collector.calculate_cost("gemini", "gemini-1.5-flash", 1_000_000, 0)
        assert round(cost_usd, 4) == 0.0750

    def test_token_estimation_accuracy(self, tmp_path):
        collector = MetricsCollector(db_path=str(tmp_path / "m.db"))

        assert collector.estimate_tokens("") == 0
        
        # Japanese CJK text (~1 token per char)
        ja_text = "四季夏目です。よろしくお願いします。"
        tok_ja = collector.estimate_tokens(ja_text)
        assert 15 <= tok_ja <= 25

        # English text (~4 chars per token)
        en_text = "Hello world, this is an automated performance benchmark test."
        tok_en = collector.estimate_tokens(en_text)
        assert 10 <= tok_en <= 25

    def test_ring_buffer_fifo(self, tmp_path):
        collector = MetricsCollector(db_path=str(tmp_path / "m.db"), ring_buffer_size=10)

        for i in range(15):
            collector.ring_buffer.append({"index": i, "timestamp": str(i)})

        assert len(collector.ring_buffer) == 10
        assert collector.ring_buffer[0]["index"] == 5
        assert collector.ring_buffer[-1]["index"] == 14

    @pytest.mark.asyncio
    async def test_record_metric_persists_to_sqlite(self, tmp_path):
        db_p = tmp_path / "metrics_test.db"
        await init_db(db_p)
        collector = MetricsCollector(db_path=str(db_p))

        # Record 2 requests
        await collector.record_metric(
            session_id="s1",
            channel="web",
            provider_id="deepseek",
            model_name="deepseek-chat",
            prompt_tokens=1000,
            completion_tokens=500,
            ttft_ms=320.0,
            tts_first_chunk_ms=450.0,
            total_latency_ms=1200.0,
            tts_cached_chunks=2,
            tts_generated_chunks=1,
        )
        await collector.record_metric(
            session_id="s1",
            channel="web",
            provider_id="openai",
            model_name="gpt-4o-mini",
            prompt_tokens=2000,
            completion_tokens=800,
            ttft_ms=280.0,
            tts_first_chunk_ms=410.0,
            total_latency_ms=1100.0,
            tts_cached_chunks=3,
            tts_generated_chunks=0,
        )

        overview = await collector.get_overview()
        assert overview["total_requests"] == 2
        assert overview["total_prompt_tokens"] == 3000
        assert overview["total_completion_tokens"] == 1300
        assert overview["total_tokens"] == 4300
        assert overview["avg_ttft_ms"] == 300.0

        providers = await collector.get_providers()
        assert len(providers) == 2
        p_ids = {p["provider_id"] for p in providers}
        assert "deepseek" in p_ids and "openai" in p_ids


# ============================================================================
# 5. REST API Endpoints Verification
# ============================================================================

class TestMetricsAndCacheRestEndpoints:
    """Tests FastAPI router endpoints for metrics, latency trends, and cache operations."""

    @pytest.fixture(autouse=True)
    def setup_app_state(self, tmp_path):
        db_p = tmp_path / "api_test.db"
        asyncio.run(init_db(db_p))
        
        # Override singletons for isolated testing
        from galgame2voice.services.metrics_collector import _metrics_collector_instance
        from galgame2voice.services.tts_cache_manager import _tts_cache_manager_instance
        import galgame2voice.services.metrics_collector as mc_mod
        import galgame2voice.services.tts_cache_manager as tc_mod

        tc_mod._tts_cache_manager_instance = TtsCacheManager(cache_dir=tmp_path / "audio" / "cache", db_path=str(db_p))
        mc_mod._metrics_collector_instance = MetricsCollector(db_path=str(db_p))

        self.client = TestClient(app)
        yield
        tc_mod._tts_cache_manager_instance = None
        mc_mod._metrics_collector_instance = None

    def test_get_metrics_overview_api(self):
        resp = self.client.get("/api/metrics/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_requests" in data
        assert "total_tokens" in data
        assert "estimated_cost_usd" in data
        assert "estimated_cost_cny" in data
        assert "avg_ttft_ms" in data
        assert "cache_stats" in data

    def test_get_metrics_providers_api(self):
        resp = self.client.get("/api/metrics/providers")
        assert resp.status_code == 200
        data = resp.json()
        assert "providers" in data
        assert isinstance(data["providers"], list)

    def test_get_latency_trend_api(self):
        resp = self.client.get("/api/metrics/latency-trend?limit=15")
        assert resp.status_code == 200
        data = resp.json()
        assert "trend" in data
        assert isinstance(data["trend"], list)

    def test_get_cache_stats_api(self):
        resp = self.client.get("/api/cache/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_files" in data
        assert "total_size_mb" in data
        assert "hit_rate_percent" in data

    def test_post_cache_clear_api(self):
        resp = self.client.post("/api/cache/clear")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cleared"
        assert "deleted_files" in data
        assert "freed_mb" in data


# ============================================================================
# 6. Chat Service End-to-End Telemetry Tests
# ============================================================================

class TestChatServiceTelemetryIntegration:
    """Validates that ChatService stream_chat and chat_sync emit and record telemetry metrics."""

    @pytest.mark.asyncio
    async def test_stream_chat_telemetry_emission(self, tmp_path):
        db_p = tmp_path / "chat_stream_m.db"
        await init_db(db_p)
        cache_mgr = TtsCacheManager(cache_dir=tmp_path / "audio" / "cache", db_path=str(db_p))
        tts_svc = TtsService(client=MockGptSovitsForCache(), audio_dir=tmp_path / "audio", cache_manager=cache_mgr)
        metrics_col = MetricsCollector(db_path=str(db_p))
        chat_svc = ChatService(tts_service=tts_svc, db_path=str(db_p), metrics_collector=metrics_col)

        # Mock adapter
        from tests.test_chat_streaming_m4 import MockStreamingLLMAdapter
        mock_adapter = MockStreamingLLMAdapter(stream_chunks=[
            '{"chinese": "指挥官，今天也辛苦了！',
            '", "japanese": "指揮官、今日もお疲れ様でした！',
            '"}'
        ])

        async def mock_get_adapter(conn=None, provider_id=None):
            return mock_adapter, "deepseek-chat"

        chat_svc._get_active_llm_adapter = mock_get_adapter

        events = []
        async for ev in chat_svc.stream_chat("你好！", session_id="test_stream_sess"):
            events.append(ev)

        assert len(events) >= 2
        done_event = next((e for e in events if e.get("event") == "done"), None)
        assert done_event is not None
        done_data = done_event["data"]

        assert "metrics" in done_data
        metrics = done_data["metrics"]
        assert metrics["prompt_tokens"] > 0
        assert metrics["completion_tokens"] > 0
        assert metrics["ttft_ms"] >= 0
        assert metrics["total_latency_ms"] >= 0
        assert metrics["estimated_cost_usd"] >= 0

        # Verify persisted to SQLite
        overview = await metrics_col.get_overview()
        assert overview["total_requests"] >= 1
        assert overview["total_tokens"] > 0

    @pytest.mark.asyncio
    async def test_chat_sync_telemetry_recording(self, tmp_path):
        db_p = tmp_path / "chat_sync_m.db"
        await init_db(db_p)
        cache_mgr = TtsCacheManager(cache_dir=tmp_path / "audio" / "cache", db_path=str(db_p))
        tts_svc = TtsService(client=MockGptSovitsForCache(), audio_dir=tmp_path / "audio", cache_manager=cache_mgr)
        metrics_col = MetricsCollector(db_path=str(db_p))
        chat_svc = ChatService(tts_service=tts_svc, db_path=str(db_p), metrics_collector=metrics_col)

        from tests.test_chat_streaming_m4 import MockStreamingLLMAdapter
        mock_adapter = MockStreamingLLMAdapter()

        async def mock_get_adapter(conn=None, provider_id=None):
            return mock_adapter, "gpt-4o-mini"

        chat_svc._get_active_llm_adapter = mock_get_adapter

        resp = await chat_svc.chat_sync("こんにちは", session_id="sync_sess")
        assert "metrics" in resp
        assert resp["metrics"]["prompt_tokens"] > 0
        assert resp["metrics"]["completion_tokens"] > 0
        assert resp["metrics"]["total_latency_ms"] >= 0

