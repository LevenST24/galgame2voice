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
from typing import AsyncGenerator

import aiosqlite

from galgame2voice.database.commit_hooks import callback_mark, discard_callbacks, notify_committed

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


async def configure_connection(conn: aiosqlite.Connection, resolved_path: str | None = None) -> None:
    """Configure SQLite pragmas for performance and data integrity."""
    conn.row_factory = aiosqlite.Row
    key = await asyncio.to_thread(os.path.abspath, resolved_path) if resolved_path else None
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
async def get_db(db_path: str | Path | None = None) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager yielding an active, configured aiosqlite connection."""
    resolved_path = str(db_path) if db_path is not None else get_database_path()
    def prepare_path() -> str:
        absolute_path = os.path.abspath(resolved_path)
        parent_dir = os.path.dirname(absolute_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        return os.path.normcase(absolute_path)
    normalized_path = await asyncio.to_thread(prepare_path)
    async with aiosqlite.connect(resolved_path, timeout=30.0) as conn:
        conn._db_path = normalized_path
        await configure_connection(conn, resolved_path)
        yield conn


async def _run_uncancellable(coro):
    """
    Awaits an aiosqlite coroutine to completion on the worker thread even if the
    caller task is cancelled. Returns (result, exc, cancelled).
    """
    task = asyncio.ensure_future(coro)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    if task.cancelled():
        return None, asyncio.CancelledError(), True
    exc = task.exception()
    if exc is not None:
        return None, exc, cancelled
    return task.result(), None, cancelled


class _ImmediateTransactionContext:
    """
    Async context manager implementing immediate_transaction without @asynccontextmanager
    generator yield boundary races on task cancellation.
    """

    __slots__ = (
        "conn",
        "max_retries",
        "base_delay",
        "_conn_lock",
        "_write_lock",
        "_prev_depth",
        "_nested",
        "_sp_id",
        "_callback_mark",
    )

    def __init__(
        self,
        conn: aiosqlite.Connection,
        max_retries: int = 10,
        base_delay: float = 0.01,
    ) -> None:
        self.conn = conn
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._conn_lock: asyncio.Lock | None = None
        self._write_lock: asyncio.Lock | None = None
        self._prev_depth: int = 0
        self._nested: bool = False
        self._sp_id: str | None = None
        self._callback_mark = 0

    async def __aenter__(self) -> aiosqlite.Connection:
        conn = self.conn
        current_task = asyncio.current_task()
        if getattr(conn, "_imm_tx_lock_owner", None) is not current_task:
            conn_lock = _get_conn_imm_tx_lock(conn)
            await conn_lock.acquire()
            conn._imm_tx_lock_owner = current_task
            self._conn_lock = conn_lock

        try:
            depth = getattr(conn, "_imm_tx_depth", 0)
            self._prev_depth = depth
            self._callback_mark = callback_mark(conn)
            is_in_tx = (
                getattr(conn, "in_transaction", False)
                or getattr(getattr(conn, "_conn", None), "in_transaction", False)
            )

            if depth > 0:
                self._nested = True
                conn._imm_tx_depth = depth + 1
                sp_id = f"sp_{uuid.uuid4().hex[:8]}"
                self._sp_id = sp_id
                sp_started = False
                try:
                    _, exc, cancelled = await _run_uncancellable(conn.execute(f"SAVEPOINT {sp_id};"))
                    if exc is not None:
                        if cancelled:
                            raise asyncio.CancelledError() from exc
                        raise exc
                    sp_started = True
                    if cancelled:
                        raise asyncio.CancelledError()
                except BaseException:
                    if sp_started:
                        await _run_uncancellable(conn.execute(f"ROLLBACK TO SAVEPOINT {sp_id};"))
                        await _run_uncancellable(conn.execute(f"RELEASE SAVEPOINT {sp_id};"))
                    conn._imm_tx_depth = depth
                    raise
                return conn

            # Outermost immediate_transaction (depth == 0)
            self._nested = False
            db_path = getattr(conn, "_db_path", None) or "default"
            write_lock = _get_db_write_lock(db_path)
            await write_lock.acquire()
            self._write_lock = write_lock
            conn._imm_tx_depth = 1
            tx_started = False
            try:
                if is_in_tx:
                    sp_id = f"sp_{uuid.uuid4().hex[:8]}"
                    self._sp_id = sp_id
                    _, exc, cancelled = await _run_uncancellable(conn.execute(f"SAVEPOINT {sp_id};"))
                    if exc is not None:
                        if cancelled:
                            raise asyncio.CancelledError() from exc
                        raise exc
                    tx_started = True
                    if cancelled:
                        raise asyncio.CancelledError()
                else:
                    for attempt in range(self.max_retries):
                        _, exc, cancelled = await _run_uncancellable(conn.execute("BEGIN IMMEDIATE;"))
                        if exc is None:
                            tx_started = True
                            if cancelled:
                                raise asyncio.CancelledError()
                            break
                        if cancelled:
                            raise asyncio.CancelledError() from exc
                        if isinstance(exc, (sqlite3.OperationalError, aiosqlite.OperationalError)):
                            err_msg = str(exc).lower()
                            if ("locked" in err_msg or "busy" in err_msg) and attempt < self.max_retries - 1:
                                await asyncio.sleep(self.base_delay * (2 ** min(attempt, 5)) + random.uniform(0.005, 0.02))
                                continue
                        raise exc
            except BaseException:
                if tx_started:
                    if self._sp_id:
                        await _run_uncancellable(conn.execute(f"ROLLBACK TO SAVEPOINT {self._sp_id};"))
                        await _run_uncancellable(conn.execute(f"RELEASE SAVEPOINT {self._sp_id};"))
                    else:
                        await _run_uncancellable(conn.rollback())
                conn._imm_tx_depth = depth
                write_lock.release()
                self._write_lock = None
                raise
            return conn
        except BaseException:
            if self._conn_lock is not None:
                conn._imm_tx_lock_owner = None
                self._conn_lock.release()
                self._conn_lock = None
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        conn = self.conn
        try:
            if self._nested:
                sp_id = self._sp_id
                try:
                    if exc_type is None:
                        _, exc, cancelled = await _run_uncancellable(conn.execute(f"RELEASE SAVEPOINT {sp_id};"))
                        if exc is not None:
                            await _run_uncancellable(conn.execute(f"ROLLBACK TO SAVEPOINT {sp_id};"))
                            await _run_uncancellable(conn.execute(f"RELEASE SAVEPOINT {sp_id};"))
                            discard_callbacks(conn, self._callback_mark)
                            if cancelled:
                                raise asyncio.CancelledError() from exc
                            raise exc
                        if cancelled:
                            raise asyncio.CancelledError()
                    else:
                        discard_callbacks(conn, self._callback_mark)
                        _, _, c1 = await _run_uncancellable(conn.execute(f"ROLLBACK TO SAVEPOINT {sp_id};"))
                        _, _, c2 = await _run_uncancellable(conn.execute(f"RELEASE SAVEPOINT {sp_id};"))
                        if (c1 or c2) and exc_type is not asyncio.CancelledError:
                            raise asyncio.CancelledError()
                finally:
                    conn._imm_tx_depth = self._prev_depth
                return False

            # Outermost immediate_transaction exit
            try:
                sp_id = self._sp_id
                if exc_type is None:
                    tx_committed = False
                    sp_released = False
                    try:
                        if sp_id:
                            _, exc1, c1 = await _run_uncancellable(conn.execute(f"RELEASE SAVEPOINT {sp_id};"))
                            if exc1 is not None:
                                if c1:
                                    raise asyncio.CancelledError() from exc1
                                raise exc1
                            sp_released = True
                            _, exc2, c2 = await _run_uncancellable(conn.commit())
                            if exc2 is not None:
                                if c1 or c2:
                                    raise asyncio.CancelledError() from exc2
                                raise exc2
                            tx_committed = True
                            notify_committed(conn)
                            if c1 or c2:
                                raise asyncio.CancelledError()
                        else:
                            _, exc, cancelled = await _run_uncancellable(conn.commit())
                            if exc is not None:
                                if cancelled:
                                    raise asyncio.CancelledError() from exc
                                raise exc
                            tx_committed = True
                            notify_committed(conn)
                            if cancelled:
                                raise asyncio.CancelledError()
                    except BaseException:
                        if not tx_committed:
                            discard_callbacks(conn, self._callback_mark)
                            if sp_id and not sp_released:
                                await _run_uncancellable(conn.execute(f"ROLLBACK TO SAVEPOINT {sp_id};"))
                                await _run_uncancellable(conn.execute(f"RELEASE SAVEPOINT {sp_id};"))
                            else:
                                await _run_uncancellable(conn.rollback())
                        raise
                else:
                    discard_callbacks(conn, self._callback_mark)
                    if sp_id:
                        _, _, c1 = await _run_uncancellable(conn.execute(f"ROLLBACK TO SAVEPOINT {sp_id};"))
                        _, _, c2 = await _run_uncancellable(conn.execute(f"RELEASE SAVEPOINT {sp_id};"))
                        if (c1 or c2) and exc_type is not asyncio.CancelledError:
                            raise asyncio.CancelledError()
                    else:
                        _, _, c_rb = await _run_uncancellable(conn.rollback())
                        if c_rb and exc_type is not asyncio.CancelledError:
                            raise asyncio.CancelledError()
            finally:
                conn._imm_tx_depth = self._prev_depth
                if self._write_lock is not None:
                    self._write_lock.release()
                    self._write_lock = None
            return False
        finally:
            if self._conn_lock is not None:
                conn._imm_tx_lock_owner = None
                self._conn_lock.release()
                self._conn_lock = None


def immediate_transaction(
    conn: aiosqlite.Connection,
    max_retries: int = 10,
    base_delay: float = 0.01,
) -> _ImmediateTransactionContext:
    """
    Begins an IMMEDIATE transaction with automatic exponential backoff retry on SQLite
    busy/locked errors, eliminating lock upgrade deadlocks in WAL mode.
    Supports nested calls via SQL savepoints for full transactional atomicity and rollback safety.
    """
    return _ImmediateTransactionContext(conn, max_retries=max_retries, base_delay=base_delay)


async def get_schema_version(conn: aiosqlite.Connection) -> int:
    """Returns the current database schema version via SQLite PRAGMA user_version."""
    cursor = await conn.execute("PRAGMA user_version;")
    row = await cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def set_schema_version(conn: aiosqlite.Connection, version: int) -> None:
    """Sets the database schema version via SQLite PRAGMA user_version."""
    await conn.execute(f"PRAGMA user_version = {int(version)};")


def _backup_database_sync(resolved_path: Path) -> None:
    """Back up committed WAL contents before migration, or once per day."""
    from contextlib import closing
    from galgame2voice.database.backups import _open_readonly, create_database_backup
    from galgame2voice.database.migrations import CURRENT_SCHEMA_VERSION

    if not resolved_path.is_file() or resolved_path.stat().st_size == 0:
        return
    # An invalid database must fail startup visibly, never replace a good backup.
    with closing(_open_readonly(resolved_path)) as reader:
        version = int(reader.execute("PRAGMA user_version").fetchone()[0])
    if version > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库来自较新的版本（数据库版本 {version}，当前程序最高支持 {CURRENT_SCHEMA_VERSION}），"
            "请使用对应的新版本程序，原数据已保留。"
        )
    create_database_backup(resolved_path, required=version < CURRENT_SCHEMA_VERSION)


async def init_db(db_path: str | Path | None = None) -> None:
    """Initialize schema with concurrency guards and a threaded pre-migration backup."""
    from galgame2voice.database.crud import init_schema_and_seeds

    async with _init_lock:
        await asyncio.to_thread(_backup_database_sync, Path(db_path or get_database_path()))
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

