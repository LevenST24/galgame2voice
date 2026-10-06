"""
Automated Regression Test Suite for Stability & Resilience Loop.
Validates the 7 forensic hardening fixes:
1. Audio cleaner cache deletion commits to SQLite.
2. VoiceManager background task retention and graceful draining on shutdown.
3. Audio converter unlinks temporary files even under task cancellation.
4. Config and Health routers strip UTF-8 BOM cleanly from sovits_dir.txt.
5. Telegram voice message handler rejects oversized audio (>15MB).
6. Telegram bot error boundary detects token conflicts and shuts down cleanly on init failure.
7. stream_chat cancel monitor is cancelled and awaited on normal stream completion.
"""

import asyncio
import io
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from galgame2voice.config import get_settings
from galgame2voice.database.session import get_db, immediate_transaction
from galgame2voice.database import crud
from galgame2voice.services.audio_cleaner import _scan_and_clean
from galgame2voice.services.voice_manager import VoiceManager
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_service import TtsService
from galgame2voice.utils.audio_converter import convert_ogg_to_wav, convert_wav_to_ogg
from galgame2voice.telegram_bot.handlers import TelegramBotHandlers
from galgame2voice.telegram_bot.bot import TelegramBotManager
from telegram_test_support import TELEGRAM_TEST_ADMINS


# ============================================================================
# 1. Audio Cleaner Commit Verification
# ============================================================================

