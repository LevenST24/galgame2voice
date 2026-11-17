"""
Regression tests for nested immediate_transaction() and its per-connection lock.

The per-connection lock added for the depth-counter fix must remain
reentrancy-aware: nested immediate_transaction() calls run in the SAME task and
are supported via SQL savepoints, so waiting on a non-reentrant asyncio.Lock
would deadlock the task against itself.
"""

import asyncio

import aiosqlite
import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, immediate_transaction, init_db


class TestDeleteMessageTransactionIsolation:
    """Verifies crud.delete_message transaction isolation and savepoint reentrancy."""

    @pytest.mark.asyncio
    async def test_delete_message_nested_reentrant_savepoint_execution(self, tmp_path):
        """crud.delete_message called inside an existing transaction must run via nested savepoints."""
        db_path = str(tmp_path / "test_del_reentrant.db")
        await init_db(db_path)

        async with get_db(db_path) as conn:
            await crud.get_or_create_session(conn, "sess_del_nested")
            msg1 = await crud.add_message(
                conn,
                MessageCreate(
                    session_id="sess_del_nested",
                    role="user",
                    content_chinese="消息一",
                ),
            )
            msg2 = await crud.add_message(
                conn,
                MessageCreate(
                    session_id="sess_del_nested",
                    role="assistant",
                    content_chinese="消息二",
                ),
            )
            assert await crud.count_session_messages(conn, "sess_del_nested") == 2

            # Outer immediate_transaction
            async with immediate_transaction(conn):
                assert getattr(conn, "_imm_tx_depth", 0) == 1
                # Nested delete_message call must not deadlock or raise 'cannot start transaction'
                await crud.delete_message(conn, msg1.id)
                # Depth must be restored cleanly to 1
                assert getattr(conn, "_imm_tx_depth", 0) == 1

            # Outside outer transaction, depth must be 0
            assert getattr(conn, "_imm_tx_depth", 0) == 0

            # Only msg2 remains
            remaining = await crud.count_session_messages(conn, "sess_del_nested")
            assert remaining == 1
            msgs = await crud.get_recent_messages(conn, "sess_del_nested", limit=10)
            assert msgs[0].id == msg2.id

    @pytest.mark.asyncio
    async def test_delete_message_outer_rollback_preserves_message(self, tmp_path):
        """When an outer transaction fails and rolls back, deletion inside savepoint is undone."""
        db_path = str(tmp_path / "test_del_rollback.db")
        await init_db(db_path)

        async with get_db(db_path) as conn:
            await crud.get_or_create_session(conn, "sess_rollback")
            msg = await crud.add_message(
                conn,
                MessageCreate(
                    session_id="sess_rollback",
                    role="user",
                    content_chinese="待保护消息",
                ),
            )
            assert await crud.count_session_messages(conn, "sess_rollback") == 1

            # Outer transaction with simulated failure after delete_message
            with pytest.raises(RuntimeError, match="Simulated crash during transaction"):
                async with immediate_transaction(conn):
                    await crud.delete_message(conn, msg.id)
                    raise RuntimeError("Simulated crash during transaction")

            # The deletion must have been rolled back
            assert await crud.count_session_messages(conn, "sess_rollback") == 1
            msgs = await crud.get_recent_messages(conn, "sess_rollback", limit=10)
            assert len(msgs) == 1
            assert msgs[0].id == msg.id

    @pytest.mark.asyncio
    async def test_delete_message_concurrent_write_stress(self, tmp_path):
        """Adversarial stress: concurrent writers adding and deleting messages via immediate_transaction."""
        db_path = str(tmp_path / "test_concurrent_del.db")
        await init_db(db_path)

        num_workers = 12
        session_id = "sess_stress"

        async def worker_task(worker_id: int):
            async with get_db(db_path) as conn:
                for i in range(5):
                    # Add message
                    msg = await crud.add_message(
                        conn,
                        MessageCreate(
                            session_id=session_id,
                            role="user",
                            content_chinese=f"worker_{worker_id}_msg_{i}",
                        ),
                    )
                    # Delete message
                    await crud.delete_message(conn, msg.id)
                    await asyncio.sleep(0.001)

        # Run concurrent workers
        await asyncio.gather(*(worker_task(w) for w in range(num_workers)))

        # Final verification: all messages were created and deleted cleanly without locks/corruption
        async with get_db(db_path) as conn:
            count = await crud.count_session_messages(conn, session_id)
            assert count == 0

    @pytest.mark.asyncio
    async def test_concurrent_burst_deleting_same_message(self, tmp_path):
        """Twenty tasks delete one message id. The row is gone and none of the tasks raise."""
        db_path = str(tmp_path / "same_msg_race.db")
        await init_db(db_path)

        async with get_db(db_path) as conn:
            await crud.get_or_create_session(conn, "race_session")
            msg = await crud.add_message(
                conn,
                MessageCreate(session_id="race_session", role="user", content_chinese="race_target"),
            )
            msg_id = msg.id

        async def worker():
            async with get_db(db_path) as c:
                await crud.delete_message(c, msg_id)

        await asyncio.gather(*(worker() for _ in range(20)))

        async with get_db(db_path) as conn:
            assert await crud.count_session_messages(conn, "race_session") == 0

    @pytest.mark.asyncio
    async def test_deeply_nested_savepoints_and_partial_rollback(self, tmp_path):
        """An inner failure restores only the inner deletes. The outer delete stays committed."""
        db_path = str(tmp_path / "deep_nest.db")
        await init_db(db_path)

        async with get_db(db_path) as conn:
            await crud.get_or_create_session(conn, "sess_nest")
            created_ids = []
            for i in range(8):
                m = await crud.add_message(
                    conn,
                    MessageCreate(session_id="sess_nest", role="user", content_chinese=f"nest_msg_{i}"),
                )
                created_ids.append(m.id)

            async def recursive_step(level: int):
                if level == 8:
                    await crud.delete_message(conn, created_ids[0])
                    await crud.delete_message(conn, created_ids[1])
                    raise RuntimeError("Innermost failure trigger")

                async with immediate_transaction(conn):
                    assert getattr(conn, "_imm_tx_depth", 0) == level + 1
                    await recursive_step(level + 1)

            async with immediate_transaction(conn):
                await crud.delete_message(conn, created_ids[7])
                try:
                    await recursive_step(1)
                except RuntimeError:
                    pass

            assert getattr(conn, "_imm_tx_depth", 0) == 0

            msgs = await crud.get_recent_messages(conn, "sess_nest", limit=20)
            remaining_ids = {m.id for m in msgs}
            assert created_ids[7] not in remaining_ids
            assert created_ids[0] in remaining_ids
            assert created_ids[1] in remaining_ids
            assert len(remaining_ids) == 7

    @pytest.mark.asyncio
    async def test_concurrent_connections_burst_load(self, tmp_path):
        """Twenty workers each keep the odd messages. Integrity stays ok and 80 rows remain."""
        db_path = str(tmp_path / "burst_load.db")
        await init_db(db_path)

        session_id = "sess_burst"

        async def worker_burst(worker_idx: int):
            async with get_db(db_path) as c:
                for i in range(8):
                    msg = await crud.add_message(
                        c,
                        MessageCreate(
                            session_id=session_id,
                            role="user",
                            content_chinese=f"w{worker_idx}_m{i}",
                        ),
                    )
                    if i % 2 == 0:
                        await crud.delete_message(c, msg.id)

        await asyncio.gather(*(worker_burst(i) for i in range(20)))

        async with get_db(db_path) as conn:
            count = await crud.count_session_messages(conn, session_id)
            assert count == 80
            cursor = await conn.execute("PRAGMA integrity_check;")
            row = await cursor.fetchone()
            assert row[0] == "ok"

    @pytest.mark.asyncio
    async def test_task_cancellation_during_delete_message(self, tmp_path):
        """Cancellation during begin, lock retry, and the body restores depth and the row."""
        import sqlite3

        db_path = str(tmp_path / "cancel_tx.db")
        await init_db(db_path)

        async with get_db(db_path) as conn:
            await crud.get_or_create_session(conn, "sess_cancel")
            msg = await crud.add_message(
                conn,
                MessageCreate(session_id="sess_cancel", role="user", content_chinese="cancel_me"),
            )

            async def cancel_at_begin():
                async with immediate_transaction(conn):
                    await crud.delete_message(conn, msg.id)
                    await asyncio.sleep(5)

            t0 = asyncio.create_task(cancel_at_begin())
            await asyncio.sleep(0)
            t0.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t0

            assert getattr(conn, "_imm_tx_depth", 0) == 0
            assert getattr(conn, "_imm_tx_lock_owner", None) is None
            assert await crud.count_session_messages(conn, "sess_cancel") == 1

            orig_execute = conn.execute
            begin_started = asyncio.Event()

            async def failing_begin_execute(sql, *args, **kwargs):
                if str(sql).strip().upper().startswith("BEGIN IMMEDIATE"):
                    begin_started.set()
                    await asyncio.sleep(0.05)
                    raise sqlite3.OperationalError("database is locked")
                return await orig_execute(sql, *args, **kwargs)

            conn.execute = failing_begin_execute
            try:
                async def cancel_on_locked_begin():
                    async with immediate_transaction(conn):
                        await crud.delete_message(conn, msg.id)

                t_locked = asyncio.create_task(cancel_on_locked_begin())
                await begin_started.wait()
                t_locked.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await t_locked
            finally:
                conn.execute = orig_execute

            assert getattr(conn, "_imm_tx_depth", 0) == 0
            assert getattr(conn, "_imm_tx_lock_owner", None) is None

            entered_body = asyncio.Event()

            async def cancel_mid_body():
                async with immediate_transaction(conn):
                    await crud.delete_message(conn, msg.id)
                    entered_body.set()
                    await asyncio.sleep(5)

            t = asyncio.create_task(cancel_mid_body())
            await entered_body.wait()
            t.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t

            assert getattr(conn, "_imm_tx_depth", 0) == 0
            assert getattr(conn, "_imm_tx_lock_owner", None) is None
            assert await crud.count_session_messages(conn, "sess_cancel") == 1

            await crud.delete_message(conn, msg.id)
            assert await crud.count_session_messages(conn, "sess_cancel") == 0


