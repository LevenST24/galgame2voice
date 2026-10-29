"""Startup and queued warm-up must not publish or restore an unconfirmed voice."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services import voice_manager, tts_scheduler
from galgame2voice.services.gpt_sovits_client import GptSovitsClient


@pytest.fixture
async def warmup(monkeypatch, mock_gpt_sovits):
    path = str(get_settings().db_path)
    await init_db(path)
    async with get_db(path) as conn:
        profiles = [
            await crud.create_voice_profile(
                conn,
                VoiceProfileCreate(
                    name=f"Synthetic {name}",
                    gpt_weights_path=f"{name}.ckpt",
                    sovits_weights_path=f"{name}.pth",
                    ref_audio_path=f"{name}.wav",
                    prompt_text=name,
                    prompt_lang="ja",
                ),
            )
            for name in ["A", "B"]
        ]
        assert await crud.set_active_voice_profile(conn, profiles[0].id)
    client = GptSovitsClient(server=mock_gpt_sovits)
    monkeypatch.setattr(client, "check_health", AsyncMock(return_value={"connected": True}))
    monkeypatch.setattr(client, "synthesize", AsyncMock(return_value=b"synthetic probe"))
    manager = voice_manager.VoiceManager(client, db_path=path)
    monkeypatch.setattr(manager, "_check_switch_memory_guard", lambda force: None)
    monkeypatch.setattr(voice_manager, "release_system_memory", lambda: None)
    monkeypatch.setattr(manager, "_spawn_background", lambda coroutine: coroutine.close())

    async def execute(factory, **kwargs):
        return await factory()

    scheduler = SimpleNamespace(schedule=AsyncMock(side_effect=execute))
    monkeypatch.setattr(tts_scheduler, "get_tts_scheduler", lambda: scheduler)
    try:
        yield SimpleNamespace(
            manager=manager, client=client, a=profiles[0], b=profiles[1], scheduler=scheduler, server=mock_gpt_sovits
        )
    finally:
        await manager.aclose()
        await client.aclose()


async def test_startup_does_not_publish_database_models_before_engine_ack(warmup, monkeypatch):
    import galgame2voice.main as main
    from galgame2voice.services import sovits_endpoint

    monkeypatch.setattr(main, "get_gpt_sovits_client", lambda: warmup.client)
    monkeypatch.setattr(voice_manager, "get_voice_manager", lambda: warmup.manager)
    monkeypatch.setattr(
        sovits_endpoint,
        "resolve_effective_sovits_endpoint",
        AsyncMock(return_value=SimpleNamespace(base_url=warmup.client.base_url.rstrip("/"), source="synthetic")),
    )
    pending = []
    monkeypatch.setattr(warmup.manager, "_spawn_background", pending.append)
    try:
        await main._init_gpt_sovits_client(get_settings())
        assert len(pending) == 1
        assert warmup.client.active_profile is None
        assert warmup.client.current_gpt_weights is None
        assert warmup.client.current_sovits_weights is None
        assert warmup.client.current_refer_audio is None
    finally:
        for coroutine in pending:
            coroutine.close()


@pytest.mark.parametrize("run_background", [False, True])
async def test_first_http_switch_after_bootstrap_really_loads_models(warmup, monkeypatch, run_background):
    import galgame2voice.main as main
    from galgame2voice.services import sovits_endpoint

    monkeypatch.setattr(main, "get_gpt_sovits_client", lambda: warmup.client)
    monkeypatch.setattr(voice_manager, "_global_voice_manager", warmup.manager)
    monkeypatch.setattr(
        sovits_endpoint,
        "resolve_effective_sovits_endpoint",
        AsyncMock(return_value=SimpleNamespace(base_url=warmup.client.base_url.rstrip("/"), source="synthetic")),
    )
    pending = []
    monkeypatch.setattr(warmup.manager, "_spawn_background", pending.append)
    try:
        await main._init_gpt_sovits_client(get_settings())
        if run_background:
            assert await pending.pop() is True
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.create_app()), base_url="http://test"
        ) as client:
            response = await client.post("/api/voice/switch", json={"profile_id": warmup.a.id})
        assert response.status_code == 200, response.text
        endpoints = [call["endpoint"] for call in warmup.server.call_history]
        assert endpoints.count("/set_gpt_weights") == 1
        assert endpoints.count("/set_sovits_weights") == 1
        assert warmup.client.active_profile.id == warmup.a.id
    finally:
        for coroutine in pending:
            coroutine.close()


@pytest.mark.parametrize("failure", ["offline", "exception"])
async def test_unsuccessful_health_check_does_not_mark_saved_voice_loaded(warmup, failure):
    if failure == "offline":
        warmup.client.check_health.return_value = {"connected": False}
    else:
        warmup.client.check_health.side_effect = OSError("Synthetic disconnected engine")
    assert await warmup.manager.warmup_current_profile() is False
    assert warmup.client.active_profile is None
    assert not warmup.server.call_history


async def test_delayed_health_does_not_restore_voice_selected_before_a_manual_switch(warmup):
    entered, release = asyncio.Event(), asyncio.Event()

    async def health():
        entered.set()
        await release.wait()
        return {"connected": True}

    warmup.client.check_health.side_effect = health
    task = asyncio.create_task(warmup.manager.warmup_current_profile())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await warmup.client.switch_voice_profile(warmup.b)
        release.set()
        assert await asyncio.wait_for(task, 1) is True
        assert warmup.client.active_profile.id == warmup.b.id
        assert warmup.client.synthesize.call_args.kwargs["options"]["voice_profile_id"] == warmup.b.id
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_queued_warmup_selects_current_voice_only_when_the_worker_runs(warmup):
    entered, release = asyncio.Event(), asyncio.Event()

    async def queued(factory, **kwargs):
        entered.set()
        await release.wait()
        return await factory()

    warmup.scheduler.schedule.side_effect = queued
    task = asyncio.create_task(warmup.manager.warmup_current_profile())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not warmup.server.call_history, "Warm-up loaded models before its low-priority turn"
        assert await warmup.client.switch_voice_profile(warmup.b)
        release.set()
        assert await asyncio.wait_for(task, 1) is True
        assert warmup.client.active_profile.id == warmup.b.id
        assert warmup.client.synthesize.call_args.kwargs["options"]["voice_profile_id"] == warmup.b.id
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_manual_switch_waits_for_owned_probe_then_remains_the_final_voice(warmup):
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(*args, **kwargs):
        entered.set()
        await release.wait()
        return b"synthetic probe"

    warmup.client.synthesize.side_effect = probe
    task = asyncio.create_task(warmup.manager.warmup_current_profile())
    switch = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert warmup.manager.switch_lock.locked()
        switch = asyncio.create_task(warmup.manager.switch_profile(warmup.b, persist=False))
        await asyncio.sleep(0)
        assert not switch.done()
        release.set()
        assert await asyncio.wait_for(task, 1) is True
        assert await asyncio.wait_for(switch, 1) is True
        assert warmup.client.active_profile.id == warmup.b.id
        assert not warmup.manager.switch_lock.locked()
    finally:
        release.set()
        await asyncio.gather(*(job for job in [task, switch] if job), return_exceptions=True)


async def test_cancelled_queued_warmup_leaves_models_and_selection_untouched(warmup):
    entered = asyncio.Event()

    async def queued(factory, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    warmup.scheduler.schedule.side_effect = queued
    task = asyncio.create_task(warmup.manager.warmup_current_profile())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert warmup.client.active_profile is None
        assert not warmup.server.call_history
        assert not warmup.manager.switch_lock.locked()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_failed_first_model_load_does_not_publish_saved_default(warmup, monkeypatch):
    monkeypatch.setattr(
        warmup.client,
        "_request_model_state",
        AsyncMock(return_value=SimpleNamespace(status_code=500, text="Synthetic load failure")),
    )
    assert await warmup.manager.warmup_current_profile() is False
    assert warmup.client.active_profile is None
    warmup.client.synthesize.assert_not_awaited()


async def test_preheat_memory_rejection_does_not_load_or_publish_models(warmup, monkeypatch):
    def unavailable(force):
        raise voice_manager.InsufficientMemoryError("Synthetic insufficient memory")

    monkeypatch.setattr(warmup.manager, "_check_switch_memory_guard", unavailable)
    assert await warmup.manager.warmup_current_profile() is False
    assert warmup.client.active_profile is None
    assert not warmup.server.call_history
    assert not warmup.manager.switch_lock.locked()


async def test_cancelled_real_scheduler_probe_releases_the_manual_switch(warmup, monkeypatch):
    scheduler = tts_scheduler.TtsScheduler()
    monkeypatch.setattr(tts_scheduler, "get_tts_scheduler", lambda: scheduler)
    entered = asyncio.Event()

    async def probe(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    warmup.client.synthesize.side_effect = probe
    task = asyncio.create_task(warmup.manager.warmup_current_profile())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.wait_for(warmup.manager.switch_profile(warmup.b, persist=False), 1)
        assert warmup.client.active_profile.id == warmup.b.id
        assert not warmup.manager.switch_lock.locked()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await scheduler.aclose()


async def test_repeated_warmups_for_one_profile_use_distinct_task_ids(warmup):
    entered, release = asyncio.Event(), asyncio.Event()
    identities = []

    async def queued(factory, **kwargs):
        identities.append(kwargs["task_id"])
        if len(identities) == 2:
            entered.set()
        await release.wait()
        return await factory()

    warmup.scheduler.schedule.side_effect = queued
    tasks = [asyncio.create_task(warmup.manager.warmup_current_profile()) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert len(set(identities)) == 2
        assert all(identity.startswith(f"warmup_{warmup.a.id}_") for identity in identities)
        release.set()
        assert await asyncio.gather(*tasks) == [True, True]
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
