"""Regular voice-switch precheck must honor GALGAME2VOICE_MIN_FREE_VRAM_GB.

The switch route is the public entry: POST /api/voice/switch resolves the
profile, then VoiceManager.switch_profile runs VoiceResourceGuard.check.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.main import create_app
from galgame2voice.services.voice_manager import VoiceManager, set_voice_manager


@pytest.fixture
async def temp_db_path(isolate_test_database):
    """REST routes and their manager must use the same initialized database."""
    from galgame2voice.database.session import init_db

    await init_db(isolate_test_database)
    return isolate_test_database


def _arm_switch_probes(monkeypatch, free_vram_gb):
    """Keep RAM plentiful and report a fixed free-VRAM reading to the switch guard."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_SKIP_MEM_CHECK", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_MIN_FREE_MEM_GB", raising=False)
    reading = {"free_gb": free_vram_gb}
    monkeypatch.setattr(
        "galgame2voice.services.voice_manager.get_system_memory_status",
        lambda: (32.0, 16.0),
    )
    monkeypatch.setattr(
        "galgame2voice.services.voice_manager.get_gpu_vram_status",
        lambda: (8.0, reading["free_gb"]),
    )
    return reading


@pytest.mark.asyncio
async def test_voice_switch_rejects_when_free_vram_below_env_threshold(
    temp_db_path, mock_gpt_sovits, sample_voice_profile, monkeypatch
):
    """1.0 GB free is above the 0.45 GB floor but below GALGAME2VOICE_MIN_FREE_VRAM_GB=2.0."""
    monkeypatch.setenv("GALGAME2VOICE_MIN_FREE_VRAM_GB", "2.0")
    _arm_switch_probes(monkeypatch, 1.0)

    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
    set_voice_manager(manager)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/voice/profiles", json=sample_voice_profile)
        assert created.status_code == 201, created.text
        profile_id = created.json()["id"]

        rejected = await client.post("/api/voice/switch", json={"profile_id": profile_id})

    assert rejected.status_code == 503, rejected.text
    assert "显存不足" in rejected.json()["detail"]
    assert "1.00 GB < 2.00 GB" in rejected.json()["detail"]
    assert mock_gpt_sovits.call_history == []


@pytest.mark.asyncio
async def test_voice_switch_keeps_builtin_vram_floor_without_env(
    temp_db_path, mock_gpt_sovits, sample_voice_profile, monkeypatch
):
    """Unset env keeps the 0.45 GB floor: 0.40 GB is refused, 1.0 GB is allowed."""
    monkeypatch.delenv("GALGAME2VOICE_MIN_FREE_VRAM_GB", raising=False)
    reading = _arm_switch_probes(monkeypatch, 0.40)

    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
    set_voice_manager(manager)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/voice/profiles", json=sample_voice_profile)
        assert created.status_code == 201, created.text
        profile_id = created.json()["id"]

        below_floor = await client.post("/api/voice/switch", json={"profile_id": profile_id})
        assert below_floor.status_code == 503, below_floor.text
        detail = below_floor.json()["detail"]
        assert "显存不足" in detail
        assert "0.40 GB < 0.45 GB" in detail
        assert mock_gpt_sovits.call_history == []

        reading["free_gb"] = 1.0
        allowed = await client.post("/api/voice/switch", json={"profile_id": profile_id})

    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "switched"
    assert any(call["endpoint"] == "/set_gpt_weights" for call in mock_gpt_sovits.call_history)
