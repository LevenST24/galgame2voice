"""
Asynchronous task management and background worker utilities for galgame2voice.
"""

from __future__ import annotations

import asyncio
from typing import Collection


async def await_protected(awaitable):
    """Finish owned cleanup before propagating cancellation (also on Python 3.10).

    shield alone leaves the caller immediately. Keep a strong reference and
    retrieve the result even when cancellation arrives repeatedly during cleanup.
    """
    future = asyncio.ensure_future(awaitable)
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    try:
        result = future.result()
    except BaseException as exc:
        if cancelled:
            raise asyncio.CancelledError() from exc
        raise
    if cancelled:
        raise asyncio.CancelledError()
    return result


async def cancel_and_drain(tasks: Collection[asyncio.Task]) -> None:
    """Cancel owned tasks once; repeated caller cancellation cannot cut cleanup short."""
    owned = tuple(tasks)
    for task in owned:
        if not task.done():
            task.cancel()
    if owned:
        await await_protected(asyncio.gather(*owned, return_exceptions=True))


async def gather_owned(tasks: Collection[asyncio.Task]):
    """Gather request-owned work, settling all siblings on failure or cancellation."""
    owned = tuple(tasks)
    joined = asyncio.gather(*owned)

    def completed(done):
        if not done.cancelled():
            done.exception()

    joined.add_done_callback(completed)
    try:
        # The finally block is the sole cancellation owner of the children.
        return await asyncio.shield(joined)
    finally:
        await cancel_and_drain(owned)


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
    "await_protected",
    "cancel_and_drain",
    "gather_owned",
    "drain_background_tasks",
]
