"""P2b uses validated complete lines throughout parsing, streaming and persistence."""

import asyncio
import codecs
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from galgame2voice.database import crud
from galgame2voice.database.session import get_db, init_db
from galgame2voice.routers.chat import ChatRequest
from galgame2voice.services import chat_service as service_module
from galgame2voice.services.chat_pipelines.text_pipeline import TextSegmentationPipeline
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.telegram_bot.handlers import TelegramBotHandlers
from tests.test_stream_reply_lines import assert_lifecycle, coordinator_factory  # noqa: F401


LINES = [
    {"index": 900, "zh": '你好，"小猫"。\\路径\n下一句😀', "ja": 'こんにちは、猫です。次です。😀',
     "emotion": "shy", "act": {"face": "smile", "motion": "nod", "blush": False}},
    {"zh": "再见。", "ja": "さようなら。", "emotion": "cool", "act": {"fx": None}},
]
ZH = "".join(line["zh"] for line in LINES)
JA = "".join(line["ja"] for line in LINES)


def reply(lines=LINES, *, ascii=False):
    return json.dumps({"tts": {"speed": 1.05, "temp": 0.95, "emotion": "gentle"}, "lines": lines}, ensure_ascii=ascii)


@pytest.mark.parametrize("ascii", [False, True])
@pytest.mark.parametrize(("prefix", "suffix"), [
    ("", ""), ("```json\n", "\n```"), ("好的～\n", ""), ("说明\n```json\n", "\n```\n尾声不属于回复"),
])
def test_side_channel_at_every_split(ascii, prefix, suffix):
    raw = prefix + reply(ascii=ascii) + suffix
    for split in range(len(raw) + 1):
        pipe = TextSegmentationPipeline()
        shown = ""
        for token in (raw[:split], raw[split:]):
            delta, sentences, _ = pipe.feed_token(token)
            shown += delta
            assert sentences == []
        zh, ja, rest, sentences, _ = pipe.finalize()
        assert (zh, ja, shown + rest, sentences) == (ZH, JA, ZH, [])
        assert [line["index"] for line in pipe.parser.normalized_lines()] == [0, 1]
        assert pipe.parser.normalized_lines()[0]["act"] == LINES[0]["act"]


def test_multibyte_transport_boundaries_and_no_partial_line_deltas():
    raw = reply().encode("utf-8")
    for split in range(len(raw) + 1):
        parser = StreamingBilingualParser()
        decoder = codecs.getincrementaldecoder("utf-8")()
        deltas = []
        for chunk in (raw[:split], raw[split:]):
            delta, sentences = parser.feed_chunk(decoder.decode(chunk))
            deltas.append(delta)
            assert sentences == []
            assert parser.chinese_extracted in ("", LINES[0]["zh"], ZH)
        assert parser.finalize() == (ZH, JA, [])
        assert "".join(deltas) == ZH


def test_truncation_at_every_character_retains_only_closed_lines():
    first = json.dumps(LINES[0], ensure_ascii=False)
    second = json.dumps(LINES[1], ensure_ascii=False)
    raw = '{"lines":[' + first + ',' + second + ']}'
    ends = [len('{"lines":[' + first), len(raw) - 2]
    for end in range(len('{"lines"'), len(raw) + 1):
        parser = StreamingBilingualParser()
        shown = ""
        for char in raw[:end]:
            delta, sentences = parser.feed_chunk(char)
            shown += delta
            assert sentences == []
        expected = LINES[:sum(end >= boundary for boundary in ends)]
        zh, ja, remaining = parser.finalize()
        assert (zh, ja, remaining) == ("".join(x["zh"] for x in expected), "".join(x["ja"] for x in expected), [])
        assert shown == zh
        assert parser.lines_truncated == (end < len(raw))


@pytest.mark.parametrize("lines", [LINES, [LINES[0]], [{"zh": "只有中文。", "ja": ""}],
                                      [{"zh": "", "ja": "日本語だけ。"}], []])
def test_parse_full_text_aggregates_without_fabricating_a_missing_language(lines):
    zh, ja, parser = StreamingBilingualParser.parse_full_text("前言\n```json\n" + reply(lines) + "\n```尾声")
    assert (zh, ja) == ("".join(x["zh"] for x in lines), "".join(x["ja"] for x in lines))
    assert len(parser.normalized_lines()) == len(lines)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_format", ["old", "lines"])
