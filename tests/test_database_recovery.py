"""Committed WAL data, recovery rollback, and atomic chat import regressions."""

from contextlib import closing
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from galgame2voice.database import backups, crud, session
from galgame2voice.database.migrations import CURRENT_SCHEMA_VERSION
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import (
    get_db,
    get_schema_version,
    init_db,
    set_schema_version,
)
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


def test_snapshots_in_one_clock_tick_keep_both(tmp_path, monkeypatch):
    database = tmp_path / "chat.db"
    database_fixture(database).close()
    tick = datetime(2026, 10, 22, 4, 25, 22, 880826)
    # A coarse Windows clock: both snapshots read the same time until it advances.
    readings = iter([tick] * 4 + [tick + timedelta(milliseconds=2)])

    class CoarseClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(readings)

    monkeypatch.setattr(backups, "datetime", CoarseClock)
    first = backups.create_database_backup(database, required=True)
    second = backups.create_database_backup(database, required=True)
    assert first != second
    assert backups.list_database_backups(database) == [second, first]


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


@pytest.mark.asyncio
async def test_chat_backup_exports_character_name_and_restores_unique_match_on_import(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from galgame2voice.database.models import VoiceProfileCreate
    from galgame2voice.main import create_app

    source_db = tmp_path / "source.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(source_db))
    await init_db(source_db)
    async with get_db(source_db) as conn:
        source_profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(name="夏目", gpt_weights_path="a.ckpt", sovits_weights_path="a.pth"),
        )
        await crud.upsert_session(
            conn,
            session_id="s_bound",
            title="夏目的对话",
            voice_profile_id=source_profile.id,
            settings_json=json.dumps({"temperature": 0.8, "voiceProfileId": source_profile.id}),
        )
        await crud.upsert_session(
            conn,
            session_id="s_unbound",
            title="未绑定对话",
            voice_profile_id=None,
            settings_json=json.dumps({"temperature": 0.6, "voiceProfileId": None}),
            update_voice_binding=True,
        )

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8080") as client:
        export_resp = await client.get("/api/chat/export")
        assert export_resp.status_code == 200
        exported_by_id = {item["id"]: item for item in export_resp.json()["sessions"]}
        assert exported_by_id["s_bound"]["characterName"] == "夏目"
        assert exported_by_id["s_unbound"].get("characterName") is None

    target_db = tmp_path / "target.db"
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(target_db))
    await init_db(target_db)
    async with get_db(target_db) as conn:
        other_profile = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(name="其他角色", gpt_weights_path="o.ckpt", sovits_weights_path="o.pth"),
        )
        target_natsume = await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(name="夏目", gpt_weights_path="n.ckpt", sovits_weights_path="n.pth"),
        )
        await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(name="Anna", gpt_weights_path="d1.ckpt", sovits_weights_path="d1.pth"),
        )
        await crud.create_voice_profile(
            conn,
            VoiceProfileCreate(name="ANNA", gpt_weights_path="d2.ckpt", sovits_weights_path="d2.pth"),
        )
        assert target_natsume.id != source_profile.id
        assert other_profile.id == source_profile.id

    payload = {
        "sessions": [
            {
                "id": "s_import_matched",
                "title": exported_by_id["s_bound"]["title"],
                "characterName": exported_by_id["s_bound"]["characterName"],
                "settings": {"temperature": 0.8, "voiceProfileId": None},
                "messages": [{"role": "user", "content": "你好"}],
            },
            {
                "id": "s_import_missing",
                "title": "找不到同名角色",
                "characterName": "不存在的角色",
                "settings": {"temperature": 0.7, "voiceProfileId": None},
                "messages": [{"role": "user", "content": "还在吗"}],
            },
            {
                "id": "s_import_ambiguous",
                "title": "有两个同名角色",
                "characterName": "Anna",
                "settings": {"temperature": 0.7, "voiceProfileId": None},
                "messages": [{"role": "user", "content": "重名测试"}],
            },
            {
                "id": "s_import_legacy",
                "title": "老备份无角色名",
                "settings": {"temperature": 0.7, "voiceProfileId": other_profile.id},
                "messages": [{"role": "user", "content": "旧备份"}],
            },
        ]
    }
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8080") as client:
        import_resp = await client.post("/api/chat/import", json=payload)
        assert import_resp.status_code == 200, import_resp.text
        body = import_resp.json()
        assert body["status"] == "ok"
        assert body["count"] == 4
        assert body["restored_sessions"] == [{"id": "s_import_matched", "voice_profile_id": target_natsume.id}]

    with closing(sqlite3.connect(target_db)) as conn:
        rows = {
            sess_id: (profile_id, json.loads(settings_json)["voiceProfileId"])
            for sess_id, profile_id, settings_json in conn.execute(
                "SELECT id, voice_profile_id, settings_json FROM sessions WHERE id LIKE 's_import_%'"
            ).fetchall()
        }
        assert rows["s_import_matched"] == (target_natsume.id, target_natsume.id)
        assert rows["s_import_missing"] == (None, None)
        assert rows["s_import_ambiguous"] == (None, None)
        assert rows["s_import_legacy"] == (None, None)


