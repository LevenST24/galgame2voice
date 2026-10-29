"""Resource probes and memory cleanup must not hold the application event loop."""
import asyncio
import builtins
import gc
from contextlib import asynccontextmanager
from importlib.machinery import ModuleSpec
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from galgame2voice.services import audio_cleaner, voice_manager
from galgame2voice.database import crud
from galgame2voice.database.models import SettingsUpdate, VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.utils import hardware


@pytest.fixture
def no_native_vram(monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("synthetic missing nvidia-smi")
    monkeypatch.setattr(hardware.subprocess, "check_output", unavailable)


@pytest.mark.parametrize("initializing", [False, True])
def test_vram_probe_never_initializes_or_waits_for_torch(monkeypatch, no_native_vram, initializing):
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    if initializing:
        module = ModuleType("torch")
        module.__spec__ = ModuleSpec("torch", loader=None)
        module.__spec__._initializing = True
        monkeypatch.setitem(sys.modules, "torch", module)
    original_import = builtins.__import__
    imports = []
    def imported(name, *args, **kwargs):
        if name == "torch":
            imports.append(name)
            raise ImportError("synthetic unavailable framework")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", imported)
    assert hardware.get_gpu_vram_status() == (None, None)
    assert imports == []


def test_native_vram_snapshot_remains_preferred(monkeypatch):
    monkeypatch.setattr(hardware.subprocess, "check_output", lambda *args, **kwargs: "8192,2048\n")
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    assert hardware.get_gpu_vram_status() == (8.0, 2.0)
    assert "torch" not in sys.modules


def test_loaded_framework_vram_queries_the_same_device_as_total_capacity(monkeypatch, no_native_vram):
    devices = []
    def memory(device):
        devices.append(device)
        return 2*1024**3, 8*1024**3
    module = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True,
        get_device_properties=lambda device: SimpleNamespace(total_memory=8*1024**3), mem_get_info=memory))
    monkeypatch.setitem(sys.modules, "torch", module)
    assert hardware.get_gpu_vram_status() == (8.0, 2.0)
    assert devices == [0]


@pytest.mark.parametrize("initializing", [False, True])
def test_memory_release_does_not_wait_for_a_framework_import(monkeypatch, initializing):
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    if initializing:
        module = ModuleType("torch")
        module.__spec__ = ModuleSpec("torch", loader=None)
        module.__spec__._initializing = True
        monkeypatch.setitem(sys.modules, "torch", module)
    monkeypatch.setattr(gc, "collect", lambda: 0)
    original_import = builtins.__import__
    imports = []
    def imported(name, *args, **kwargs):
        if name == "torch":
            imports.append(name)
            raise ImportError("synthetic framework initializing")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", imported)
    hardware.release_system_memory()
    assert imports == []


@pytest.fixture
async def switcher(tmp_path, monkeypatch):
    client = GptSovitsClient(server=SimpleNamespace(handle_request=AsyncMock()))
    manager = voice_manager.VoiceManager(client, db_path=str(tmp_path/"switch.db"))
    monkeypatch.setattr(client, "switch_voice_profile", AsyncMock(return_value=True))
    monkeypatch.setattr(manager, "warmup_current_profile", AsyncMock(return_value=True))
    yield manager
    await manager.aclose()
    await client.aclose()


@pytest.mark.parametrize("force", [False, True])
async def test_manual_switch_keeps_resource_work_off_the_event_loop(switcher, monkeypatch, force):
    loop_thread = threading.get_ident()
    calls = []
    monkeypatch.setattr(switcher, "_check_switch_memory_guard", lambda value: calls.append(("guard", threading.get_ident(), value)))
    monkeypatch.setattr(voice_manager, "release_system_memory", lambda: calls.append(("release", threading.get_ident(), None)))
    profile = SimpleNamespace(id=7, name="Synthetic resource probe")
    assert await switcher.switch_profile(profile, persist=False, force=force)
    assert [kind for kind, _, _ in calls] == ["guard", "release", "release"]
    assert calls[0][2] is force
    assert all(thread != loop_thread for _, thread, _ in calls)
    switcher.client.switch_voice_profile.assert_awaited_once_with(profile, force=force)


