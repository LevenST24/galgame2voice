"""A retired TTS job must never untrack or cancel a newer job with the same ID."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from galgame2voice.services import tts_service
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_scheduler import TtsScheduler


@pytest.fixture
async def scheduler():
    owner = TtsScheduler()
    try:
        yield owner
    finally:
        await owner.aclose()


@pytest.fixture
async def retiring(scheduler):
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def old_job():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    caller = asyncio.create_task(scheduler.schedule(old_job, task_id="shared", generation_id="old"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert scheduler.cancel_task("shared")
        await asyncio.wait_for(cleaning.wait(), 1)
        with pytest.raises(asyncio.CancelledError):
            await caller
        yield release
    finally:
        release.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.parametrize("kind", ["bytes", "stream"])
async def test_live_duplicate_id_is_rejected_without_replacing_the_original(scheduler, kind):
    entered, release = asyncio.Event(), asyncio.Event()

    async def first():
        entered.set()
        await release.wait()

    async def unused_stream():
        yield b"must not execute"

    caller = asyncio.create_task(scheduler.schedule(first, task_id="shared", generation_id="first"))
    stream = scheduler.schedule_stream(unused_stream, task_id="shared", generation_id="second")
    try:
        await asyncio.wait_for(entered.wait(), 1)
        duplicate = (
            scheduler.schedule(lambda: asyncio.sleep(0), task_id="shared", generation_id="second")
            if kind == "bytes"
            else anext(stream)
        )
        with pytest.raises(ValueError, match="already|duplicate|标识已占用"):
            await asyncio.wait_for(duplicate, 1)
        assert scheduler.cancel_generation("second") == 0
        assert scheduler.cancel_generation("first") == 1
        with pytest.raises(asyncio.CancelledError):
            await caller
    finally:
        release.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        await stream.aclose()


@pytest.mark.parametrize("same_generation", [False, True])
@pytest.mark.parametrize("cancel_by", ["task", "generation"])
async def test_cancelled_owner_cleanup_cannot_remove_a_reused_id(scheduler, retiring, same_generation, cancel_by):
    entered = asyncio.Event()
    generation = "old" if same_generation else "new"

    async def replacement():
        entered.set()
        await asyncio.Event().wait()

    caller = asyncio.create_task(scheduler.schedule(replacement, task_id="shared", generation_id=generation))
    try:
        await asyncio.sleep(0)
        retiring.set()
        await asyncio.wait_for(entered.wait(), 1)
        if cancel_by == "task":
            assert scheduler.cancel_task("shared") is True
        else:
            assert scheduler.cancel_generation(generation) == 1
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 1)
    finally:
        retiring.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)


async def test_cancel_old_generation_cannot_cancel_a_replacement_from_another(scheduler, retiring):
    entered, release = asyncio.Event(), asyncio.Event()

    async def replacement():
        entered.set()
        await release.wait()
        return "new voice"

    caller = asyncio.create_task(scheduler.schedule(replacement, task_id="shared", generation_id="new"))
    try:
        await asyncio.sleep(0)
        assert scheduler.cancel_generation("old") == 0
        assert not caller.done()
        retiring.set()
        await asyncio.wait_for(entered.wait(), 1)
        release.set()
        assert await asyncio.wait_for(caller, 1) == "new voice"
    finally:
        retiring.set()
        release.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.parametrize("kind", ["bytes", "file"])
async def test_same_round_same_sentence_has_independently_cancellable_jobs(
    scheduler, configured_voice, mock_gpt_sovits, monkeypatch, kind
):
    client = GptSovitsClient(server=mock_gpt_sovits)
    entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def synthesize(*args, **kwargs):
        entered.set()
        await release.wait()
        return b"synthetic audio"

    monkeypatch.setattr(client, "synthesize", AsyncMock(side_effect=synthesize))
    monkeypatch.setattr(tts_service, "get_tts_scheduler", lambda: scheduler)
    sequence = 0

    def next_uuid():
        nonlocal sequence
        sequence += 1
        return SimpleNamespace(hex=f"feed{sequence:028x}")

    monkeypatch.setattr(tts_service.uuid, "uuid4", next_uuid)
    enqueue = scheduler._enqueue_task
    accepted = []

    async def observe(task):
        await enqueue(task)
        accepted.append(task.task_id)
        if len(accepted) == 2:
            queued.set()

    monkeypatch.setattr(scheduler, "_enqueue_task", observe)
    service = tts_service.TtsService(client=client)
    call = service.synthesize if kind == "bytes" else service.synthesize_to_file
    options = {"voice_profile_id": configured_voice.id, "_generation_id": "same-round"}
    first = asyncio.create_task(call("同じ言葉。", options=options))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(call("同じ言葉。", options=options))
        await asyncio.wait_for(queued.wait(), 2)
        assert scheduler.cancel_generation("same-round") == 2
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
    finally:
        release.set()
        for task in [first, second]:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in [first, second] if task is not None), return_exceptions=True)
        await scheduler.aclose()
        await client.aclose()
