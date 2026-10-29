"""Synchronous notifications owned by a connection's immediate transaction.

Savepoint rollback removes its own notifications. Only the durable outer commit
publishes them; notification errors cannot undo or misreport a successful write.
"""

import asyncio
from collections.abc import Callable
import logging
from typing import Hashable

import aiosqlite

logger = logging.getLogger(__name__)


def callback_mark(conn: aiosqlite.Connection) -> int:
    return len(getattr(conn, "_after_commit_callbacks", ()))


def after_commit(conn: aiosqlite.Connection, callback: Callable[[], None], *, key: Hashable | None = None) -> None:
    if getattr(conn, "_imm_tx_lock_owner", None) is not asyncio.current_task() or not getattr(conn, "_imm_tx_depth", 0):
        raise RuntimeError("after_commit requires the current task's immediate transaction")
    callbacks = getattr(conn, "_after_commit_callbacks", None)
    if callbacks is None:
        callbacks = conn._after_commit_callbacks = []
    callbacks.append((key, callback))


def discard_callbacks(conn: aiosqlite.Connection, mark: int) -> None:
    callbacks = getattr(conn, "_after_commit_callbacks", None)
    if callbacks is not None:
        del callbacks[mark:]


def notify_committed(conn: aiosqlite.Connection) -> None:
    callbacks = getattr(conn, "_after_commit_callbacks", ())
    conn._after_commit_callbacks = []
    notified = set()
    for key, callback in callbacks:
        if key is not None:
            if key in notified:
                continue
            notified.add(key)
        try:
            callback()
        except Exception:
            logger.exception("Post-commit notification failed; database changes remain committed")
