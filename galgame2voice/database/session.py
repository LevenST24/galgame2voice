"""
Database session and connection management for galgame2voice.
Enforces WAL mode, foreign keys, and async connection management via aiosqlite.
"""

import asyncio
import os
import random
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional, Union
import aiosqlite


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



import weakref

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


@asynccontextmanager
async def get_db(db_path: Optional[Union[str, Path]] = None) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager yielding an active, configured aiosqlite connection."""
    resolved_path = str(db_path) if db_path is not None else get_database_path()
    parent_dir = os.path.dirname(os.path.abspath(resolved_path))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    async with aiosqlite.connect(resolved_path, timeout=30.0) as conn:
        setattr(conn, "_db_path", os.path.normcase(os.path.abspath(resolved_path)))
        await configure_connection(conn, resolved_path)
        yield conn


@asynccontextmanager
async def immediate_transaction(
    conn: aiosqlite.Connection,
    max_retries: int = 5,
    base_delay: float = 0.05,
) -> AsyncGenerator[aiosqlite.Connection, None]:
    """
    Begins an IMMEDIATE transaction with automatic exponential backoff retry on SQLite
    busy/locked errors, eliminating lock upgrade deadlocks in WAL mode.
    Supports nested calls via SQL savepoints for full transactional atomicity and rollback safety.
    """
    depth = getattr(conn, "_imm_tx_depth", 0)
    is_in_tx = (
        getattr(conn, "in_transaction", False)
        or getattr(getattr(conn, "_conn", None), "in_transaction", False)
    )

    if depth > 0:
        # Nested transaction: use savepoint so inner transactions roll back independently
        # without committing outer changes prematurely.
        setattr(conn, "_imm_tx_depth", depth + 1)
        sp_id = f"sp_{uuid.uuid4().hex[:8]}"
        await conn.execute(f"SAVEPOINT {sp_id};")
        try:
            yield conn
            await conn.execute(f"RELEASE SAVEPOINT {sp_id};")
        except Exception:
            try:
                await conn.execute(f"ROLLBACK TO SAVEPOINT {sp_id};")
                await conn.execute(f"RELEASE SAVEPOINT {sp_id};")
            except Exception:
                pass
            raise
        finally:
            setattr(conn, "_imm_tx_depth", depth)
        return

    # Outermost immediate_transaction (depth == 0)
    db_path = getattr(conn, "_db_path", None) or "default"
    write_lock = _get_db_write_lock(db_path)
    await write_lock.acquire()
    try:
        setattr(conn, "_imm_tx_depth", 1)
        try:
            sp_id = None
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
                            await asyncio.sleep(base_delay * (2 ** attempt) + random.uniform(0.01, 0.04))
                            continue
                        raise

            try:
                yield conn
                if sp_id:
                    await conn.execute(f"RELEASE SAVEPOINT {sp_id};")
                await conn.commit()
            except Exception:
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
            setattr(conn, "_imm_tx_depth", 0)
    finally:
        write_lock.release()


async def init_db(db_path: Optional[Union[str, Path]] = None) -> None:
    """Initialize database schema, tables, indexes, and seed data with concurrency guards."""
    from galgame2voice.database.crud import init_schema_and_seeds
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

