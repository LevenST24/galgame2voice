"""
Background task ownership regression tests.

A coroutine spawned and never awaited must still have its exception retrieved.
Otherwise the failure is deferred to garbage-collection time, where the
interpreter reports a context-free "Task exception was never retrieved" and the
original cause is lost. Two services used to keep only a strong reference and
discard the task, so these tests pin both the logging behaviour and the absence
of the deferred-report leak.
"""

import asyncio
import gc
import logging

from galgame2voice.services.chat_pipelines.stream_coordinator import StreamCoordinator
from galgame2voice.services.voice_manager import VoiceManager


async def _boom(message: str = "boom"):
    raise ValueError(message)


async def _settle(times: int = 3) -> None:
    """Lets scheduled tasks and their done-callbacks run."""
    for _ in range(times):
        await asyncio.sleep(0)


def _unretrieved_reports(loop) -> list:
    """Collects 'exception was never retrieved' reports the loop would emit."""
    reported = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    return reported


async def test_voice_manager_logs_background_failure(caplog):
    manager = VoiceManager.__new__(VoiceManager)
    manager._bg_tasks = set()

    with caplog.at_level(logging.WARNING, logger="galgame2voice.services.voice_manager"):
        task = manager._spawn_background(_boom("warmup exploded"))
        await _settle()

    assert task.done()
    assert isinstance(task.exception(), ValueError)
    assert "warmup exploded" in caplog.text
    assert not manager._bg_tasks, "finished task must not be retained"


async def test_voice_manager_background_failure_is_not_deferred_to_gc():
    loop = asyncio.get_running_loop()
    reported = _unretrieved_reports(loop)

    manager = VoiceManager.__new__(VoiceManager)
    manager._bg_tasks = set()
    manager._spawn_background(_boom("dropped silently"))
    await _settle()
    manager._bg_tasks.clear()

    gc.collect()
    await _settle()

    assert reported == [], f"unretrieved task exception leaked: {reported}"


async def test_stream_coordinator_logs_background_failure(caplog):
    coordinator = StreamCoordinator.__new__(StreamCoordinator)
    coordinator.spawn_background = None

    with caplog.at_level(
        logging.WARNING, logger="galgame2voice.services.chat_pipelines.stream_coordinator"
    ):
        coordinator._spawn_bg(_boom("pipeline exploded"))
        await _settle()

    assert "pipeline exploded" in caplog.text


async def test_stream_coordinator_background_failure_is_not_deferred_to_gc():
    loop = asyncio.get_running_loop()
    reported = _unretrieved_reports(loop)

    coordinator = StreamCoordinator.__new__(StreamCoordinator)
    coordinator.spawn_background = None
    coordinator._spawn_bg(_boom("dropped silently"))
    await _settle()

    gc.collect()
    await _settle()

    assert reported == [], f"unretrieved task exception leaked: {reported}"


async def test_stream_coordinator_defers_to_injected_spawner():
    """A caller-supplied spawner (tests, schedulers) must keep ownership."""
    coordinator = StreamCoordinator.__new__(StreamCoordinator)
    spawned = []
    coordinator.spawn_background = spawned.append

    coro = _boom("handed over")
    coordinator._spawn_bg(coro)
    assert len(spawned) == 1

    coro.close()