async def test_audio_cleanup_releases_memory_off_the_event_loop(tmp_path, monkeypatch):
    connection = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(fetchall=AsyncMock(return_value=[]))))
    @asynccontextmanager
    async def database():
        yield connection
    monkeypatch.setattr(audio_cleaner, "_resolve_get_db", lambda: database)
    monkeypatch.setattr(audio_cleaner.crud, "get_settings_raw", AsyncMock(return_value=SimpleNamespace(audio_retention_minutes=30)))
    monkeypatch.setattr(audio_cleaner, "_fetch_protected_audio_names", AsyncMock(return_value=set()))
    monkeypatch.setattr(audio_cleaner, "_clean_lru_cache_from_db", AsyncMock(return_value=(0, [])))
    monkeypatch.setattr(audio_cleaner, "_scan_and_clean", lambda *args: (1, []))
    calls = []
    monkeypatch.setattr(hardware, "release_system_memory", lambda: calls.append(threading.get_ident()))
    loop_thread = threading.get_ident()
    await audio_cleaner._run_audio_cleanup_cycle(tmp_path/"audio")
    assert len(calls) == 1 and calls[0] != loop_thread


@pytest.mark.parametrize("phase", ["guard", "pre-cleanup"])
async def test_cancelled_resource_preparation_never_starts_a_model_load(switcher, monkeypatch, phase):
    loop = asyncio.get_running_loop()
    entered, release, finished = asyncio.Event(), threading.Event(), threading.Event()
    def blocked():
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(2), "test resource probe was not released"
        finally:
            finished.set()
    monkeypatch.setattr(switcher, "_check_switch_memory_guard", lambda force: blocked() if phase == "guard" else None)
    monkeypatch.setattr(voice_manager, "release_system_memory", lambda: blocked() if phase == "pre-cleanup" else None)
    operation = asyncio.create_task(switcher.switch_profile(SimpleNamespace(id=8), persist=False))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 1)
        assert not switcher.switch_lock.locked()
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        switcher.client.switch_voice_profile.assert_not_awaited()
        switcher.warmup_current_profile.assert_not_awaited()
    finally:
        release.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("persist", [False, True])
async def test_cancellation_after_model_commit_finishes_cleanup_and_requested_persistence(switcher, monkeypatch, persist):
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    calls = []
    def cleanup():
        calls.append(True)
        if len(calls) == 2:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2), "test cleanup was not released"
    monkeypatch.setattr(switcher, "_check_switch_memory_guard", lambda force: None)
    monkeypatch.setattr(voice_manager, "release_system_memory", cleanup)
    saved = AsyncMock()
    monkeypatch.setattr(switcher, "_persist_active_switch_profile", saved)
    profile = SimpleNamespace(id=9)
    operation = asyncio.create_task(switcher.switch_profile(profile, persist=persist))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done() and switcher.switch_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 1)
        if persist:
            saved.assert_awaited_once_with(profile)
        else:
            saved.assert_not_awaited()
        assert not switcher.switch_lock.locked()
        switcher.warmup_current_profile.assert_not_awaited()
    finally:
        release.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_failed_model_switch_never_persists_or_schedules_warmup(switcher, monkeypatch):
    monkeypatch.setattr(switcher, "_check_switch_memory_guard", lambda force: None)
    monkeypatch.setattr(voice_manager, "release_system_memory", lambda: None)
    switcher.client.switch_voice_profile.return_value = False
    saved = AsyncMock()
    monkeypatch.setattr(switcher, "_persist_active_switch_profile", saved)
    assert await switcher.switch_profile(SimpleNamespace(id=10)) is False
    saved.assert_not_awaited()
    switcher.warmup_current_profile.assert_not_awaited()


async def test_cancel_after_engine_commit_saves_the_real_active_profile(switcher, monkeypatch):
    await init_db(switcher.db_path)
    async with get_db(switcher.db_path) as connection:
        profiles = [await crud.create_voice_profile(connection, VoiceProfileCreate(name=name,
            gpt_weights_path=f'{name}.ckpt', sovits_weights_path=f'{name}.pth')) for name in ['Synthetic before', 'Synthetic after']]
        await crud.update_settings(connection, SettingsUpdate(active_voice_profile_id=profiles[0].id))
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    calls = []
    def cleanup():
        calls.append(True)
        if len(calls) == 2:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
    monkeypatch.setattr(switcher, "_check_switch_memory_guard", lambda force: None)
    monkeypatch.setattr(voice_manager, "release_system_memory", cleanup)
    operation = asyncio.create_task(switcher.switch_profile(profiles[1]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        operation.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 1)
        async with get_db(switcher.db_path) as connection:
            assert (await crud.get_active_voice_profile(connection)).id == profiles[1].id
        switcher.warmup_current_profile.assert_not_awaited()
    finally:
        release.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
