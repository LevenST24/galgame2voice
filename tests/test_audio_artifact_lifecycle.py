"""Real filesystem and worker-thread races for newly synthesized audio."""

import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from test_tts_cache_io_budget import stack


@pytest.mark.parametrize("mode", ["error", "cancel"])
async def test_failed_or_cancelled_output_never_leaves_a_partial_wav(stack, monkeypatch, mode):
    service, _, _ = stack
    old = service.audio_dir / "old.wav"
    old.write_bytes(b"existing history audio")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_open = Path.open
    class Writer:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            self.handle.close()
            finished.set()
        def write(self, data):
            self.handle.write(data[:3])
            self.handle.flush()
            started.set()
            if mode == "error":
                raise OSError("synthetic disk write failure")
            assert release.wait(5), "test did not release the owned writer"
            self.handle.write(data[3:])
            return len(data)
    def open_output(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if path.parent == service.audio_dir and args and args[0] in {"wb", "xb"}:
            return Writer(handle)
        return handle
    monkeypatch.setattr(Path, "open", open_output)
    operation = asyncio.create_task(service.synthesize_to_file("こんにちは。", {"_pre_resolved": True}, use_cache=False))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        if mode == "cancel":
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, 2)
            assert not release.is_set()
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)
            # The writer closes before its ownership cleanup finally runs.
            for _ in range(100):
                if list(service.audio_dir.glob("*.wav")) == [old]:
                    break
                await asyncio.sleep(0.01)
        else:
            with pytest.raises(OSError, match="synthetic disk"):
                await operation
        assert list(service.audio_dir.glob("*.wav")) == [old]
        assert old.read_bytes() == b"existing history audio"
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)
        await asyncio.to_thread(finished.wait, 5)


async def test_audio_filename_collision_cannot_overwrite_an_existing_file(stack, monkeypatch):
    service, _, _ = stack
    existing = service.audio_dir / "voice_0123456789ab.wav"
    existing.write_bytes(b"previous audio must survive")
    monkeypatch.setattr("galgame2voice.services.tts_service.uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="0123456789abcdef")))
    with pytest.raises(FileExistsError):
        await service.synthesize_to_file("こんにちは。", {"_pre_resolved": True}, use_cache=False)
    assert existing.read_bytes() == b"previous audio must survive"


@pytest.mark.parametrize("location", ["repair_0123456789ab.wav", "voice_0123456789ab.wav", "cache/repair_0123456789ab.wav", "../repair_0123456789ab.wav"])
async def test_rejected_repair_cleanup_is_restricted_to_its_owned_namespace(tmp_path, location):
    from galgame2voice.services.audio_artifacts import discard_repair_audio

    audio = tmp_path / "audio"
    audio.mkdir()
    candidate = audio / location
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"artifact ownership test")
    await discard_repair_audio(audio, candidate)
    assert candidate.exists() is (location != "repair_0123456789ab.wav")


async def test_locked_repair_cleanup_defers_without_replacing_the_original_error(tmp_path, monkeypatch, caplog):
    from galgame2voice.services.audio_artifacts import discard_repair_audio

    audio = tmp_path / "audio"
    audio.mkdir()
    candidate = audio / "repair_0123456789ab.wav"
    candidate.write_bytes(b"unpublished audio")
    real_unlink = Path.unlink
    def denied(path, *args, **kwargs):
        if path == candidate:
            raise PermissionError("synthetic Windows sharing violation")
        return real_unlink(path, *args, **kwargs)
    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", denied)
        await discard_repair_audio(audio, candidate)
    assert candidate.exists() and "Deferred cleanup" in caplog.text
    await discard_repair_audio(audio, candidate)
    assert not candidate.exists()


async def test_cancellation_during_scheduler_result_handoff_discards_the_unreceived_file(stack, monkeypatch):
    from galgame2voice.services.tts_service import get_tts_scheduler

    service, _, _ = stack
    scheduler = get_tts_scheduler()
    original_cleanup = scheduler._cleanup_task_tracking
    operation = None
    def cancel_before_delivery(task):
        original_cleanup(task)
        operation.cancel()
    monkeypatch.setattr(scheduler, "_cleanup_task_tracking", cancel_before_delivery)
    operation = asyncio.create_task(service.synthesize_to_file("こんにちは。", {"_pre_resolved": True}, use_cache=False))
    with pytest.raises(asyncio.CancelledError):
        await operation
    await scheduler._queue.join()
    assert not list(service.audio_dir.glob("*.wav"))


async def test_a_client_finishing_after_cancellation_cannot_leave_unowned_audio(stack):
    from galgame2voice.services.tts_service import get_tts_scheduler

    service, _, client = stack
    scheduler = get_tts_scheduler()
    started, release = asyncio.Event(), asyncio.Event()
    async def slow_client(*args, **kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return b"late synthesized audio"
    client.synthesize = slow_client
    operation = asyncio.create_task(service.synthesize_to_file("こんにちは。", {"_pre_resolved": True}, use_cache=False))
    try:
        await asyncio.wait_for(started.wait(), 3)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 2)
        release.set()
        await asyncio.wait_for(scheduler._queue.join(), 3)
        assert not list(service.audio_dir.glob("*.wav"))
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)
