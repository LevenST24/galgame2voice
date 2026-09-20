"""
Database session and connection management for galgame2voice.
Enforces WAL mode, foreign keys, and async connection management via aiosqlite.
"""

import asyncio
import logging
import os
import random
import sqlite3
import uuid
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional, Union

import aiosqlite

logger = logging.getLogger("galgame2voice.database.session")


DEFAULT_DB_PATH = "data/galgame2voice.db"
_init_lock = asyncio.Lock()
# journal_mode=WAL is persistent in the database file; remember paths already
# in WAL so per-connection setup skips the (lock-taking) SET.
_wal_confirmed_paths: set[str] = set()


def get_database_path() -> str:
    """Resolve database path from environment or defaults."""
    env_path = os.getenv("GALGAME2VOICE_DB_PATH") or os.getenv("GALGAME_DB_PATH")
    if env_path:
        return env_path
    try:
        from galgame2voice.config import get_settings
        return str(get_settings().db_path)
    except Exception:
        return DEFAULT_DB_PATH


async def configure_connection(conn: aiosqlite.Connection, resolved_path: Optional[str] = None) -> None:
    """Configure SQLite pragmas for performance and data integrity."""
    conn.row_factory = aiosqlite.Row
    key = str(os.path.abspath(resolved_path)) if resolved_path else None
    if key is None or key not in _wal_confirmed_paths:
        mode = (await (await conn.execute("PRAGMA journal_mode;")).fetchone())[0]
        if str(mode).lower() != "wal":
            await conn.execute("PRAGMA journal_mode = WAL;")
        if key is not None:
            _wal_confirmed_paths.add(key)
    await conn.executescript(
        "PRAGMA foreign_keys = ON; "
        "PRAGMA busy_timeout = 5000; "
        "PRAGMA synchronous = NORMAL; "
        "PRAGMA cache_size = -64000; "
        "PRAGMA temp_store = MEMORY; "
        "PRAGMA mmap_size = 268435456;"
    )
_loop_db_write_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = weakref.WeakKeyDictionary()

