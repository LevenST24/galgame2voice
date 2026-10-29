"""Streamed speech must not mix a turn's stale profile with newer weights."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import wave

import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate, VoiceProfileUpdate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.chat_pipelines.tts_pipeline import TtsStreamPipeline
from galgame2voice.services.tts_options import resolve_tts_options
from galgame2voice.services.tts_service import TtsService


def make_wave(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x01\x00" * 8000 * 4)


@pytest.fixture
async def voice(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    for name in ("old.wav", "new.wav", "custom.wav"):
        make_wave(tmp_path / name)
    for name in ("old.ckpt", "old.pth", "new.ckpt", "new.pth"):
        (tmp_path / name).write_bytes(b"synthetic weights")
    database = tmp_path / "voices.db"
    await init_db(database)
    async with get_db(database) as conn:
        old = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="SyntheticVoice", gpt_weights_path=str(tmp_path / "old.ckpt"),
            sovits_weights_path=str(tmp_path / "old.pth"), ref_audio_path=str(tmp_path / "old.wav"),
            prompt_text="old transcript", prompt_lang="zh", text_lang="zh",
        ))
        await crud.update_voice_profile(conn, old.id, VoiceProfileUpdate(
            gpt_weights_path=str(tmp_path / "new.ckpt"), sovits_weights_path=str(tmp_path / "new.pth"),
            ref_audio_path=str(tmp_path / "new.wav"), prompt_text="new transcript", prompt_lang="ja", text_lang="ja",
        ))
    service = TtsService(client=SimpleNamespace(server=object()), audio_dir=tmp_path / "audio",
                         cache_manager=SimpleNamespace(db_path=str(database)), db_path=str(database))
    return SimpleNamespace(root=tmp_path, old=old, service=service,
                           pipe=TtsStreamPipeline(service, "synthetic"))


@pytest.mark.parametrize("adaptive", [False, True])
async def test_stream_uses_one_current_profile_after_mid_turn_update(voice, adaptive):
    # `old` is the profile captured when the chat turn began; a later edit has
    # committed before this sentence is prepared and the TTS context is read.
    options = voice.pipe.prepare_chunk_options(
        {"ai_adaptive_voice": adaptive, "emotion": "no_matching_reference"}, voice.old, 0, "こんにちは"
    )
    resolved = await voice.service._populate_voice_profile_opts(options)
    assert Path(resolved["gpt_weights_path"]) == voice.root / "new.ckpt"
    assert Path(resolved["sovits_weights_path"]) == voice.root / "new.pth"
    assert Path(resolved["ref_audio_path"]) == voice.root / "new.wav"
    assert (resolved["prompt_text"], resolved["prompt_lang"], resolved["text_lang"]) == ("new transcript", "ja", "ja")


@pytest.mark.parametrize("aliases", [False, True])
@pytest.mark.parametrize("adaptive", [False, True])
async def test_explicit_reference_override_survives_profile_refresh(voice, aliases, adaptive):
    custom = str(voice.root / "custom.wav")
    explicit = ({"refer_audio_path": custom, "refer_text": "custom transcript",
                 "refer_language": "en", "text_language": "en"} if aliases else
                {"ref_audio_path": custom, "prompt_text": "custom transcript",
                 "prompt_lang": "en", "text_lang": "en"})
    options = voice.pipe.prepare_chunk_options(
        {**explicit, "ai_adaptive_voice": adaptive, "emotion": "no_matching_reference"}, voice.old, 0, "hello"
    )
    resolved = await voice.service._populate_voice_profile_opts(options)
    engine = resolve_tts_options(resolved)
    assert Path(resolved["gpt_weights_path"]) == voice.root / "new.ckpt"
    assert Path(resolved["sovits_weights_path"]) == voice.root / "new.pth"
    assert Path(engine["ref_audio_path"]) == voice.root / "custom.wav"
    assert (engine["prompt_text"], engine["prompt_lang"], engine["text_lang"]) == ("custom transcript", "en", "en")


@pytest.mark.parametrize("aliases", [False, True])
async def test_reference_only_uses_text_from_the_resolved_profile(voice, aliases):
    # The engine can still have a previous voice loaded; its text must not be
    # borrowed when the user supplies a new reference without a transcript.
    voice.service.client.current_refer_text = "unrelated engine transcript"
    key = "refer_audio_path" if aliases else "ref_audio_path"
    options = voice.pipe.prepare_chunk_options(
        {key: str(voice.root / "custom.wav"), "ai_adaptive_voice": False}, voice.old, 0, "hello"
    )
    resolved = await voice.service._populate_voice_profile_opts(options)
    engine = resolve_tts_options(resolved)
    assert Path(engine["ref_audio_path"]) == voice.root / "custom.wav"
    assert (engine["prompt_text"], engine["prompt_lang"], engine["text_lang"]) == ("new transcript", "ja", "ja")


async def test_matching_adaptive_emotion_keeps_existing_precedence(voice, monkeypatch):
    from galgame2voice.services import voice_resolver

    emotion_audio = voice.root / "happy.wav"
    make_wave(emotion_audio)
    context = await voice_resolver.get_voice_resolver().resolve_context(
        str(voice.root / "voices.db"), profile_id=voice.old.id
    )
    context.emotions["happy"] = {
        "ref_audio_path": str(emotion_audio), "prompt_text": "happy transcript", "prompt_lang": "ja",
    }
    monkeypatch.setattr(voice_resolver, "get_voice_resolver", lambda: SimpleNamespace(
        resolve_context=AsyncMock(return_value=context)
    ))
    options = voice.pipe.prepare_chunk_options({
        "ref_audio_path": str(voice.root / "custom.wav"), "prompt_text": "custom transcript",
        "ai_adaptive_voice": True, "emotion": "happy",
    }, voice.old, 0, "hello")
    resolved = await voice.service._populate_voice_profile_opts(options)
    assert Path(resolved["ref_audio_path"]) == emotion_audio
    assert (resolved["prompt_text"], resolved["prompt_lang"]) == ("happy transcript", "ja")


@pytest.mark.parametrize("explicit", [False, True])
async def test_empty_profile_prompt_does_not_borrow_previous_engine_text(voice, explicit):
    from galgame2voice.services.gpt_sovits_client import GptSovitsClient

    async with get_db(voice.root / "voices.db") as conn:
        await crud.update_voice_profile(conn, voice.old.id, VoiceProfileUpdate(prompt_text=""))
        await crud.set_active_voice_profile(conn, voice.old.id)
    client = GptSovitsClient(server=object())
    client.current_refer_text = "unrelated previous voice"
    client.current_refer_audio = str(voice.root / "old.wav")
    voice.service.client = client
    options = voice.pipe.prepare_chunk_options(
        {"ai_adaptive_voice": False}, voice.old if explicit else None, 0, "hello"
    )
    try:
        resolved = await voice.service._populate_voice_profile_opts(options)
        payload = client._build_tts_payload("hello", resolved)
        assert Path(payload["ref_audio_path"]) == voice.root / "new.wav"
        assert payload["prompt_text"] == ""
    finally:
        await client.aclose()


async def test_unselected_engine_defaults_still_work(voice):
    from galgame2voice.services.audio_identity import bind_audio_options
    from galgame2voice.services.gpt_sovits_client import GptSovitsClient

    client = GptSovitsClient(server=object())
    client.current_refer_audio = str(voice.root / "old.wav")
    client.current_refer_text = "engine default"
    try:
        bound = await bind_audio_options({}, client)
        payload = client._build_tts_payload("hello", bound)
        assert Path(payload["ref_audio_path"]) == voice.root / "old.wav"
        assert payload["prompt_text"] == "engine default"
    finally:
        await client.aclose()


def test_cache_identity_keeps_resolved_empty_prompt_even_with_legacy_fallback():
    from galgame2voice.services.tts_cache_manager import _extract_voice_profile_cache_fields

    options = {"_profile_prompt_resolved": True, "prompt_text": "", "refer_text": "stale alias"}
    fields = _extract_voice_profile_cache_fields(options, {"prompt_text": "old profile text"})
    assert fields["prompt_text"] == ""


@pytest.mark.parametrize("selector", ["active", "id", "name"])
async def test_missing_profile_reference_never_borrows_another_engine_voice(voice, selector):
    async with get_db(voice.root / "voices.db") as conn:
        await crud.update_voice_profile(conn, voice.old.id, VoiceProfileUpdate(ref_audio_path=""))
        await crud.set_active_voice_profile(conn, voice.old.id)
    voice.service.client.current_refer_audio = str(voice.root / "old.wav")
    voice.service.client.current_refer_text = "previous voice"
    voice.service.client.synthesize = AsyncMock(return_value=b"wrong voice")
    options = {"ai_adaptive_voice": False}
    if selector == "id":
        options["voice_profile_id"] = voice.old.id
    elif selector == "name":
        options["character_name"] = voice.old.name
    with pytest.raises(ValueError, match="参考音频"):
        await voice.service.synthesize("hello", options=options, use_cache=False)
    voice.service.client.synthesize.assert_not_awaited()


async def test_valid_explicit_reference_works_when_profile_reference_is_empty(voice):
    async with get_db(voice.root / "voices.db") as conn:
        await crud.update_voice_profile(conn, voice.old.id, VoiceProfileUpdate(ref_audio_path=""))
    options = voice.pipe.prepare_chunk_options({
        "ai_adaptive_voice": False, "ref_audio_path": str(voice.root / "custom.wav"),
        "prompt_text": "explicit reference",
    }, voice.old, 0, "hello")
    resolved = await voice.service._populate_voice_profile_opts(options)
    assert Path(resolved["ref_audio_path"]) == voice.root / "custom.wav"
    assert resolved["prompt_text"] == "explicit reference"


@pytest.mark.parametrize("alias", [False, True])
async def test_invalid_override_cannot_borrow_engine_reference(voice, alias):
    async with get_db(voice.root / "voices.db") as conn:
        await crud.update_voice_profile(conn, voice.old.id, VoiceProfileUpdate(ref_audio_path=""))
    short = voice.root / "short.wav"
    with wave.open(str(short), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x01\x00" * 4000)
    voice.service.client.current_refer_audio = str(voice.root / "old.wav")
    key = "refer_audio_path" if alias else "ref_audio_path"
    options = voice.pipe.prepare_chunk_options({key: str(short), "ai_adaptive_voice": False}, voice.old, 0, "hello")
    with pytest.raises(ValueError, match="参考音频"):
        await voice.service._populate_voice_profile_opts(options)


async def test_pre_resolved_voice_with_no_reference_does_not_borrow_engine(voice):
    voice.service.client.current_refer_audio = str(voice.root / "old.wav")
    options = {"_pre_resolved": True, "voice_profile_id": voice.old.id,
               "gpt_weights_path": str(voice.root / "new.ckpt"),
               "sovits_weights_path": str(voice.root / "new.pth")}
    with pytest.raises(ValueError, match="参考音频"):
        await voice.service._populate_voice_profile_opts(options)


async def test_pre_resolved_engine_without_voice_selection_keeps_default(voice):
    voice.service.client.current_refer_audio = str(voice.root / "old.wav")
    result = await voice.service._populate_voice_profile_opts({"_pre_resolved": True})
    assert Path(result["ref_audio_path"]) == voice.root / "old.wav"


async def test_no_profile_keeps_engine_default_reference(voice):
    database = voice.root / "no-voices.db"
    await init_db(database)
    voice.service.db_path = str(database)
    voice.service.client.current_refer_audio = str(voice.root / "old.wav")
    voice.service.client.current_refer_text = "engine default"
    result = await voice.service._populate_voice_profile_opts({})
    assert Path(result["ref_audio_path"]) == voice.root / "old.wav"
    assert result["prompt_text"] == "engine default"


async def test_emotion_reference_works_when_profile_reference_is_empty(voice, monkeypatch):
    from galgame2voice.services import voice_resolver

    context = await voice_resolver.get_voice_resolver().resolve_context(
        str(voice.root / "voices.db"), profile_id=voice.old.id
    )
    context.ref_audio_path = ""
    context.emotions["happy"] = {
        "ref_audio_path": str(voice.root / "custom.wav"), "prompt_text": "happy", "prompt_lang": "ja",
    }
    monkeypatch.setattr(voice_resolver, "get_voice_resolver", lambda: SimpleNamespace(
        resolve_context=AsyncMock(return_value=context)
    ))
    result = await voice.service._populate_voice_profile_opts({
        "voice_profile_id": voice.old.id, "ai_adaptive_voice": True, "emotion": "happy",
    })
    assert Path(result["ref_audio_path"]) == voice.root / "custom.wav"


@pytest.mark.parametrize("stream", [False, True])
async def test_voice_api_reports_missing_reference_before_audio_response(voice, monkeypatch, stream):
    import httpx
    from fastapi import FastAPI
    from galgame2voice.routers import voice as voice_router
    from galgame2voice.services.voice_manager import VoiceManager

    async with get_db(voice.root / "voices.db") as conn:
        await crud.update_voice_profile(conn, voice.old.id, VoiceProfileUpdate(ref_audio_path=""))
    voice.service.client.current_refer_audio = str(voice.root / "old.wav")
    voice.service.client.synthesize = AsyncMock(return_value=b"wrong voice")

    manager = VoiceManager.__new__(VoiceManager)
    manager.client, manager.tts_service = voice.service.client, voice.service
    manager.db_path = str(voice.root / "voices.db")
    monkeypatch.setattr(voice_router, "get_voice_manager", lambda: manager)
    monkeypatch.setattr(voice_router, "_ensure_voice_profile_available", AsyncMock())
    app = FastAPI()
    app.include_router(voice_router.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/voice/synthesize", json={
            "text": "こんにちは", "voice_profile_id": voice.old.id, "stream": stream,
        })
    assert response.status_code == 422, response.text
    assert "参考音频" in response.json()["detail"]
    voice.service.client.synthesize.assert_not_awaited()


@pytest.mark.parametrize("engine_has_reference", [False, True])
async def test_voice_manager_keeps_selection_explicit_and_does_not_copy_stale_profile(voice, engine_has_reference):
    from galgame2voice.services.voice_manager import VoiceManager

    manager = VoiceManager.__new__(VoiceManager)
    manager.client = SimpleNamespace(current_refer_audio=str(voice.root / "old.wav") if engine_has_reference else None)
    manager.get_profile = AsyncMock(return_value=voice.old)
    manager.get_active_profile = AsyncMock(return_value=voice.old)

    by_name = await manager._resolve_active_options({"character_name": "Other"})
    assert "voice_profile_id" not in by_name
    by_alias = await manager._resolve_active_options({"profile_id": voice.old.id})
    assert by_alias["voice_profile_id"] == voice.old.id
    assert not {"gpt_weights_path", "sovits_weights_path", "ref_audio_path", "prompt_text"}.intersection(by_alias)
    resolved = await voice.service._populate_voice_profile_opts(by_alias)
    assert Path(resolved["gpt_weights_path"]) == voice.root / "new.ckpt"
    assert Path(resolved["ref_audio_path"]) == voice.root / "new.wav"
    manager.get_profile.assert_not_awaited()
    manager.get_active_profile.assert_not_awaited()


async def test_legacy_id_and_canonical_id_keep_selection_precedence(voice):
    from galgame2voice.services.voice_manager import VoiceManager

    manager = VoiceManager.__new__(VoiceManager)
    options = await manager._resolve_active_options({"profile_id": 999, "voice_profile_id": voice.old.id})
    assert options["voice_profile_id"] == voice.old.id
    invalid = await manager._resolve_active_options({"profile_id": 999})
    with pytest.raises(ValueError, match="所选音色"):
        await voice.service._populate_voice_profile_opts(invalid)


async def test_named_voice_does_not_fall_back_to_another_active_voice(voice):
    from galgame2voice.services.voice_manager import VoiceManager

    async with get_db(voice.root / "voices.db") as conn:
        await crud.set_active_voice_profile(conn, voice.old.id)
        other = await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="OtherVoice", gpt_weights_path=str(voice.root / "old.ckpt"),
            sovits_weights_path=str(voice.root / "old.pth"), ref_audio_path=str(voice.root / "custom.wav"),
            prompt_text="other voice", prompt_lang="ja",
        ))
    manager = VoiceManager.__new__(VoiceManager)
    options = await manager._resolve_active_options({"character_name": "OtherVoice"})
    resolved = await voice.service._populate_voice_profile_opts(options)
    assert resolved["voice_profile_id"] == other.id
    assert Path(resolved["ref_audio_path"]) == voice.root / "custom.wav"


async def test_nonlocal_profile_reference_is_not_rejected(voice, monkeypatch):
    from galgame2voice.services import voice_resolver

    context = await voice_resolver.get_voice_resolver().resolve_context(
        str(voice.root / "voices.db"), profile_id=voice.old.id
    )
    context.ref_audio_path = "Z:/remote-engine/reference.wav"
    monkeypatch.setattr(voice_resolver, "get_voice_resolver", lambda: SimpleNamespace(
        resolve_context=AsyncMock(return_value=context)
    ))
    result = await voice.service._populate_voice_profile_opts({"voice_profile_id": voice.old.id})
    assert result["ref_audio_path"] == "Z:/remote-engine/reference.wav"


@pytest.mark.parametrize("chunks", [[b"first", b"second"], []])
async def test_voice_api_stream_preserves_chunks_and_closes_generator(monkeypatch, chunks):
    import httpx
    from fastapi import FastAPI
    from galgame2voice.routers import voice as voice_router

    closed = []

    class Manager:
        async def stream_tts(self, text, options):
            try:
                for chunk in chunks:
                    yield chunk
            finally:
                closed.append(True)

    monkeypatch.setattr(voice_router, "get_voice_manager", lambda: Manager())
    monkeypatch.setattr(voice_router, "_ensure_voice_profile_available", AsyncMock())
    app = FastAPI()
    app.include_router(voice_router.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/voice/synthesize", json={"text": "こんにちは", "stream": True})
    assert response.status_code == 200 and response.content == b"".join(chunks)
    assert closed == [True]


async def test_voice_api_cancel_before_first_chunk_closes_generator(monkeypatch):
    import asyncio
    from starlette.requests import Request
    from galgame2voice.routers import voice as voice_router

    started, closed = asyncio.Event(), asyncio.Event()

    class Manager:
        async def stream_tts(self, text, options):
            try:
                started.set()
                await asyncio.Event().wait()
                yield b"unreachable"
            finally:
                closed.set()

    monkeypatch.setattr(voice_router, "get_voice_manager", lambda: Manager())
    monkeypatch.setattr(voice_router, "_ensure_voice_profile_available", AsyncMock())

    async def receive():
        await asyncio.Event().wait()

    request = Request(_voice_asgi_scope("2.3"), receive=receive)
    task = asyncio.create_task(voice_router.synthesize_speech(
        voice_router.SynthesizeRequest(text="こんにちは", stream=True), request=request,
    ))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


def _voice_asgi_scope(spec_version):
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/api/voice/synthesize", "raw_path": b"/api/voice/synthesize",
        "query_string": b"", "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000), "root_path": "",
    }


async def test_voice_api_disconnect_before_first_chunk_closes_generator(monkeypatch):
    import asyncio
    from fastapi import FastAPI
    from starlette.requests import ClientDisconnect
    from galgame2voice.routers import voice as voice_router

    started, closed, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Manager:
        async def stream_tts(self, text, options):
            try:
                started.set()
                await asyncio.Event().wait()
                yield b"unreachable"
            finally:
                closed.set()

    monkeypatch.setattr(voice_router, "get_voice_manager", lambda: Manager())
    monkeypatch.setattr(voice_router, "_ensure_voice_profile_available", AsyncMock())
    app = FastAPI()
    app.include_router(voice_router.router)
    body_read = False

    async def receive():
        nonlocal body_read
        if not body_read:
            body_read = True
            return {"type": "http.request", "body": b'{"text":"hello","stream":true}', "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    task = asyncio.create_task(app(_voice_asgi_scope("2.3"), receive, send))
    try:
        await asyncio.wait_for(started.wait(), 5)
        disconnect.set()
        await asyncio.wait_for(closed.wait(), 1)
        result, = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert result is None or isinstance(result, (ClientDisconnect, asyncio.CancelledError))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("failed_message", ["http.response.start", "http.response.body"])
async def test_voice_api_send_disconnect_closes_prefetched_generator(monkeypatch, failed_message):
    import asyncio
    from fastapi import FastAPI
    from starlette.requests import ClientDisconnect
    from galgame2voice.routers import voice as voice_router

    closed = asyncio.Event()

    class Manager:
        async def stream_tts(self, text, options):
            try:
                yield b"first"
                await asyncio.Event().wait()
            finally:
                closed.set()

    monkeypatch.setattr(voice_router, "get_voice_manager", lambda: Manager())
    monkeypatch.setattr(voice_router, "_ensure_voice_profile_available", AsyncMock())
    app = FastAPI()
    app.include_router(voice_router.router)
    body_read = False

    async def receive():
        nonlocal body_read
        if not body_read:
            body_read = True
            return {"type": "http.request", "body": b'{"text":"hello","stream":true}', "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == failed_message:
            raise OSError("synthetic client disconnect")

    with pytest.raises(ClientDisconnect):
        await app(_voice_asgi_scope("2.4"), receive, send)
    assert closed.is_set()


@pytest.mark.parametrize("asgi_spec", ["2.3", "2.4"])
async def test_voice_api_disconnect_after_first_chunk_closes_stalled_stream(monkeypatch, asgi_spec):
    import asyncio
    from fastapi import FastAPI
    from starlette.requests import ClientDisconnect
    from galgame2voice.routers import voice as voice_router

    sent, closed, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Manager:
        async def stream_tts(self, text, options):
            try:
                yield b"first"
                await asyncio.Event().wait()
                yield b"unreachable"
            finally:
                closed.set()

    monkeypatch.setattr(voice_router, "get_voice_manager", lambda: Manager())
    monkeypatch.setattr(voice_router, "_ensure_voice_profile_available", AsyncMock())
    app = FastAPI()
    app.include_router(voice_router.router)
    body_read = False

    async def receive():
        nonlocal body_read
        if not body_read:
            body_read = True
            return {"type": "http.request", "body": b'{"text":"hello","stream":true}', "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            sent.set()

    task = asyncio.create_task(app(_voice_asgi_scope(asgi_spec), receive, send))
    try:
        await asyncio.wait_for(sent.wait(), 5)
        disconnect.set()
        await asyncio.wait_for(closed.wait(), 1)
        result, = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        assert result is None or isinstance(result, (ClientDisconnect, asyncio.CancelledError))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
