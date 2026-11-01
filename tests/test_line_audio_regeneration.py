"""Explicit single-line repairs preserve text, history and the selected voice."""

import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import wave

from fastapi import FastAPI
import httpx
import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate, VoiceProfileCreate, VoiceProfileUpdate
from galgame2voice.database.session import get_db, immediate_transaction, init_db
from galgame2voice.routers.voice import router
from galgame2voice.services.tts_cache_manager import TtsCacheManager
from galgame2voice.services.tts_scheduler import TtsScheduler
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.voice_manager import get_voice_manager
from galgame2voice.services.chat_service import ChatService


def wav(amplitude):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(amplitude.to_bytes(2, "little", signed=True) * 32000)
    return output.getvalue()


@pytest.fixture
async def repair(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    db = str(tmp_path / "repair.db")
    await init_db(db)
    reference = tmp_path / "ref.wav"
    reference.write_bytes(wav(100))
    lines = [{"index": i, "zh": f"原中文{i}", "ja": f"こんにちは{i}。", "emotion": "neutral", "act": {},
              "audio": {"status": "failed", "chunks": []}} for i in range(2)]
    async with get_db(db) as conn:
        profiles = [await crud.create_voice_profile(conn, VoiceProfileCreate(
            name=name, gpt_weights_path="models/synthetic.ckpt", sovits_weights_path="models/synthetic.pth", ref_audio_path=str(reference),
            prompt_text="こんにちは", prompt_lang="ja",
        )) for name in ("原音色", "当前音色")]
        await crud.set_active_voice_profile(conn, profiles[0].id)
        message = await crud.add_message(conn, MessageCreate(
            session_id="repair-session", role="assistant", content_chinese="原中文0原中文1",
            content_japanese="こんにちは0。こんにちは1。", audio_url="/audio/old-full.wav", lines=lines,
        ))
    engine = SimpleNamespace(calls=[], current_refer_audio=None, current_gpt_weights=None, current_sovits_weights=None)
    async def synthesize(text, options=None):
        engine.calls.append((text, options))
        return wav(options["voice_profile_id"] * 100)
    engine.synthesize = synthesize
    scheduler = TtsScheduler()
    monkeypatch.setattr("galgame2voice.services.tts_service.get_tts_scheduler", lambda: scheduler)
    cache = TtsCacheManager(cache_dir=tmp_path / "audio/cache", db_path=db)
    service = TtsService(client=engine, audio_dir=tmp_path / "audio", cache_manager=cache, db_path=db)
    manager = SimpleNamespace(tts_service=service)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_voice_manager] = lambda: manager
    body = {"session_id": "repair-session", "message_id": message.id, "line_index": 1,
            "text": lines[1]["ja"], "voice_profile_id": profiles[1].id}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield SimpleNamespace(root=tmp_path, db=db, lines=lines, message=message, profiles=profiles,
                                  engine=engine, cache=cache, service=service, body=body, client=client)
    finally:
        await scheduler.aclose()
        await cache.aclose()


