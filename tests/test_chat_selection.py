"""Requested identities cannot silently become another character or provider."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from galgame2voice.database import crud
from galgame2voice.database.models import ProviderCreate, VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.routers import chat
from galgame2voice.services import chat_service
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.voice_resolver import VoiceProfileResolver


@pytest.fixture
async def selected(tmp_path, monkeypatch, mock_gpt_sovits):
    path = tmp_path / "selection.db"
    await init_db(path)
    async with get_db(path) as conn:
        profiles = [await crud.create_voice_profile(conn, VoiceProfileCreate(name=name,
            gpt_weights_path="", sovits_weights_path="", prompt_text="")) for name in ("Alpha", "Bravo")]
        await crud.set_active_voice_profile(conn, profiles[0].id)
        for name, active in (("synthetic-a", True), ("synthetic-b", False)):
            await crud.create_provider(conn, ProviderCreate(id=name, name=name, api_base_url="https://synthetic.invalid/v1",
                api_key="synthetic", chat_model=name, is_active=active))
    factory = Mock(return_value=SimpleNamespace(provider_type="synthetic", api_key="synthetic"))
    monkeypatch.setattr(chat_service, "get_llm_adapter", factory)
    client = GptSovitsClient(server=mock_gpt_sovits)
    service = ChatService(tts_service=TtsService(client=client, audio_dir=tmp_path / "audio"), db_path=path)
    service._extract_memory_safe = AsyncMock()
    try:
        yield SimpleNamespace(path=path, service=service, profiles=profiles, factory=factory)
    finally:
        await service.aclose()
        await client.aclose()


@pytest.mark.parametrize("selector", ["explicit", "options", "session", "name", "zero"])
async def test_missing_voice_selection_never_borrows_the_active_profile(selected, selector):
    arguments = {
        "explicit": (99999, None, None, None),
        "options": (None, {"voice_profile_id": 99999}, None, None),
        "session": (None, None, SimpleNamespace(voice_profile_id=99999), None),
        "name": (None, None, None, "Synthetic missing character"),
        "zero": (0, None, None, None),
    }[selector]
    async with get_db(selected.path) as conn:
        with pytest.raises(ValueError) as error:
            await selected.service._resolve_voice_profile(conn, *arguments)
        assert getattr(error.value, "error_code", None) == "VOICE_PROFILE_NOT_FOUND"
        assert "角色与会话" in str(error.value)
        assert (await crud.get_active_voice_profile(conn)).id == selected.profiles[0].id


async def test_explicit_id_and_named_fallback_keep_the_existing_identity_precedence(selected):
    a, b = selected.profiles
    async with get_db(selected.path) as conn:
        chosen = await selected.service._resolve_voice_profile(conn, b.id, None, SimpleNamespace(voice_profile_id=a.id), "Missing")
        assert chosen.id == b.id
        fallback = await selected.service._resolve_voice_profile(conn, 99999, None, None, "Bravo")
        assert fallback.id == b.id
        bound = await selected.service._resolve_voice_profile(conn, None, None, SimpleNamespace(voice_profile_id=b.id), None)
        assert bound.id == b.id
        named = await selected.service._resolve_voice_profile(conn, None, None, SimpleNamespace(voice_profile_id=a.id), "Bravo")
        assert named.id == b.id, "an explicit name precedes the implicit session binding"
        assert (await selected.service._resolve_voice_profile(conn, None, None, None, None)).id == a.id


async def test_chat_and_tts_use_the_same_unique_package_alias(selected, tmp_path, monkeypatch):
    from test_character_identity import catalog, install
    from galgame2voice.services import character_manager

    for package_id, label, aliases in (("alpha", "Alpha", ["shared"]), ("bravo", "Bravo", ["shared", "B alias"])):
        install(tmp_path, package_id, label, aliases)
    monkeypatch.setattr(character_manager, "get_character_manager", lambda: catalog(tmp_path))
    async with get_db(selected.path) as conn:
        chosen = await selected.service._resolve_voice_profile(conn, None, None, None, "B alias")
    context = await VoiceProfileResolver()._fetch_raw_profile(selected.path, None, "B alias")
    assert chosen.id == context.id == selected.profiles[1].id
    async with get_db(selected.path) as conn:
        with pytest.raises(ValueError) as error:
            await selected.service._resolve_voice_profile(conn, None, None, None, "shared")
        assert getattr(error.value, "error_code", None) == "VOICE_PROFILE_AMBIGUOUS"


async def test_missing_explicit_provider_never_constructs_the_default_adapter(selected):
    async with get_db(selected.path) as conn:
        with pytest.raises(ValueError) as error:
            await selected.service._get_active_llm_adapter(conn, provider_id="synthetic-missing")
        assert getattr(error.value, "error_code", None) == "PROVIDER_NOT_FOUND"
        assert "对话模型" in str(error.value)
    selected.factory.assert_not_called()


async def test_existing_provider_can_be_selected_without_changing_the_global_active_one(selected):
    async with get_db(selected.path) as conn:
        _, model, provider = await selected.service._get_active_llm_adapter(conn, provider_id="synthetic-b")
        assert provider == model == "synthetic-b"
        assert selected.factory.call_args.args[0].id == "synthetic-b"
        assert (await crud.get_active_provider_raw(conn)).id == "synthetic-a"
        _, _, provider = await selected.service._get_active_llm_adapter(conn)
        assert provider == "synthetic-a"


@pytest.mark.parametrize("kind", ["voice", "provider"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_rejected_selection_does_not_generate_or_process_memory(selected, kind, streaming):
    options = {"voice_profile_id": 99999} if kind == "voice" else {"provider_id": "synthetic-missing"}
    expected = "VOICE_PROFILE_NOT_FOUND" if kind == "voice" else "PROVIDER_NOT_FOUND"
    if streaming:
        events = [event async for event in selected.service.stream_chat("Synthetic question", session_id="rejected", **options)]
        assert [event["event"] for event in events] == ["error"]
        assert events[0]["data"]["error_code"] == expected
        assert events[0]["data"]["status_code"] == 404
    else:
        with pytest.raises(ValueError) as error:
            await selected.service.chat_sync("Synthetic question", session_id="rejected", **options)
        assert getattr(error.value, "error_code", None) == expected
    await selected.service.aclose()
    selected.factory.assert_not_called()
    selected.service._extract_memory_safe.assert_not_awaited()
    async with get_db(selected.path) as conn:
        row = await (await conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='rejected'")).fetchone()
        assert row[0] == 0


@pytest.mark.parametrize("method,path,body,code", [
    ("POST", "/api/chat", {"voice_profile_id": 99999}, "VOICE_PROFILE_NOT_FOUND"),
    ("POST", "/ai/chat", {"provider_id": "synthetic-missing"}, "PROVIDER_NOT_FOUND"),
    ("GET", "/ai/chat", {"character_name": "Synthetic missing"}, "VOICE_PROFILE_NOT_FOUND"),
])
async def test_sync_http_errors_explain_the_relevant_settings_page(selected, monkeypatch, method, path, body, code):
    monkeypatch.setattr(chat, "get_chat_service", lambda: selected.service)
    app = FastAPI()
    app.include_router(chat.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        if method == "GET":
            response = await client.get(path, params={"prompt": "synthetic", "session_id": "http-rejected", **body})
        else:
            response = await client.post(path, json={"prompt": "synthetic", "session_id": "http-rejected", **body})
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["error_code"] == code
    selected.factory.assert_not_called()
