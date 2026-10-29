"""Share bounded diagnostic work without tying it to any one HTTP reader."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Hashable
import weakref

from galgame2voice.utils.async_tasks import cancel_and_drain


class TelemetryJobs:
    """Application-owned in-flight work, isolated between event loops.

    A reader's cancellation leaves the shared measurement running. Successful
    callers can reuse the result; shutdown cancels the owning async jobs before
    the application's executor is drained. Completed jobs are never retained.
    """

    def __init__(self) -> None:
        self._jobs: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._closed_loops: weakref.WeakSet = weakref.WeakSet()

    async def run(self, key: Hashable, operation: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        loop = asyncio.get_running_loop()
        if loop in self._closed_loops:
            raise asyncio.CancelledError()
        jobs = self._jobs.setdefault(loop, {})
        task = jobs.get(key)
        if task is None:
            task = asyncio.create_task(operation())
            jobs[key] = task

            def completed(done):
                if jobs.get(key) is done:
                    jobs.pop(key, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def aclose(self) -> None:
        loop = asyncio.get_running_loop()
        self._closed_loops.add(loop)
        jobs = self._jobs.pop(loop, {})
        tasks = list(jobs.values())
        await cancel_and_drain(tasks)


default_telemetry_jobs = TelemetryJobs()
