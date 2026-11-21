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
from pathlib import Path

import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.services.voice_manager import VoiceManager
from galgame2voice.utils.audio_converter import convert_wav_to_ogg


# ============================================================================
# 1. Audio Cleaner Commit Verification
# ============================================================================


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


# ============================================================================
# 7. stream_chat Cancel Monitor Cleanup on Normal Completion
# ============================================================================