async def test_first_line_and_audio_before_provider_eof(coordinator_factory, reply_format):
    release = asyncio.Event()
    eof = asyncio.Event()

    async def tokens(*args, **kwargs):
        yield '{"lines":[' + json.dumps(LINES[0], ensure_ascii=False)
        await release.wait()
        yield ',' + json.dumps(LINES[1], ensure_ascii=False) + ']}'
        eof.set()

    coordinator, tts, _ = coordinator_factory(tokens=tokens, reply_format=reply_format)
    stream = coordinator.stream()
    events = []
    try:
        while not any(e["event"] == "line_audio" for e in events):
            events.append(await asyncio.wait_for(anext(stream), 2))
        assert not eof.is_set()
        assert [e["data"]["index"] for e in events if e["event"] == "line"] == [0]
        assert tts.synthesize_to_file.call_count == 1
        assert tts.synthesize_to_file.call_args.args == (LINES[0]["ja"],)
        release.set()
        events.extend([e async for e in stream])
        assert_lifecycle(events)
        assert events[-1]["data"]["chinese"] == ZH
        assert events[-1]["data"]["reply_format"] == "lines"
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_line_jobs_emotions_skips_errors_and_persistence(coordinator_factory, monkeypatch):
    lines = [*LINES, {"zh": "失败。", "ja": "失敗です。", "emotion": "angry"},
             {"zh": "无语音。", "ja": "", "emotion": "invalid"},
             {"zh": "难过，悲伤。", "ja": "悲しい。"}, {"zh": "沉默。", "ja": "……"}]

    async def tokens(*args, **kwargs):
        for char in reply(lines):
            yield char
            await asyncio.sleep(0)

    coordinator, tts, database = coordinator_factory(tokens=tokens, reply_format="lines")
    run = coordinator._init_stream_run()
    jobs = []
    put = coordinator._put_with_cancel

    async def capture(queue, item, *args):
        if queue is run.tts_queue and item is not None:
            jobs.append(item)
        return await put(queue, item, *args)

    monkeypatch.setattr(coordinator, "_init_stream_run", lambda: run)
    monkeypatch.setattr(coordinator, "_put_with_cancel", capture)
    events = [e async for e in coordinator]
    published, audio = assert_lifecycle(events)
    assert [x["status"] for x in audio] == ["ready", "ready", "failed", "skipped", "ready", "skipped"]
    assert jobs == [{"line": i, "ja": line["ja"], "emotion": published[i]["emotion"]} for i, line in enumerate(lines)]
    assert [x["emotion"] for x in published] == ["shy", "cool", "angry", "gentle", "sad", "gentle"]
    assert tts.synthesize_to_file.call_count == 4
    assert tts.synthesize_to_file.call_args_list[0].kwargs["options"]["emotion"] == "shy"
    assert all("line" in e["data"] for e in events if e["event"] in {"audio_chunk", "audio_chunk_error"})
    assert all("line" in chunk for chunk in events[-1]["data"]["chunks"])
    assert not run.aligner.has_chinese
    async with get_db(database) as conn:
        saved = (await crud.get_recent_messages(conn, "lines"))[-1]
    assert saved.lines == events[-1]["data"]["lines"]


@pytest.mark.asyncio
async def test_truncated_stream_has_only_complete_lines(coordinator_factory):
    async def tokens(*args, **kwargs):
        yield '{"lines":[' + json.dumps(LINES[0], ensure_ascii=False) + ',{"zh":"不能补全","ja":"未完'

    coordinator, _, _ = coordinator_factory(tokens=tokens)
    events = [e async for e in coordinator]
    lines, _ = assert_lifecycle(events)
    assert len(lines) == 1
    assert events[-1]["data"]["truncated"] is True
    assert events[-1]["data"]["chinese"] == LINES[0]["zh"]
    assert events[-1]["data"]["japanese"] == LINES[0]["ja"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "error", "disconnect"])
