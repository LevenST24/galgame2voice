"""Owned task cleanup works on supported runtimes and survives repeated cancel."""

import asyncio

import pytest


async def test_close_running_job_on_the_supported_python_runtime():
    from galgame2voice.services.telemetry_jobs import TelemetryJobs

    owner = TelemetryJobs()
    started, closed, workers = asyncio.Event(), asyncio.Event(), []
    async def work():
        workers.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    reader = asyncio.create_task(owner.run("pending", work))
    try:
        await started.wait()
        await owner.aclose()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert closed.is_set()
    finally:
        for task in [reader, *workers]:
            if not task.done():
                task.cancel()
        await asyncio.gather(reader, *workers, return_exceptions=True)


async def test_repeated_parent_cancellation_does_not_interrupt_child_cleanup():
    from galgame2voice.utils.async_tasks import cancel_and_drain

    started, cleaning, release, closed = (asyncio.Event() for _ in range(4))
    async def child():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            closed.set()
    task = asyncio.create_task(child())
    await started.wait()
    owner = asyncio.create_task(cancel_and_drain([task]))
    try:
        await cleaning.wait()
        owner.cancel()
        await asyncio.sleep(0)
        owner.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not closed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert task.cancelled() and closed.is_set()
    finally:
        release.set()
        await asyncio.gather(owner, task, return_exceptions=True)


async def test_failed_owned_group_waits_for_sibling_cleanup():
    from galgame2voice.utils.async_tasks import gather_owned

    started, fail_now, cleaning, release, closed = (asyncio.Event() for _ in range(5))
    async def failing():
        await fail_now.wait()
        raise ValueError("synthetic diagnostic failure")
    async def sibling():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            closed.set()
    tasks = [asyncio.create_task(failing()), asyncio.create_task(sibling())]
    owner = asyncio.create_task(gather_owned(tasks))
    try:
        await started.wait()
        fail_now.set()
        await asyncio.wait_for(cleaning.wait(), 3)
        assert not owner.done()
        release.set()
        with pytest.raises(ValueError, match="synthetic diagnostic"):
            await owner
        assert closed.is_set() and tasks[1].cancelled()
    finally:
        fail_now.set()
        release.set()
        await asyncio.gather(owner, *tasks, return_exceptions=True)


async def test_protected_cleanup_retrieves_failure_before_propagating_cancellation():
    from galgame2voice.utils.async_tasks import await_protected

    started, release = asyncio.Event(), asyncio.Event()
    async def operation():
        started.set()
        await release.wait()
        raise OSError("synthetic cleanup failure")
    worker = asyncio.create_task(operation())
    owner = asyncio.create_task(await_protected(worker))
    try:
        await started.wait()
        owner.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert isinstance(worker.exception(), OSError)
    finally:
        release.set()
        await asyncio.gather(owner, worker, return_exceptions=True)
