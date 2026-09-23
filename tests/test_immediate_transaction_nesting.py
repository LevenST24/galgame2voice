"""
Regression tests for nested immediate_transaction() and its per-connection lock.

The per-connection lock added for the depth-counter fix must remain
reentrancy-aware: nested immediate_transaction() calls run in the SAME task and
are supported via SQL savepoints, so waiting on a non-reentrant asyncio.Lock
would deadlock the task against itself.
"""

import asyncio
import os
import tempfile

import pytest

from galgame2voice.database.session import get_db, immediate_transaction, init_db


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="imm_tx_")
    os.close(fd)
    yield path
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_nested_immediate_transaction_does_not_deadlock(temp_db):
    """Same-task nesting must complete well within the timeout instead of hanging."""
    await init_db(temp_db)

    async def scenario():
        async with get_db(temp_db) as conn:
            await conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
            async with immediate_transaction(conn):
                await conn.execute("INSERT INTO t (v) VALUES ('outer')")
                async with immediate_transaction(conn):
                    await conn.execute("INSERT INTO t (v) VALUES ('inner')")

    await asyncio.wait_for(scenario(), timeout=15.0)

    async with get_db(temp_db) as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM t")
        row = await cursor.fetchone()
        assert row[0] == 2, "nested transaction did not persist both rows"


@pytest.mark.asyncio
async def test_inner_rollback_does_not_discard_outer_changes(temp_db):
    """Savepoint semantics: an inner failure rolls back only the inner writes."""
    await init_db(temp_db)

    async with get_db(temp_db) as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
        async with immediate_transaction(conn):
            await conn.execute("INSERT INTO t (v) VALUES ('outer')")
            with pytest.raises(RuntimeError):
                async with immediate_transaction(conn):
                    await conn.execute("INSERT INTO t (v) VALUES ('inner')")
                    raise RuntimeError("boom")

    async with get_db(temp_db) as conn:
        cursor = await conn.execute("SELECT v FROM t ORDER BY id")
        rows = await cursor.fetchall()
        assert [r[0] for r in rows] == ["outer"], f"expected only the outer row, got {rows}"


@pytest.mark.asyncio
async def test_depth_counter_is_restored_after_nesting(temp_db):
    """The depth counter must return to its entry value, not leak or hard-reset."""
    await init_db(temp_db)

    async with get_db(temp_db) as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
        assert getattr(conn, "_imm_tx_depth", 0) == 0
        async with immediate_transaction(conn):
            assert conn._imm_tx_depth == 1
            async with immediate_transaction(conn):
                assert conn._imm_tx_depth == 2
            assert conn._imm_tx_depth == 1
        assert conn._imm_tx_depth == 0
        assert getattr(conn, "_imm_tx_lock_owner", None) is None


@pytest.mark.asyncio
async def test_sequential_transactions_on_shared_connection(temp_db):
    """Two consecutive transactions on one connection must not deadlock."""
    await init_db(temp_db)

    async def scenario():
        async with get_db(temp_db) as conn:
            await conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
            for i in range(3):
                async with immediate_transaction(conn):
                    await conn.execute("INSERT INTO t (v) VALUES (?)", (f"row{i}",))

    await asyncio.wait_for(scenario(), timeout=15.0)

    async with get_db(temp_db) as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM t")
        assert (await cursor.fetchone())[0] == 3