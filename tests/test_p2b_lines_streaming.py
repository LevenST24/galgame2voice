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
from galgame2voice.services.chat_pipelines.context_builder import BuiltContext
from galgame2voice.services.chat_pipelines.text_pipeline import TextSegmentationPipeline
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.streaming_parser import StreamingBilingualParser
from tests.test_stream_reply_lines import assert_lifecycle, coordinator_factory  # noqa: F401


LINES = [
    {"index": 900, "zh": '你好，"小猫"。\\路径\n下一句😀', "ja": 'こんにちは、猫です。次です。😀',
     "emotion": "shy", "act": {"face": "smile", "motion": "nod", "blush": False}},
    {"zh": "再见。", "ja": "さようなら。", "emotion": "cool", "act": {"fx": None}},
]
ZH = "".join(line["zh"] for line in LINES)
JA = "".join(line["ja"] for line in LINES)
MENU = {"faces": ["smile"], "poses": ["a"], "blush": False}
# An unknown face, a pose and blush the pack lacks, beside a valid motion and face.
DIRTY_LINES = [
    {"zh": "你好。", "ja": "こんにちは。", "emotion": "shy",
     "act": {"face": "unknown", "pose": "b", "blush": True, "motion": "nod"}},
    {"zh": "再见。", "ja": "さようなら。", "emotion": "cool", "act": {"face": "smile"}},
]


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
def test_finalize_aggregates_without_fabricating_a_missing_language(lines):
    parser = StreamingBilingualParser()
    parser.feed_chunk("前言\n```json\n" + reply(lines) + "\n```尾声")
    zh, ja, _ = parser.finalize()
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
    assert ChatRequest(prompt="hi").reply_format == "lines"
    assert ChatRequest(prompt="hi", reply_format="old").reply_format == "old"
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
@pytest.mark.parametrize("reply_format", ["old", "lines"])
@pytest.mark.parametrize(("lines", "acts"), [
    (LINES, [line["act"] for line in LINES]),
    (DIRTY_LINES, [{"motion": "nod"}, {"face": "smile"}]),
], ids=["valid", "dirty"])
async def test_service_forwards_format_and_persists_aggregate(tmp_path, monkeypatch, reply_format, lines, acts):
    database = tmp_path / "service.db"
    await init_db(database)

    async def tokens(*args, **kwargs):
        yield reply(lines)

    tts = SimpleNamespace(audio_dir=tmp_path, synthesize_to_file=AsyncMock(return_value=("/audio/test.wav", tmp_path / "test.wav", {})))
    service = ChatService(db_path=database, tts_service=tts)
    service._get_active_llm_adapter = AsyncMock(return_value=(
        SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content=reply(lines))), stream_chat=tokens), "test", "custom",
    ))
    service._extract_memory_safe = AsyncMock()
    # The turn's acting menu travels with the built context.
    context = AsyncMock(return_value=BuiltContext([], menu=MENU))
    monkeypatch.setattr(service_module, "build_chat_context", context)
    coordinators = []
    original = service_module.StreamCoordinator

    def capture(**kwargs):
        coordinators.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(service_module, "StreamCoordinator", capture)
    try:
        events = await _collect(service.stream_chat("问题", session_id="service", reply_format=reply_format))
        result = events[-1]["data"]
        assert coordinators[0]["reply_format"] == "lines"
        assert [event["data"]["act"] for event in events if event["event"] == "line"] == acts
        assert context.call_args.kwargs["reply_format"] == "lines"
        zh, ja = "".join(line["zh"] for line in lines), "".join(line["ja"] for line in lines)
        assert (result["chinese"], result["japanese"]) == (zh, ja)
        assert [x["act"] for x in result["lines"]] == acts
        async with get_db(database) as conn:
            saved = (await crud.get_recent_messages(conn, "service"))[-1]
        assert (saved.content_chinese, saved.content_japanese, saved.lines) == (zh, ja, result["lines"])
    finally:
        await service.aclose()


def test_lines_tts_defaults_clamps_and_user_overrides():
    parser = StreamingBilingualParser()
    parser.feed_chunk(reply())
    parser.finalize()
    options = parser.get_dynamic_tts_options({"speed_factor": 1.2, "temp": 0.8}, sentence_text=JA)
    assert options["speed_factor"] == 1.2
    assert options["temp"] == 0.8
    options = parser.get_dynamic_tts_options(sentence_text=JA)
    assert options["speed"] == 1.05
    assert options["temperature"] == 0.95
    raw = json.dumps({"tts": {"speed": 999, "temp": -999}, "lines": LINES})
    parser = StreamingBilingualParser()
    parser.feed_chunk(raw)
    parser.finalize()
    assert parser.tts_speed < 999 and parser.tts_temperature > -999


