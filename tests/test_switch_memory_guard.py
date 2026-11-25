"""The memory check before a voice switch runs only when model weights will load."""

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.main import create_app
from galgame2voice.services.voice_manager import VoiceManager, set_voice_manager


@pytest.fixture
async def temp_db_path(isolate_test_database):
    from galgame2voice.database.session import init_db

    await init_db(isolate_test_database)
    return isolate_test_database


def _weight_loads(server):
    return [c for c in server.call_history if "weights" in c["endpoint"]]


async def _switch_with_low_memory(temp_db_path, server, profile, other, monkeypatch):
    manager = VoiceManager(gpt_sovits_client_or_server=server, db_path=temp_db_path)
    set_voice_manager(manager)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        a = (await client.post("/api/voice/profiles", json=profile)).json()["id"]
        b = (await client.post("/api/voice/profiles", json=other)).json()["id"]
        assert (await client.post("/api/voice/switch", json={"profile_id": a})).status_code == 200
        before = _weight_loads(server)

        monkeypatch.delenv("GALGAME2VOICE_SKIP_MEM_CHECK", raising=False)
        monkeypatch.setenv("GALGAME2VOICE_MIN_FREE_MEM_GB", "100000")
        resp = await client.post("/api/voice/switch", json={"profile_id": b})
        return resp, before, _weight_loads(server)


@pytest.mark.asyncio
async def test_voice_sharing_the_same_weights_switches_when_memory_is_low(
    temp_db_path, mock_gpt_sovits, sample_voice_profile, monkeypatch
):
    other = dict(sample_voice_profile, name="Arona-calm", ref_audio_path="ref/arona_calm.wav", is_default=0)
    resp, before, after = await _switch_with_low_memory(
        temp_db_path, mock_gpt_sovits, sample_voice_profile, other, monkeypatch)

    assert resp.status_code == 200, resp.json()
    assert after == before


@pytest.mark.asyncio
async def test_voice_with_other_weights_is_still_refused_when_memory_is_low(
    temp_db_path, mock_gpt_sovits, sample_voice_profile, monkeypatch
):
    other = dict(sample_voice_profile, name="Hoshino", gpt_weights_path="weights/hoshino.ckpt",
                 sovits_weights_path="weights/hoshino.pth", is_default=0)
    resp, before, after = await _switch_with_low_memory(
        temp_db_path, mock_gpt_sovits, sample_voice_profile, other, monkeypatch)

    assert resp.status_code == 503, resp.json()
    assert after == before
