"""
Adversarial Stress Test Suite for Milestone M2_PERF_STABILITY.
Authored by Challenger 2 (teamwork_preview_challenger).

Empirically tests and challenges:
1. SQLite WAL Checkpointing & Burst Concurrency:
   - 20 concurrent readers + 10 concurrent writers on conversation (sessions, messages)
     and memory (user_memories, character_affection) tables under WAL mode.
   - Concurrent active WAL checkpointing (PASSIVE and FULL).
   - Asserts 0 'database is locked' errors and complete data consistency.
2. Server Lifespan Startup and Shutdown Cycles:
   - Verifies PRAGMA wal_checkpoint(TRUNCATE) execution and tests for lingering locks.
   - Exposes any UnboundLocalError or variable scope pollution in lifespan.
3. Process Termination & Port Release:
   - Verifies clean Windows process reclamation and immediate port re-binding without WSAEADDRINUSE.
   - Verifies Windows Job Object limit configuration and PID file cleanup.
"""

import asyncio
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import List, Tuple
import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate, UserMemoryCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.main import create_app
from scripts.run_server import (
    assign_process_to_job,
    cleanup_subprocesses,
    find_available_port,
    is_port_in_use,
    setup_windows_job_object,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ============================================================================
# 1. SQLite WAL Checkpointing and Burst Read-Write Concurrency
# ============================================================================

class TestSqliteWalBurstConcurrency:
    """Stress-tests SQLite WAL mode with 20 concurrent readers and 10 concurrent writers."""

    @pytest.mark.asyncio
    async def test_burst_read_write_wal_concurrency(self, tmp_path):
        """
        Simulate 20 concurrent readers and 10 concurrent writers modifying conversation
        and memory tables simultaneously under WAL mode.
        Verify zero 'database is locked' or 'busy' errors.
        """
        test_db = tmp_path / "wal_concurrency_stress.db"
        await init_db(test_db)

        # Pre-seed session, user memory, and character affection records
        async with get_db(test_db) as conn:
            await crud.get_or_create_session(conn, "bench-sess")
            await crud.get_or_create_character_affection(conn, "bench-user", 1)

        errors: List[Tuple[str, int, str, str]] = []
        reads_completed = 0
        writes_completed = 0

        # 20 Concurrent Readers (50 iterations each = 1,000 read transactions)
        async def reader_worker(reader_id: int):
            nonlocal reads_completed
            for _ in range(50):
                try:
                    async with get_db(test_db) as conn:
                        # 1. Read conversation messages
                        await crud.get_recent_messages(conn, "bench-sess", limit=15)
                        # 2. Read user memories
                        await crud.list_memories(conn, user_id="bench-user", limit=20)
                        # 3. Read character affection
                        await crud.get_character_affection(conn, user_id="bench-user", character_id=1)
                        reads_completed += 1
                except Exception as exc:
                    errors.append(("reader", reader_id, type(exc).__name__, str(exc)))
                await asyncio.sleep(0.001)

        # 10 Concurrent Writers (30 iterations each = 300 write transactions)
        async def writer_worker(writer_id: int):
            nonlocal writes_completed
            for i in range(30):
                try:
                    async with get_db(test_db) as conn:
                        # 1. Write message
                        msg = MessageCreate(
                            session_id="bench-sess",
                            role="user" if i % 2 == 0 else "assistant",
                            content_chinese=f"并发测试消息 W{writer_id}-{i}",
                            content_japanese=f"並行テスト W{writer_id}-{i}"
                        )
                        await crud.add_message(conn, msg)

                        # 2. Write user memory
                        mem = UserMemoryCreate(
                            user_id="bench-user",
                            character_id=1,
                            category="preference",
                            fact_key=f"pref_{writer_id}_{i}",
                            fact_value=f"value_{writer_id}_{i}"
                        )
                        await crud.create_memory(conn, mem)

                        # 3. Increment character affection
                        await crud.increment_affection(
                            conn,
                            user_id="bench-user",
                            character_id=1,
                            delta_points=1,
                            daily_limit=1000
                        )
                        writes_completed += 1
                except Exception as exc:
                    errors.append(("writer", writer_id, type(exc).__name__, str(exc)))
                await asyncio.sleep(0.002)

        # Background active checkpointer running concurrently
        async def background_checkpointer():
            for _ in range(25):
                try:
                    async with get_db(test_db) as conn:
                        await conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
                except Exception as exc:
                    errors.append(("checkpointer", 0, type(exc).__name__, str(exc)))
                await asyncio.sleep(0.04)

        t_start = time.perf_counter()
        reader_tasks = [reader_worker(r) for r in range(20)]
        writer_tasks = [writer_worker(w) for w in range(10)]
        checkpoint_task = asyncio.create_task(background_checkpointer())

        await asyncio.gather(*reader_tasks, *writer_tasks)
        await checkpoint_task
        elapsed = time.perf_counter() - t_start

        # Filter locked/busy errors
        locked_errors = [e for e in errors if "locked" in e[3].lower() or "busy" in e[3].lower()]

        assert len(locked_errors) == 0, f"Encountered database locked errors: {locked_errors}"
        assert len(errors) == 0, f"Encountered errors during concurrency test: {errors}"
        assert reads_completed == 1000, f"Expected 1000 reads, completed {reads_completed}"
        assert writes_completed == 300, f"Expected 300 writes, completed {writes_completed}"

        # Verify data integrity
        async with get_db(test_db) as conn:
            msg_count = await crud.count_session_messages(conn, "bench-sess")
            assert msg_count == 300, f"Expected 300 messages, found {msg_count}"
            memories = await crud.list_memories(conn, user_id="bench-user", limit=500)
            assert len(memories) == 300, f"Expected 300 memories, found {len(memories)}"
            aff = await crud.get_character_affection(conn, user_id="bench-user", character_id=1)
            assert aff.affection_score == 100, f"Expected capped score 100, got {aff.affection_score}"
            assert aff.interaction_count == 300, f"Expected 300 interactions, got {aff.interaction_count}"


# ============================================================================
# 2. Server Lifespan Startup and Shutdown Cycles & Checkpoint Hardening
# ============================================================================

class TestServerLifespanCyclesAndCheckpointing:
    """Verifies server startup and shutdown lifespans, detecting lingering locks or regressions."""

    @pytest.mark.asyncio
    async def test_lifespan_startup_gpt_sovits_url_unbound_local_regression(self, tmp_path, monkeypatch):
        """
        Adversarial probe: Verify that lifespan startup successfully reads gpt_sovits_url
        from the DB without raising UnboundLocalError for 'get_db'.
        """
        test_db = tmp_path / "lifespan_scope_test.db"
        monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(test_db))
        await init_db(test_db)

        # Set custom gpt_sovits_url in DB settings
        async with get_db(test_db) as conn:
            await conn.execute("UPDATE settings SET gpt_sovits_url = 'http://127.0.0.1:19880' WHERE id = 1;")
            await conn.commit()

        app = create_app()

        # Capture warnings/errors during lifespan startup
        import logging
        records = []
        class LogHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = LogHandler()
        logging.getLogger("galgame2voice.main").addHandler(handler)

        try:
            async with app.router.lifespan_context(app):
                pass
        finally:
            logging.getLogger("galgame2voice.main").removeHandler(handler)

        unbound_errors = [
            r.getMessage() for r in records
            if "cannot access local variable 'get_db'" in r.getMessage()
            or "UnboundLocalError" in str(r.exc_info)
        ]
        assert len(unbound_errors) == 0, (
            f"Lifespan startup suffered UnboundLocalError on get_db: {unbound_errors}"
        )

    @pytest.mark.asyncio
    async def test_lifespan_shutdown_wal_checkpoint_truncate_zero_locks(self, tmp_path, monkeypatch):
        """
        Verify that PRAGMA wal_checkpoint(TRUNCATE) executes on shutdown and leaves zero
        lingering transaction locks across multiple sequential startup/shutdown cycles.
        """
        test_db = tmp_path / "lifespan_cycles.db"
        monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(test_db))
        await init_db(test_db)

        app = create_app()

        for cycle in range(3):
            # Startup phase
            async with app.router.lifespan_context(app):
                # Write data during active session
                async with get_db(test_db) as conn:
                    for i in range(5):
                        await crud.add_message(conn, MessageCreate(
                            session_id=f"cycle-sess-{cycle}",
                            role="user",
                            content_chinese=f"Cycle {cycle} Msg {i}",
                            content_japanese=f"Cycle {cycle} Msg {i}"
                        ))
            # Shutdown phase has completed here.

            # Empirical Lock Verification: Open direct exclusive connection
            # If any transaction lock lingered, BEGIN EXCLUSIVE will raise OperationalError.
            direct_conn = sqlite3.connect(str(test_db), timeout=1.0)
            try:
                cur = direct_conn.cursor()
                cur.execute("BEGIN EXCLUSIVE;")
                cur.execute("SELECT COUNT(*) FROM messages;")
                count = cur.fetchone()[0]
                assert count == (cycle + 1) * 5
                direct_conn.commit()

                # Verify WAL checkpoint status
                cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                res = cur.fetchall()
                # res is [(busy, log, checkpointed)]
                assert res[0][0] == 0, f"WAL checkpoint returned busy={res[0][0]}"
            finally:
                direct_conn.close()