def _parse_sse_frames(raw: str) -> list[dict]:
    frames = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        ev_type = "message"
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event:"):
                ev_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if data_lines:
            frames.append({"event": ev_type, "data": json.loads("\n".join(data_lines))})
    return frames


@pytest.mark.asyncio
async def test_explicit_old_reply_format_uses_lines_contract_and_parser_without_422(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from galgame2voice import config
    from galgame2voice.main import create_app
    from galgame2voice.routers.chat import set_chat_service

    db_file = tmp_path / "explicit-old.db"
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db_file))
    await init_db(db_file)

    captured_messages: list[list] = []

    async def stream_tokens(messages, *args, **kwargs):
        captured_messages.append(list(messages))
        yield reply(LINES)

    wav_path = audio_dir / "line.wav"
    wav_path.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
    tts = SimpleNamespace(
        audio_dir=audio_dir,
        synthesize_to_file=AsyncMock(return_value=("/audio/line.wav", wav_path, {})),
    )
    service = ChatService(db_path=db_file, tts_service=tts)
    service._get_active_llm_adapter = AsyncMock(
        return_value=(SimpleNamespace(stream_chat=stream_tokens, api_key="sk-test"), "mock-model", "deepseek")
    )
    service._extract_memory_safe = AsyncMock()
    # Keep the data directory the database was seeded in. Without DPAPI (Linux, macOS) the seeded console token is
    # encrypted with the master key file in that directory, and decrypting it elsewhere raises MasterKeyError.
    data_dir = config.get_settings().data_dir
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(audio_dir=audio_dir, data_dir=data_dir))
    app = create_app()
    set_chat_service(service)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/chat/stream", json={
                "prompt": "旧标签页发来的请求",
                "session_id": "s_old_tab",
                "reply_format": "old",
            })
            assert resp.status_code == 200, resp.text
            events = _parse_sse_frames(resp.text)
            assert len(captured_messages) == 1
            sys_prompt = captured_messages[0][0].content
            assert '"lines":' in sys_prompt
            assert '"chinese":' not in sys_prompt
            line_events = [e["data"] for e in events if e["event"] == "line"]
            assert [item["zh"] for item in line_events] == [item["zh"] for item in LINES]
            done = next(e["data"] for e in events if e["event"] == "done")
            assert done["reply_format"] == "lines"
            assert [item["zh"] for item in done["lines"]] == [item["zh"] for item in LINES]

            # Second turn with explicit "old" also replays assistant history in lines JSON
            resp2 = await client.post("/api/chat/stream", json={
                "prompt": "第二轮请求",
                "session_id": "s_old_tab",
                "reply_format": "old",
            })
            assert resp2.status_code == 200, resp2.text
            assistant_ctx = [m for m in captured_messages[1] if m.role == "assistant"]
            assert len(assistant_ctx) == 1
            replayed = json.loads(assistant_ctx[0].content)
            assert "lines" in replayed and "chinese" not in replayed
    finally:
        set_chat_service(None)
        await service.aclose()


