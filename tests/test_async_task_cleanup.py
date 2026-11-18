"""Owned task cleanup works on supported runtimes and survives repeated cancel."""

import asyncio

import pytest


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