async def test_unfinished_line_audio_lifecycle(coordinator_factory, monkeypatch, ending):
    cancel, release, synthesis_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def tokens(*args, **kwargs):
        yield '{"lines":[' + json.dumps(LINES[0], ensure_ascii=False)
        await release.wait()
        raise RuntimeError("synthetic provider disconnect")

    coordinator, tts, database = coordinator_factory(tokens=tokens, cancel_event=cancel)
    run = coordinator._init_stream_run()
    monkeypatch.setattr(coordinator, "_init_stream_run", lambda: run)

    async def blocked_synthesis(*args, **kwargs):
        synthesis_started.set()
        await asyncio.Event().wait()

    tts.synthesize_to_file.side_effect = blocked_synthesis
    stream = coordinator.stream()
    try:
        while (await asyncio.wait_for(anext(stream), 2))["event"] != "line":
            pass
        await asyncio.wait_for(synthesis_started.wait(), 2)
        if ending == "disconnect":
            await asyncio.wait_for(stream.aclose(), 2)
        else:
            cancel.set() if ending == "cancel" else release.set()
            rest = await asyncio.wait_for(_collect(stream), 2)
            if ending == "cancel":
                assert rest[-1]["event"] == "done"
                assert rest[-1]["data"]["truncated"] is True
            else:
                assert rest[-1]["event"] == "error"
        assert run.producer_task.done() and run.worker_task.done()
        async with get_db(database) as conn:
            history = await crud.get_recent_messages(conn, "lines")
        assert [m.role for m in history] == ["user", "assistant"]
        assert history[-1].lines[0]["audio"] == {"status": "failed", "chunks": []}
        assert history[-1].content_chinese == LINES[0]["zh"]
    finally:
        release.set()
        await stream.aclose()


async def _collect(stream):
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_disconnect_under_lines_backpressure(coordinator_factory, monkeypatch):
    async def tokens(*args, **kwargs):
        yield reply(LINES * 150)

    coordinator, _, database = coordinator_factory(tokens=tokens)
    run = coordinator._init_stream_run()
    full = asyncio.Event()
    put = coordinator._put_with_cancel

    async def observe(queue, item, *args):
        if queue is run.event_queue and queue.full():
            full.set()
        return await put(queue, item, *args)

    monkeypatch.setattr(coordinator, "_init_stream_run", lambda: run)
    monkeypatch.setattr(coordinator, "_put_with_cancel", observe)
    stream = coordinator.stream()
    try:
        while (await asyncio.wait_for(anext(stream), 3))["event"] != "line":
            pass
        await asyncio.wait_for(full.wait(), 3)
        await asyncio.wait_for(stream.aclose(), 2)
        assert run.producer_task.done() and run.worker_task.done()
        async with get_db(database) as conn:
            saved = (await crud.get_recent_messages(conn, "lines"))[-1]
        assert [line["index"] for line in saved.lines] == [0]
        assert saved.lines[0]["audio"]["status"] in {"ready", "failed"}
    finally:
        await stream.aclose()


def test_request_format_defaults_and_validation():
    assert ChatRequest(prompt="hi").reply_format == "old"
    assert ChatRequest(prompt="hi", reply_format="lines").reply_format == "lines"
    with pytest.raises(ValidationError):
        ChatRequest(prompt="hi", reply_format="invalid")