@pytest.mark.asyncio
async def test_historical_legacy_message_displays_replays_and_regenerates_audio(tmp_path, monkeypatch):
    import io
    import wave
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from galgame2voice.config import get_settings
    from galgame2voice.database.models import MessageCreate, VoiceProfileCreate
    from galgame2voice.routers import chat as chat_router
    from galgame2voice.routers.voice import router as voice_router
    from galgame2voice.services.tts_cache_manager import TtsCacheManager
    from galgame2voice.services.tts_scheduler import TtsScheduler
    from galgame2voice.services.tts_service import TtsService
    from galgame2voice.services.voice_manager import get_voice_manager

    def make_pcm_wav(amplitude: int = 120) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(amplitude.to_bytes(2, "little", signed=True) * 8000)
        return buf.getvalue()

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    db_file = tmp_path / "legacy-history.db"
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    ref_wav = tmp_path / "ref.wav"
    ref_wav.write_bytes(make_pcm_wav(100))
    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", str(db_file))
    await init_db(db_file)

    async with get_db(db_file) as conn:
        profile = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="历史角色",
            gpt_weights_path="models/m.ckpt",
            sovits_weights_path="models/m.pth",
            ref_audio_path=str(ref_wav),
            prompt_text="テスト",
            prompt_lang="ja",
            text_lang="ja",
            is_default=True,
        ))
        await crud.set_active_voice_profile(conn, profile.id)
        await crud.add_message(conn, MessageCreate(
            session_id="s_legacy_hist",
            role="user",
            content_chinese="以前的问候",
        ))
        legacy_msg = await crud.add_message(conn, MessageCreate(
            session_id="s_legacy_hist",
            role="assistant",
            content_chinese="旧版第一句。旧版第二句。",
            content_japanese="旧版の一文目。旧版の二文目。",
            audio_url="/audio/legacy-whole.wav",
            lines=None,
        ))

    captured_messages: list[list] = []

    async def stream_tokens(messages, *args, **kwargs):
        captured_messages.append(list(messages))
        yield reply(LINES)

    engine = SimpleNamespace(
        current_refer_audio=None,
        current_gpt_weights=None,
        current_sovits_weights=None,
        synthesize=AsyncMock(return_value=make_pcm_wav(180)),
    )
    scheduler = TtsScheduler()
    monkeypatch.setattr("galgame2voice.services.tts_service.get_tts_scheduler", lambda: scheduler)
    cache = TtsCacheManager(cache_dir=audio_dir / "cache", db_path=str(db_file))
    tts_service = TtsService(client=engine, audio_dir=audio_dir, cache_manager=cache, db_path=str(db_file))
    service = ChatService(db_path=db_file, tts_service=tts_service)
    service._get_active_llm_adapter = AsyncMock(
        return_value=(SimpleNamespace(stream_chat=stream_tokens, api_key="sk-test"), "mock-model", "deepseek")
    )
    service._extract_memory_safe = AsyncMock()

    app = FastAPI()
    app.include_router(chat_router.router)
    app.include_router(voice_router)
    chat_router.set_chat_service(service)
    app.dependency_overrides[get_voice_manager] = lambda: SimpleNamespace(tts_service=tts_service)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. History API returns the legacy message with lines=None and original whole-text + audio_url
            hist_resp = await client.get("/api/chat/history", params={"session_id": "s_legacy_hist"})
            assert hist_resp.status_code == 200, hist_resp.text
            hist_msgs = hist_resp.json()["messages"]
            legacy_out = next(m for m in hist_msgs if m["id"] == legacy_msg.id)
            assert legacy_out["lines"] is None
            assert legacy_out["content_chinese"] == "旧版第一句。旧版第二句。"
            assert legacy_out["content_japanese"] == "旧版の一文目。旧版の二文目。"
            assert legacy_out["audio_url"] == "/audio/legacy-whole.wav"

            # 2. Default /api/chat/stream (no reply_format) replays legacy turn in lines format without rewriting DB row
            stream_resp = await client.post("/api/chat/stream", json={
                "prompt": "继续聊天",
                "session_id": "s_legacy_hist",
            })
            assert stream_resp.status_code == 200, stream_resp.text
            assistant_ctx = [m for m in captured_messages[0] if m.role == "assistant"]
            assert len(assistant_ctx) == 1
            replayed = json.loads(assistant_ctx[0].content)
            assert [item["zh"] for item in replayed["lines"]] == ["旧版第一句。", "旧版第二句。"]
            assert [item["ja"] for item in replayed["lines"]] == ["旧版の一文目。", "旧版の二文目。"]

            async with get_db(db_file) as conn:
                row_still_null = await (
                    await conn.execute("SELECT lines_json FROM messages WHERE id=?", (legacy_msg.id,))
                ).fetchone()
                assert row_still_null["lines_json"] is None

            # 3. Line audio regeneration on line 0 of the historical legacy message still works
            regen_resp = await client.post("/api/voice/regenerate-line", json={
                "session_id": "s_legacy_hist",
                "message_id": legacy_msg.id,
                "line_index": 0,
                "text": "旧版の一文目。旧版の二文目。",
                "voice_profile_id": profile.id,
            })
            assert regen_resp.status_code == 200, regen_resp.text
            regen_data = regen_resp.json()
            assert regen_data["persisted"] is True
            assert regen_data["audio"]["status"] == "ready"
            assert regen_data["audio"]["chunks"][0]["audio_url"].startswith("/audio/repair_")
    finally:
        chat_router.set_chat_service(None)
        await service.aclose()
        await scheduler.aclose()
        await cache.aclose()
        get_settings.cache_clear()