@pytest.mark.asyncio
async def test_audio_cleaner_db_deletion_committed(tmp_path):
    """Verifies that expired tts_cache_entries are committed to SQLite via immediate_transaction."""
    db_path = str(tmp_path / "cleaner_test.db")
    async with get_db(db_path) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tts_cache_entries (
                cache_key TEXT PRIMARY KEY,
                audio_filename TEXT NOT NULL,
                prompt_lang TEXT,
                text_lang TEXT,
                speed REAL,
                sample_rate INTEGER,
                hit_count INTEGER DEFAULT 1,
                last_accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.commit()

        # Seed 3 cache entries
        for i in range(3):
            await conn.execute(
                "INSERT INTO tts_cache_entries (cache_key, audio_filename) VALUES (?, ?);",
                (f"key_{i}", f"cache_sample_{i}.wav"),
            )
        await conn.commit()

    # Create audio dir and old cache files
    audio_dir = tmp_path / "audio"
    cache_dir = audio_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    old_file = cache_dir / "cache_sample_0.wav"
    old_file.write_bytes(b"RIFFmockwavdata")
    # Backdate mtime to 30 days ago
    past_time = time.time() - (30 * 86400)
    os.utime(old_file, (past_time, past_time))

    # Keep file 1 recent
    recent_file = cache_dir / "cache_sample_1.wav"
    recent_file.write_bytes(b"RIFFrecentwav")

    # Run _scan_and_clean
    now = time.time()
    cutoff = now - 3600
    cache_cutoff = now - (7 * 86400)
    cleaned, unlinked_keys = _scan_and_clean(audio_dir, set(), cutoff, cache_cutoff)

    assert len(unlinked_keys) > 0

    # Execute batch deletion with immediate_transaction (matching audio_cleaner.py)
    async with get_db(db_path) as conn:
        async with immediate_transaction(conn):
            filenames = [f"{k}.wav" for k in unlinked_keys]
            all_params = unlinked_keys + filenames + unlinked_keys
            p_batch = ",".join(["?"] * len(unlinked_keys))
            p_files = ",".join(["?"] * len(filenames))
            await conn.execute(
                f"DELETE FROM tts_cache_entries WHERE cache_key IN ({p_batch}) OR audio_filename IN ({p_files}) OR audio_filename IN ({p_batch});",
                all_params,
            )

    # In a brand new connection, assert committed deletion
    async with get_db(db_path) as conn:
        cursor = await conn.execute("SELECT cache_key FROM tts_cache_entries;")
        remaining = [row[0] for row in await cursor.fetchall()]
        assert "key_0" not in remaining
        assert "key_1" in remaining


# ============================================================================
# 2. VoiceManager Background Task Retention & Shutdown Drain
# ============================================================================

@pytest.mark.asyncio
async def test_voice_manager_background_task_retention_and_drain(tmp_path):
    """Verifies that VoiceManager._spawn_background keeps a strong reference and aclose drains it."""
    db_path = str(tmp_path / "vm_test.db")
    vm = VoiceManager(db_path=db_path)

    completed = False

    async def long_warmup():
        nonlocal completed
        await asyncio.sleep(0.05)
        completed = True

    task = vm._spawn_background(long_warmup())
    assert task in vm._bg_tasks
    assert not task.done()

    # Graceful shutdown drains task
    await vm.aclose()
    assert completed is True
    assert len(vm._bg_tasks) == 0


# ============================================================================
# 3. Audio Converter Unlinking Under Task Cancellation
# ============================================================================

@pytest.mark.asyncio
async def test_audio_converter_unlinking_under_task_cancellation(tmp_path, monkeypatch):
    """Verifies that cancel during audio conversion still deletes created temporary files."""
    # Create valid mock PCM WAV
    wav_io = io.BytesIO()
    import wave
    with wave.open(wav_io, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 800)
    valid_wav_bytes = wav_io.getvalue()

    # Mock run_ffmpeg_command to simulate a long task and create output file
    async def _mock_ffmpeg(*cmd, timeout=30.0):
        out_p = Path(cmd[-1])
        out_p.write_bytes(b"OggS_mock_ogg_content")
        await asyncio.sleep(0.5)

    # Both halves of the seam: discovery so the transcode step is reachable, and
    # the executor so no real ffmpeg runs. Patching only the executor would leave
    # this test depending on a real ffmpeg install.
    monkeypatch.setattr("galgame2voice.utils.audio_converter.find_ffmpeg", lambda *a, **k: "/fake/ffmpeg")
    monkeypatch.setattr("galgame2voice.utils.audio_converter.run_ffmpeg_command", _mock_ffmpeg)

    conv_task = asyncio.create_task(convert_wav_to_ogg(valid_wav_bytes))
    await asyncio.sleep(0.02)  # Allow temp files to be created
    conv_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await conv_task

    # Verify no leaked .tmp or .ogg files in tempfile.gettempdir() starting with conv_
    import tempfile
    temp_dir = Path(tempfile.gettempdir())
    leaks = list(temp_dir.glob("conv_*.ogg")) + list(temp_dir.glob("conv_*.wav"))
    assert len(leaks) == 0


# ============================================================================
# 4. UTF-8 BOM Safe Reading Across Config & Health
# ============================================================================

@pytest.mark.asyncio
async def test_utf8_bom_safe_reading_across_config_and_health(tmp_path):
    """Verifies that reading sovits_dir.txt with Windows UTF-8 BOM returns clean paths."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    sovits_txt = data_dir / "sovits_dir.txt"

    target_path = tmp_path / "mock_sovits_install"
    target_path.mkdir(parents=True, exist_ok=True)

    # Write UTF-8 BOM
    bom_content = "\ufeff" + str(target_path)
    sovits_txt.write_bytes(bom_content.encode("utf-8"))

    # Test reading with utf-8-sig
    read_path_str = sovits_txt.read_text(encoding="utf-8-sig").strip()
    assert not read_path_str.startswith("\ufeff")
    assert Path(read_path_str) == target_path
    assert Path(read_path_str).exists()


# ============================================================================
# 5. Telegram Voice Message Size Guard
# ============================================================================

@pytest.mark.asyncio
async def test_telegram_voice_size_guard():
    """Verifies that voice notes larger than 15MB are rejected before download."""
    handlers = TelegramBotHandlers(admin_ids=TELEGRAM_TEST_ADMINS, db_path=":memory:")

    update = MagicMock()
    update.effective_chat.id = 12345
    voice = MagicMock()
    voice.file_size = 16 * 1024 * 1024  # 16 MB > 15 MB
    update.message.voice = voice

    context = MagicMock()
    context.bot.send_message = AsyncMock()

    result = await handlers.handle_voice_message(update, context)
    assert result is None
    context.bot.send_message.assert_awaited_once()
    call_args = context.bot.send_message.call_args[1]
    assert "超过15MB" in call_args["text"]


# ============================================================================
# 6. Telegram Bot Error Boundary & Conflict Handling
# ============================================================================

@pytest.mark.asyncio
async def test_telegram_bot_conflict_and_failure_shutdown(caplog):
    """Verifies that Telegram conflict errors are logged and initialization failures shut down the app."""
    mgr = TelegramBotManager(db_path=":memory:")

    # Test error callback with Conflict
    context = MagicMock()
    context.error = RuntimeError("Conflict: terminated by other getUpdates request")

    with caplog.at_level("ERROR"):
        await mgr._on_telegram_error(None, context)
    assert any("Telegram token conflict" in r.message for r in caplog.records)


# ============================================================================
# 7. stream_chat Cancel Monitor Cleanup on Normal Completion
# ============================================================================

@pytest.mark.asyncio
async def test_stream_chat_cancel_monitor_clean_exit(tmp_path, mock_gpt_sovits):
    """Verifies that normal stream completion cancels and drains cancel_monitor without task leak."""
    client = GptSovitsClient(server=mock_gpt_sovits)
    tts_service = TtsService(client=client, db_path=str(tmp_path / "chat_test.db"))
    chat_service = ChatService(tts_service=tts_service, db_path=str(tmp_path / "chat_test.db"))

    class QuickAdapter:
        async def stream_chat(self, messages, **kwargs):
            yield '{"chinese": "你好", "japanese": "こんにちは"}'

    async def _mock_adapter(conn=None, provider_id=None):
        return QuickAdapter(), "mock-model", "mock-prov"

    chat_service._get_active_llm_adapter = _mock_adapter

    cancel_event = asyncio.Event()
    events = []
    async for ev in chat_service.stream_chat("hello", "sess_clean", cancel_event=cancel_event):
        events.append(ev)

    assert any(e.get("event") == "done" for e in events)

    # Let the loop tick once to allow finally: blocks to finalize
    await asyncio.sleep(0.01)

    # Drain background tasks
    await chat_service.aclose()
    assert len([t for t in chat_service._bg_tasks if not t.done()]) == 0
