"""Committed WAL data, recovery rollback, and atomic chat import regressions."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from galgame2voice.database import backups, session
from galgame2voice.database.migrations import CURRENT_SCHEMA_VERSION
from galgame2voice.routers.chat import ChatImportRequest, import_chat_sessions
from scripts import database_recovery, desktop_entry, desktop_instance


def database_fixture(path, *, version=CURRENT_SCHEMA_VERSION):
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE settings (id INTEGER); CREATE TABLE providers (id TEXT); "
                       "CREATE TABLE sessions (id TEXT); CREATE TABLE messages (content TEXT);")
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    return conn


def test_snapshot_includes_uncheckpointed_commits(tmp_path):
    database = tmp_path / "中文 ! chat.db"
    with closing(database_fixture(database)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO messages VALUES ('latest committed message')")
        writer.commit()
        with closing(sqlite3.connect(database.as_uri() + "?immutable=1", uri=True)) as main_only:
            assert main_only.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
        snapshot = backups.create_database_backup(database, required=True)
        with closing(sqlite3.connect(snapshot)) as restored:
            assert restored.execute("SELECT content FROM messages").fetchone() == ("latest committed message",)
            assert restored.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_daily_backup_dedup_and_required_upgrade_snapshot(tmp_path):
    database = tmp_path / "chat.db"
    database_fixture(database).close()
    first = backups.create_database_backup(database)
    assert first
    assert backups.create_database_backup(database) is None
    assert backups.create_database_backup(database, required=True) != first
    assert len(backups.list_database_backups(database)) == 2


def test_retention_preserves_latest_five_and_recovery_originals(tmp_path):
    database = tmp_path / "chat.db"
    database_fixture(database).close()
    preservation = tmp_path / "backups" / "before_restore_original"
    preservation.mkdir(parents=True)
    (preservation / "chat.db").write_bytes(b"preserved original")
    snapshots = [backups.create_database_backup(database, required=True) for _ in range(7)]
    assert set(backups.list_database_backups(database)) == set(snapshots[-5:])
    assert (preservation / "chat.db").read_bytes() == b"preserved original"


@pytest.mark.parametrize("required", [False, True])
def test_failed_backup_does_not_prune_valid_snapshots(tmp_path, monkeypatch, required):
    database = tmp_path / "chat.db"
    database_fixture(database).close()
    original = backups.create_database_backup(database, required=True)
    before = original.read_bytes()
    # Remove the daily suffix from this valid backup so the daily branch runs.
    original = original.rename(original.with_name("chat.db.bak_old"))
    def denied(_source, destination):
        destination.write_bytes(b"incomplete")
        raise PermissionError("write denied")
    monkeypatch.setattr(backups, "_snapshot", denied)
    if required:
        with pytest.raises(RuntimeError, match="停止升级"):
            backups.create_database_backup(database, required=True)
    else:
        assert backups.create_database_backup(database) is None
    assert backups.list_database_backups(database) == [original]
    assert original.read_bytes() == before
    assert not list(original.parent.glob(".pending_*"))


def test_corrupt_database_never_becomes_a_published_backup(tmp_path):
    database = tmp_path / "chat.db"
    database.write_bytes(b"broken database")
    with pytest.raises(RuntimeError, match="原数据库"):
        backups.create_database_backup(database, required=True)
    assert not backups.list_database_backups(database)
    assert database.read_bytes() == b"broken database"


def test_failed_pre_upgrade_backup_stops_migrations(tmp_path, monkeypatch):
    database = tmp_path / "chat.db"
    database_fixture(database, version=1).close()
    migrate = AsyncMock()
    monkeypatch.setattr("galgame2voice.database.crud.init_schema_and_seeds", migrate)
    monkeypatch.setattr(backups, "_snapshot", MagicMock(side_effect=PermissionError()))
    import asyncio
    with pytest.raises(RuntimeError, match="停止升级"):
        asyncio.run(session.init_db(database))
    migrate.assert_not_called()
    with closing(sqlite3.connect(database)) as reader:
        assert reader.execute("PRAGMA user_version").fetchone() == (1,)


def test_newer_schema_is_not_modified_by_an_older_app(tmp_path):
    database = tmp_path / "chat.db"
    database_fixture(database, version=CURRENT_SCHEMA_VERSION + 1).close()
    before = database.read_bytes()
    with pytest.raises(RuntimeError, match="较新的版本"):
        session._backup_database_sync(database)
    assert database.read_bytes() == before


def recovery_fixture(tmp_path):
    database = tmp_path / "chat.db"
    with closing(database_fixture(database)) as writer:
        writer.execute("INSERT INTO messages VALUES ('restore me')")
        writer.commit()
    selected = backups.create_database_backup(database, required=True)
    originals = {database: b"damaged database", Path(str(database) + "-wal"): b"original wal",
                 Path(str(database) + "-shm"): b"original shm"}
    for path, data in originals.items():
        path.write_bytes(data)
    return database, selected, originals


def test_recovery_preserves_all_originals_and_credential_files(tmp_path):
    database, selected, originals = recovery_fixture(tmp_path)
    master = tmp_path / ".master_key"
    master.write_bytes(b"unchanged key")
    preserved = backups.restore_database(database, selected, max_schema_version=CURRENT_SCHEMA_VERSION)
    with closing(sqlite3.connect(database)) as reader:
        assert reader.execute("SELECT content FROM messages").fetchone() == ("restore me",)
    for path, data in originals.items():
        assert (preserved / path.name).read_bytes() == data
    assert master.read_bytes() == b"unchanged key"
    assert not Path(str(database) + "-wal").exists()


def test_failed_replacement_rolls_back_original_files(tmp_path, monkeypatch):
    database, selected, originals = recovery_fixture(tmp_path)
    real_replace = backups.os.replace
    def fail_replacement(source, target):
        if Path(source).name.startswith(".restore_") and Path(target) == database:
            raise PermissionError("replacement denied")
        return real_replace(source, target)
    monkeypatch.setattr(backups.os, "replace", fail_replacement)
    with pytest.raises(PermissionError):
        backups.restore_database(database, selected, max_schema_version=CURRENT_SCHEMA_VERSION)
    assert all(path.read_bytes() == data for path, data in originals.items())
    assert not list(tmp_path.glob(".restore_*"))


@pytest.mark.parametrize("kind", ["outside", "invalid", "future"])
def test_invalid_recovery_choice_never_changes_original(tmp_path, kind):
    database, selected, originals = recovery_fixture(tmp_path)
    if kind == "outside":
        selected = tmp_path / "other.db"
        database_fixture(selected).close()
    elif kind == "invalid":
        selected.write_bytes(b"corrupt snapshot")
    else:
        with closing(sqlite3.connect(selected)) as conn:
            conn.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION + 1}")
    with pytest.raises((ValueError, sqlite3.Error)):
        backups.restore_database(database, selected, max_schema_version=CURRENT_SCHEMA_VERSION)
    assert all(path.read_bytes() == data for path, data in originals.items())


def test_recovery_flow_cancel_does_not_restore(tmp_path, monkeypatch):
    database, _selected, originals = recovery_fixture(tmp_path)
    monkeypatch.setattr(database_recovery, "get_database_path", lambda: str(database))
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert database_recovery.recover_interactively() == 0
    assert all(path.read_bytes() == data for path, data in originals.items())


def test_recovery_refuses_to_run_while_desktop_is_running(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(desktop_entry, "get_install_root", lambda: tmp_path)
    monkeypatch.setattr(desktop_entry.sys, "argv", ["app.exe", "--restore-database"])
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    owner = MagicMock()
    owner.acquire.return_value = False
    monkeypatch.setattr(desktop_instance, "DesktopInstance", lambda _root: owner)
    restore = MagicMock()
    monkeypatch.setattr(database_recovery, "recover_interactively", restore)
    assert desktop_entry.main() == 1
    restore.assert_not_called()
    owner.close.assert_called_once()


def import_request(*ids):
    return ChatImportRequest(sessions=[{"id": id_, "title": "Imported conversation",
        "settings": {"voiceProfileId": 99, "systemPrompt": "人设"},
        "messages": [{"role": "user", "content": "Remember this"},
                     {"role": "assistant", "content": "I remember", "japanese": "覚えた"}]} for id_ in ids])


@pytest.mark.asyncio
async def test_chat_import_restores_server_context_and_clears_cross_install_voice(isolate_test_database):
    await session.init_db()
    result = await import_chat_sessions(import_request("s_import_context"))
    assert result["count"] == 1
    with closing(sqlite3.connect(isolate_test_database)) as conn:
        settings, profile = conn.execute("SELECT settings_json, voice_profile_id FROM sessions WHERE id='s_import_context'").fetchone()
        assert json.loads(settings)["voiceProfileId"] is None and profile is None
        rows = conn.execute("SELECT role, content_chinese FROM messages WHERE session_id='s_import_context' ORDER BY id").fetchall()
        assert rows == [("user", "Remember this"), ("assistant", "I remember")]


@pytest.mark.asyncio
async def test_chat_import_collision_rolls_back_whole_batch(isolate_test_database):
    await session.init_db()
    await import_chat_sessions(import_request("s_import_existing"))
    with pytest.raises(HTTPException, match="409"):
        await import_chat_sessions(import_request("s_import_new", "s_import_existing"))
    with closing(sqlite3.connect(isolate_test_database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id='s_import_new'").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='s_import_existing'").fetchone() == (2,)


def test_chat_import_rejects_invalid_and_duplicate_ids():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        import_request("ordinary-existing-id")
    with pytest.raises(ValidationError):
        import_request("s_import_duplicate", "s_import_duplicate")
