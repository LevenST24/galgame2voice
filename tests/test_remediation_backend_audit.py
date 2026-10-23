"""
Targeted regression and adversarial test suites covering M18 backend defect remediations:
1. FFmpeg discovery fallback to bundled imageio-ffmpeg binary when PATH is stripped.
2. ASGI Cache-Control header preservation: immutable for /audio/cache/*.wav, private for /audio/*.wav.
3. Database transaction isolation and reentrant savepoint execution for crud.delete_message.
"""

import asyncio
import io
import struct
import wave
from pathlib import Path
from unittest.mock import patch

import imageio_ffmpeg
import pytest
from fastapi.testclient import TestClient

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db, immediate_transaction, init_db
from galgame2voice.main import create_app
from galgame2voice.database.models import MessageCreate
from galgame2voice.utils.audio_converter import (
    convert_ogg_to_wav,
    convert_wav_to_ogg,
    find_ffmpeg,
    is_ffmpeg_available,
    is_target_wav_pcm,
    reset_ffmpeg_cache,
)


def _generate_synthetic_pcm_wav(
    sample_rate: int = 16000,
    channels: int = 1,
    sampwidth: int = 2,
    duration_s: float = 0.2,
) -> bytes:
    """Generates synthetic 16-bit mono PCM WAV bytes for audio converter verification."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        num_frames = int(sample_rate * duration_s)
        wf.writeframes(struct.pack(f"<{num_frames * channels}h", *([0] * (num_frames * channels))))
    return buf.getvalue()


# ============================================================================
# 1. FFmpeg Discovery & Bundled imageio-ffmpeg Fallback
# ============================================================================


class TestFFmpegDiscoveryBundledFallback:
    """Verifies that find_ffmpeg discovers imageio-ffmpeg when PATH is stripped."""

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_path_stripped_discovers_bundled_imageio(self, monkeypatch):
        """When PATH is empty and no env override exists, find_ffmpeg must return imageio-ffmpeg."""
        reset_ffmpeg_cache()
        monkeypatch.setenv("PATH", "")
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)
        monkeypatch.setattr("galgame2voice.utils.audio_converter._find_ffmpeg_in_python_env", lambda *a: None)
        monkeypatch.setattr("galgame2voice.utils.audio_converter._find_ffmpeg_in_project_dirs", lambda *a: None)

        # Ensure imageio_ffmpeg binary path is valid on this machine
        expected_bin = imageio_ffmpeg.get_ffmpeg_exe()
        assert Path(expected_bin).is_file(), f"imageio_ffmpeg binary missing at {expected_bin}"

        discovered = find_ffmpeg()
        assert discovered is not None
        assert Path(discovered).is_file()
        assert Path(discovered).resolve() == Path(expected_bin).resolve()
        assert is_ffmpeg_available() is True

        # Second invocation must be served from cache
        cached = find_ffmpeg()
        assert cached == discovered

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_custom_path_override(self, tmp_path):
        """Explicit custom_path takes precedence when valid, and returns None if invalid."""
        reset_ffmpeg_cache()
        # Non-existent file
        assert find_ffmpeg(str(tmp_path / "non_existent_binary")) is None

        # Existing file probe
        fake_bin = tmp_path / "custom_ffmpeg"
        fake_bin.touch(mode=0o755)
        assert find_ffmpeg(str(fake_bin)) == str(fake_bin.resolve())

    @pytest.mark.requires_piped_subprocess
    def test_find_ffmpeg_missing_imageio_handles_cleanly(self, monkeypatch):
        """If imageio_ffmpeg raises an error or is missing, find_ffmpeg falls back gracefully to None."""
        reset_ffmpeg_cache()
        monkeypatch.setenv("PATH", "")
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)
        monkeypatch.setattr("galgame2voice.utils.audio_converter._find_ffmpeg_in_python_env", lambda *a: None)
        monkeypatch.setattr("galgame2voice.utils.audio_converter._find_ffmpeg_in_project_dirs", lambda *a: None)

        with patch("imageio_ffmpeg.get_ffmpeg_exe", side_effect=RuntimeError("Bundled binary corrupted")):
            assert find_ffmpeg() is None
            assert is_ffmpeg_available() is False

    def test_find_ffmpeg_in_python_env_skips_posix_system_prefixes(self, monkeypatch, tmp_path):
        """_find_ffmpeg_in_python_env ignores global /usr prefix on POSIX while discovering isolated venvs."""
        from galgame2voice.utils.audio_converter import _find_ffmpeg_in_python_env

        venv_dir = tmp_path / "isolated_venv"
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir(parents=True)
        fake_ffmpeg = bin_dir / "ffmpeg"
        fake_ffmpeg.write_text("#!/bin/sh\n")

        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr("sys.prefix", "/usr")
        monkeypatch.setattr("sys.base_prefix", "/usr")
        assert _find_ffmpeg_in_python_env("ffmpeg", "bin") is None

        monkeypatch.setattr("sys.prefix", str(venv_dir))
        assert _find_ffmpeg_in_python_env("ffmpeg", "bin") == str(fake_ffmpeg.resolve())

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_audio_converter_roundtrip_with_bundled_imageio(self, monkeypatch):
        """Verifies convert_wav_to_ogg and convert_ogg_to_wav roundtrip without global ffmpeg on PATH."""
        reset_ffmpeg_cache()
        monkeypatch.setenv("PATH", "")
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)

        sample_wav = _generate_synthetic_pcm_wav(sample_rate=16000, channels=1, duration_s=0.2)
        assert is_target_wav_pcm(sample_wav, sample_rate=16000, channels=1)

        # Transcode PCM WAV -> OGG Opus
        converted_ogg = await convert_wav_to_ogg(sample_wav)
        assert len(converted_ogg) > 0
        assert converted_ogg.startswith(b"OggS")

        # Transcode OGG Opus -> PCM WAV (16kHz mono 16-bit)
        roundtrip_wav = await convert_ogg_to_wav(converted_ogg, sample_rate=16000, channels=1)
        assert roundtrip_wav.startswith(b"RIFF")
        assert is_target_wav_pcm(roundtrip_wav, sample_rate=16000, channels=1)

    @pytest.mark.requires_piped_subprocess
    @pytest.mark.asyncio
    async def test_convert_ogg_to_wav_adversarial_corrupted_input(self, monkeypatch):
        """Adversarial corrupted payload must raise ValueError and clean up processes."""
        reset_ffmpeg_cache()
        monkeypatch.setenv("PATH", "")
        monkeypatch.delenv("FFMPEG_PATH", raising=False)
        monkeypatch.delenv("FFMPEG_BIN", raising=False)

        with pytest.raises(ValueError, match="Corrupted or unsupported|too short"):
            await convert_ogg_to_wav(b"\x00\x01\x02\x03CORRUPTED_NON_AUDIO_BYTES_PAYLOAD")


# ============================================================================
# 2. Audio Cache-Control Immutable Header Preservation
# ============================================================================


class TestAudioCacheControlHeaders:
    """Verifies ASGI StaticCacheControlMiddleware preserving immutable headers on /audio/cache/."""

    def test_asgi_cache_audio_immutable_header(self, tmp_path):
        """GET /audio/cache/*.wav must return HTTP 200 with Cache-Control: public, max-age=31536000, immutable."""
        settings = get_settings()
        cache_dir = settings.audio_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        test_wav = cache_dir / "tts_immutable_sample.wav"
        test_wav.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00")

        app = create_app()
        client = TestClient(app)

        resp = client.get("/audio/cache/tts_immutable_sample.wav")
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "public, max-age=31536000, immutable"
        assert resp.content.startswith(b"RIFF")

    def test_asgi_non_cache_audio_private_header(self):
        """GET /audio/*.wav (non-cache) must return HTTP 200 with Cache-Control: private, max-age=0."""
        settings = get_settings()
        settings.audio_dir.mkdir(parents=True, exist_ok=True)
        user_audio = settings.audio_dir / "user_mic_ephemeral.wav"
        user_audio.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00")

        app = create_app()
        client = TestClient(app)

        resp = client.get("/audio/user_mic_ephemeral.wav")
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "private, max-age=0"

    def test_asgi_cache_control_error_responses_never_immutable(self):
        """404 error responses under /audio/cache/ must never receive immutable cache headers."""
        app = create_app()
        client = TestClient(app)

        resp = client.get("/audio/cache/missing_audio_segment_404.wav")
        assert resp.status_code == 404
        assert resp.headers.get("Cache-Control") == "no-cache, no-store, must-revalidate"

    def test_asgi_cache_control_traversal_attempts_protected(self):
        """Path traversal payloads under /audio/cache/ must be blocked or not receive immutable headers."""
        app = create_app()
        client = TestClient(app)

        resp = client.get("/audio/cache/../../secret.txt")
        assert resp.status_code in (403, 404)
        assert resp.headers.get("Cache-Control") != "public, max-age=31536000, immutable"


# ============================================================================
# 3. Database Transaction Isolation & Savepoint Reentrancy
# ============================================================================


class TestDeleteMessageTransactionIsolation:
    """Verifies crud.delete_message transaction isolation and savepoint reentrancy."""

    @pytest.mark.asyncio
    async def test_delete_message_operates_inside_immediate_transaction(self, tmp_path):
        """crud.delete_message must execute and commit atomically via immediate_transaction."""
        db_path = str(tmp_path / "test_del_atomic.db")
        await init_db(db_path)

        async with get_db(db_path) as conn:
            # 1. Insert a session and message
            await crud.get_or_create_session(conn, "sess_del_1")
            msg = await crud.add_message(
                conn,
                MessageCreate(
                    session_id="sess_del_1",
                    role="user",
                    content_chinese="测试待删除消息",
                ),
            )
            assert await crud.count_session_messages(conn, "sess_del_1") == 1

            # 2. Delete message directly
            await crud.delete_message(conn, msg.id)

            # 3. Message should be permanently removed
            assert await crud.count_session_messages(conn, "sess_del_1") == 0

    @pytest.mark.asyncio
    async def test_delete_message_nested_reentrant_savepoint_execution(self, tmp_path):
        """crud.delete_message called inside an existing transaction must run via nested savepoints."""
        db_path = str(tmp_path / "test_del_reentrant.db")
        await init_db(db_path)

        async with get_db(db_path) as conn:
            await crud.get_or_create_session(conn, "sess_del_nested")
            msg1 = await crud.add_message(
                conn,
                MessageCreate(
                    session_id="sess_del_nested",
                    role="user",
                    content_chinese="消息一",
                ),
            )
            msg2 = await crud.add_message(
                conn,
                MessageCreate(
                    session_id="sess_del_nested",
                    role="assistant",
                    content_chinese="消息二",
                ),
            )
            assert await crud.count_session_messages(conn, "sess_del_nested") == 2

            # Outer immediate_transaction
            async with immediate_transaction(conn):
                assert getattr(conn, "_imm_tx_depth", 0) == 1
                # Nested delete_message call must not deadlock or raise 'cannot start transaction'
                await crud.delete_message(conn, msg1.id)
                # Depth must be restored cleanly to 1
                assert getattr(conn, "_imm_tx_depth", 0) == 1

            # Outside outer transaction, depth must be 0
            assert getattr(conn, "_imm_tx_depth", 0) == 0

            # Only msg2 remains
            remaining = await crud.count_session_messages(conn, "sess_del_nested")
            assert remaining == 1
            msgs = await crud.get_recent_messages(conn, "sess_del_nested", limit=10)
            assert msgs[0].id == msg2.id

    @pytest.mark.asyncio
    async def test_delete_message_outer_rollback_preserves_message(self, tmp_path):
        """When an outer transaction fails and rolls back, deletion inside savepoint is undone."""
        db_path = str(tmp_path / "test_del_rollback.db")
        await init_db(db_path)

        async with get_db(db_path) as conn:
            await crud.get_or_create_session(conn, "sess_rollback")
            msg = await crud.add_message(
                conn,
                MessageCreate(
                    session_id="sess_rollback",
                    role="user",
                    content_chinese="待保护消息",
                ),
            )
            assert await crud.count_session_messages(conn, "sess_rollback") == 1

            # Outer transaction with simulated failure after delete_message
            with pytest.raises(RuntimeError, match="Simulated crash during transaction"):
                async with immediate_transaction(conn):
                    await crud.delete_message(conn, msg.id)
                    raise RuntimeError("Simulated crash during transaction")

            # The deletion must have been rolled back
            assert await crud.count_session_messages(conn, "sess_rollback") == 1
            msgs = await crud.get_recent_messages(conn, "sess_rollback", limit=10)
            assert len(msgs) == 1
            assert msgs[0].id == msg.id

    @pytest.mark.asyncio
    async def test_delete_message_concurrent_write_stress(self, tmp_path):
        """Adversarial stress: concurrent writers adding and deleting messages via immediate_transaction."""
        db_path = str(tmp_path / "test_concurrent_del.db")
        await init_db(db_path)

        num_workers = 12
        session_id = "sess_stress"

        async def worker_task(worker_id: int):
            async with get_db(db_path) as conn:
                for i in range(5):
                    # Add message
                    msg = await crud.add_message(
                        conn,
                        MessageCreate(
                            session_id=session_id,
                            role="user",
                            content_chinese=f"worker_{worker_id}_msg_{i}",
                        ),
                    )
                    # Delete message
                    await crud.delete_message(conn, msg.id)
                    await asyncio.sleep(0.001)

        # Run concurrent workers
        await asyncio.gather(*(worker_task(w) for w in range(num_workers)))

        # Final verification: all messages were created and deleted cleanly without locks/corruption
        async with get_db(db_path) as conn:
            count = await crud.count_session_messages(conn, session_id)
            assert count == 0


# ============================================================================
# 4. SSRF URL Guard Hardening, Voice Profile Default Exclusivity & Proxy Env
# ============================================================================


class TestUrlGuardAndVoiceProfileHardening:
    """Verifies URL guard malformed port/IPv6/CGNAT handling, voice profile default exclusivity, and Telegram proxy env."""

    def test_url_guard_malformed_urls_and_cgnat_blocked(self):
        """validate_llm_base_url rejects malformed IPv6, invalid ports, CGNAT, and mapped IPv6 without raising ValueError."""
        from galgame2voice.security.url_guard import validate_llm_base_url

        for malformed in (
            "http://[::1",
            "http://example.com:99999/v1",
            "http://example.com:notaport/v1",
            "http://100.64.0.1:8000/v1",
            "http://100.127.255.254:8000/v1",
            "http://[::ffff:127.0.0.1]:8000/v1",
            "http://[::ffff:100.64.0.1]:8000/v1",
            "http://[fe80::1%25eth0]:8000/v1",
        ):
            ok, reason = validate_llm_base_url(malformed, allow_private=False)
            assert ok is False, f"Expected {malformed} to be rejected, but got ok=True"
            assert reason

        # Even with allow_private=True, syntactically invalid ports/URLs must be rejected
        for bad_syntax in ("http://[::1", "http://127.0.0.1:99999", "http://localhost:abc"):
            ok, _ = validate_llm_base_url(bad_syntax, allow_private=True)
            assert ok is False

    @pytest.mark.asyncio
    async def test_update_voice_profile_enforces_single_default(self, tmp_path):
        """Setting is_default=True via update_voice_profile must clear is_default on all other profiles."""
        from galgame2voice.database.models import VoiceProfileCreate, VoiceProfileUpdate

        db_path = str(tmp_path / "voice_default_excl.db")
        await init_db(db_path)

        async with get_db(db_path) as conn:
            p1 = await crud.create_voice_profile(
                conn,
                VoiceProfileCreate(
                    name="CharA",
                    gpt_weights_path="models/a.ckpt",
                    sovits_weights_path="models/a.pth",
                    is_default=True,
                ),
            )
            p2 = await crud.create_voice_profile(
                conn,
                VoiceProfileCreate(
                    name="CharB",
                    gpt_weights_path="models/b.ckpt",
                    sovits_weights_path="models/b.pth",
                    is_default=False,
                ),
            )
            assert p1.is_default is True
            assert p2.is_default is False

            updated_p2 = await crud.update_voice_profile(
                conn,
                p2.id,
                VoiceProfileUpdate(is_default=True),
            )
            assert updated_p2 is not None and updated_p2.is_default is True

            refreshed_p1 = await crud.get_voice_profile(conn, p1.id)
            assert refreshed_p1 is not None and refreshed_p1.is_default is False

            profiles = await crud.list_voice_profiles(conn)
            defaults = [p for p in profiles if p.is_default]
            assert len(defaults) == 1
            assert defaults[0].id == p2.id

    @pytest.mark.asyncio
    async def test_telegram_connectivity_survives_bracketed_ipv6_no_proxy(self, monkeypatch):
        """TelegramBotManager.test_token and test_proxy_connectivity survive NO_PROXY='[::1]'."""
        import httpx
        from galgame2voice.telegram_bot.bot import TelegramBotManager
        from galgame2voice.telegram_bot.proxy import test_proxy_connectivity

        monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,[::1]")

        class DummyResp:
            status_code = 200

            @staticmethod
            def json():
                return {"ok": True, "result": {"username": "galgame_test_bot"}}

        async def fake_get(self, url, **kwargs):
            return DummyResp()

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        mgr = TelegramBotManager()
        res = await mgr.test_token("123456789:ABCDEF1234567890abcdef")
        assert res["success"] is True
        assert "galgame_test_bot" in res["message"]

        ok = await test_proxy_connectivity(None)
        assert ok is True

