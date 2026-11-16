"""An enabled voice keeps the engine on its previous languages after an edit."""

import asyncio

import httpx
import pytest

from galgame2voice.database.session import init_db
from galgame2voice.main import create_app
from galgame2voice.services.voice_manager import VoiceManager, set_voice_manager


async def _settle(manager: VoiceManager) -> None:
    """Wait out the switch warm-up so its engine calls are not mixed into the next step."""
    pending = {task for task in manager._bg_tasks if not task.done()}
    if pending:
        await asyncio.wait(pending, timeout=5)


def _tts_calls(server) -> list[dict]:
    return [call["payload"] for call in server.call_history if call["endpoint"] == "/tts"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("prompt_lang", "en"), ("text_lang", "zh")],
)
async def test_editing_enabled_voice_languages_reaches_the_engine(
    isolate_test_database, mock_gpt_sovits, field, value,
):
    """PUT of one loaded-voice language, then switch, must retarget the engine.

    Synthesis re-reads the saved profile by itself. Re-applying the enabled voice
    is the path that used to skip the engine, so the assertion reads the language
    parameters on the synthesis the engine stub records after that switch.
    """
    await init_db(isolate_test_database)
    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=isolate_test_database)
    set_voice_manager(manager)
    expected = {"prompt_lang": "ja", "text_lang": "ja", field: value}

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        created = await client.post("/api/voice/profiles", json={
            "name": "Language voice",
            "gpt_weights_path": "weights/voice.ckpt",
            "sovits_weights_path": "weights/voice.pth",
            "ref_audio_path": "ref/voice.wav",
            "prompt_text": "こんにちは",
            "prompt_lang": "ja",
            "text_lang": "ja",
        })
        assert created.status_code == 201, created.text
        profile_id = created.json()["id"]

        first = await client.post("/api/voice/switch", json={"profile_id": profile_id})
        assert first.status_code == 200, first.text
        await _settle(manager)
        mock_gpt_sovits.call_history.clear()

        updated = await client.put(f"/api/voice/profiles/{profile_id}", json={field: value})
        assert updated.status_code == 200, updated.text
        assert updated.json()["profile"][field] == value

        again = await client.post("/api/voice/switch", json={"profile_id": profile_id})
        assert again.status_code == 200, again.text
        await _settle(manager)

    payloads = _tts_calls(mock_gpt_sovits)
    assert payloads, [
        {"endpoint": call["endpoint"], "payload": call["payload"]}
        for call in mock_gpt_sovits.call_history
    ]
    assert payloads[-1]["prompt_lang"] == expected["prompt_lang"], payloads
    assert payloads[-1]["text_lang"] == expected["text_lang"], payloads


@pytest.fixture
async def temp_db_path(isolate_test_database):
    """REST routes and their manager must use the same initialized database."""
    await init_db(isolate_test_database)
    return isolate_test_database


@pytest.mark.asyncio
async def test_voice_switch_same_profile_with_different_refer_audio_triggers_switch(
    temp_db_path, mock_gpt_sovits, sample_voice_profile, monkeypatch
):
    """
    If a profile's refer audio or prompt text changes, switch_voice must NOT skip switching,
    ensuring updated reference audio is dispatched to GPT-SoVITS.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_SKIP_MEM_CHECK", raising=False)
    # Mock memory check to return safe value — this test validates refer audio
    # change detection, not memory gating.
    monkeypatch.setattr("galgame2voice.services.voice_manager.get_system_memory_status", lambda: (16.0, 10.0))

    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
    set_voice_manager(manager)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        # Create initial profile
        p_resp = await client.post("/api/voice/profiles", json=sample_voice_profile)
        p_id = p_resp.json()["id"]

        # Initial switch
        sw1 = await client.post("/api/voice/switch", json={"profile_id": p_id})
        assert sw1.status_code == 200

        # Update profile's refer audio
        new_audio = "updated_refer.wav"
        up_resp = await client.put(f"/api/voice/profiles/{p_id}", json={"refer_audio_path": new_audio})
        assert up_resp.status_code == 200

        # Track set_refer_audio calls on mock server
        refer_calls = []
        orig_req = manager.client._request
        async def mock_req(method, endpoint, **kwargs):
            if endpoint == "/set_refer_audio":
                refer_calls.append(kwargs.get("params", {}).get("refer_audio_path"))
            return await orig_req(method, endpoint, **kwargs)

        monkeypatch.setattr(manager.client, "_request", mock_req)

        # Switch without force -> must NOT skip because refer audio is updated
        sw2 = await client.post("/api/voice/switch", json={"profile_id": p_id})
        assert sw2.status_code == 200
        assert new_audio in refer_calls
