"""Line lifecycle contracts against the real parser/coordinator and shared fakes."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_pipelines.stream_coordinator import StreamCoordinator
from galgame2voice.services.chat_pipelines.line_builder import build_lines


@pytest.fixture
async def coordinator_factory(tmp_path, mock_llm_server, mock_gpt_sovits):
    database = tmp_path / "stream.db"
    await init_db(database)
    async with get_db(database) as conn:
        user = await crud.add_message(conn, MessageCreate(session_id="lines", role="user", content_chinese="问题"))

    async def synthesize(sentence, **kwargs):
        mock_gpt_sovits.fail_on_step("tts" if "失敗" in sentence else None)
        response = await mock_gpt_sovits.handle_request("GET", "/tts", params={"text": sentence})
        if response.is_error:
            raise RuntimeError("synthetic TTS failure")
        path = tmp_path / f"{kwargs['filename_prefix']}.wav"
        path.write_bytes(response.content)
        return f"/audio/{path.name}", path, {}

    tts = SimpleNamespace(audio_dir=tmp_path, synthesize_to_file=AsyncMock(side_effect=synthesize))

    def create(*, zh="第一句。第二句。第三句。", ja="一番です。二番です。三番です。", tokens=None, **kwargs):
        async def default_tokens(*args, **options):
            raw = json.dumps({"tts": {"emotion": "gentle"}, "chinese": zh, "japanese": ja}, ensure_ascii=False)
            for event in mock_llm_server.generate_streaming_chunks(raw):
                if event.startswith("data: [DONE]"):
                    break
                payload = json.loads(event.removeprefix("data: "))
                yield payload["choices"][0]["delta"].get("content", "")
                await asyncio.sleep(0)

        coordinator = StreamCoordinator(
            adapter=SimpleNamespace(stream_chat=tokens or default_tokens, api_key="synthetic"),
            messages=[], model_name="test", actual_provider_id="custom", session_id="lines", prompt="问题",
            user_msg=user, tts_service=tts, db_path=str(database),
            metrics_collector=SimpleNamespace(record_chat_turn=AsyncMock(return_value={})),
            affection_service=SimpleNamespace(handle_turn_affection=AsyncMock(return_value={"emotion": "gentle"})),
            concat_wav_fn=lambda *args: True, **kwargs,
        )
        return coordinator, tts, database

    return create


def assert_lifecycle(events):
    assert events[-1]["event"] == "done"
    lines = [event["data"] for event in events if event["event"] == "line"]
    audio = [event["data"] for event in events if event["event"] == "line_audio"]
    assert [line["index"] for line in lines] == list(range(len(lines)))
    assert [item["line"] for item in audio] == list(range(len(lines)))
    for line in lines:
        assert set(line) == {"index", "zh", "ja", "emotion", "act"}
        line_position = next(i for i, e in enumerate(events) if e["event"] == "line" and e["data"]["index"] == line["index"])
        audio_position = next(i for i, e in enumerate(events) if e["event"] == "line_audio" and e["data"]["line"] == line["index"])
        assert line_position < audio_position < len(events) - 1
        chunks = [e["data"] for e in events[:audio_position] if e["event"] == "audio_chunk" and e["data"].get("line") == line["index"]]
        actual = next(item for item in audio if item["line"] == line["index"])
        expected_chunks = [
            {"index": c["index"], "line": c["line"], "audio_url": c["audio_url"], "sentence": c["sentence"]}
            for c in chunks
        ]
        assert actual["chunks"] == expected_chunks
    expected_done_lines = [
        dict(
            line,
            audio={
                "status": next(item["status"] for item in audio if item["line"] == line["index"]),
                "chunks": next(item["chunks"] for item in audio if item["line"] == line["index"]),
            },
        )
        for line in lines
    ]
    assert events[-1]["data"]["lines"] == expected_done_lines
    return lines, audio


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "failure"])
async def test_line_events_and_tts_status(coordinator_factory, mode):
    coordinator, tts, database = coordinator_factory(
        ja="一番です。失敗です。三番です。" if mode == "failure" else "一番です。二番です。三番です。",
    )
    events = [event async for event in coordinator]
    lines, audio = assert_lifecycle(events)
    assert len(lines) == 3
    assert [item["status"] for item in audio] == {
        "normal": ["ready"] * 3,
        "failure": ["ready", "failed", "ready"],
    }[mode]
    if mode == "failure":
        assert audio[1]["error"]
        assert len([e for e in events if e["event"] == "audio_chunk_error"]) == 1
    async with get_db(database) as conn:
        history = await crud.get_recent_messages(conn, "lines")
        assert history[-1].lines == events[-1]["data"]["lines"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("zh", "ja", "statuses"), [
    ("沉默……", "……", ["skipped"]),
    ("第一句。第二句。第三句。", "一番です。", ["ready"]),
    ("第一句。", "一番です。二番です。三番です。", ["ready"]),
    ("只有中文。再说一句。", "", ["skipped", "skipped"]),
    ("", "一番です。二番です。", ["ready", "ready"]),
    ("", "", []),
])
async def test_nonvocal_and_trailing_lines(coordinator_factory, zh, ja, statuses):
    coordinator, tts, database = coordinator_factory(zh=zh, ja=ja)
    events = [event async for event in coordinator]
    if not zh and not ja:
        assert events == [{"event": "error", "data": {
            "error": "模型这次没有返回任何内容，可以重新发送或换个模型试试。",
            "error_code": "empty_reply", "kind": "empty_reply", "status_code": 200,
        }}]
        tts.synthesize_to_file.assert_not_awaited()
        async with get_db(database) as conn:
            assert await crud.get_recent_messages(conn, "lines") == []
        return
    lines, audio = assert_lifecycle(events)
    assert [item["status"] for item in audio] == statuses


@pytest.mark.asyncio
async def test_early_comma_audio_belongs_to_same_line_and_waits_for_remaining_tts(coordinator_factory):
    release = asyncio.Event()

    async def tokens(*args, **kwargs):
        yield '{"chinese":"你好，今天一起出去吧。再见。","japanese":"こんにちは、'
        await release.wait()
        yield '今日は一緒に出かけましょう。さようなら。"}'

    coordinator, _, _ = coordinator_factory(tokens=tokens)
    stream = coordinator.stream()
    events = []
    try:
        while True:
            event = await asyncio.wait_for(stream.__anext__(), 2)
            events.append(event)
            if event["event"] == "audio_chunk":
                break
        assert events[-1]["data"]["line"] == 0
        assert not any(e["event"] in {"line", "line_audio"} for e in events)
        release.set()
        events.extend([event async for event in stream])
        lines, audio = assert_lifecycle(events)
        assert lines[0]["ja"] == "こんにちは、今日は一緒に出かけましょう。"
        assert len(audio[0]["chunks"]) == 2
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "error"])
async def test_cancel_or_fatal_error_is_terminal(coordinator_factory, ending):
    cancel, release = asyncio.Event(), asyncio.Event()

    async def tokens(*args, **kwargs):
        yield '{"tts":{"emotion":"gentle"},"chinese":"第一句。第二句。第三句。","japanese":"一番です。二番です。'
        await release.wait()
        if ending == "error":
            raise RuntimeError("synthetic provider disconnect")
        yield '三番です。"}'

    coordinator, _, database = coordinator_factory(tokens=tokens, cancel_event=cancel)
    stream = coordinator.stream()
    events = []
    try:
        while not any(event["event"] == "line" for event in events):
            events.append(await asyncio.wait_for(stream.__anext__(), 2))
        if ending == "cancel":
            cancel.set()
        else:
            release.set()
        remainder = [event async for event in stream]
        if ending == "cancel":
            assert len(remainder) == 1
            assert remainder[0]["event"] == "done"
            assert remainder[0]["data"]["truncated"] is True
            assert len(remainder[0]["data"]["lines"]) >= 1
            assert remainder[0]["data"]["lines"][-1]["audio"]["status"] in {"ready", "failed"}
        else:
            assert remainder[-1]["event"] == "error"
            assert not any(event["event"] == "done" for event in remainder)
        async with get_db(database) as conn:
            saved = await crud.get_recent_messages(conn, "lines")
            assert saved[-1].role == "assistant" and saved[-1].lines is not None
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_first_line_is_published_before_provider_eof(coordinator_factory):
    release = asyncio.Event()

    async def tokens(*args, **kwargs):
        yield '{"tts":{"emotion":"gentle"},"chinese":"第一句。第二句。第三句。","japanese":"一番です。二番です。'
        await release.wait()
        yield '三番です。"}'

    coordinator, _, _ = coordinator_factory(tokens=tokens)
    stream = coordinator.stream()
    try:
        while True:
            event = await asyncio.wait_for(stream.__anext__(), 2)
            if event["event"] == "line":
                assert event["data"]["index"] == 0
                assert event["data"]["ja"] == "一番です。"
                assert not release.is_set()
                break
        release.set()
        assert [event async for event in stream][-1]["event"] == "done"
    finally:
        release.set()
        await stream.aclose()


async def collect_stream(stream):
    return [event async for event in stream]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["_concat_audio_chunks", "metrics", "_persist_turn_message", "_update_turn_affection"])
async def test_cancel_during_success_finalization_keeps_done(coordinator_factory, monkeypatch, operation):
    cancel = asyncio.Event()
    coordinator, _, database = coordinator_factory(zh="A!", ja="one!", cancel_event=cancel)
    owner = coordinator.metrics_collector if operation == "metrics" else coordinator
    name = "record_chat_turn" if operation == "metrics" else operation
    original = getattr(owner, name)

    async def cancel_during_await(*args, **kwargs):
        cancel.set()
        await asyncio.sleep(0)
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner, name, cancel_during_await)
    events = await asyncio.wait_for(collect_stream(coordinator), 3)
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["truncated"] is True
    assert len([e for e in events if e["event"] == "done"]) == 1
    async with get_db(database) as conn:
        saved = await crud.get_recent_messages(conn, "lines")
        assert saved[-1].lines == events[-1]["data"]["lines"]


@pytest.mark.asyncio
async def test_disconnect_with_full_event_queue_reaps_tasks_and_persists(coordinator_factory, monkeypatch):
    coordinator, _, database = coordinator_factory(zh="A!" * 100, ja="one!" * 100)
    run = coordinator._init_stream_run()
    full = asyncio.Event()
    original = coordinator._put_with_cancel

    async def observe_full(queue, item, *args, **kwargs):
        if queue is run.event_queue and queue.full():
            full.set()
        return await original(queue, item, *args, **kwargs)

    monkeypatch.setattr(coordinator, "_init_stream_run", lambda: run)
    monkeypatch.setattr(coordinator, "_put_with_cancel", observe_full)
    stream = coordinator.stream()
    close_task = None
    try:
        while (await asyncio.wait_for(anext(stream), 3))["event"] != "line":
            pass
        await asyncio.wait_for(full.wait(), 3)
        assert run.event_queue.full()
        close_task = asyncio.create_task(stream.aclose())
        await asyncio.wait_for(asyncio.shield(close_task), 2)
        assert run.producer_task.done() and run.worker_task.done()
        async with get_db(database) as conn:
            saved = await crud.get_recent_messages(conn, "lines")
            assert saved[-1].role == "assistant"
            assert [line["index"] for line in saved[-1].lines] == [0]
    finally:
        # A failing implementation must still leave the test runner responsive.
        while not run.event_queue.empty():
            run.event_queue.get_nowait()
        while not run.tts_queue.empty():
            run.tts_queue.get_nowait()
        for task in (run.producer_task, run.worker_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(run.producer_task, run.worker_task, return_exceptions=True), 2)
        if close_task is not None:
            await asyncio.wait_for(close_task, 2)
        await asyncio.wait_for(stream.aclose(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["settled", "in_flight"])
@pytest.mark.parametrize("chinese", [None, "A!B!"])
async def test_japanese_first_chunks_settle_after_late_attribution(coordinator_factory, monkeypatch, timing, chinese):
    release, started = asyncio.Event(), asyncio.Event()

    async def tokens(*args, **kwargs):
        yield '{"japanese":"one!失敗!"'
        await release.wait()
        yield '}' if chinese is None else ',"chinese":"A!B!"}'

    coordinator, _, _ = coordinator_factory(tokens=tokens)
    run = coordinator._init_stream_run()
    monkeypatch.setattr(coordinator, "_init_stream_run", lambda: run)
    original = run.tts_pipe.synthesize_chunk

    async def synthesize(**kwargs):
        started.set()
        if timing == "in_flight":
            await release.wait()
            # Force attribution to happen while this synthesis is suspended.
            await asyncio.wait_for(run.producer_task, 2)
        return await original(**kwargs)

    monkeypatch.setattr(run.tts_pipe, "synthesize_chunk", synthesize)
    stream = coordinator.stream()
    pending = asyncio.create_task(collect_stream(stream))
    try:
        await asyncio.wait_for(started.wait(), 2)
        if timing == "settled":
            await asyncio.wait_for(run.tts_queue.join(), 2)
        release.set()
        events = await asyncio.wait_for(pending, 3)
        audio = [e["data"] for e in events if e["event"] == "line_audio"]
        assert [item["line"] for item in audio] == [0, 1]
        assert [item["status"] for item in audio] == ["ready", "failed"]
        chunk = next(e["data"] for e in events if e["event"] == "audio_chunk")
        assert audio[0]["chunks"] == [{
            "index": chunk["index"], "line": 0, "audio_url": chunk["audio_url"], "sentence": chunk["sentence"],
        }]
        assert "synthetic TTS failure" in audio[1]["error"]
        assert events[-1]["data"]["lines"][0]["audio"]["chunks"] == audio[0]["chunks"]
        assert all(state["pending"] == 0 for state in run.line_audio_states.values())
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2)
        await asyncio.wait_for(stream.aclose(), 2)


@pytest.mark.asyncio
async def test_overlapping_line_audio_emitters_do_not_skip_backpressured_line(coordinator_factory, monkeypatch):
    coordinator, _, _ = coordinator_factory()
    run = coordinator._init_stream_run()
    run.event_queue = asyncio.Queue(maxsize=1)
    run.event_queue.put_nowait("occupied")
    run.lines = build_lines("A!B!", "one!two!", "gentle")
    first = coordinator._line_audio_state(run, 0)
    second = coordinator._line_audio_state(run, 1)
    first["sealed"] = second["sealed"] = True
    second["pending"] = 1
    blocked, overlap = asyncio.Event(), asyncio.Event()
    original_put = coordinator._put_with_cancel
    original_state = coordinator._line_audio_state

    async def observe_put(queue, item, *args, **kwargs):
        if queue.full():
            blocked.set()
        return await original_put(queue, item, *args, **kwargs)

    def observe_state(current_run, index):
        if index == 1:
            overlap.set()
        return original_state(current_run, index)

    monkeypatch.setattr(coordinator, "_put_with_cancel", observe_put)
    monkeypatch.setattr(coordinator, "_line_audio_state", observe_state)
    emitter1 = asyncio.create_task(coordinator._try_emit_line_audio(run))
    await asyncio.wait_for(blocked.wait(), 1)
    entered = asyncio.Event()

    async def concurrent_emit():
        entered.set()
        await coordinator._try_emit_line_audio(run)

    emitter2 = asyncio.create_task(concurrent_emit())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        # The old emitter reaches line 1; the fixed emitter waits on the lock.
        assert overlap.is_set() or not emitter2.done()
        assert run.event_queue.get_nowait() == "occupied"
        await asyncio.wait_for(asyncio.gather(emitter1, emitter2), 1)
        assert (await asyncio.wait_for(run.event_queue.get(), 1))["data"]["line"] == 0
        second["pending"] = 0
        await asyncio.wait_for(coordinator._try_emit_line_audio(run), 1)
        assert (await asyncio.wait_for(run.event_queue.get(), 1))["data"]["line"] == 1
        assert run.next_line_audio_index == 2
    finally:
        for task in (emitter1, emitter2):
            if not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(emitter1, emitter2, return_exceptions=True), 1)


@pytest.mark.asyncio
async def test_backpressure_keeps_chunk_before_terminal_line_audio(coordinator_factory, monkeypatch):
    blocked, release = asyncio.Event(), asyncio.Event()

    async def tokens(*args, **kwargs):
        yield '{"tts":{"emotion":"gentle"},"chinese":"A!B!","japanese":"one!'
        await blocked.wait()
        yield 'two!"}'

    coordinator, _, _ = coordinator_factory(tokens=tokens)
    run = coordinator._init_stream_run()
    monkeypatch.setattr(coordinator, "_init_stream_run", lambda: run)
    original = coordinator._put_with_cancel

    async def backpressure(queue, item, *args, **kwargs):
        if isinstance(item, dict) and item["event"] == "audio_chunk" and item["data"]["index"] == 0:
            blocked.set()
            await release.wait()
        return await original(queue, item, *args, **kwargs)

    monkeypatch.setattr(coordinator, "_put_with_cancel", backpressure)
    stream = coordinator.stream()
    collecting = asyncio.create_task(collect_stream(stream))
    try:
        await asyncio.wait_for(blocked.wait(), 2)
        await asyncio.wait_for(run.producer_task, 2)
        assert not coordinator._line_audio_state(run, 0)["emitted"]
        release.set()
        events = await asyncio.wait_for(collecting, 3)
        assert_lifecycle(events)
    finally:
        release.set()
        if not collecting.done():
            collecting.cancel()
        await asyncio.wait_for(asyncio.gather(collecting, return_exceptions=True), 2)
        await asyncio.wait_for(stream.aclose(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("token_mode", ["whole", "characters"])
async def test_real_parser_preserves_newline_chunk_boundaries(coordinator_factory, token_mode):
    ja = "one\n  two"
    raw = json.dumps({"tts": {"emotion": "gentle"}, "chinese": "A!B!", "japanese": ja})

    async def tokens(*args, **kwargs):
        for token in ([raw] if token_mode == "whole" else raw):
            yield token
            await asyncio.sleep(0)

    coordinator, _, _ = coordinator_factory(tokens=tokens)
    events = await asyncio.wait_for(collect_stream(coordinator), 3)
    lines, audio = assert_lifecycle(events)
    assert [line["ja"] for line in lines] == ["one", "two"]
    assert [item["status"] for item in audio] == ["ready", "ready"]
    assert [[chunk["sentence"] for chunk in item["chunks"]] for item in audio] == [["one"], ["two"]]
    with pytest.raises(ValueError, match="offset"):
        coordinator._find_containing_sentence_idx(ja, "onetwo", 0)


@pytest.mark.asyncio
async def test_unplaceable_chunk_keeps_the_turn_alive(coordinator_factory):
    coordinator, _, _ = coordinator_factory()
    run = coordinator._init_stream_run()
    run.parser.japanese_extracted = "一番です。"

    await coordinator._queue_sentences(run, ["一番です。", "消えた文。"])

    assert [run.tts_queue.get_nowait() for _ in range(2)] == [("一番です。", 0), ("消えた文。", 0)]
    assert run.chunk_s_map == {0: 0, 1: 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "error", "disconnect"])
@pytest.mark.parametrize("audio_settled", [False, True])
async def test_interrupted_snapshot_equals_delivered_line_prefix(coordinator_factory, monkeypatch, ending, audio_settled):
    cancel = asyncio.Event()
    coordinator, _, database = coordinator_factory(cancel_event=cancel)
    run = coordinator._init_stream_run()
    monkeypatch.setattr(coordinator, "_init_stream_run", lambda: run)
    synthesis_release = asyncio.Event()

    async def unfinished_synthesis(**kwargs):
        await synthesis_release.wait()

    if not audio_settled:
        monkeypatch.setattr(run.tts_pipe, "synthesize_chunk", unfinished_synthesis)

    async def complete_producer_before_pump():
        await asyncio.wait_for(run.producer_task, 2)
        if audio_settled:
            await asyncio.wait_for(run.worker_task, 2)
        if ending == "error":
            # Put a fatal error immediately after the first announced line.
            queued = []
            while not run.event_queue.empty():
                queued.append(run.event_queue.get_nowait())
            for event in queued:
                run.event_queue.put_nowait(event)
                if isinstance(event, dict) and event["event"] == "line":
                    run.event_queue.put_nowait({"event": "error", "data": {"error": "synthetic"}})
                    break

    original_pump = coordinator._pump_events

    async def pump(current_run):
        await complete_producer_before_pump()
        async for event in original_pump(current_run):
            yield event

    monkeypatch.setattr(coordinator, "_pump_events", pump)
    stream = coordinator.stream()
    received = []
    try:
        while not received:
            event = await asyncio.wait_for(anext(stream), 3)
            if event["event"] == "line":
                received.append(event["data"])
        assert len(run.lines) == 3
        if ending == "cancel":
            cancel.set()
        if ending == "disconnect":
            await asyncio.wait_for(stream.aclose(), 2)
        else:
            remainder = await asyncio.wait_for(collect_stream(stream), 3)
            assert not any(e["event"] in {"line", "line_audio", "audio_chunk"} for e in remainder)
            if ending == "cancel":
                assert [{k: v for k, v in line.items() if k != "audio"} for line in remainder[-1]["data"]["lines"]] == received
        async with get_db(database) as conn:
            saved = (await crud.get_recent_messages(conn, "lines"))[-1].lines
            assert [{k: v for k, v in line.items() if k != "audio"} for line in saved] == received
            assert saved[0]["audio"]["status"] == ("ready" if audio_settled else "failed")
    finally:
        synthesis_release.set()
        await asyncio.wait_for(stream.aclose(), 2)


@pytest.mark.asyncio
async def test_stream_matches_offline_when_late_cue_changes_reply_emotion(coordinator_factory):
    release = asyncio.Event()

    async def tokens(*args, **kwargs):
        yield '{"chinese":"A!B!","japanese":"one!two!'
        await release.wait()
        yield '悲しい!"}'

    coordinator, _, _ = coordinator_factory(tokens=tokens)
    stream = coordinator.stream()
    events = []
    try:
        while not any(e["event"] == "audio_chunk" for e in events):
            events.append(await asyncio.wait_for(anext(stream), 2))
        assert not any(e["event"] == "line" for e in events)
        release.set()
        events.extend(await asyncio.wait_for(collect_stream(stream), 3))
        lines, _ = assert_lifecycle(events)
        offline = build_lines("A!B!", "one!two!悲しい!", "sad")
        assert lines == offline
        assert lines[0]["emotion"] == "sad"
    finally:
        release.set()
        await asyncio.wait_for(stream.aclose(), 2)


@pytest.mark.asyncio
async def test_overlong_fallback_unit_preserves_lifecycle_and_persisted_lines(coordinator_factory):
    coordinator, _, database = coordinator_factory(
        zh="长句。早安。",
        ja=f"{'あ' * 10005}。おはよう。",
    )
    events = [event async for event in coordinator]
    lines, audio = assert_lifecycle(events)
    assert [(line["zh"], line["ja"]) for line in lines] == [
        ("长句。", "あ" * 10000),
        ("", "あ" * 5 + "。"),
        ("早安。", "おはよう。"),
    ]
    assert [item["status"] for item in audio] == ["ready", "skipped", "ready"]
    assert audio[2]["chunks"][0]["sentence"] == "おはよう。"
    assert audio[2]["chunks"][0]["line"] == 2
    async with get_db(database) as conn:
        history = await crud.get_recent_messages(conn, "lines")
        assert history[-1].lines == events[-1]["data"]["lines"]


@pytest.mark.asyncio
async def test_overlong_tail_sentences_keep_their_audio_on_the_line_holding_them(coordinator_factory):
    sentence = "あ" * 3000 + "。"
    coordinator, _, database = coordinator_factory(zh="总结。", ja=sentence * 4)
    events = [event async for event in coordinator]
    lines, audio = assert_lifecycle(events)
    assert [(line["zh"], line["ja"]) for line in lines] == [("总结。", sentence * 3), ("", sentence)]
    assert [item["status"] for item in audio] == ["ready", "ready"]
    assert [chunk["sentence"] for chunk in audio[0]["chunks"]] == [sentence] * 3
    assert [chunk["sentence"] for chunk in audio[1]["chunks"]] == [sentence]
    assert audio[1]["chunks"][0]["line"] == 1
    async with get_db(database) as conn:
        history = await crud.get_recent_messages(conn, "lines")
        assert history[-1].lines == events[-1]["data"]["lines"]


@pytest.mark.asyncio
async def test_overlong_chinese_sentence_keeps_following_translation_aligned(coordinator_factory):
    coordinator, _, database = coordinator_factory(
        zh=f"{'哈' * 10005}。早安。",
        ja=f"{'あ' * 10005}。おはよう。",
    )
    events = [event async for event in coordinator]
    lines, audio = assert_lifecycle(events)
    assert [(line["zh"], line["ja"]) for line in lines] == [
        ("哈" * 10000, "あ" * 10000),
        ("哈" * 5 + "。", "あ" * 5 + "。"),
        ("早安。", "おはよう。"),
    ]
    assert [item["status"] for item in audio] == ["ready", "skipped", "ready"]
    assert audio[2]["chunks"][0]["sentence"] == "おはよう。"
    async with get_db(database) as conn:
        history = await crud.get_recent_messages(conn, "lines")
        assert history[-1].lines == events[-1]["data"]["lines"]