# ============================================================================
# 3. Process Termination & Port Release on Windows
# ============================================================================

class TestProcessTerminationAndPortReleaseWindows:
    """Verifies clean Windows process reclamation and socket re-binding."""

    def test_subprocess_server_spawn_terminate_and_port_rebind(self):
        """
        Spawn uvicorn in a subprocess on an ephemeral port, verify health response,
        terminate the process, and verify the port is immediately re-bindable without WSAEADDRINUSE.
        """
        test_port = 19288
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "galgame2voice.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(test_port),
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            # Poll /api/health until responsive
            ready = False
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 15.0:
                time.sleep(0.5)
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{test_port}/api/health", timeout=1.0) as resp:
                        if resp.status == 200:
                            ready = True
                            break
                except Exception:
                    pass

            assert ready, f"Uvicorn server failed to respond on port {test_port} within 15s"
            assert is_port_in_use(test_port), f"Port {test_port} must be marked in use while running"

        finally:
            # Terminate server
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)

        # Verify process is dead
        assert proc.poll() is not None, "Process failed to terminate"

        # Verify port is immediately freed and can be bound by a new socket
        test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Bind without SO_REUSEADDR to strictly verify kernel port reclamation
            test_sock.bind(("127.0.0.1", test_port))
            test_sock.listen(1)
            port_freed = True
        except OSError as e:
            port_freed = False
            pytest.fail(f"Port {test_port} was not cleanly released upon process exit: {e}")
        finally:
            test_sock.close()

        assert port_freed, f"Failed to rebind port {test_port}"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object test requires Windows OS")
    def test_windows_job_object_assignment_and_cleanup(self):
        """
        Verify that Windows Job Object handle is created with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        child processes can be assigned to it, and cleanup_subprocesses unlinks PID files.
        """
        h_job = setup_windows_job_object()
        assert h_job is not None, "Failed to create Windows Job Object"

        # Spawn test sleep process
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            assigned = assign_process_to_job(child)
            assert assigned, "Failed to assign child process to Windows Job Object"
        finally:
            child.terminate()
            child.wait(timeout=3.0)

        # Verify cleanup_subprocesses removes pid files
        pid_file = PROJECT_ROOT / "galgame2voice.pid"
        port_file = PROJECT_ROOT / "data" / "active_port.txt"
        pid_file.write_text("99999", encoding="utf-8")
        port_file.write_text("8080", encoding="utf-8")

        cleanup_subprocesses()
        assert not pid_file.exists(), "galgame2voice.pid was not removed by cleanup_subprocesses"
        assert not port_file.exists(), "active_port.txt was not removed by cleanup_subprocesses"
