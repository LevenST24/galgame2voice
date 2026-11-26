"""A cleanup interval of 0 or less no longer makes the cleanup loop spin."""

import asyncio
import logging

import pytest

from galgame2voice.services import audio_cleaner


@pytest.mark.parametrize("configured", [0, -5])
def test_zero_or_negative_interval_does_not_rerun_cleanup_without_pause(monkeypatch, tmp_path, configured):
    cycles = 0

    async def fake_cycle(audio_dir):
        nonlocal cycles
        cycles += 1

    monkeypatch.setattr(audio_cleaner, "_run_audio_cleanup_cycle", fake_cycle)

    async def run_briefly():
        task = asyncio.create_task(audio_cleaner._audio_cleanup_loop(tmp_path, configured))
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run_briefly())

    assert cycles == 0


def test_small_interval_is_raised_to_a_minute_with_one_warning(caplog):
    audio_cleaner._cleanup_interval_seconds.cache_clear()
    with caplog.at_level(logging.WARNING, logger="galgame2voice.services.audio_cleaner"):
        assert audio_cleaner._cleanup_interval_seconds(0) == 60
        assert audio_cleaner._cleanup_interval_seconds(0) == 60
    warnings = [r for r in caplog.records if "AUDIO_CLEANUP_INTERVAL_SECONDS" in r.getMessage()]
    assert len(warnings) == 1


def test_normal_interval_is_kept():
    assert audio_cleaner._cleanup_interval_seconds(600) == 600
    assert audio_cleaner._cleanup_interval_seconds(60) == 60
