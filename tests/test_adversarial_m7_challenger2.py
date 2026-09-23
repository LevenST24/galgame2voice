"""
Adversarial Challenge Test Suite M7 for Challenger R4-2.

Coverage & Objectives:
1. Frontend LRU & URL Revocation Stress:
   - Executes empirical Node.js test simulating 250 audio entries in BoundedAudioStore.
   - Asserts size <= 30 at every step and revokeObjectURL called 170+ times.
   - Validates static source AST / exports in frontend/src/voice.js.
2. VRAM Watermark Guard Boundary Testing:
   - Tests VoiceManager._check_vram_guard across edge values:
     0.44GB, 0.45GB, 0.46GB, None, (None, None), (8.0, None).
   - Tests dynamic memory recovery (0.44GB -> release_system_memory -> 0.52GB -> success).
   - Tests environment variable threshold override.
3. SQLite WAL High-Burst Concurrency:
   - Concurrently executes 36 simultaneous reader/writer tasks (18 writers, 18 readers).
   - Asserts zero 'database is locked' errors and exact record consistency.
   - Tests nested transaction rollback resilience under concurrent writes.
"""

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from typing import List, Tuple
from unittest.mock import MagicMock, patch

import pytest

from galgame2voice.database.session import get_db, immediate_transaction
from galgame2voice.services.voice_manager import (
    InsufficientMemoryError,
    VoiceManager,
    _MIN_FREE_VRAM_FLOOR_GB,
    _get_switch_min_free_vram_gb,
)
from tests.conftest import DATABASE_SCHEMA_SQL

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ============================================================================
# 1. Frontend LRU & URL Revocation Stress Testing
# ============================================================================

