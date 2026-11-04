"""Hardened permanent tests for audio snapshot status invariants under normal and early-exit conditions.

Verifies:
1. Completed, sealed, pending=0 audio without errors is preserved as status='ready' even if
   cancel/early-exit happens before the line_audio SSE event is emitted.
   Verifies that the ready chunk resolves to a non-empty valid synthetic WAV file on disk with
   expected PCM header parameters and frame count.
2. Unsealed or still-pending audio remains status='failed' on cancel.
3. Audio with errors remains status='failed' on cancel.
4. Normal completion correctly maps vocal lines to 'ready' and non-vocal lines to 'skipped'.
5. Non-vocal line settled before cancel records status='skipped', not 'failed'.
6. Direct invariant matrix: ready requires sealed=True, pending=0, errors=[], chunks non-empty.

All stream consumption and asynchronous event waits are strictly bounded (2-5s per step)
with timeout-safe cleanup, preventing any potential 2400s hang in CI/regression harnesses.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import struct
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock
import uuid
import wave
import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_pipelines.stream_coordinator import StreamCoordinator

DEFAULT_FRAME_COUNT = 80


def create_synthetic_wav(directory: Path, prefix: str = "chunk", frame_count: int = DEFAULT_FRAME_COUNT) -> Path:
    """Creates a tiny valid, non-empty PCM WAV file with actual audio frames using stdlib wave."""
    directory.mkdir(parents=True, exist_ok=True)
    wav_path = directory / f"{prefix}_{uuid.uuid4().hex[:8]}.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)      # mono
        wf.setsampwidth(2)      # 16-bit
        wf.setframerate(44100)  # 44.1 kHz
        wf.writeframes(struct.pack(f"<{frame_count}h", *(0 for _ in range(frame_count))))
    return wav_path


def read_wav_properties(path: Path) -> tuple[int, int, int]:
    """Inspects a WAV file on disk using stdlib wave and returns (nchannels, sampwidth, nframes)."""
    with wave.open(str(path), "rb") as wf:
        return wf.getnchannels(), wf.getsampwidth(), wf.getnframes()


async def consume_stream_bounded(
    stream: Any,
    *,
    until_predicate: Callable[[dict[str, Any], list[dict[str, Any]]], bool] | None = None,
    timeout: float = 3.0,
    close_on_exit: bool = True,
) -> list[dict[str, Any]]:
    """Consumes coordinator stream with a per-step timeout and bounded cleanup.

    Fails quickly if the stream or coordinator stalls instead of hanging.
    """
    events: list[dict[str, Any]] = []
    try:
        while True:
            try:
                ev = await asyncio.wait_for(stream.__anext__(), timeout=timeout)
                events.append(ev)
                if until_predicate and until_predicate(ev, events):
                    break
            except StopAsyncIteration:
                break
    finally:
        if close_on_exit:
            await asyncio.wait_for(stream.aclose(), timeout=timeout)
    return events


@pytest.fixture
async def coordinator_setup(tmp_path: Path):
    database = tmp_path / "test_snapshot.db"
    await init_db(database)
    async with get_db(database) as conn:
        user_msg = await crud.add_message(
            conn,
            MessageCreate(session_id="snap_session", role="user", content_chinese="你好"),
        )

    def make_coordinator(*, tokens_fn, synthesize_fn, cancel_event=None, reply_format="old"):
        tts = SimpleNamespace(
            audio_dir=tmp_path,
            synthesize_to_file=AsyncMock(side_effect=synthesize_fn),
        )
        coord = StreamCoordinator(
            adapter=SimpleNamespace(stream_chat=tokens_fn, api_key="synthetic_key"),
            messages=[],
            model_name="test_model",
            actual_provider_id="synthetic_provider",
            session_id="snap_session",
            prompt="你好",
            user_msg=user_msg,
            tts_service=tts,
            db_path=str(database),
            metrics_collector=SimpleNamespace(record_chat_turn=AsyncMock(return_value={})),
            affection_service=SimpleNamespace(handle_turn_affection=AsyncMock(return_value={"emotion": "gentle"})),
            concat_wav_fn=lambda *args: True,
            cancel_event=cancel_event,
            reply_format=reply_format,
        )
        return coord, tts, database

    return make_coordinator


@pytest.mark.asyncio
async def test_completed_unemitted_audio_is_ready_on_cancel(coordinator_setup, tmp_path: Path):
    """When a line's audio is fully synthesized before cancel but line_audio SSE frame

    was not emitted, early exit snapshot must record status='ready', not 'failed'.
    Also verifies the ready chunk resolves to a non-empty valid synthetic WAV file on disk.
    """
    cancel_event = asyncio.Event()
    line0_announced = asyncio.Event()
    chunk0_settled = asyncio.Event()
    created_wav_path: Path | None = None

    async def tokens(*args, **kwargs):
        # Two lines in lines format
        yield '{"lines": ['
        yield '{"zh": "第一句。", "ja": "一番です。", "emotion": "gentle", "act": {}},'
        yield '{"zh": "第二句。", "ja": "二番です。", "emotion": "gentle", "act": {}}'
        yield ']}'
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    async def synth_mock(sentence, **kwargs):
        nonlocal created_wav_path
        if "一番" in sentence:
            await asyncio.wait_for(line0_announced.wait(), timeout=3.0)
            prefix = kwargs.get("filename_prefix", "chunk_0")
            created_wav_path = create_synthetic_wav(tmp_path, prefix)
            return f"/audio/{created_wav_path.name}", created_wav_path, {}
        # Line 1 is cancelled or pending
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=3.0)
            wav_1 = create_synthetic_wav(tmp_path, "chunk_1")
            return f"/audio/{wav_1.name}", wav_1, {}
        except (asyncio.TimeoutError, asyncio.CancelledError) as err:
            raise asyncio.CancelledError() from err

    make_coord = coordinator_setup
    coord, tts, database = make_coord(
        tokens_fn=tokens,
        synthesize_fn=synth_mock,
        cancel_event=cancel_event,
        reply_format="lines",
    )

    # Cancel exactly after line 0 is settled so line_audio SSE cannot be emitted
    orig_settle = coord._settle_chunk

    def settle_and_cancel(run, chunk_index, **kwargs):
        res = orig_settle(run, chunk_index, **kwargs)
        if chunk_index == 0:
            chunk0_settled.set()
            cancel_event.set()
        return res

    coord._settle_chunk = settle_and_cancel

    stream = coord.stream()
    events = []
    try:
        # 1. Read bounded stream until line 0 is announced
        events.extend(await consume_stream_bounded(
            stream,
            until_predicate=lambda ev, _: ev.get("event") == "line" and ev.get("data", {}).get("index") == 0,
            timeout=3.0,
            close_on_exit=False,
        ))
        assert any(e.get("event") == "line" for e in events), "Line 0 should be announced"
        line0_announced.set()

        # 2. Wait until chunk 0 has been settled into line_audio_state and cancel triggered
        await asyncio.wait_for(chunk0_settled.wait(), timeout=3.0)

        # 3. Drain remaining events (e.g. done event with truncated=True) with strict timeout
        events.extend(await consume_stream_bounded(stream, timeout=3.0, close_on_exit=True))
    finally:
        await asyncio.wait_for(stream.aclose(), timeout=3.0)

    # Read SQLite to check the persisted partial message
    async with get_db(database) as conn:
        messages = await crud.get_recent_messages(conn, "snap_session")
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        assert len(assistant_msgs) == 1, "Assistant partial message should be persisted"
        persisted_lines = assistant_msgs[0].lines
        assert persisted_lines is not None and len(persisted_lines) >= 1

        line0 = persisted_lines[0]
        # Line 0 audio was fully synthesized (chunks populated, pending=0, sealed=True, errors=[]).
        # It must be recorded as 'ready', never 'failed'.
        assert line0["audio"]["chunks"], "Line 0 should have chunks"
        assert line0["audio"]["status"] == "ready", (
            f"Line 0 status was '{line0['audio']['status']}', expected 'ready' "
            "because its audio was fully synthesized before cancel."
        )

        # Verify persisted ready chunk resolves to the real created non-empty WAV file on disk
        chunk0 = line0["audio"]["chunks"][0]
        audio_url = chunk0["audio_url"]
        filename = audio_url.removeprefix("/audio/")
        resolved_file = tmp_path / filename
        assert await asyncio.to_thread(resolved_file.is_file), f"Expected audio file to exist on disk: {resolved_file}"
        if created_wav_path is not None:
            assert resolved_file.name == created_wav_path.name

        nchannels, sampwidth, nframes = await asyncio.to_thread(read_wav_properties, resolved_file)
        assert nchannels == 1, f"Expected mono audio (1 channel), got {nchannels}"
        assert sampwidth == 2, f"Expected 16-bit PCM (sample width 2), got {sampwidth}"
        assert nframes == DEFAULT_FRAME_COUNT, f"Expected {DEFAULT_FRAME_COUNT} frames, got {nframes}"


@pytest.mark.asyncio
async def test_unsealed_or_pending_audio_is_failed_on_cancel(coordinator_setup, tmp_path: Path):
    """Lines whose audio was still pending or unsealed at cancel time must be marked failed."""
    cancel_event = asyncio.Event()
    line0_announced = asyncio.Event()

    async def tokens(*args, **kwargs):
        yield '{"lines": ['
        yield '{"zh": "第一句。", "ja": "一番です。", "emotion": "gentle", "act": {}}'
        yield ']}'
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    async def synth_mock(sentence, **kwargs):
        # Pending in synthesis until cancel
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=3.0)
            wav_path = create_synthetic_wav(tmp_path, "chunk_dummy")
            return f"/audio/{wav_path.name}", wav_path, {}
        except (asyncio.TimeoutError, asyncio.CancelledError) as err:
            raise asyncio.CancelledError() from err

    make_coord = coordinator_setup
    coord, tts, database = make_coord(
        tokens_fn=tokens,
        synthesize_fn=synth_mock,
        cancel_event=cancel_event,
        reply_format="lines",
    )

    stream = coord.stream()
    try:
        events = await consume_stream_bounded(
            stream,
            until_predicate=lambda ev, _: ev.get("event") == "line" and ev.get("data", {}).get("index") == 0,
            timeout=3.0,
            close_on_exit=False,
        )
        line0_announced.set()
        cancel_event.set()
        events.extend(await consume_stream_bounded(stream, timeout=3.0, close_on_exit=True))
    finally:
        await asyncio.wait_for(stream.aclose(), timeout=3.0)

    async with get_db(database) as conn:
        messages = await crud.get_recent_messages(conn, "snap_session")
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        line0 = assistant_msgs[0].lines[0]
        assert line0["audio"]["status"] == "failed"


@pytest.mark.asyncio
async def test_audio_with_error_is_failed_on_cancel(coordinator_setup):
    """Lines with synthesis errors must be marked failed even if cancelled later."""
    cancel_event = asyncio.Event()
    line0_announced = asyncio.Event()

    async def tokens(*args, **kwargs):
        yield '{"lines": ['
        yield '{"zh": "失败句。", "ja": "失败です。", "emotion": "gentle", "act": {}}'
        yield ']}'
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    async def synth_mock(sentence, **kwargs):
        await asyncio.wait_for(line0_announced.wait(), timeout=3.0)
        cancel_event.set()
        raise RuntimeError("Synthetic synthesis failure")

    make_coord = coordinator_setup
    coord, tts, database = make_coord(
        tokens_fn=tokens,
        synthesize_fn=synth_mock,
        cancel_event=cancel_event,
        reply_format="lines",
    )

    stream = coord.stream()
    try:
        events = await consume_stream_bounded(
            stream,
            until_predicate=lambda ev, _: ev.get("event") == "line",
            timeout=3.0,
            close_on_exit=False,
        )
        line0_announced.set()
        events.extend(await consume_stream_bounded(stream, timeout=3.0, close_on_exit=True))
    finally:
        await asyncio.wait_for(stream.aclose(), timeout=3.0)

    async with get_db(database) as conn:
        messages = await crud.get_recent_messages(conn, "snap_session")
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        line0 = assistant_msgs[0].lines[0]
        assert line0["audio"]["status"] == "failed"


@pytest.mark.asyncio
async def test_normal_completion_status_ready_and_skipped(coordinator_setup, tmp_path: Path):
    """Normal completion records ready for vocal lines and skipped for non-vocal lines."""
    async def tokens(*args, **kwargs):
        yield '{"chinese": "第一句。沉默……", "japanese": "一番です。……", "tts": {"emotion": "gentle"}}\n'

    async def synth_mock(sentence, **kwargs):
        wav_path = create_synthetic_wav(tmp_path, kwargs.get("filename_prefix", "chunk"))
        return f"/audio/{wav_path.name}", wav_path, {}

    make_coord = coordinator_setup
    coord, tts, database = make_coord(
        tokens_fn=tokens,
        synthesize_fn=synth_mock,
        reply_format="old",
    )

    stream = coord.stream()
    try:
        events = await consume_stream_bounded(stream, timeout=3.0, close_on_exit=True)
        done_events = [e for e in events if e.get("event") == "done"]
        assert len(done_events) == 1
        lines = done_events[0]["data"]["lines"]
        assert len(lines) == 2
        assert lines[0]["audio"]["status"] == "ready"
        assert lines[1]["audio"]["status"] == "skipped"
    finally:
        await asyncio.wait_for(stream.aclose(), timeout=3.0)


@pytest.mark.asyncio
async def test_nonvocal_line_is_skipped_on_cancel(coordinator_setup, tmp_path: Path):
    """Non-vocal completed line before cancel records status='skipped', not 'failed'."""
    cancel_event = asyncio.Event()
    line0_announced = asyncio.Event()

    async def tokens(*args, **kwargs):
        yield '{"lines": ['
        yield '{"zh": "……", "ja": "……", "emotion": "gentle", "act": {}}'
        yield ']}'
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    async def synth_mock(sentence, **kwargs):
        wav_path = create_synthetic_wav(tmp_path, "chunk_nonvocal")
        return f"/audio/{wav_path.name}", wav_path, {}

    make_coord = coordinator_setup
    coord, tts, database = make_coord(
        tokens_fn=tokens,
        synthesize_fn=synth_mock,
        cancel_event=cancel_event,
        reply_format="lines",
    )

    stream = coord.stream()
    try:
        events = await consume_stream_bounded(
            stream,
            until_predicate=lambda ev, _: ev.get("event") == "line",
            timeout=3.0,
            close_on_exit=False,
        )
        line0_announced.set()
        cancel_event.set()
        events.extend(await consume_stream_bounded(stream, timeout=3.0, close_on_exit=True))
    finally:
        await asyncio.wait_for(stream.aclose(), timeout=3.0)

    async with get_db(database) as conn:
        messages = await crud.get_recent_messages(conn, "snap_session")
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        line0 = assistant_msgs[0].lines[0]
        # Non-vocal line was settled with chunks=[] and errors=[] before cancel.
        # It must be 'skipped', not 'failed'.
        assert line0["audio"]["status"] == "skipped"


def test_snapshot_invariants_matrix():
    """Direct invariant matrix verification of _snapshot_lines_with_audio rules."""
    coord = StreamCoordinator.__new__(StreamCoordinator)

    def make_run(audio_state_overrides: dict | None):
        return SimpleNamespace(
            announced_lines=[{"index": 0, "zh": "测试", "ja": "テスト"}],
            line_audio_states={0: audio_state_overrides} if audio_state_overrides is not None else {},
        )

    # 1. sealed=True, pending=0, errors=[], chunks non-empty -> ready even with mark_unfinished_failed=True
    run1 = make_run({
        "line": 0, "sealed": True, "pending": 0, "errors": [],
        "chunks": [{"index": 0, "audio_url": "/audio/test.wav"}],
        "emitted": False, "status": None,
    })
    snap1 = coord._snapshot_lines_with_audio(run1, mark_unfinished_failed=True)
    assert snap1[0]["audio"]["status"] == "ready"

    # 2. unsealed -> failed when mark_unfinished_failed=True
    run2 = make_run({
        "line": 0, "sealed": False, "pending": 0, "errors": [],
        "chunks": [{"index": 0, "audio_url": "/audio/test.wav"}],
        "emitted": False, "status": None,
    })
    snap2 = coord._snapshot_lines_with_audio(run2, mark_unfinished_failed=True)
    assert snap2[0]["audio"]["status"] == "failed"

    # 3. pending > 0 -> failed when mark_unfinished_failed=True
    run3 = make_run({
        "line": 0, "sealed": True, "pending": 1, "errors": [],
        "chunks": [{"index": 0, "audio_url": "/audio/test.wav"}],
        "emitted": False, "status": None,
    })
    snap3 = coord._snapshot_lines_with_audio(run3, mark_unfinished_failed=True)
    assert snap3[0]["audio"]["status"] == "failed"

    # 4. errors non-empty -> failed even if sealed and pending=0
    run4 = make_run({
        "line": 0, "sealed": True, "pending": 0, "errors": ["error occurred"],
        "chunks": [{"index": 0, "audio_url": "/audio/test.wav"}],
        "emitted": False, "status": None,
    })
    snap4 = coord._snapshot_lines_with_audio(run4, mark_unfinished_failed=True)
    assert snap4[0]["audio"]["status"] == "failed"

    # 5. sealed=True, pending=0, errors=[], chunks=[] (non-vocal) -> skipped
    run5 = make_run({
        "line": 0, "sealed": True, "pending": 0, "errors": [],
        "chunks": [],
        "emitted": False, "status": None,
    })
    snap5 = coord._snapshot_lines_with_audio(run5, mark_unfinished_failed=True)
    assert snap5[0]["audio"]["status"] == "skipped"

    # 6. emitted=True retains its emitted status
    run6 = make_run({
        "line": 0, "sealed": True, "pending": 0, "errors": [],
        "chunks": [{"index": 0, "audio_url": "/audio/test.wav"}],
        "emitted": True, "status": "ready",
    })
    snap6 = coord._snapshot_lines_with_audio(run6, mark_unfinished_failed=True)
    assert snap6[0]["audio"]["status"] == "ready"

    # 7. state is None -> failed when mark_unfinished_failed=True, skipped otherwise
    run7 = make_run(None)
    snap7_fail = coord._snapshot_lines_with_audio(run7, mark_unfinished_failed=True)
    assert snap7_fail[0]["audio"]["status"] == "failed"
    snap7_skip = coord._snapshot_lines_with_audio(run7, mark_unfinished_failed=False)
    assert snap7_skip[0]["audio"]["status"] == "skipped"
