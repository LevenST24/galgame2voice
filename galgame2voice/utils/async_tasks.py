"""
Asynchronous task management and background worker utilities for galgame2voice.
"""

from __future__ import annotations

import asyncio
from typing import Collection


async def drain_background_tasks(
    tasks: Collection[asyncio.Task],
    timeout: float = 3.0,
) -> None:
    """
    Gracefully waits for pending background tasks to finish within timeout,
    then cancels any remaining stragglers and collects their completions.
    Clears the collection if it provides a clear() method.
    """
    pending = [t for t in tasks if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    stragglers = [t for t in tasks if not t.done()]
    for t in stragglers:
        t.cancel()
    if stragglers:
        await asyncio.gather(*stragglers, return_exceptions=True)
    if hasattr(tasks, "clear"):
        tasks.clear()


__all__ = [
    "drain_background_tasks",
]
