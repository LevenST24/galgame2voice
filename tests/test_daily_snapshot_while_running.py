"""The app keeps taking one database snapshot per day while it stays open."""

import asyncio
import shutil
import sqlite3
from datetime import datetime, timedelta

import pytest

from galgame2voice.database import backups
from galgame2voice.services import audio_cleaner


def _make_db(path):
    conn = sqlite3.connect(path)
    for table in ("settings", "providers", "sessions", "messages"):
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()


def _snapshots(db):
    return sorted((db.parent / "backups").glob(f"{db.name}.bak_*"))


@pytest.fixture(autouse=True)
def fresh_attempts(monkeypatch):
    monkeypatch.setattr(backups, "_last_daily_attempt", {})


def test_new_day_gets_a_snapshot_and_same_day_is_checked_once(tmp_path, monkeypatch):
    db = tmp_path / "galgame2voice.db"
    _make_db(db)
    (tmp_path / "backups").mkdir()
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    shutil.copy2(db, tmp_path / "backups" / f"{db.name}.bak_{yesterday}_120000_000000")

    created = backups.ensure_daily_database_backup(db)
    assert created is not None and datetime.now().strftime("%Y%m%d") in created.name
    assert len(_snapshots(db)) == 2

    calls = []
    monkeypatch.setattr(backups, "create_database_backup", lambda *a, **k: calls.append(a))
    assert backups.ensure_daily_database_backup(db) is None
    assert calls == []


@pytest.mark.asyncio
async def test_cleanup_loop_checks_the_daily_snapshot(monkeypatch, tmp_path):
    seen = []

    async def no_cycle(_audio_dir):
        return None

    def record(path):
        seen.append(path)
        raise asyncio.CancelledError

    monkeypatch.setattr(audio_cleaner, "_cleanup_interval_seconds", lambda _configured: 0)  # the real floor is 60 s
    monkeypatch.setattr(audio_cleaner, "_run_audio_cleanup_cycle", no_cycle)
    monkeypatch.setattr(backups, "ensure_daily_database_backup", record)
    await asyncio.wait_for(audio_cleaner._audio_cleanup_loop(tmp_path, 0), timeout=5)
    assert len(seen) == 1
