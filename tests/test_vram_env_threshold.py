"""Regular voice-switch precheck must honor GALGAME2VOICE_MIN_FREE_VRAM_GB.

The switch route is the public entry: POST /api/voice/switch resolves the
profile, then VoiceManager.switch_profile runs VoiceResourceGuard.check.
"""

import asyncio

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
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
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
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
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


@pytest.mark.asyncio
async def test_voice_switch_nonexistent_profile_404_precedence(temp_db_path, mock_gpt_sovits, monkeypatch):
    """
    Non-existent voice profile switch should return 404 Not Found,
    NOT 503 Service Unavailable, even when memory is low.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_SKIP_MEM_CHECK", raising=False)
    # Simulate low memory (e.g. 0.8 GB free < scaled threshold)
    monkeypatch.setattr("galgame2voice.services.voice_manager.get_system_memory_status", lambda: (16.0, 0.8))

    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
    set_voice_manager(manager)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        resp = await client.post("/api/voice/switch", json={"profile_id": 99999})
        # REST semantics: Non-existent profile should be 404, not 503!
        assert resp.status_code == 404, f"Expected 404 for missing profile, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_voice_switch_low_memory_error_keeps_two_decimal_places(
    temp_db_path, mock_gpt_sovits, sample_voice_profile, monkeypatch
):
    """
    When free RAM is 0.87 GB and threshold is 0.90 GB (15.0 * 0.06), 1-decimal
    formatting renders both sides as '0.9 GB < 0.9 GB'. Error detail must show
    two decimal places so the inequality stays visibly true.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_SKIP_MEM_CHECK", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_MIN_FREE_MEM_GB", raising=False)
    monkeypatch.setattr("galgame2voice.services.voice_manager.get_system_memory_status", lambda: (15.0, 0.87))

    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
    set_voice_manager(manager)

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        created = await client.post("/api/voice/profiles", json=sample_voice_profile)
        assert created.status_code == 201, created.text
        profile_id = created.json()["id"]

        rejected = await client.post("/api/voice/switch", json={"profile_id": profile_id})

    assert rejected.status_code == 503, rejected.text
    detail = rejected.json()["detail"]
    assert "系统空闲内存不足" in detail
    assert "0.87 GB < 0.90 GB" in detail
    assert mock_gpt_sovits.call_history == []


@pytest.mark.asyncio
async def test_concurrent_voice_switch_serialization_and_toctou_prevention(temp_db_path, mock_gpt_sovits, monkeypatch):
    """
    Simulates concurrent voice switch requests where memory is sufficient for the first
    switch but drops below threshold before the second switch.
    Without switch_lock, both would race through the memory check.
    Under switch_lock, the second switch detects depleted memory under the lock and gets 503.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_SKIP_MEM_CHECK", raising=False)

    # Memory starts at 2.0GB, drops to 0.8GB after first check
    mem_readings = [2.0, 0.8, 0.8, 0.8]
    def mock_free_mem(*args, **kwargs):
        return (16.0, mem_readings.pop(0) if mem_readings else 0.8)

    monkeypatch.setattr("galgame2voice.services.voice_manager.get_system_memory_status", mock_free_mem)

    manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=temp_db_path)
    set_voice_manager(manager)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        # Create Profile A
        p_a = await client.post("/api/voice/profiles", json={
            "name": "Character A",
            "gpt_weights_path": "a.ckpt",
            "sovits_weights_path": "a.pth",
            "refer_audio_path": "a.wav",
            "refer_text": "こんにちは",
        })
        id_a = p_a.json()["id"]

        # Create Profile B
        p_b = await client.post("/api/voice/profiles", json={
            "name": "Character B",
            "gpt_weights_path": "b.ckpt",
            "sovits_weights_path": "b.pth",
            "refer_audio_path": "b.wav",
            "refer_text": "さようなら",
        })
        id_b = p_b.json()["id"]

        # Concurrently send switch requests
        res_a, res_b = await asyncio.gather(
            client.post("/api/voice/switch", json={"profile_id": id_a}),
            client.post("/api/voice/switch", json={"profile_id": id_b}),
            return_exceptions=True,
        )

        status_codes = {res_a.status_code, res_b.status_code}
        # One succeeded (200), one safely rejected due to serialized low memory check (503)
        assert 200 in status_codes, f"Expected at least one 200, got {status_codes}"
        assert 503 in status_codes, f"Expected one 503 from TOCTOU guard, got {status_codes}"
