"""Chat turns that are still streaming, so the launcher can settle them before exit.

Closing the Windows console window ends the process about five seconds after
Windows reports the close. Without a request to stop, a turn in progress never
reaches its teardown, and the lines the player already saw are never saved.
"""

from __future__ import annotations

import asyncio
import itertools
import threading

_changed = threading.Condition()
_turns: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}
_tokens = itertools.count()


def begin_turn(cancel_event: asyncio.Event) -> int:
    """Record a streaming turn; call from the event loop that serves it."""
    loop = asyncio.get_running_loop()
    with _changed:
        token = next(_tokens)
        _turns[token] = (loop, cancel_event)
    return token


def end_turn(token: int) -> None:
    """Forget a turn once its history has been settled."""
    with _changed:
        _turns.pop(token, None)
        _changed.notify_all()


def stop_active_turns(timeout: float) -> bool:
    """Stop every streaming turn and wait until each has saved its history.

    Call from a thread other than the event loop. Returns False when a turn
    was still settling after ``timeout`` seconds.
    """
    with _changed:
        turns = list(_turns.values())
    for loop, cancel_event in turns:
        try:
            loop.call_soon_threadsafe(cancel_event.set)
        except RuntimeError:
            pass  # The loop already closed; nothing left to stop there.
    with _changed:
        return _changed.wait_for(lambda: not _turns, timeout)
