"""Existing v7 installations remain usable after syncing application code."""

from contextlib import closing
import sqlite3

import aiosqlite
import pytest

from galgame2voice.database import migrations, session


async def existing_database(path, *, version, cascade=False):
    await session.init_db(path)
    async with aiosqlite.connect(path) as conn:
        conn.row_factory = aiosqlite.Row
        if cascade:
            for table in ("user_memories", "character_affection"):
                sql = (await (await conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                )).fetchone())[0]
                await conn.execute(f'DROP TABLE "{table}"')
                sql = sql.replace("character_id INTEGER", "character_id INTEGER REFERENCES voice_profiles(id) ON DELETE CASCADE")
                await conn.execute(sql)
            await conn.execute("CREATE INDEX custom_memory_index ON user_memories(fact_value)")
            await conn.execute("CREATE TABLE migration_audit (record_id INTEGER)")
            await conn.execute("""CREATE TRIGGER custom_memory_trigger AFTER INSERT ON user_memories
                BEGIN INSERT INTO migration_audit VALUES (NEW.id); END""")
        await conn.execute("""INSERT INTO voice_profiles
            (id,name,gpt_weights_path,sovits_weights_path,ref_audio_path,prompt_text)
            VALUES (9,'Demo','gpt.ckpt','sovits.pth','ref.wav','demo')""")
        await conn.execute("INSERT INTO sessions (id,title) VALUES ('existing','Keep my conversation')")
        await conn.execute("""INSERT INTO messages (id,session_id,role,content_chinese)
            VALUES (5,'existing','user','Keep my message')""")
        await conn.execute("""INSERT INTO user_memories (id,character_id,fact_key,fact_value,source_message_id)
            VALUES (8,9,'preference','Keep my memory',5)""")
        await conn.execute("""INSERT INTO character_affection (id,character_id,affection_score)
            VALUES (4,9,42)""")
        await conn.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='user_memories'")
        await conn.execute(f"PRAGMA user_version={version}")
        await conn.commit()


@pytest.mark.asyncio
async def test_existing_version_7_database_can_initialize_without_losing_records(tmp_path):
    path = tmp_path / "existing.db"
    await existing_database(path, version=7)
    with closing(sqlite3.connect(path)) as conn:
        before = conn.execute("SELECT * FROM messages").fetchall()
    await session.init_db(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (7,)
        assert conn.execute("SELECT * FROM messages").fetchall() == before
        assert conn.execute("SELECT fact_value FROM user_memories").fetchone() == ("Keep my memory",)
        assert conn.execute("SELECT affection_score FROM character_affection").fetchone() == (42,)
    assert list((tmp_path / "backups").glob("existing.db.bak_*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [6, 7])
async def test_legacy_character_cascades_are_removed_without_losing_data(tmp_path, version):
    path = tmp_path / "legacy.db"
    await existing_database(path, version=version, cascade=True)
    await session.init_db(path)
    async with aiosqlite.connect(path) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        assert (await (await conn.execute("PRAGMA user_version")).fetchone()) == (7,)
        assert not any(row[2] == "voice_profiles" for row in await (await conn.execute(
            "PRAGMA foreign_key_list(user_memories)"
        )).fetchall())
        await conn.execute("DELETE FROM voice_profiles WHERE id=9")
        assert (await (await conn.execute("SELECT fact_value FROM user_memories")).fetchone()) == ("Keep my memory",)
        assert (await (await conn.execute("SELECT affection_score FROM character_affection")).fetchone()) == (42,)
        assert (await (await conn.execute("SELECT source_message_id FROM user_memories")).fetchone()) == (5,)
        assert (await (await conn.execute("SELECT seq FROM sqlite_sequence WHERE name='user_memories'")).fetchone()) == (100,)
        assert (await (await conn.execute("SELECT name FROM sqlite_master WHERE name='custom_memory_index'")).fetchone())
        await conn.execute("INSERT INTO user_memories (character_id,fact_key,fact_value) VALUES (9,'second','new')")
        assert (await (await conn.execute("SELECT record_id FROM migration_audit ORDER BY record_id DESC LIMIT 1")).fetchone()) == (101,)
        await conn.execute("DELETE FROM messages WHERE id=5")
        assert (await (await conn.execute("SELECT source_message_id FROM user_memories WHERE id=8")).fetchone()) == (None,)


@pytest.mark.asyncio
async def test_migration_entry_point_rejects_unknown_future_schema_without_writes(tmp_path):
    path = tmp_path / "future.db"
    await existing_database(path, version=100)
    before = path.read_bytes()
    async with aiosqlite.connect(path) as conn:
        with pytest.raises(RuntimeError, match="较新的版本"):
            await migrations.init_schema_and_seeds(conn)
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_legacy_migration_rolls_back_both_tables_when_rebuild_fails(tmp_path, monkeypatch):
    path = tmp_path / "rollback.db"
    await existing_database(path, version=6, cascade=True)
    original_execute = aiosqlite.Connection.execute

    async def fail_second_rebuild(conn, sql, parameters=None):
        if sql == "DROP TABLE character_affection":
            raise sqlite3.OperationalError("simulated disk failure")
        return await original_execute(conn, sql, parameters or ())

    monkeypatch.setattr(aiosqlite.Connection, "execute", fail_second_rebuild)
    with pytest.raises(sqlite3.OperationalError, match="simulated disk failure"):
        await session.init_db(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (6,)
        assert conn.execute("SELECT fact_value FROM user_memories").fetchone() == ("Keep my memory",)
        assert conn.execute("SELECT affection_score FROM character_affection").fetchone() == (42,)
        for table in ("user_memories", "character_affection"):
            assert any(row[2] == "voice_profiles" for row in conn.execute(f"PRAGMA foreign_key_list({table})"))
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%__fk_rebuild'").fetchall()


@pytest.mark.asyncio
async def test_unknown_legacy_columns_are_preserved_instead_of_discarded(tmp_path):
    path = tmp_path / "unknown-column.db"
    await existing_database(path, version=6, cascade=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("ALTER TABLE user_memories ADD COLUMN custom_note TEXT")
        conn.execute("UPDATE user_memories SET custom_note='Keep my extra field'")
        conn.commit()
    with pytest.raises(RuntimeError, match="无法安全迁移"):
        await session.init_db(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (6,)
        assert conn.execute("SELECT custom_note FROM user_memories").fetchone() == ("Keep my extra field",)
