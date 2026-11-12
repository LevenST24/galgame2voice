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
from galgame2voice.database.models import MessageCreate, VoiceProfileCreate, VoiceProfileUpdate, decode_lines
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


async def test_legacy_assistant_message_with_null_lines_regenerates_line_zero(repair):
    async with get_db(repair.db) as conn:
        legacy_msg = await crud.add_message(conn, MessageCreate(
            session_id="repair-session", role="assistant", content_chinese="旧版中文台词",
            content_japanese="旧版の日本語セリフ。", audio_url="/audio/old-full.wav", lines=None,
        ))
        row_before = await (await conn.execute("SELECT lines_json, audio_url FROM messages WHERE id=?", (legacy_msg.id,))).fetchone()
        assert row_before["lines_json"] is None
        assert row_before["audio_url"] == "/audio/old-full.wav"

    body = {
        "session_id": "repair-session",
        "message_id": legacy_msg.id,
        "line_index": 0,
        "text": "旧版の日本語セリフ。",
        "voice_profile_id": repair.profiles[1].id,
    }
    response = await repair.client.post("/api/voice/regenerate-line", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["persisted"] is True
    assert result["line_index"] == 0
    assert result["audio"]["voice_profile_id"] == repair.profiles[1].id
    assert result["audio"]["voice_name"] == "当前音色"
    chunk = result["audio"]["chunks"][0]
    assert chunk["line"] == 0
    assert chunk["sentence"] == "旧版の日本語セリフ。"
    assert chunk["audio_url"].startswith("/audio/repair_")

    async with get_db(repair.db) as conn:
        row_after = await (await conn.execute("SELECT lines_json, audio_url FROM messages WHERE id=?", (legacy_msg.id,))).fetchone()
        assert row_after["audio_url"] == ""
        stored_lines = json.loads(row_after["lines_json"])
        assert len(stored_lines) == 1
        line0 = stored_lines[0]
        assert line0["index"] == 0
        assert line0["zh"] == "旧版中文台词"
        assert line0["ja"] == "旧版の日本語セリフ。"
        assert line0["emotion"] == "neutral"
        assert line0["act"] == {}
        assert line0["audio"]["status"] == "ready"


@pytest.mark.parametrize("mismatch", ["role", "session", "text", "index"])
async def test_legacy_message_wrong_role_or_session_or_text_or_index_returns_error(repair, mismatch):
    async with get_db(repair.db) as conn:
        msg = await crud.add_message(conn, MessageCreate(
            session_id="repair-session",
            role="user" if mismatch == "role" else "assistant",
            content_chinese="原中文",
            content_japanese="日本語セリフ。",
            audio_url="/audio/old.wav",
            lines=None,
        ))

    body = {
        "session_id": "other-session" if mismatch == "session" else "repair-session",
        "message_id": msg.id,
        "line_index": 1 if mismatch == "index" else 0,
        "text": "違う日本語。" if mismatch == "text" else "日本語セリフ。",
        "voice_profile_id": repair.profiles[1].id,
    }
    response = await repair.client.post("/api/voice/regenerate-line", json=body)
    expected_status = 409 if mismatch == "text" else 404
    assert response.status_code == expected_status, response.text
    assert not list((repair.root / "audio").glob("repair_*.wav"))

    async with get_db(repair.db) as conn:
        row = await (await conn.execute("SELECT lines_json, audio_url FROM messages WHERE id=?", (msg.id,))).fetchone()
        assert row["lines_json"] is None
        assert row["audio_url"] == "/audio/old.wav"


@pytest.mark.parametrize("corrupt_val", [
    "{bad json",
    '{"not": "a list"}',
    '[{"index": 0, "bad": "schema"}]',
    "[]",
], ids=["malformed_json", "not_a_list", "invalid_schema", "empty_array"])
async def test_corrupt_or_empty_lines_json_not_treated_as_legacy(repair, corrupt_val):
    async with get_db(repair.db) as conn:
        async with immediate_transaction(conn):
            cursor = await conn.execute(
                "INSERT INTO messages (session_id, role, content_chinese, content_japanese, audio_url, lines_json) "
                "VALUES (?, 'assistant', '测试中文', 'テスト日本語。', '/audio/corrupt.wav', ?)",
                ("repair-session", corrupt_val),
            )
            msg_id = cursor.lastrowid

    body = {
        "session_id": "repair-session",
        "message_id": msg_id,
        "line_index": 0,
        "text": "テスト日本語。",
        "voice_profile_id": repair.profiles[1].id,
    }
    response = await repair.client.post("/api/voice/regenerate-line", json=body)
    expected_status = 404 if corrupt_val == "[]" else 422
    assert response.status_code == expected_status, response.text
    if expected_status == 422:
        assert "历史台词数据损坏，原记录未修改。" in response.text
    assert not list((repair.root / "audio").glob("repair_*.wav"))

    async with get_db(repair.db) as conn:
        row = await (await conn.execute("SELECT lines_json, audio_url FROM messages WHERE id=?", (msg_id,))).fetchone()
        assert row["lines_json"] == corrupt_val
        assert row["audio_url"] == "/audio/corrupt.wav"


async def test_legacy_repair_failure_and_delete_preserves_db_and_discards_audio(repair):
    async with get_db(repair.db) as conn:
        legacy_msg = await crud.add_message(conn, MessageCreate(
            session_id="repair-session", role="assistant", content_chinese="旧版中文",
            content_japanese="旧版日文。", audio_url="/audio/old.wav", lines=None,
        ))

    # 1. Synthesis error -> 502, DB preserved, no orphan audio
    original_engine_synth = repair.engine.synthesize
    repair.engine.synthesize = AsyncMock(side_effect=RuntimeError("TTS engine crashed"))
    body = {
        "session_id": "repair-session",
        "message_id": legacy_msg.id,
        "line_index": 0,
        "text": "旧版日文。",
        "voice_profile_id": repair.profiles[1].id,
    }
    resp = await repair.client.post("/api/voice/regenerate-line", json=body)
    assert resp.status_code == 502
    assert not list((repair.root / "audio").glob("repair_*.wav"))

    # 2. Deletion during synthesis -> 404, orphan audio discarded
    started, release = asyncio.Event(), asyncio.Event()
    original_synth = repair.service.synthesize_to_file

    async def delayed_synth(*args, **kwargs):
        started.set()
        await release.wait()
        return await original_synth(*args, **kwargs)

    repair.engine.synthesize = original_engine_synth  # restore engine synthesize
    repair.service.synthesize_to_file = delayed_synth
    pending = asyncio.create_task(repair.client.post("/api/voice/regenerate-line", json=body))
    try:
        await asyncio.wait_for(started.wait(), 3)
        async with get_db(repair.db) as conn:
            async with immediate_transaction(conn):
                await conn.execute("DELETE FROM messages WHERE id=?", (legacy_msg.id,))
        release.set()
        del_resp = await asyncio.wait_for(pending, 5)
        assert del_resp.status_code == 404
        assert not list((repair.root / "audio").glob("repair_*.wav"))
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_legacy_repair_concurrent_contenders_race(repair):
    async with get_db(repair.db) as conn:
        legacy_msg = await crud.add_message(conn, MessageCreate(
            session_id="repair-session", role="assistant", content_chinese="旧版中文",
            content_japanese="旧版日文。", audio_url="/audio/old.wav", lines=None,
        ))

    started1, started2 = asyncio.Event(), asyncio.Event()
    release1, release2 = asyncio.Event(), asyncio.Event()
    original_synth = repair.service.synthesize_to_file
    call_count = 0

    async def gated_synth(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        my_num = call_count
        if my_num == 1:
            started1.set()
            await release1.wait()
        else:
            started2.set()
            await release2.wait()
        return await original_synth(*args, **kwargs)

    repair.service.synthesize_to_file = gated_synth
    body = {
        "session_id": "repair-session",
        "message_id": legacy_msg.id,
        "line_index": 0,
        "text": "旧版日文。",
        "voice_profile_id": repair.profiles[1].id,
    }

    t1 = asyncio.create_task(repair.client.post("/api/voice/regenerate-line", json=body))
    await asyncio.wait_for(started1.wait(), 3)
    t2 = asyncio.create_task(repair.client.post("/api/voice/regenerate-line", json=body))
    await asyncio.wait_for(started2.wait(), 3)

    # Let contender 1 finish first
    release1.set()
    resp1 = await asyncio.wait_for(t1, 5)
    assert resp1.status_code == 200, resp1.text

    # Now let contender 2 try to commit
    release2.set()
    resp2 = await asyncio.wait_for(t2, 5)
    assert resp2.status_code == 409, resp2.text
    assert "这句已有其他音频修复结果" in resp2.text

    # Exactly 1 repair audio file should remain (from contender 1), contender 2's was cleaned up
    remaining_wavs = list((repair.root / "audio").glob("repair_*.wav"))
    assert len(remaining_wavs) == 1
    assert remaining_wavs[0].name in resp1.json()["audio"]["chunks"][0]["audio_url"]


@pytest.mark.parametrize("change_type", ["audio_url", "chinese", "japanese", "lines_json"])
async def test_legacy_repair_concurrent_modifications_conflict(repair, change_type):
    async with get_db(repair.db) as conn:
        legacy_msg = await crud.add_message(conn, MessageCreate(
            session_id="repair-session", role="assistant", content_chinese="原中文",
            content_japanese="原日文。", audio_url="/audio/old.wav", lines=None,
        ))

    started, release = asyncio.Event(), asyncio.Event()
    original_synth = repair.service.synthesize_to_file

    async def delayed_synth(*args, **kwargs):
        started.set()
        await release.wait()
        return await original_synth(*args, **kwargs)

    repair.service.synthesize_to_file = delayed_synth
    body = {
        "session_id": "repair-session",
        "message_id": legacy_msg.id,
        "line_index": 0,
        "text": "原日文。",
        "voice_profile_id": repair.profiles[1].id,
    }
    pending = asyncio.create_task(repair.client.post("/api/voice/regenerate-line", json=body))
    try:
        await asyncio.wait_for(started.wait(), 3)
        async with get_db(repair.db) as conn:
            async with immediate_transaction(conn):
                if change_type == "audio_url":
                    await conn.execute("UPDATE messages SET audio_url='/audio/newer.wav' WHERE id=?", (legacy_msg.id,))
                elif change_type == "chinese":
                    await conn.execute("UPDATE messages SET content_chinese='新中文' WHERE id=?", (legacy_msg.id,))
                elif change_type == "japanese":
                    await conn.execute("UPDATE messages SET content_japanese='新日文。' WHERE id=?", (legacy_msg.id,))
                elif change_type == "lines_json":
                    await conn.execute("UPDATE messages SET lines_json='[]' WHERE id=?", (legacy_msg.id,))
        release.set()
        resp = await asyncio.wait_for(pending, 5)
        assert resp.status_code == 409, resp.text
        assert not list((repair.root / "audio").glob("repair_*.wav"))
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_repaired_legacy_line_survives_history_export_and_fresh_connection(repair, monkeypatch):
    from galgame2voice.services.chat_backup import export_chat_backup

    monkeypatch.setenv("GALGAME2VOICE_DB_PATH", repair.db)
    monkeypatch.setenv("GALGAME_DB_PATH", repair.db)

    async with get_db(repair.db) as conn:
        await crud.get_or_create_session(conn, "repair-session")
        legacy_msg = await crud.add_message(conn, MessageCreate(
            session_id="repair-session", role="assistant", content_chinese="旧版中文持久",
            content_japanese="旧版日文持久。", audio_url="/audio/old.wav", lines=None,
        ))

    body = {
        "session_id": "repair-session",
        "message_id": legacy_msg.id,
        "line_index": 0,
        "text": "旧版日文持久。",
        "voice_profile_id": repair.profiles[1].id,
    }
    resp = await repair.client.post("/api/voice/regenerate-line", json=body)
    assert resp.status_code == 200

    # 1. Fresh connection query
    async with get_db(repair.db) as fresh_conn:
        recent = await crud.get_recent_messages(fresh_conn, "repair-session", limit=10)
        repaired_in_recent = next(m for m in recent if m.id == legacy_msg.id)
        assert repaired_in_recent.audio_url == ""
        assert repaired_in_recent.lines is not None
        assert len(repaired_in_recent.lines) == 1
        assert repaired_in_recent.lines[0]["ja"] == "旧版日文持久。"
        assert repaired_in_recent.lines[0]["audio"]["status"] == "ready"

    # 2. Export chat history
    exported_data = await export_chat_backup()
    session_data = next(s for s in exported_data["sessions"] if s["id"] == "repair-session")
    exported_msg = next(m for m in session_data["messages"] if m["id"] == f"m_{legacy_msg.id}")
    assert len(exported_msg["lines"]) == 1
    assert exported_msg["lines"][0]["ja"] == "旧版日文持久。"
    assert exported_msg["lines"][0]["zh"] == "旧版中文持久"


async def test_unrepresentable_legacy_chinese_must_not_destroy_old_audio(repair):
    async with get_db(repair.db) as conn:
        message = await crud.add_message(conn, MessageCreate(
            session_id="repair-session", role="assistant", content_chinese="旧" * 10001,
            content_japanese="こんにちは。", audio_url="/audio/original.wav", lines=None,
        ))
    response = await repair.client.post("/api/voice/regenerate-line", json={
        "session_id": "repair-session", "message_id": message.id, "line_index": 0,
        "text": "こんにちは。", "voice_profile_id": repair.profiles[1].id,
    })
    assert response.status_code == 422, response.text
    assert "这句历史台词超出单句规格" in response.text
    async with get_db(repair.db) as conn:
        row = await (await conn.execute("SELECT lines_json, audio_url FROM messages WHERE id=?", (message.id,))).fetchone()
    assert row["lines_json"] is None
    assert row["audio_url"] == "/audio/original.wav"
    assert not list((repair.root / "audio").glob("repair_*.wav"))


async def test_legacy_chinese_exact_boundary_10000_succeeds_and_roundtrips(repair):
    async with get_db(repair.db) as conn:
        message = await crud.add_message(conn, MessageCreate(
            session_id="repair-session", role="assistant", content_chinese="旧" * 10000,
            content_japanese="こんにちは。", audio_url="/audio/original.wav", lines=None,
        ))
    response = await repair.client.post("/api/voice/regenerate-line", json={
        "session_id": "repair-session", "message_id": message.id, "line_index": 0,
        "text": "こんにちは。", "voice_profile_id": repair.profiles[1].id,
    })
    assert response.status_code == 200, response.text
    async with get_db(repair.db) as conn:
        row = await (await conn.execute("SELECT lines_json, audio_url FROM messages WHERE id=?", (message.id,))).fetchone()
    assert row["audio_url"] == ""
    assert row["lines_json"] is not None
    decoded = decode_lines(row["lines_json"])
    assert decoded is not None
    assert len(decoded) == 1
    assert decoded[0]["index"] == 0
    assert len(decoded[0]["zh"]) == 10000
    assert decoded[0]["ja"] == "こんにちは。"
    assert decoded[0]["audio"]["status"] == "ready"
