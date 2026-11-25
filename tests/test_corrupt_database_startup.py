"""A damaged database file stops startup with a Chinese message that points at the recovery tool."""
import asyncio
import sqlite3
from contextlib import closing

import pytest

from galgame2voice.database import session


@pytest.mark.parametrize("content", [
    b"this is not a database at all" * 40,
    b"SQLite format 3\x00" + b"\x00" * 50,
], ids=["text", "cut-header"])
def test_damaged_database_names_the_file_and_the_recovery_tool(tmp_path, content):
    database = tmp_path / "chat.db"
    database.write_bytes(content)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(session.init_db(database))
    message = str(caught.value)
    assert "已损坏" in message
    assert str(database) in message
    assert "数据恢复.bat" in message
    assert database.read_bytes() == content


def test_damaged_pages_inside_a_readable_file_get_the_same_message(tmp_path):
    database = tmp_path / "chat.db"
    asyncio.run(session.init_db(database))
    data = bytearray(database.read_bytes())
    for offset in range(4096 * 2, len(data), 4096):
        data[offset:offset + 200] = b"\xff" * 200
    database.write_bytes(bytes(data))
    with pytest.raises(RuntimeError, match="已损坏") as caught:
        asyncio.run(session.init_db(database))
    assert "数据恢复.bat" in str(caught.value)


def test_a_locked_database_is_not_called_damaged(tmp_path, monkeypatch):
    database = tmp_path / "chat.db"
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("CREATE TABLE t (x)")
        writer.commit()

    def locked(_path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("galgame2voice.database.backups._open_readonly", locked)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        session._backup_database_sync(database)


def test_a_migration_bug_is_not_called_damaged(tmp_path, monkeypatch):
    database = tmp_path / "chat.db"

    async def broken(_conn):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: t.x")

    monkeypatch.setattr("galgame2voice.database.crud.init_schema_and_seeds", broken)
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(session.init_db(database))