async def test_repair_regenerates_one_line_and_persists_without_rewriting_text(repair):
    # A nonempty but broken disk cache must not be returned by the repair.
    _, broken, _ = await repair.service.synthesize_to_file(repair.body["text"], {"voice_profile_id": repair.profiles[1].id})
    broken.write_bytes(b"broken cached WAV")
    repair.engine.calls.clear()
    response = await repair.client.post("/api/voice/regenerate-line", json=repair.body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["persisted"] is True
    audio = result["audio"]
    assert audio["voice_profile_id"] == repair.profiles[1].id and audio["voice_name"] == "当前音色"
    assert audio["generated_at"] > 0
    chunk = audio["chunks"][0]
    assert chunk["line"] == 1 and chunk["sentence"] == repair.body["text"]
    assert chunk["audio_url"].startswith("/audio/repair_")
    assert (repair.root / chunk["audio_url"].lstrip("/")).read_bytes() == wav(repair.profiles[1].id * 100)
    assert broken.read_bytes() == b"broken cached WAV"
    assert len(repair.engine.calls) == 1
    async with get_db(repair.db) as conn:
        row = await (await conn.execute("SELECT * FROM messages WHERE id=?", (repair.message.id,))).fetchone()
        stored = json.loads(row["lines_json"])
        assert stored[0] == repair.lines[0] and stored[1]["audio"] == audio
        assert stored[1]["zh"] == repair.lines[1]["zh"] and stored[1]["ja"] == repair.lines[1]["ja"]
        assert row["content_chinese"] == repair.message.content_chinese
        assert row["created_at"] == repair.message.created_at and row["audio_url"] == ""
        assert (await crud.get_active_voice_profile(conn)).id == repair.profiles[0].id


@pytest.mark.parametrize("patch,status", [
    ({"voice_profile_id": None}, 422), ({"voice_profile_id": 99999}, 404),
    ({"session_id": "other-session"}, 404), ({"line_index": 99}, 404),
    ({"text": "changed text"}, 409), ({"text": ""}, 422),
])
async def test_invalid_repair_does_not_request_the_engine(repair, patch, status):
    response = await repair.client.post("/api/voice/regenerate-line", json={**repair.body, **patch})
    assert response.status_code == status, response.text
    assert repair.engine.calls == []


async def test_browser_only_line_returns_an_independent_audio_url(repair):
    body = {**repair.body, "message_id": None}
    response = await repair.client.post("/api/voice/regenerate-line", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["persisted"] is False
    async with get_db(repair.db) as conn:
        row = await (await conn.execute("SELECT lines_json FROM messages WHERE id=?", (repair.message.id,))).fetchone()
        assert json.loads(row[0]) == repair.lines


async def test_unavailable_voice_context_prompts_retry_without_replacing_history(repair, monkeypatch):
    from galgame2voice.services.voice_resolver import get_voice_resolver

    resolver = get_voice_resolver()
    monkeypatch.setattr(resolver, "_fetch_raw_profile", AsyncMock(side_effect=OSError("synthetic unavailable DB")))
    response = await repair.client.post("/api/voice/regenerate-line", json=repair.body)
    assert response.status_code == 503
    assert "音色" in response.json()["detail"] and "稍后重试" in response.json()["detail"]
    assert repair.engine.calls == []
    async with get_db(repair.db) as conn:
        row = await (await conn.execute("SELECT lines_json FROM messages WHERE id=?", (repair.message.id,))).fetchone()
    assert json.loads(row[0]) == repair.lines


async def test_single_line_synthesis_failure_does_not_hang_waiting_for_disconnect():
    from starlette.requests import Request
    from galgame2voice.routers.line_audio import _synthesize_connected

    async def receive():
        await asyncio.Event().wait()

    async def fail(*args, **kwargs):
        raise ValueError("synthetic failed synthesis")

    before = asyncio.all_tasks()
    operation = asyncio.create_task(_synthesize_connected(
        Request({"type": "http"}, receive), SimpleNamespace(synthesize_to_file=fail), "hello", {},
    ))
    try:
        done, _ = await asyncio.wait({operation}, timeout=0.3)
        assert operation in done, "failed synthesis must not wait forever for client disconnect"
        with pytest.raises(ValueError, match="synthetic failed synthesis"):
            await operation
    finally:
        # Avoid stranding the buggy watcher when this regression fails before the fix.
        for _ in range(3):
            for task in asyncio.all_tasks() - before - {operation}:
                if "disconnected" in task.get_coro().__qualname__:
                    task.cancel()
            if operation.done():
                break
            await asyncio.sleep(0.05)
        await asyncio.gather(operation, return_exceptions=True)


async def test_engine_failure_preserves_original_line(repair):
    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic engine unavailable")
    repair.engine.synthesize = fail
    response = await repair.client.post("/api/voice/regenerate-line", json=repair.body)
    assert response.status_code == 502 and "原音频" in response.json()["detail"]
    async with get_db(repair.db) as conn:
        row = await (await conn.execute("SELECT lines_json FROM messages WHERE id=?", (repair.message.id,))).fetchone()
        assert json.loads(row[0]) == repair.lines


@pytest.mark.parametrize("missing, expected", [("model", "模型"), ("reference", "参考音频")])
async def test_missing_voice_assets_explain_how_to_fix_without_mutating_history(repair, missing, expected):
    update = VoiceProfileUpdate(**({"gpt_weights_path": ""} if missing == "model" else {"ref_audio_path": ""}))
    async with get_db(repair.db) as conn:
        await crud.update_voice_profile(conn, repair.profiles[1].id, update)
    response = await repair.client.post("/api/voice/regenerate-line", json=repair.body)
    assert response.status_code == 422, response.text
    assert expected in response.json()["detail"] and "设置中心" in response.json()["detail"]
    assert repair.engine.calls == []
    assert not list((repair.root / "audio").glob("repair_*.wav"))
    async with get_db(repair.db) as conn:
        row = await (await conn.execute("SELECT lines_json FROM messages WHERE id=?", (repair.message.id,))).fetchone()
        assert json.loads(row[0]) == repair.lines


@pytest.mark.parametrize("change", ["target", "other", "delete"])
async def test_repair_merges_only_its_unchanged_target_after_inference(repair, change):
    started, release = asyncio.Event(), asyncio.Event()
    original = repair.engine.synthesize
    async def delayed(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)
    repair.engine.synthesize = delayed
    pending = asyncio.create_task(repair.client.post("/api/voice/regenerate-line", json=repair.body))
    try:
        await asyncio.wait_for(started.wait(), 3)
        updated = json.loads(json.dumps(repair.lines))
        async with get_db(repair.db) as conn:
            async with immediate_transaction(conn):
                if change == "delete":
                    await conn.execute("DELETE FROM messages WHERE id=?", (repair.message.id,))
                else:
                    updated[1 if change == "target" else 0]["audio"] = {"status": "skipped", "chunks": []}
                    await conn.execute("UPDATE messages SET lines_json=? WHERE id=?", (json.dumps(updated), repair.message.id))
        release.set()
        response = await asyncio.wait_for(pending, 5)
        assert response.status_code == {"target": 409, "other": 200, "delete": 404}[change], response.text
        assert len(list((repair.root / "audio").glob("repair_*.wav"))) == (1 if change == "other" else 0)
        async with get_db(repair.db) as conn:
            row = await (await conn.execute("SELECT lines_json FROM messages WHERE id=?", (repair.message.id,))).fetchone()
        if change == "delete":
            assert row is None
        else:
            stored = json.loads(row[0])
            assert stored[0] == updated[0]
            assert stored[1]["audio"] == (response.json()["audio"] if change == "other" else updated[1]["audio"])
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_client_disconnect_cancels_owned_inference_without_publishing(repair):
    from galgame2voice.routers.line_audio import LineAudioRequest, regenerate_line
    disconnected, started, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Request:
        async def is_disconnected(self):
            return disconnected.is_set()
        async def receive(self):
            await disconnected.wait()
            return {"type": "http.disconnect"}
    async def blocked(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
    repair.engine.synthesize = blocked
    operation = asyncio.create_task(regenerate_line(LineAudioRequest(**repair.body), Request(), SimpleNamespace(tts_service=repair.service)))
    try:
        await asyncio.wait_for(started.wait(), 3)
        disconnected.set()
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(operation, 3)
        assert error.value.status_code == 499 and finished.is_set()
        async with get_db(repair.db) as conn:
            row = await (await conn.execute("SELECT lines_json FROM messages WHERE id=?", (repair.message.id,))).fetchone()
            assert json.loads(row[0]) == repair.lines
    finally:
        disconnected.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_completed_stream_publishes_the_committed_message_id(repair):
    async def tokens(*args, **kwargs):
        yield json.dumps({"lines": [{"zh": "新的一句", "ja": "新しい台詞。", "emotion": "neutral"}]}, ensure_ascii=False)
    chat = ChatService(tts_service=repair.service, db_path=repair.db)
    chat._get_active_llm_adapter = AsyncMock(return_value=(SimpleNamespace(stream_chat=tokens, api_key="synthetic"), "synthetic", "custom"))
    chat._extract_memory_safe = AsyncMock()
    try:
        events = [event async for event in chat.stream_chat("问题", session_id="new-turn", reply_format="lines",
                                                          tts_options={"voice_profile_id": repair.profiles[1].id})]
        done = next(event["data"] for event in events if event["event"] == "done")
        assert isinstance(done["message_id"], int)
        async with get_db(repair.db) as conn:
            row = await (await conn.execute("SELECT role, content_chinese FROM messages WHERE id=? AND session_id=?",
                                           (done["message_id"], "new-turn"))).fetchone()
        assert tuple(row) == ("assistant", "新的一句")
    finally:
        await chat.aclose()


@pytest.mark.parametrize("failure", ["deleted", "lookup-error", "missing-name"])
async def test_an_unavailable_explicit_voice_never_falls_back_to_engine_defaults(repair, monkeypatch, failure):
    selector = {"voice_profile_id": repair.profiles[1].id}
    if failure == "deleted":
        async with get_db(repair.db) as conn:
            await crud.delete_voice_profile(conn, repair.profiles[1].id)
    elif failure == "missing-name":
        selector = {"character_name": "并不存在的音色"}
    else:
        from galgame2voice.services.voice_resolver import get_voice_resolver
        monkeypatch.setattr(get_voice_resolver(), "resolve_context", AsyncMock(side_effect=OSError("synthetic lookup failure")))
    with pytest.raises(ValueError, match="音色"):
        await repair.service.synthesize("こんにちは。", selector)
    assert repair.engine.calls == []


@pytest.mark.parametrize("age,retained", [(3600, True), (8 * 86400, False)])
async def test_repaired_history_survives_temporary_audio_cleanup_for_seven_days(repair, monkeypatch, age, retained):
    import os
    import time
    from galgame2voice.services import audio_cleaner

    response = await repair.client.post("/api/voice/regenerate-line", json=repair.body)
    assert response.status_code == 200, response.text
    path = repair.root / response.json()["audio"]["chunks"][0]["audio_url"].lstrip("/")
    old = time.time() - age
    os.utime(path, (old, old))
    temporary = repair.root / "audio/voice_unpublished.wav"
    temporary.write_bytes(b"temporary synthesis")
    os.utime(temporary, (old, old))
    protected = repair.root / "audio/repair_ffffffffffff.wav"
    protected.write_bytes(wav(100))
    expired_reference = time.time() - 10 * 86400
    os.utime(protected, (expired_reference, expired_reference))
    from galgame2voice.database.models import VoiceProfileUpdate
    async with get_db(repair.db) as conn:
        await crud.update_voice_profile(conn, repair.profiles[0].id, VoiceProfileUpdate(ref_audio_path=str(protected)))
    monkeypatch.setattr(audio_cleaner, "_resolve_get_db", lambda: lambda: get_db(repair.db))
    await audio_cleaner._run_audio_cleanup_cycle(repair.root / "audio")
    assert path.exists() is retained
    assert not temporary.exists()
    assert (repair.root / "ref.wav").exists()
    assert protected.is_file()


async def test_disconnected_completed_repair_discards_only_its_unpublished_file(repair):
    from fastapi import HTTPException
    from galgame2voice.routers.line_audio import LineAudioRequest, regenerate_line

    disconnected = asyncio.Event()
    class Request:
        async def is_disconnected(self):
            return disconnected.is_set()
        async def receive(self):
            await disconnected.wait()
            return {"type": "http.disconnect"}
    original = repair.service.synthesize_to_file
    async def completed(*args, **kwargs):
        result = await original(*args, **kwargs)
        disconnected.set()
        return result
    repair.service.synthesize_to_file = completed
    with pytest.raises(HTTPException) as error:
        await regenerate_line(LineAudioRequest(**repair.body), Request(), SimpleNamespace(tts_service=repair.service))
    assert error.value.status_code == 499
    assert not list((repair.root / "audio").glob("repair_*.wav"))
    async with get_db(repair.db) as conn:
        row = await (await conn.execute("SELECT lines_json FROM messages WHERE id=?", (repair.message.id,))).fetchone()
    assert json.loads(row[0]) == repair.lines


async def test_disconnect_while_waiting_for_history_write_cannot_publish_audio(repair, monkeypatch):
    from contextlib import asynccontextmanager
    from fastapi import HTTPException
    from galgame2voice.routers import line_audio

    disconnected = False
    class Request:
        async def is_disconnected(self):
            return disconnected
        async def receive(self):
            await asyncio.Event().wait()
    @asynccontextmanager
    async def acquired_after_disconnect(conn):
        nonlocal disconnected
        async with immediate_transaction(conn):
            disconnected = True
            yield conn
    monkeypatch.setattr(line_audio, "immediate_transaction", acquired_after_disconnect)
    with pytest.raises(HTTPException) as error:
        await line_audio.regenerate_line(line_audio.LineAudioRequest(**repair.body), Request(), SimpleNamespace(tts_service=repair.service))
    assert error.value.status_code == 499
    assert not list((repair.root / "audio").glob("repair_*.wav"))
    async with get_db(repair.db) as conn:
        row = await (await conn.execute("SELECT lines_json FROM messages WHERE id=?", (repair.message.id,))).fetchone()
    assert json.loads(row[0]) == repair.lines


async def test_cancellation_after_durable_commit_keeps_the_referenced_repair(repair, monkeypatch):
    import aiosqlite
    from galgame2voice.routers.line_audio import LineAudioRequest, regenerate_line

    class Request:
        async def is_disconnected(self):
            return False
        async def receive(self):
            await asyncio.Event().wait()
    real_commit = aiosqlite.Connection.commit
    operation = None
    async def commit_then_cancel(conn):
        await real_commit(conn)
        operation.cancel()
    monkeypatch.setattr(aiosqlite.Connection, "commit", commit_then_cancel)
    operation = asyncio.create_task(regenerate_line(LineAudioRequest(**repair.body), Request(), SimpleNamespace(tts_service=repair.service)))
    with pytest.raises(asyncio.CancelledError):
        await operation
    monkeypatch.setattr(aiosqlite.Connection, "commit", real_commit)
    async with get_db(repair.db) as conn:
        row = await (await conn.execute("SELECT lines_json FROM messages WHERE id=?", (repair.message.id,))).fetchone()
    stored = json.loads(row[0])[1]["audio"]
    assert stored["status"] == "ready"
    assert (repair.root / stored["chunks"][0]["audio_url"].lstrip("/")).is_file()


async def test_empty_repair_does_not_leave_a_dead_audio_file(repair):
    repair.engine.synthesize = AsyncMock(return_value=b"")
    response = await repair.client.post("/api/voice/regenerate-line", json=repair.body)
    assert response.status_code == 502
    assert not list((repair.root / "audio").glob("repair_*.wav"))


@pytest.mark.parametrize(
    ("raw_reply", "expected_zh", "expected_ja"),
    [
        (
            f"中文：早安。今天有很多话要说。\n日文：おはよう。{'あ' * 10005}。",
            "早安。今天有很多话要说。",
            f"おはよう。{'あ' * 10005}。",
        ),
        (
            f"中文：今天有很多话要说。早安。\n日文：{'あ' * 6000}、{'い' * 5000}。おはよう。",
            "今天有很多话要说。早安。",
            f"{'あ' * 6000}、{'い' * 5000}。おはよう。",
        ),
    ],
    ids=["tail_overlong", "comma_overlong"],
)
async def test_fallback_reply_with_overlong_line_keeps_lines_json_and_regenerates_short_line(
    repair, raw_reply, expected_zh, expected_ja,
):
    async def tokens(*args, **kwargs):
        for pos in range(0, len(raw_reply), 1024):
            yield raw_reply[pos:pos + 1024]

    session_id = "fallback-overlong-turn"
    chat = ChatService(tts_service=repair.service, db_path=repair.db)
    chat._get_active_llm_adapter = AsyncMock(
        return_value=(SimpleNamespace(stream_chat=tokens, api_key="synthetic"), "synthetic", "custom")
    )
    chat._extract_memory_safe = AsyncMock()
    try:
        events = [
            event
            async for event in chat.stream_chat(
                "问题",
                session_id=session_id,
                reply_format="lines",
                tts_options={"voice_profile_id": repair.profiles[0].id},
            )
        ]
    finally:
        await chat.aclose()

    done = next(event["data"] for event in events if event["event"] == "done")
    assert done["reply_format"] == "unknown"
    assert isinstance(done["message_id"], int)

    async with get_db(repair.db) as conn:
        row = await (
            await conn.execute(
                "SELECT lines_json FROM messages WHERE id=? AND session_id=?",
                (done["message_id"], session_id),
            )
        ).fetchone()
    assert row is not None and row["lines_json"] is not None
    stored_lines = json.loads(row["lines_json"])
    assert len(stored_lines) >= 3
    assert all(len(line["zh"]) <= 10000 and len(line["ja"]) <= 10000 for line in stored_lines)
    assert "".join(line["zh"] for line in stored_lines) == expected_zh
    assert "".join(line["ja"] for line in stored_lines) == expected_ja

    short_line = next(line for line in stored_lines if line["ja"] == "おはよう。")
    repair.engine.calls.clear()
    response = await repair.client.post(
        "/api/voice/regenerate-line",
        json={
            "session_id": session_id,
            "message_id": done["message_id"],
            "line_index": short_line["index"],
            "text": "おはよう。",
            "voice_profile_id": repair.profiles[1].id,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["persisted"] is True
    assert payload["audio"]["voice_profile_id"] == repair.profiles[1].id
