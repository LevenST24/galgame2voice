"""
Database session and connection management for galgame2voice.
Enforces WAL mode, foreign keys, and async connection management via aiosqlite.
"""

import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional, Union
import aiosqlite


DEFAULT_DB_PATH = "data/galgame2voice.db"
_init_lock = asyncio.Lock()


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


async def configure_connection(conn: aiosqlite.Connection) -> None:
    """Configure SQLite pragmas for performance and data integrity."""
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode = WAL;")
    await conn.execute("PRAGMA foreign_keys = ON;")
    await conn.execute("PRAGMA busy_timeout = 5000;")
    await conn.execute("PRAGMA synchronous = NORMAL;")


@asynccontextmanager
async def get_db(db_path: Optional[Union[str, Path]] = None) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager yielding an active, configured aiosqlite connection."""
    resolved_path = str(db_path) if db_path is not None else get_database_path()
    parent_dir = os.path.dirname(os.path.abspath(resolved_path))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    async with aiosqlite.connect(resolved_path, timeout=30.0) as conn:
        await configure_connection(conn)
        yield conn


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