class TestR4FrontendLruRevocationChallenger:
    """Empirical verification of BoundedAudioStore size ceiling and URL revocation."""

    def test_bounded_audio_store_node_stress_harness(self):
        """
        Executes Node.js empirical test harness tests/test_m7_audio_lru_challenger2.js.
        Asserts 250 sequential insertions never exceed size 30, and revokeObjectURL
        is invoked 170+ times with FIFO eviction.
        """
        js_test_path = PROJECT_ROOT / "tests" / "test_m7_audio_lru_challenger2.js"
        assert js_test_path.exists(), f"Missing JS test file: {js_test_path}"

        result = subprocess.run(
            ["node", str(js_test_path)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"Node test failed (exit {result.returncode}):\n{result.stderr}\n{result.stdout}"
        assert "All BoundedAudioStore Tests PASSED Successfully" in result.stdout
        assert "asserted <= 30" in result.stdout

    def test_bounded_audio_store_source_invariants(self):
        """Validates source contracts in frontend/src/voice.js."""
        voice_js = PROJECT_ROOT / "frontend" / "src" / "voice.js"
        assert voice_js.exists(), "frontend/src/voice.js must exist"
        content = voice_js.read_text(encoding="utf-8")

        assert "export const MAX_AUDIO_STORE_ENTRIES = 30;" in content
        assert "export class BoundedAudioStore extends Map" in content
        assert "safeRevokeUrl" in content
        assert "URL.revokeObjectURL" in content
        assert "export const audioStore = new BoundedAudioStore();" in content


# ============================================================================
# 2. VRAM Watermark Guard Boundary Testing
# ============================================================================

class TestR4VramWatermarkGuardBoundaryChallenger:
    """Adversarial stress-testing of VoiceManager._check_vram_guard() edge boundaries."""

    @pytest.fixture
    def voice_manager(self):
        return VoiceManager(gpt_sovits_client_or_server="http://127.0.0.1:9880")

    def test_vram_guard_at_0_44gb_below_floor_raises_insufficient_memory(self, voice_manager):
        """Edge boundary 0.44GB (< 0.45GB floor): must call release_system_memory and raise."""
        with patch("galgame2voice.services.voice_manager.get_gpu_vram_status", return_value=(8.0, 0.44)):
            with patch("galgame2voice.services.voice_manager.release_system_memory") as mock_rel:
                with pytest.raises(InsufficientMemoryError) as exc_info:
                    voice_manager._check_vram_guard(min_free_vram_gb=0.45)

                assert "显卡可用显存不足" in str(exc_info.value)
                assert "0.44 GB < 0.45 GB" in str(exc_info.value)
                mock_rel.assert_called_once()

    def test_vram_guard_at_0_45gb_exact_boundary_passes(self, voice_manager):
        """Edge boundary 0.45GB (== 0.45GB floor): exactly at threshold, must pass cleanly."""
        with patch("galgame2voice.services.voice_manager.get_gpu_vram_status", return_value=(8.0, 0.45)):
            with patch("galgame2voice.services.voice_manager.release_system_memory") as mock_rel:
                # 0.45 < 0.45 is False -> clean pass without memory release
                voice_manager._check_vram_guard(min_free_vram_gb=0.45)
                mock_rel.assert_not_called()

    def test_vram_guard_at_0_46gb_above_floor_passes(self, voice_manager):
        """Edge boundary 0.46GB (> 0.45GB floor): comfortably above threshold, must pass cleanly."""
        with patch("galgame2voice.services.voice_manager.get_gpu_vram_status", return_value=(8.0, 0.46)):
            with patch("galgame2voice.services.voice_manager.release_system_memory") as mock_rel:
                voice_manager._check_vram_guard(min_free_vram_gb=0.45)
                mock_rel.assert_not_called()

    def test_vram_guard_none_parameter_falls_back_to_default_floor(self, voice_manager):
        """min_free_vram_gb=None: must use _get_switch_min_free_vram_gb() (default 0.45GB)."""
        # A: 0.40GB < default 0.45GB -> raises
        with patch("galgame2voice.services.voice_manager.get_gpu_vram_status", return_value=(8.0, 0.40)):
            with patch("galgame2voice.services.voice_manager.release_system_memory") as mock_rel:
                with pytest.raises(InsufficientMemoryError) as exc_info:
                    voice_manager._check_vram_guard(min_free_vram_gb=None)
                assert "0.40 GB < 0.45 GB" in str(exc_info.value)
                mock_rel.assert_called_once()

        # B: 0.50GB >= default 0.45GB -> passes
        with patch("galgame2voice.services.voice_manager.get_gpu_vram_status", return_value=(8.0, 0.50)):
            with patch("galgame2voice.services.voice_manager.release_system_memory") as mock_rel:
                voice_manager._check_vram_guard(min_free_vram_gb=None)
                mock_rel.assert_not_called()

    def test_vram_guard_none_tuple_cpu_ci_skips_cleanly(self, voice_manager):
        """get_gpu_vram_status returning (None, None): CPU or non-CUDA environment, skips cleanly."""
        with patch("galgame2voice.services.voice_manager.get_gpu_vram_status", return_value=(None, None)):
            with patch("galgame2voice.services.voice_manager.release_system_memory") as mock_rel:
                voice_manager._check_vram_guard(min_free_vram_gb=0.45)
                mock_rel.assert_not_called()

    def test_vram_guard_total_present_free_none_skips_cleanly(self, voice_manager):
        """get_gpu_vram_status returning (8.0, None): driver present but free VRAM unknown, skips."""
        with patch("galgame2voice.services.voice_manager.get_gpu_vram_status", return_value=(8.0, None)):
            with patch("galgame2voice.services.voice_manager.release_system_memory") as mock_rel:
                voice_manager._check_vram_guard(min_free_vram_gb=0.45)
                mock_rel.assert_not_called()

    def test_vram_guard_dynamic_memory_recovery(self, voice_manager):
        """
        Adversarial dynamic test:
        First check has 0.44GB (< 0.45GB).
        release_system_memory() successfully frees buffers.
        Second check returns 0.52GB (>= 0.45GB).
        VoiceManager must recover cleanly without raising InsufficientMemoryError.
        """
        vram_sequence = [(8.0, 0.44), (8.0, 0.52)]

        def dynamic_vram():
            if vram_sequence:
                return vram_sequence.pop(0)
            return (8.0, 0.52)

        with patch("galgame2voice.services.voice_manager.get_gpu_vram_status", side_effect=dynamic_vram):
            with patch("galgame2voice.services.voice_manager.release_system_memory") as mock_rel:
                # Should NOT raise!
                voice_manager._check_vram_guard(min_free_vram_gb=0.45)
                mock_rel.assert_called_once()

    def test_vram_guard_env_var_override(self, voice_manager, monkeypatch):
        """Verifies GALGAME2VOICE_MIN_FREE_VRAM_GB environment variable configuration."""
        monkeypatch.setenv("GALGAME2VOICE_MIN_FREE_VRAM_GB", "0.75")
        assert _get_switch_min_free_vram_gb() == 0.75

        # At 0.60GB (< 0.75GB threshold): must raise
        with patch("galgame2voice.services.voice_manager.get_gpu_vram_status", return_value=(8.0, 0.60)):
            with patch("galgame2voice.services.voice_manager.release_system_memory"):
                with pytest.raises(InsufficientMemoryError) as exc_info:
                    voice_manager._check_vram_guard(min_free_vram_gb=None)
                assert "0.60 GB < 0.75 GB" in str(exc_info.value)


# ============================================================================
# 3. SQLite WAL High-Burst Concurrency Testing
# ============================================================================

class TestR4SqliteWalConcurrencyChallenger:
    """Empirical concurrency stress test: 36 simultaneous tasks, zero lock conflicts."""

    @pytest.mark.asyncio
    async def test_sqlite_wal_36_tasks_high_burst_stress(self, temp_db_path):
        """
        Spawns 18 concurrent writer tasks and 18 concurrent reader tasks simultaneously (36 total).
        Each writer writes 10 rows under immediate_transaction.
        Each reader performs 10 queries (count + recent rows).
        Asserts:
        - 0 OperationalError ('database is locked')
        - 0 unhandled exceptions
        - Exact row count: 18 * 10 = 180 rows
        - 100% data integrity
        """
        num_writers = 18
        num_readers = 18
        writes_per_writer = 10
        expected_total = num_writers * writes_per_writer

        writer_errors: List[str] = []
        reader_errors: List[str] = []

        async def concurrent_writer(wid: int):
            try:
                for step in range(writes_per_writer):
                    async with get_db(temp_db_path) as conn:
                        async with immediate_transaction(conn):
                            await conn.execute(
                                "INSERT INTO session_messages (session_id, role, content_chinese, content_japanese) "
                                "VALUES (?, ?, ?, ?);",
                                (f"sess_w_{wid}", "user", f"cn_{wid}_{step}", f"jp_{wid}_{step}")
                            )
                    # Yield briefly to interleave with other coroutines
                    await asyncio.sleep(0.001)
            except Exception as e:
                writer_errors.append(f"Writer {wid}: {type(e).__name__} - {e}")

        async def concurrent_reader(rid: int):
            try:
                for _ in range(writes_per_writer):
                    async with get_db(temp_db_path) as conn:
                        c_row = await (await conn.execute("SELECT count(*) FROM session_messages;")).fetchone()
                        assert c_row is not None
                        assert c_row[0] >= 0

                        rows = await (await conn.execute(
                            "SELECT * FROM session_messages ORDER BY id DESC LIMIT 10;"
                        )).fetchall()
                        assert isinstance(rows, list)
                    await asyncio.sleep(0.001)
            except Exception as e:
                reader_errors.append(f"Reader {rid}: {type(e).__name__} - {e}")

        tasks = [concurrent_writer(i) for i in range(num_writers)] + [concurrent_reader(j) for j in range(num_readers)]
        await asyncio.gather(*tasks)

        assert not writer_errors, f"Encountered writer errors:\n{writer_errors}"
        assert not reader_errors, f"Encountered reader errors:\n{reader_errors}"

        # Final verification of total record count and data completeness
        async with get_db(temp_db_path) as conn:
            final_count = (await (await conn.execute("SELECT count(*) FROM session_messages;")).fetchone())[0]
            assert final_count == expected_total, f"Expected {expected_total} rows, got {final_count}"

            # Verify every writer has exactly writes_per_writer records
            for wid in range(num_writers):
                w_count = (await (await conn.execute(
                    "SELECT count(*) FROM session_messages WHERE session_id = ?;", (f"sess_w_{wid}",)
                )).fetchone())[0]
                assert w_count == writes_per_writer, f"Writer {wid} record count mismatch: {w_count}"

    @pytest.mark.asyncio
    async def test_sqlite_wal_nested_savepoint_rollback_under_concurrency(self, temp_db_path):
        """
        Adversarial test: tests nested immediate_transaction rollback under concurrent load.
        Ensures failed inner transactions roll back via savepoint without polluting outer transaction
        or locking the database for other concurrent tasks.
        """
        success_writer_done = asyncio.Event()

        async def failing_nested_writer():
            async with get_db(temp_db_path) as conn:
                async with immediate_transaction(conn):
                    await conn.execute(
                        "INSERT INTO session_messages (session_id, role, content_chinese, content_japanese) "
                        "VALUES (?, ?, ?, ?);",
                        ("sess_rollback", "system", "outer_committable", "outer_jp")
                    )
                    # Inner nested transaction that intentionally raises
                    try:
                        async with immediate_transaction(conn):
                            await conn.execute(
                                "INSERT INTO session_messages (session_id, role, content_chinese, content_japanese) "
                                "VALUES (?, ?, ?, ?);",
                                ("sess_rollback", "system", "inner_doomed", "inner_jp")
                            )
                            raise ValueError("Simulated nested failure")
                    except ValueError:
                        pass  # Handled: outer transaction should commit outer row, but inner row rolled back

        async def parallel_writer():
            async with get_db(temp_db_path) as conn:
                async with immediate_transaction(conn):
                    await conn.execute(
                        "INSERT INTO session_messages (session_id, role, content_chinese, content_japanese) "
                        "VALUES (?, ?, ?, ?);",
                        ("sess_parallel", "user", "parallel_ok", "parallel_jp")
                    )
            success_writer_done.set()

        await asyncio.gather(failing_nested_writer(), parallel_writer())

        async with get_db(temp_db_path) as conn:
            all_rows = await (await conn.execute("SELECT content_chinese FROM session_messages;")).fetchall()
            contents = [r[0] for r in all_rows]

            assert "parallel_ok" in contents, "Parallel writer must succeed"
            assert "outer_committable" in contents, "Outer transaction row must be committed"
            assert "inner_doomed" not in contents, "Inner aborted row must be rolled back via savepoint"
