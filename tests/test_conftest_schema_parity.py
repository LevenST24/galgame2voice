"""Contract tests ensuring test SQLite fixtures match real migrations.py schema and seeds."""

from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.migrations import CURRENT_SCHEMA_VERSION
from galgame2voice.database.session import get_db, init_db
from galgame2voice.security.crypto import decrypt_secret


def _introspect_schema(db_path: str | Path) -> tuple[int, set[str], dict[str, list[tuple[object, ...]]]]:
    """Returns (user_version, user_table_names, {table: [(name, type, notnull, dflt_value, pk), ...]})."""
    with closing(sqlite3.connect(str(db_path))) as conn:
        user_version = conn.execute("PRAGMA user_version;").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%';"
            ).fetchall()
        }
        table_columns: dict[str, list[tuple[object, ...]]] = {}
        for tbl in sorted(tables):
            cols = [
                (r[1], r[2], r[3], r[4], r[5])
                for r in conn.execute(f'PRAGMA table_info("{tbl}");').fetchall()
            ]
            table_columns[tbl] = cols
    return user_version, tables, table_columns


@pytest.mark.asyncio
async def test_isolated_test_db_matches_fresh_init_db_schema_and_version(
    isolate_test_database, temp_db_path, tmp_path, monkeypatch
):
    """isolate_test_database and temp_db_path must match a fresh real init_db() schema and version."""
    isolated_data_dir = get_settings().data_dir
    token_file = isolated_data_dir / ".console_token"
    assert token_file.is_file(), "isolate_test_database must copy .console_token into test data_dir"
    expected_token = token_file.read_text(encoding="utf-8").strip()
    assert expected_token

    async with get_db(isolate_test_database) as conn:
        assert await crud.verify_console_token(conn, expected_token) is True
        cur = await conn.execute("SELECT console_token FROM settings WHERE id = 1;")
        stored_cipher = (await cur.fetchone())[0]
        assert decrypt_secret(stored_cipher) == expected_token

    ref_data_dir = tmp_path / "reference_data"
    ref_data_dir.mkdir(parents=True, exist_ok=True)
    ref_path = tmp_path / "reference.db"

    with monkeypatch.context() as m:
        m.setenv("DATA_DIR_NAME", str(ref_data_dir))
        m.setenv("GALGAME2VOICE_DB_PATH", str(ref_path))
        m.setenv("GALGAME_DB_PATH", str(ref_path))
        get_settings.cache_clear()
        try:
            await init_db(ref_path)
        finally:
            get_settings.cache_clear()

    ref_version, ref_tables, ref_cols = _introspect_schema(ref_path)
    iso_version, iso_tables, iso_cols = _introspect_schema(isolate_test_database)
    tmp_version, tmp_tables, tmp_cols = _introspect_schema(temp_db_path)

    assert ref_version == CURRENT_SCHEMA_VERSION
    assert iso_version == CURRENT_SCHEMA_VERSION
    assert tmp_version == CURRENT_SCHEMA_VERSION

    assert "tts_cache_entries" in iso_tables
    assert "token_usage_metrics" in iso_tables
    assert "session_messages" not in iso_tables

    assert iso_tables == ref_tables
    assert tmp_tables == ref_tables

    assert iso_cols == ref_cols
    assert tmp_cols == ref_cols

    session_col_names = {c[0] for c in iso_cols["sessions"]}
    assert {"title", "settings_json"}.issubset(session_col_names)

    settings_col_names = {c[0] for c in iso_cols["settings"]}
    assert "stt_engine" in settings_col_names
    assert settings_col_names.isdisjoint({"key", "value", "description"})

    voice_col_names = {c[0] for c in iso_cols["voice_profiles"]}
    assert voice_col_names.isdisjoint(
        {"refer_audio_path", "refer_text", "refer_language", "prompt_language", "text_language"}
    )

    provider_col_names = {c[0] for c in iso_cols["providers"]}
    assert provider_col_names.isdisjoint({"provider_type", "base_url", "model", "extra_config"})


def test_isolated_test_db_enforces_settings_singleton_check_and_null_initial_voice(
    isolate_test_database,
):
    """settings must have pre-seeded ('deepseek', None) at id=1 and enforce CHECK (id = 1)."""
    with closing(sqlite3.connect(str(isolate_test_database))) as conn:
        row = conn.execute(
            "SELECT active_provider_id, active_voice_profile_id FROM settings WHERE id = 1;"
        ).fetchone()
        assert row == ("deepseek", None)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO settings (id, active_provider_id) VALUES (2, 'openai');"
            )
            conn.commit()