class TestImmediateTransactionSafety:
    """Validates immediate_transaction under concurrency and nested transactions."""

    async def test_immediate_transaction_nested_in_active_transaction(self, tmp_path):
        """Nested immediate_transaction should not fail with 'cannot start transaction'."""
        db_file = tmp_path / "nested_tx.db"
        await init_db(str(db_file))

        async with get_db(str(db_file)) as conn:
            # Simulate an existing deferred transaction
            await conn.execute("INSERT INTO settings (id, active_provider_id) VALUES (1, 'deepseek') ON CONFLICT(id) DO UPDATE SET active_provider_id='deepseek';")
            assert conn.in_transaction or getattr(getattr(conn, "_conn", None), "in_transaction", False)

            # Enter immediate_transaction while already in transaction
            async with immediate_transaction(conn):
                await conn.execute("UPDATE settings SET active_provider_id='openai' WHERE id=1;")

        # Verify change was committed
        async with get_db(str(db_file)) as conn:
            cur = await conn.execute("SELECT active_provider_id FROM settings WHERE id=1;")
            row = await cur.fetchone()
            assert row[0] == "openai"


class TestImmediateTransactionCancellationRollback:
    """Verifies that immediate_transaction cleanly rolls back when cancelled."""

    @pytest.mark.asyncio
    async def test_immediate_transaction_rollback_on_cancelled_error(self, tmp_path):
        db_path = tmp_path / "tx_test.db"
        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.execute("CREATE TABLE test_tbl (id INT PRIMARY KEY, val TEXT);")
            await conn.commit()

            async def cancelled_task():
                async with immediate_transaction(conn):
                    await conn.execute("INSERT INTO test_tbl VALUES (1, 'should_rollback');")
                    raise asyncio.CancelledError()

            with pytest.raises(asyncio.CancelledError):
                await cancelled_task()

            cursor = await conn.execute("SELECT COUNT(*) FROM test_tbl;")
            row = await cursor.fetchone()
            assert row[0] == 0

            async with immediate_transaction(conn):
                await conn.execute("INSERT INTO test_tbl VALUES (2, 'committed');")

            cursor = await conn.execute("SELECT val FROM test_tbl WHERE id = 2;")
            row = await cursor.fetchone()
            assert row[0] == "committed"
