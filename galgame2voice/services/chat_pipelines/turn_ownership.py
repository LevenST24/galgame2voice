"""Coordinate turns sharing a database and session within this process."""

import asyncio
from functools import wraps
from inspect import signature
import os
from weakref import WeakValueDictionary

from galgame2voice.services.chat_pipelines.stream_coordinator import SseKeepAlive
from galgame2voice.utils.async_tasks import await_protected, cancel_and_drain


_locks: WeakValueDictionary = WeakValueDictionary()
_KEEPALIVE_SECONDS = 5.0


def _turn_lock(db_path, session_id):
    key = (asyncio.get_running_loop(), os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(db_path)))), session_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def session_turn(db_path, session_id):
    """Return the shared in-process lock for one database session."""
    return _turn_lock(db_path, session_id)


def serialize_sync_turn(func):
    @wraps(func)
    async def wrapped(self, prompt, session_id="default", *args, **kwargs):
        async with session_turn(self.db_path, session_id):
            return await func(self, prompt, session_id, *args, **kwargs)
    return wrapped


def serialize_stream_turn(func):
    call_signature = signature(func)

    @wraps(func)
    async def wrapped(self, prompt, session_id="default", *args, **kwargs):
        bound = call_signature.bind(self, prompt, session_id, *args, **kwargs)
        cancel_event = bound.arguments.get("cancel_event")
        if cancel_event is not None and cancel_event.is_set():
            return
        lock = _turn_lock(self.db_path, session_id)
        acquire = asyncio.create_task(lock.acquire())
        cancelled = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        tasks = (acquire, cancelled) if cancelled is not None else (acquire,)
        inner = None
        try:
            while True:
                done, _ = await asyncio.wait(tasks, timeout=_KEEPALIVE_SECONDS, return_when=asyncio.FIRST_COMPLETED)
                if cancelled in done or (cancel_event is not None and cancel_event.is_set()):
                    return
                if acquire in done:
                    await acquire
                    break
                yield SseKeepAlive()
            inner = func(self, prompt, session_id, *args, **kwargs)
            async for event in inner:
                yield event
        finally:
            try:
                if inner is not None:
                    await await_protected(inner.aclose())
            finally:
                try:
                    await cancel_and_drain(tasks)
                finally:
                    if acquire.done() and not acquire.cancelled() and acquire.exception() is None and acquire.result():
                        lock.release()

    return wrapped
