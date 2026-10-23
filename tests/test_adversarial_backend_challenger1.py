"""
Adversarial challenge and stress-test suite (Challenger 1):
1. FFmpeg discovery fallback under stripped PATH, invalid binaries, and concurrent probes.
2. ASGI Cache-Control middleware under edge-case paths, traversal probes, error responses, uppercase extensions.
3. SQLite delete_message and immediate_transaction under concurrent bursts, savepoint rollbacks, and cancellation.
"""

import asyncio
import io
import struct
import wave
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, immediate_transaction, init_db
from galgame2voice.main import create_app
from galgame2voice.utils.audio_converter import (
    find_ffmpeg,
    is_ffmpeg_available,
    reset_ffmpeg_cache,
)


def _generate_synthetic_pcm_wav(
    sample_rate: int = 16000,
    channels: int = 1,
    sampwidth: int = 2,
    duration_s: float = 0.2,
) -> bytes:
    """Generates synthetic 16-bit mono PCM WAV bytes for audio tests."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        num_frames = int(sample_rate * duration_s)
        wf.writeframes(struct.pack(f"<{num_frames * channels}h", *([0] * (num_frames * channels))))
    return buf.getvalue()


# ============================================================================
# Challenge 1: FFmpeg Discovery Fallback & Concurrency
# ============================================================================


class TestAdversarialFFmpegDiscovery:
    """Adversarial stress-testing of find_ffmpeg discovery fallback."""

    @pytest.mark.requires_piped_subprocess
    def test_stripped_and_corrupted_path_variations(self, monkeypatch):
        """Stress-tests discovery under empty, unset, and non-existent PATH configurations."""
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)

        for dummy_path in ["", "/nonexistent/dir:/dev/null/notdir", None]:
            reset_ffmpeg_cache()
            if dummy_path is None:
                monkeypatch.delenv("PATH", raising=False)
            else:
                monkeypatch.setenv("PATH", dummy_path)

            discovered = find_ffmpeg()
            assert discovered is not None, f"Discovery failed under PATH={dummy_path}"
            assert Path(discovered).is_file()
            assert is_ffmpeg_available() is True

    @pytest.mark.requires_piped_subprocess
    def test_invalid_and_non_executable_custom_paths(self, tmp_path):
        """Probes find_ffmpeg with directories, non-existent files, and non-executables."""
        reset_ffmpeg_cache()

        # 1. Directory as custom_path
        sub_dir = tmp_path / "not_a_binary_dir"
        sub_dir.mkdir()
        assert find_ffmpeg(str(sub_dir)) is None

        # 2. Non-existent file
        assert find_ffmpeg(str(tmp_path / "ghost_ffmpeg")) is None

        # 3. Non-executable text file probe via custom_path
        script = tmp_path / "fake_ffmpeg.sh"
        script.write_text("#!/bin/sh\necho not ffmpeg")
        script.chmod(0o644)
        # shutil.which returns None for non-executable, falls back to str(p.resolve())
        res = find_ffmpeg(str(script))
        assert res == str(script.resolve())

    @pytest.mark.requires_piped_subprocess
    def test_broken_imageio_ffmpeg_graceful_degradation(self, monkeypatch):
        """Verifies find_ffmpeg gracefully returns None when imageio_ffmpeg fails unexpectedly."""
        reset_ffmpeg_cache()
        monkeypatch.setenv("PATH", "")
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)
        monkeypatch.setattr("galgame2voice.utils.audio_converter._find_ffmpeg_in_python_env", lambda *a: None)
        monkeypatch.setattr("galgame2voice.utils.audio_converter._find_ffmpeg_in_project_dirs", lambda *a: None)

        # imageio_ffmpeg raises generic Exception
        with patch("imageio_ffmpeg.get_ffmpeg_exe", side_effect=Exception("Simulated DLL missing")):
            assert find_ffmpeg() is None
            assert is_ffmpeg_available() is False

        # imageio_ffmpeg returns non-existent path
        reset_ffmpeg_cache()
        with patch("imageio_ffmpeg.get_ffmpeg_exe", return_value="/nonexistent/ffmpeg/bin"):
            assert find_ffmpeg() is None
            assert is_ffmpeg_available() is False

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_concurrent_discovery_probes_under_load(self):
        """Stress-tests 50 concurrent async tasks calling find_ffmpeg with periodic cache resets."""
        results = []
        errors = []

        async def worker(idx: int):
            try:
                if idx % 10 == 0:
                    reset_ffmpeg_cache()
                res = await asyncio.to_thread(find_ffmpeg)
                results.append(res)
            except Exception as exc:
                errors.append(exc)

        await asyncio.gather(*(worker(i) for i in range(50)))
        assert len(errors) == 0, f"Encountered errors during concurrent probes: {errors}"
        assert len(results) == 50
        assert all(r is not None for r in results)


# ============================================================================
# Challenge 2: Cache-Control Middleware Matrix & Traversal
# ============================================================================


class TestAdversarialCacheControlMiddleware:
    """Adversarial stress-testing of ASGI StaticCacheControlMiddleware."""

    @pytest.fixture(autouse=True)
    def setup_audio_files(self, tmp_path):
        """Prepares audio directories and sample files for cache probing."""
        settings = get_settings()
        self.audio_dir = settings.audio_dir
        self.cache_dir = self.audio_dir / "cache"
        self.fake_cache_dir = self.audio_dir / "cache_not_really"

        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.fake_cache_dir.mkdir(parents=True, exist_ok=True)

        sample_bytes = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00"

        self.valid_wav = self.cache_dir / "tts_sample_valid.wav"
        self.valid_wav.write_bytes(sample_bytes)

        self.valid_upper_wav = self.cache_dir / "tts_sample_upper.WAV"
        self.valid_upper_wav.write_bytes(sample_bytes)

        self.private_wav = self.audio_dir / "user_private_recording.wav"
        self.private_wav.write_bytes(sample_bytes)

        self.fake_cache_wav = self.fake_cache_dir / "misleading_cache.wav"
        self.fake_cache_wav.write_bytes(sample_bytes)

        self.client = TestClient(create_app())

    def test_immutable_headers_on_cache_audio_and_range_requests(self):
        """GET / HEAD / 206 Partial Content under /audio/cache/ receive immutable headers."""
        # 1. Full GET
        resp = self.client.get("/audio/cache/tts_sample_valid.wav")
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "public, max-age=31536000, immutable"

        # 2. HEAD request
        head_resp = self.client.head("/audio/cache/tts_sample_valid.wav")
        assert head_resp.status_code == 200
        assert head_resp.headers.get("Cache-Control") == "public, max-age=31536000, immutable"

        # 3. Valid Byte-Range (206)
        range_resp = self.client.get(
            "/audio/cache/tts_sample_valid.wav", headers={"Range": "bytes=0-3"}
        )
        assert range_resp.status_code == 206
        assert range_resp.headers.get("Cache-Control") == "public, max-age=31536000, immutable"
        assert "bytes 0-3/" in range_resp.headers.get("Content-Range", "")

    def test_error_statuses_never_cached_immutably(self):
        """404, 416, and 405 statuses must never receive immutable cache headers."""
        # 404 missing cache file
        resp_404 = self.client.get("/audio/cache/non_existent_file_404.wav")
        assert resp_404.status_code == 404
        assert resp_404.headers.get("Cache-Control") == "no-cache, no-store, must-revalidate"

        # 416 Unsatisfiable Range
        resp_416 = self.client.get(
            "/audio/cache/tts_sample_valid.wav", headers={"Range": "bytes=9999-9999"}
        )
        assert resp_416.status_code == 416
        assert resp_416.headers.get("Cache-Control") == "no-cache, no-store, must-revalidate"

        # 405 Method Not Allowed on POST
        resp_405 = self.client.post("/audio/cache/tts_sample_valid.wav")
        assert resp_405.status_code == 405
        assert "immutable" not in resp_405.headers.get("Cache-Control", "")

    def test_private_headers_on_non_cache_and_fake_cache_paths(self):
        """Non-cache audio and paths mimicking cache (cache_not_really) must receive private, max-age=0."""
        # Root audio file
        resp_priv = self.client.get("/audio/user_private_recording.wav")
        assert resp_priv.status_code == 200
        assert resp_priv.headers.get("Cache-Control") == "private, max-age=0"

        # Directory named cache_not_really
        resp_fake = self.client.get("/audio/cache_not_really/misleading_cache.wav")
        assert resp_fake.status_code == 200
        assert resp_fake.headers.get("Cache-Control") == "private, max-age=0"

    def test_uppercase_extensions_and_url_encoded_paths(self):
        """Uppercase extension (.WAV) and percent-encoded paths (%63ache) preserve immutable headers."""
        # Uppercase extension
        resp_upper = self.client.get("/audio/cache/tts_sample_upper.WAV")
        assert resp_upper.status_code == 200
        assert resp_upper.headers.get("Cache-Control") == "public, max-age=31536000, immutable"

        # Percent-encoded path
        resp_enc = self.client.get("/audio/%63ache/tts_sample_valid.wav")
        assert resp_enc.status_code == 200
        assert resp_enc.headers.get("Cache-Control") == "public, max-age=31536000, immutable"

    def test_directory_traversal_out_of_bounds(self):
        """Path traversal escaping audio directory returns 404 and never receives immutable cache."""
        # 1. Traversal escaping out of /audio/ entirely (/secret.txt)
        resp_esc = self.client.get("/audio/cache/../../secret.txt")
        assert resp_esc.status_code in (403, 404)
        assert resp_esc.headers.get("Cache-Control") != "public, max-age=31536000, immutable"

        # 2. Traversal within /audio/ (/audio/missing_traversal.wav)
        resp_inner = self.client.get("/audio/cache/../missing_traversal.wav")
        assert resp_inner.status_code == 404
        assert resp_inner.headers.get("Cache-Control") == "no-cache, no-store, must-revalidate"


# ============================================================================
# Challenge 3: SQLite delete_message & Transaction Savepoint Safety
# ============================================================================


class TestAdversarialDeleteMessageAndTransactions:
    """Adversarial stress-testing of crud.delete_message and immediate_transaction."""

    @pytest.mark.asyncio
    async def test_concurrent_burst_deleting_same_message(self, tmp_path):
        """Race condition test: 20 concurrent tasks attempt to delete the same message ID simultaneously."""
        db_path = str(tmp_path / "same_msg_race.db")
        await init_db(db_path)

        async with get_db(db_path) as conn:
            await crud.get_or_create_session(conn, "race_session")
            msg = await crud.add_message(
                conn,
                MessageCreate(session_id="race_session", role="user", content_chinese="race_target"),
            )
            msg_id = msg.id

        async def worker():
            async with get_db(db_path) as c:
                await crud.delete_message(c, msg_id)

        # Run 20 concurrent deletion requests on distinct connections
        await asyncio.gather(*(worker() for _ in range(20)))

        async with get_db(db_path) as conn:
            assert await crud.count_session_messages(conn, "race_session") == 0

    @pytest.mark.asyncio
    async def test_deeply_nested_savepoints_and_partial_rollback(self, tmp_path):
        """Deeply nested savepoints (8 levels deep): inner rollback restores only inner deletions."""
        db_path = str(tmp_path / "deep_nest.db")
        await init_db(db_path)

        async with get_db(db_path) as conn:
            await crud.get_or_create_session(conn, "sess_nest")
            created_ids = []
            for i in range(8):
                m = await crud.add_message(
                    conn,
                    MessageCreate(session_id="sess_nest", role="user", content_chinese=f"nest_msg_{i}"),
                )
                created_ids.append(m.id)

            async def recursive_step(level: int):
                if level == 8:
                    # Innermost level: delete msg 0 and 1, then simulate failure
                    await crud.delete_message(conn, created_ids[0])
                    await crud.delete_message(conn, created_ids[1])
                    raise RuntimeError("Innermost failure trigger")

                async with immediate_transaction(conn):
                    assert getattr(conn, "_imm_tx_depth", 0) == level + 1
                    await recursive_step(level + 1)

            async with immediate_transaction(conn):
                # Delete msg 7 at outer level
                await crud.delete_message(conn, created_ids[7])
                try:
                    await recursive_step(1)
                except RuntimeError:
                    pass  # Inner failure caught; outer transaction proceeds

            # Depth must be restored to 0
            assert getattr(conn, "_imm_tx_depth", 0) == 0

            # Verify msg 7 is deleted, but msg 0 and 1 were restored
            msgs = await crud.get_recent_messages(conn, "sess_nest", limit=20)
            remaining_ids = {m.id for m in msgs}
            assert created_ids[7] not in remaining_ids
            assert created_ids[0] in remaining_ids
            assert created_ids[1] in remaining_ids
            assert len(remaining_ids) == 7

    @pytest.mark.asyncio
    async def test_concurrent_connections_burst_load(self, tmp_path):
        """20 concurrent workers creating and deleting messages simultaneously under WAL contention."""
        db_path = str(tmp_path / "burst_load.db")
        await init_db(db_path)

        session_id = "sess_burst"

        async def worker_burst(worker_idx: int):
            async with get_db(db_path) as c:
                for i in range(8):
                    msg = await crud.add_message(
                        c,
                        MessageCreate(
                            session_id=session_id,
                            role="user",
                            content_chinese=f"w{worker_idx}_m{i}",
                        ),
                    )
                    # Every even message is deleted immediately
                    if i % 2 == 0:
                        await crud.delete_message(c, msg.id)

        await asyncio.gather(*(worker_burst(i) for i in range(20)))

        async with get_db(db_path) as conn:
            # 20 workers * 4 remaining messages = 80
            count = await crud.count_session_messages(conn, session_id)
            assert count == 80
            # Database integrity check
            cursor = await conn.execute("PRAGMA integrity_check;")
            row = await cursor.fetchone()
            assert row[0] == "ok"

    @pytest.mark.asyncio
    async def test_task_cancellation_during_delete_message(self, tmp_path):
        """Task cancellation while in transaction (both during BEGIN IMMEDIATE and mid-body) must restore transaction depth and locks cleanly."""
        db_path = str(tmp_path / "cancel_tx.db")
        await init_db(db_path)

        async with get_db(db_path) as conn:
            await crud.get_or_create_session(conn, "sess_cancel")
            msg = await crud.add_message(
                conn,
                MessageCreate(session_id="sess_cancel", role="user", content_chinese="cancel_me"),
            )

            # 1. Immediate cancellation while BEGIN IMMEDIATE is in-flight on the worker thread
            async def cancel_at_begin():
                async with immediate_transaction(conn):
                    await crud.delete_message(conn, msg.id)
                    await asyncio.sleep(5)

            t0 = asyncio.create_task(cancel_at_begin())
            await asyncio.sleep(0)
            t0.cancel()
            try:
                await t0
            except asyncio.CancelledError:
                pass

            assert getattr(conn, "_imm_tx_depth", 0) == 0
            assert getattr(conn, "_imm_tx_lock_owner", None) is None
            assert await crud.count_session_messages(conn, "sess_cancel") == 1

            # 2. Deterministic mid-transaction cancellation after nested savepoint delete
            entered_body = asyncio.Event()

            async def cancel_mid_body():
                async with immediate_transaction(conn):
                    await crud.delete_message(conn, msg.id)
                    entered_body.set()
                    await asyncio.sleep(5)  # Cancelled here

            t = asyncio.create_task(cancel_mid_body())
            await entered_body.wait()
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

            # Depth and owner must be restored, and deletion rolled back
            assert getattr(conn, "_imm_tx_depth", 0) == 0
            assert getattr(conn, "_imm_tx_lock_owner", None) is None
            assert await crud.count_session_messages(conn, "sess_cancel") == 1

            # Next transaction on the same connection must succeed normally
            await crud.delete_message(conn, msg.id)
            assert await crud.count_session_messages(conn, "sess_cancel") == 0