class TestDatabaseTimeoutsAndConcurrencySafety:
    """Tests SQLite connection configuration and dual-table cleanup safety."""


    @pytest.mark.asyncio
    async def test_clear_session_messages_clears_both_tables_if_present(self, tmp_path):
        """Verify clear_session_messages purges from both session_messages and messages if both exist."""
        db_p = tmp_path / "dual_table.db"
        await init_db(db_p)

        async with get_db(db_p) as conn:
            # Create session_messages table if not already existing
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS session_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content_chinese TEXT DEFAULT '',
                    content_japanese TEXT DEFAULT '',
                    raw_content TEXT DEFAULT ''
                );
            """)
            # Insert into both
            await conn.execute("""
                INSERT INTO session_messages (session_id, role, content_chinese)
                VALUES ('dual_sess', 'user', 'Dual Table Test');
            """)
            await crud.add_message(conn, MessageCreate(
                session_id="dual_sess",
                role="assistant",
                content_chinese="Dual Table Reply",
            ))
            await conn.commit()

            # Call clear_session_messages
            cleared = await crud.clear_session_messages(conn, "dual_sess")
            assert cleared is True

            # Verify both are empty
            cur1 = await conn.execute("SELECT COUNT(*) FROM session_messages WHERE session_id = 'dual_sess';")
            assert (await cur1.fetchone())[0] == 0
            cur2 = await conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = 'dual_sess';")
            assert (await cur2.fetchone())[0] == 0


class TestDatabasePragmasAndIndexCoverage:
    """Verifies SQLite performance tuning pragmas and index existence."""

    @pytest.mark.asyncio
    async def test_performance_pragmas_configured(self, tmp_path):
        db_p = tmp_path / "pragma_test.db"
        async with get_db(db_p) as conn:
            mode = (await (await conn.execute("PRAGMA journal_mode;")).fetchone())[0]
            assert str(mode).lower() == "wal"

            timeout = (await (await conn.execute("PRAGMA busy_timeout;")).fetchone())[0]
            assert timeout == 5000

            sync = (await (await conn.execute("PRAGMA synchronous;")).fetchone())[0]
            assert sync == 1

            cache = (await (await conn.execute("PRAGMA cache_size;")).fetchone())[0]
            assert cache == -64000

            temp = (await (await conn.execute("PRAGMA temp_store;")).fetchone())[0]
            assert temp == 2

    @pytest.mark.asyncio
    async def test_database_indices_coverage(self, tmp_path):
        db_p = tmp_path / "index_coverage.db"
        await init_db(db_p)

        async with get_db(db_p) as conn:
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='index';")
            rows = await cursor.fetchall()
            index_names = {r[0] for r in rows}

            expected_indices = {
                "idx_messages_session_id",
                "idx_messages_created_at",
                "idx_messages_session_created",
                "idx_sessions_channel",
                "idx_sessions_updated_at",
                "idx_sessions_user_id",
                "idx_sessions_voice_profile",
                "idx_voice_profiles_is_default",
                "idx_user_memories_user_cat",
                "idx_user_memories_char_key",
                "idx_affection_user_char",
                "idx_tts_cache_last_accessed",
                "idx_tts_cache_clean_text",
            }
            for exp in expected_indices:
                assert exp in index_names, f"Expected index {exp} not found in DB schema!"


class TestDatabaseSchemaMigrations:
    """Tests for SQLite PRAGMA user_version tracking, incremental migrations, and idempotency."""

    @pytest.fixture
    def temp_db_path(self):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="test_phase4_mig_")
        os.close(fd)
        yield path
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    @pytest.mark.asyncio
    async def test_fresh_database_initialization_sets_version(self, temp_db_path):
        async with get_db(temp_db_path) as conn:
            v0 = await get_schema_version(conn)
            assert v0 == 0

        await init_db(temp_db_path)

        async with get_db(temp_db_path) as conn:
            v_final = await get_schema_version(conn)
            assert v_final == CURRENT_SCHEMA_VERSION
            assert v_final >= 4

    @pytest.mark.asyncio
    async def test_schema_migrations_idempotency(self, temp_db_path):
        await init_db(temp_db_path)
        await init_db(temp_db_path)
        await init_db(temp_db_path)

        async with get_db(temp_db_path) as conn:
            v = await get_schema_version(conn)
            assert v == CURRENT_SCHEMA_VERSION

            cur = await conn.execute("SELECT COUNT(*) FROM providers;")
            count = (await cur.fetchone())[0]
            assert count >= 8

            cur = await conn.execute("SELECT COUNT(*) FROM voice_profiles WHERE id = 1;")
            assert (await cur.fetchone())[0] == 0

            cur = await conn.execute("SELECT COUNT(*) FROM settings WHERE id = 1;")
            assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_migration_preserves_existing_user_data(self, temp_db_path):
        await init_db(temp_db_path)

        async with get_db(temp_db_path) as conn:
            await conn.execute("INSERT INTO sessions (id, channel, user_id) VALUES ('sess_custom', 'web', 'u1');")
            await conn.execute(
                "INSERT INTO messages (session_id, role, content_chinese, content_japanese) "
                "VALUES ('sess_custom', 'user', '你好夏目', 'こんにちは');"
            )
            await conn.commit()

        async with get_db(temp_db_path) as conn:
            await set_schema_version(conn, 2)
            v_low = await get_schema_version(conn)
            assert v_low == 2

        await init_db(temp_db_path)

        async with get_db(temp_db_path) as conn:
            v_upgraded = await get_schema_version(conn)
            assert v_upgraded == CURRENT_SCHEMA_VERSION

            cur = await conn.execute("SELECT content_chinese FROM messages WHERE session_id = 'sess_custom';")
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == "你好夏目"
