"""Test runtime fixtures explicitly own local voice-manager background jobs."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.requires_piped_subprocess
def test_runtime_fixture_closes_local_voice_manager(tmp_path):
    root = Path(__file__).resolve().parents[1]
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "test_owner.py").write_text('''
import asyncio
from pathlib import Path
import pytest
from galgame2voice.services.voice_manager import VoiceManager

@pytest.mark.asyncio
async def test_local_owner(monkeypatch):
    manager = VoiceManager()
    started = asyncio.Event()
    async def pending():
        started.set()
        await asyncio.Event().wait()
    original = manager.aclose
    async def close():
        Path("owner-closed").write_text("closed", encoding="utf-8")
        for task in manager._bg_tasks:
            task.cancel()
        await original()
    monkeypatch.setattr(manager, "aclose", close)
    manager._spawn_background(pending())
    await started.wait()
''', encoding="utf-8")
    env = os.environ.copy()
    env.update(PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
               GALGAME2VOICE_PROJECT_ROOT=str(probe), DATA_DIR_NAME=str(probe / "data"),
               AUDIO_DIR_NAME=str(probe / "audio"), LOGS_DIR_NAME=str(probe / "logs"))
    result = subprocess.run([
        sys.executable, "-B", "-m", "pytest", "-q", "-p", "tests.conftest",
        "--confcutdir", str(probe), "--basetemp", str(probe / "runtime"), "test_owner.py",
    ], cwd=probe, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (probe / "owner-closed").is_file(), "runtime fixture did not close a local voice manager"
