"""A server that stops mid-import leaves its upload (up-*.bin) and staging
folder (imp-*) in data/character_import. Packages can be gigabytes, and
nothing used to remove them.
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from galgame2voice import main as main_module
from galgame2voice.config import get_settings
from galgame2voice.services.character_package_import import sweep_import_leftovers


def _leftovers(data_dir, age_seconds):
    temp_root = data_dir / "character_import"
    staging = temp_root / "imp-abc123" / "extracted" / "yuzu"
    staging.mkdir(parents=True)
    (staging / "gpt.ckpt").write_bytes(b"x" * 1024)
    upload = temp_root / "up-def456.bin"
    upload.write_bytes(b"PK" + b"\0" * 1024)
    unrelated = temp_root / "notes.txt"
    unrelated.write_text("keep")
    old = time.time() - age_seconds
    for path in (temp_root / "imp-abc123", upload):
        os.utime(path, (old, old))
    return temp_root / "imp-abc123", upload, unrelated


def test_import_time_sweep_removes_only_stale_leftovers(tmp_path):
    stale_dir, stale_upload, unrelated = _leftovers(tmp_path, 2 * 86400)
    fresh_upload = tmp_path / "character_import" / "up-running.bin"
    fresh_upload.write_bytes(b"PK")  # another import still streaming

    assert sweep_import_leftovers(tmp_path) == 2

    assert not stale_dir.exists() and not stale_upload.exists()
    assert fresh_upload.exists() and unrelated.exists()


@pytest.mark.asyncio
async def test_startup_removes_every_leftover(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    staging, upload, unrelated = _leftovers(get_settings().data_dir, 60)

    monkeypatch.setattr(main_module, "_init_database_and_characters", AsyncMock())
    monkeypatch.setattr(main_module, "_init_gpt_sovits_client", AsyncMock())

    async def cleanup_loop(**kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(main_module, "_audio_cleanup_loop", cleanup_loop)

    async def shutdown(settings, cleanup_task):
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)
    monkeypatch.setattr(main_module, "_shutdown_services", shutdown)

    async with main_module.lifespan(MagicMock()):
        assert not staging.exists() and not upload.exists()
        assert unrelated.exists()