@pytest.mark.asyncio
async def test_bounded_queues_preserve_chunk_then_terminal_order(coordinator_factory, monkeypatch):
    async def tokens(*args, **kwargs):
        yield reply(LINES * 6)

    coordinator, tts, _ = coordinator_factory(tokens=tokens)
    run = coordinator._init_stream_run()
    run.tts_queue = asyncio.Queue(maxsize=1)
    run.event_queue = asyncio.Queue(maxsize=1)
    monkeypatch.setattr(coordinator, "_init_stream_run", lambda: run)

    async def slow_consumer():
        events = []
        async for event in coordinator:
            events.append(event)
            await asyncio.sleep(0.001)
        return events

    events = await asyncio.wait_for(slow_consumer(), 5)
    lines, audio = assert_lifecycle(events)
    assert len(lines) == tts.synthesize_to_file.call_count == 12
    assert [x["status"] for x in audio] == ["ready"] * 12


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "stream"])
@pytest.mark.parametrize("reply_format", ["old", "lines"])
async def test_service_forwards_format_and_persists_aggregate(tmp_path, monkeypatch, mode, reply_format):
    database = tmp_path / "service.db"
    await init_db(database)

    async def tokens(*args, **kwargs):
        yield reply()

    tts = SimpleNamespace(audio_dir=tmp_path, synthesize_to_file=AsyncMock(return_value=("/audio/test.wav", tmp_path / "test.wav", {})))
    service = ChatService(db_path=database, tts_service=tts)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content=reply())), stream_chat=tokens), "test", "custom",
    ))
    service._extract_memory_safe = AsyncMock()
    context = AsyncMock(return_value=[])
    monkeypatch.setattr(service_module, "build_chat_context", context)
    coordinators = []
    original = service_module.StreamCoordinator

    def capture(**kwargs):
        coordinators.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(service_module, "StreamCoordinator", capture)
    try:
        if mode == "sync":
            result = await service.chat_sync("问题", session_id="service", reply_format=reply_format)
        else:
            result = (await _collect(service.stream_chat("问题", session_id="service", reply_format=reply_format)))[-1]["data"]
            assert coordinators[0]["reply_format"] == reply_format
        assert context.call_args.kwargs["reply_format"] == reply_format
        assert (result["chinese"], result["japanese"]) == (ZH, JA)
        assert [x["act"] for x in result["lines"]] == [x["act"] for x in LINES]
        async with get_db(database) as conn:
            saved = (await crud.get_recent_messages(conn, "service"))[-1]
        assert (saved.content_chinese, saved.content_japanese, saved.lines) == (ZH, JA, result["lines"])
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("lines", [LINES, [{"zh": "", "ja": "日本語だけ。"}]])
async def test_telegram_old_prompt_understands_lines_aggregate(tmp_path, monkeypatch, lines):
    database = tmp_path / "telegram.db"
    await init_db(database)
    service = ChatService(db_path=database)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content=reply(lines)))), "test", "custom",
    ))
    service.memory_service.process_user_message = AsyncMock()
    service.prepare_messages = AsyncMock(return_value=[])
    service.affection_service.handle_turn_affection = AsyncMock()
    handlers = TelegramBotHandlers(chat_service=service, db_path=str(database))
    bot = SimpleNamespace(send_message=AsyncMock(), send_chat_action=AsyncMock())
    voice = []

    def capture_voice(chat_id, bot, japanese, *args, **kwargs):
        voice.append(japanese)
        return asyncio.create_task(asyncio.sleep(0))

    monkeypatch.setattr(handlers, "_spawn_background_voice_task", capture_voice)
    try:
        await (await handlers.process_text_chat(4242, "问题", bot))
        zh, ja = "".join(x["zh"] for x in lines), "".join(x["ja"] for x in lines)
        assert bot.send_message.call_args.kwargs["text"] == (zh or ja)
        assert voice == [ja]
        assert service.prepare_messages.call_args.kwargs["reply_format"] == "old"
        async with get_db(database) as conn:
            saved = (await crud.get_recent_messages(conn, "tg_4242"))[-1]
        assert (saved.content_chinese, saved.content_japanese) == (zh or ja, ja)
        assert [x["ja"] for x in saved.lines] == [x["ja"] for x in lines]
    finally:
        await service.aclose()


def test_lines_tts_defaults_clamps_and_user_overrides():
    _, _, parser = StreamingBilingualParser.parse_full_text(reply())
    options = parser.get_dynamic_tts_options({"speed_factor": 1.2, "temp": 0.8}, sentence_text=JA)
    assert options["speed_factor"] == 1.2
    assert options["temp"] == 0.8
    options = parser.get_dynamic_tts_options(sentence_text=JA)
    assert options["speed"] == 1.05
    assert options["temperature"] == 0.95
    raw = json.dumps({"tts": {"speed": 999, "temp": -999}, "lines": LINES})
    _, _, parser = StreamingBilingualParser.parse_full_text(raw)
    assert parser.tts_speed < 999 and parser.tts_temperature > -999


@pytest.mark.asyncio
async def test_done_reports_a_legacy_reply_format(coordinator_factory):
    async def tokens(*args, **kwargs):
        yield '{"tts":{"emotion":"gentle"},"chinese":"你好。","japanese":"こんにちは。"}'

    coordinator, _, _ = coordinator_factory(tokens=tokens)
    events = [e async for e in coordinator]
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["reply_format"] == "legacy"