def _get_db_write_lock(db_path: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _loop_db_write_locks.get(loop)
    if locks is None:
        locks = {}
        _loop_db_write_locks[loop] = locks
    norm_key = os.path.normcase(os.path.abspath(db_path)) if db_path and db_path != "default" else db_path
    lock = locks.get(norm_key)
    if lock is None:
        lock = asyncio.Lock()
        locks[norm_key] = lock
    return lock


# Per-connection asyncio.Locks keyed by id(conn), weakly cleaned when the
# connection is garbage collected. Serializes entry/exit of
# immediate_transaction so concurrent tasks sharing one connection cannot
# corrupt the _imm_tx_depth counter (mis-nesting savepoints vs BEGIN IMMEDIATE).
_loop_conn_imm_tx_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


class _ConnImmTxLockFinalizer:
    """Strong-ref free finalizer that drops a connection's lock entry on GC."""

    __slots__ = ("loop_ref", "conn_id")

    def __init__(self, loop: "asyncio.AbstractEventLoop", conn_id: int):
        self.loop_ref = weakref.ref(loop)
        self.conn_id = conn_id

    def __call__(self) -> None:
        # weakref.finalize invokes the callback with no arguments.
        loop = self.loop_ref()
        if loop is None:
            return
        locks = _loop_conn_imm_tx_locks.get(loop)
        if locks is not None:
            locks.pop(self.conn_id, None)


def _get_conn_imm_tx_lock(conn: aiosqlite.Connection) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _loop_conn_imm_tx_locks.get(loop)
    if locks is None:
        locks = {}
        _loop_conn_imm_tx_locks[loop] = locks
    key = id(conn)
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
        # Drop the lock entry when the connection dies so the dict cannot leak.
        weakref.finalize(conn, _ConnImmTxLockFinalizer(loop, key))
    return lock


@asynccontextmanager
async def get_db(db_path: Optional[Union[str, Path]] = None) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager yielding an active, configured aiosqlite connection."""
    resolved_path = str(db_path) if db_path is not None else get_database_path()
    parent_dir = os.path.dirname(os.path.abspath(resolved_path))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    async with aiosqlite.connect(resolved_path, timeout=30.0) as conn:
        conn._db_path = os.path.normcase(os.path.abspath(resolved_path))
        await configure_connection(conn, resolved_path)
        yield conn


@asynccontextmanager
async def immediate_transaction(
    conn: aiosqlite.Connection,
    max_retries: int = 10,
    base_delay: float = 0.01,
) -> AsyncGenerator[aiosqlite.Connection, None]:
    """
    Begins an IMMEDIATE transaction with automatic exponential backoff retry on SQLite
    busy/locked errors, eliminating lock upgrade deadlocks in WAL mode.
    Supports nested calls via SQL savepoints for full transactional atomicity and rollback safety.
    """
    # Reentrancy-aware serialization: the outermost call owns a per-connection
    # asyncio.Lock and releases it when its `async with` block exits. Nested calls
    # made by the SAME task must NOT wait for that lock -- asyncio.Lock is not
    # reentrant and nested immediate_transaction() calls are explicitly supported
    # through SQL savepoints, so waiting would deadlock the task against itself.
    # A DIFFERENT task sharing the same connection still waits.
    #
    # NOTE: the transactional logic below deliberately lives in THIS single async
    # generator instead of being delegated to a helper generator. Driving a helper
    # with `async for` defers its cleanup to the event loop's async-generator
    # finalizer, so the inner `except BaseException` (ROLLBACK TO SAVEPOINT) and
    # the depth restore would run asynchronously -- racing the outer commit and
    # letting rolled-back writes be committed anyway.
    current_task = asyncio.current_task()
    conn_lock: Optional[asyncio.Lock] = None
    if getattr(conn, "_imm_tx_lock_owner", None) is not current_task:
        conn_lock = _get_conn_imm_tx_lock(conn)
        await conn_lock.acquire()
        conn._imm_tx_lock_owner = current_task

    try:
        depth = getattr(conn, "_imm_tx_depth", 0)
        is_in_tx = (
            getattr(conn, "in_transaction", False)
            or getattr(getattr(conn, "_conn", None), "in_transaction", False)
        )

        if depth > 0:
            # Nested transaction: use savepoint so inner transactions roll back independently
            # without committing outer changes prematurely.
            conn._imm_tx_depth = depth + 1
            sp_id = f"sp_{uuid.uuid4().hex[:8]}"
            await conn.execute(f"SAVEPOINT {sp_id};")
            try:
                yield conn
                await conn.execute(f"RELEASE SAVEPOINT {sp_id};")
            except BaseException:
                try:
                    await conn.execute(f"ROLLBACK TO SAVEPOINT {sp_id};")
                    await conn.execute(f"RELEASE SAVEPOINT {sp_id};")
                except Exception:
                    pass
                raise
            finally:
                conn._imm_tx_depth = depth
            return

        # Outermost immediate_transaction (depth == 0)
        db_path = getattr(conn, "_db_path", None) or "default"
        write_lock = _get_db_write_lock(db_path)
        await write_lock.acquire()
        try:
            conn._imm_tx_depth = 1
            try:
                sp_id: Optional[str] = None
                if is_in_tx:
                    # Connection was already in a transaction (e.g. uncommitted raw DML), use savepoint under outermost block
                    sp_id = f"sp_{uuid.uuid4().hex[:8]}"
                    await conn.execute(f"SAVEPOINT {sp_id};")
                else:
                    for attempt in range(max_retries):
                        try:
                            await conn.execute("BEGIN IMMEDIATE;")
                            break
                        except (sqlite3.OperationalError, aiosqlite.OperationalError) as err:
                            err_msg = str(err).lower()
                            if ("locked" in err_msg or "busy" in err_msg) and attempt < max_retries - 1:
                                await asyncio.sleep(base_delay * (2 ** min(attempt, 5)) + random.uniform(0.005, 0.02))
                                continue
                            raise

                try:
                    yield conn
                    if sp_id:
                        await conn.execute(f"RELEASE SAVEPOINT {sp_id};")
                    await conn.commit()
                except BaseException:
                    if sp_id:
                        try:
                            await conn.execute(f"ROLLBACK TO SAVEPOINT {sp_id};")
                            await conn.execute(f"RELEASE SAVEPOINT {sp_id};")
                        except Exception:
                            pass
                    else:
                        try:
                            await conn.rollback()
                        except Exception:
                            pass
                    raise
            finally:
                # Restore the depth observed on entry (always 0 on the outermost
                # branch) instead of hard-resetting, preserving nesting semantics
                # even if the counter was left in an unexpected state.
                conn._imm_tx_depth = depth
        finally:
            write_lock.release()
    finally:
        # Only the lock owner releases it; nested same-task calls skip acquisition.
        if conn_lock is not None:
            conn._imm_tx_lock_owner = None
            conn_lock.release()


async def get_schema_version(conn: aiosqlite.Connection) -> int:
    """Returns the current database schema version via SQLite PRAGMA user_version."""
    cursor = await conn.execute("PRAGMA user_version;")
    row = await cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def set_schema_version(conn: aiosqlite.Connection, version: int) -> None:
    """Sets the database schema version via SQLite PRAGMA user_version."""
    await conn.execute(f"PRAGMA user_version = {int(version)};")


async def init_db(db_path: Optional[Union[str, Path]] = None) -> None:
    """Initialize database schema, tables, indexes, and seed data with concurrency guards and pre-migration backup."""
    from galgame2voice.database.crud import init_schema_and_seeds

    resolved_path = Path(db_path or get_database_path())
    # Automated pre-migration restorable backup
    if resolved_path.exists() and resolved_path.is_file() and resolved_path.stat().st_size > 0:
        try:
            from datetime import datetime
            import shutil
            backup_dir = resolved_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = backup_dir / f"{resolved_path.name}.bak_{ts}"
            shutil.copy2(resolved_path, backup_file)
            # Prune older database backups, retaining the 5 most recent
            backups = sorted(backup_dir.glob(f"{resolved_path.name}.bak_*"), key=lambda p: p.stat().st_mtime)
            while len(backups) > 5:
                backups.pop(0).unlink(missing_ok=True)
            logger.info("Created automated pre-migration database backup: %s", backup_file)
        except Exception as exc:
            logger.warning("Failed creating pre-migration database backup: %s", exc)

    async with _init_lock:
        max_retries = 5
        for attempt in range(max_retries):
            try:
                async with get_db(db_path) as conn:
                    await init_schema_and_seeds(conn)
                break
            except (sqlite3.OperationalError, aiosqlite.OperationalError) as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    if attempt == max_retries - 1:
                        raise
                    await asyncio.sleep(0.05 * (2 ** attempt))
                else:
                    raise

