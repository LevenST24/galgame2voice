"""A server that stops mid-import leaves its upload (up-*.bin) and staging
folder (imp-*) in data/character_import. Packages can be gigabytes, and
nothing used to remove them.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from galgame2voice import main as main_module
from galgame2voice.config import get_settings
from galgame2voice.database.session import init_db
from galgame2voice.services import package_redeem
from galgame2voice.services.character_manager import CharacterManager
from scripts.g2v_dist.g2v_dist import encode_code
from tests.test_character_package_import import _make_package_bytes


def _leftovers(data_dir, age_seconds, *, part_bytes: bytes = b"PK\0"):
    temp_root = data_dir / "character_import"
    staging = temp_root / "imp-abc123" / "extracted" / "yuzu"
    staging.mkdir(parents=True)
    (staging / "gpt.ckpt").write_bytes(b"x" * 1024)
    upload = temp_root / "up-def456.bin"
    upload.write_bytes(b"PK" + b"\0" * 1024)
    redeem = temp_root / "dl-other.zip.import"
    redeem.write_bytes(b"PK" + b"\0" * 1024)
    part = temp_root / "dl-alice.zip.part"
    part.write_bytes(part_bytes)
    unrelated = temp_root / "notes.txt"
    unrelated.write_text("keep")
    old = time.time() - age_seconds
    for path in (temp_root / "imp-abc123", upload, redeem, part):
        os.utime(path, (old, old))
    return temp_root / "imp-abc123", upload, redeem, part, unrelated


@pytest.mark.asyncio
@pytest.mark.parametrize("via_redeem", [False, True])
async def test_import_time_sweep_removes_only_stale_leftovers(tmp_path, monkeypatch, via_redeem):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    CharacterManager.reset_instance()
    pkg = _make_package_bytes("zip", "alice", "爱丽丝")
    data_dir = get_settings().data_dir
    stale_dir, stale_upload, stale_redeem, kept_part, unrelated = _leftovers(data_dir, 2 * 86400, part_bytes=pkg)
    fresh_upload = data_dir / "character_import" / "up-running.bin"
    fresh_upload.write_bytes(b"PK")  # another import still streaming
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=main_module.create_app()), base_url="http://127.0.0.1:8080") as c:
        if via_redeem:
            pkg_info = {"id": "alice", "name": "爱丽丝", "size": len(pkg), "sha256": hashlib.sha256(pkg).hexdigest(), "remaining": 1}
            monkeypatch.setattr(package_redeem, "_packages", AsyncMock(return_value=[pkg_info]))
            res = await c.post("/api/characters/redeem/download", json={"code": encode_code("https://dist.example.com", b"x" * 32), "package_id": "alice"})
        else:
            res = await c.post("/api/characters/import", params={"filename": "alice.zip"}, content=pkg)
    assert res.status_code == 200, res.text
    assert not stale_dir.exists() and not stale_upload.exists() and not stale_redeem.exists()
    assert fresh_upload.exists() and unrelated.exists() and (via_redeem or kept_part.exists())


@pytest.mark.asyncio
async def test_startup_removes_every_leftover(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    staging, upload, redeem, kept_part, unrelated = _leftovers(get_settings().data_dir, 60)

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
        assert not staging.exists() and not upload.exists() and not redeem.exists()
        assert kept_part.exists() and unrelated.exists()
