"""Concurrent settings reads share finite filesystem work without cancellation cascades."""

import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from galgame2voice.routers import health


def status_case(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from starlette.requests import Request
    from scripts import run_server

    settings = SimpleNamespace(project_root=tmp_path, data_dir=tmp_path / "data", audio_dir=tmp_path / "audio",
        db_path=tmp_path / "data/probe.db", data_dir_name="data", app_name="telemetry-test", app_version="test")
    monkeypatch.setattr(health, "get_settings", lambda: settings)
    async def endpoint():
        return None
    async def target():
        return "http://127.0.0.1:1"
    async def probe(url):
        return health.GptSovitsTelemetry(status="reachable", base_url=url)
    async def telegram(path):
        return health.TelegramTelemetry(enabled=False, status="disabled")
    monkeypatch.setattr(health, "_resolve_sovits_endpoint_best_effort", endpoint)
    monkeypatch.setattr(health, "get_effective_sovits_url", target)
    monkeypatch.setattr(health, "_probe_gpt_sovits", probe)
    monkeypatch.setattr(health, "_collect_telegram_telemetry", telegram)
    monkeypatch.setattr(health, "_get_process_memory_mb", lambda: 10)
    monkeypatch.setattr(health, "get_system_memory_status", lambda: (16, 8))
    monkeypatch.setattr(health, "read_sovits_directory", lambda root: None)
    monkeypatch.setattr(health, "read_db_precision", lambda *args, **kwargs: "cpu")
    monkeypatch.setattr(health, "resolve_initial_device_and_half", lambda *args, **kwargs: ("cpu", False, "test"))
    monkeypatch.setattr(run_server, "get_managed_engine_precision", lambda endpoint: None)
    monkeypatch.setattr(health, "_gpu_telemetry_cache", None)
    app = FastAPI()
    async def never_receive():
        await asyncio.Event().wait()
    def request():
        return Request({"type": "http", "method": "GET", "path": "/api/system/status", "headers": [], "app": app}, receive=never_receive)
    return app, request


@pytest.fixture(autouse=True)
def clear_metrics():
    health._dir_metrics_cache.clear()
    yield
    health._dir_metrics_cache.clear()


async def test_concurrent_directory_reads_share_the_scan_and_survive_one_cancellation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()
    calls = []
    def scan(path):
        calls.append(path)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return 3, 1.5
    monkeypatch.setattr(health, "_scan_dir_sync", scan)
    tasks = [asyncio.create_task(health._get_dir_metrics_cached(tmp_path / "audio" if i % 2 else Path("audio"))) for i in range(6)]
    try:
        await asyncio.wait_for(started.wait(), 3)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        release.set()
        assert await asyncio.gather(*tasks[1:]) == [(3, 1.5)] * 5
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_a_slow_scan_gets_a_full_cache_lifetime_after_completion(tmp_path, monkeypatch):
    clock, calls = [0.0], []
    monkeypatch.setattr(health, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def scan(path):
        calls.append(path)
        clock[0] += 30
        return 2, 1.0
    monkeypatch.setattr(health, "_scan_dir_sync", scan)
    assert await health._get_dir_metrics_cached(tmp_path) == (2, 1.0)
    assert await health._get_dir_metrics_cached(tmp_path) == (2, 1.0)
    assert len(calls) == 1, "a completed scan must not already be expired"


async def test_completed_directory_metrics_have_a_bounded_retention_table(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "_scan_dir_sync", lambda path: (1, 0.5))
    for index in range(96):
        assert await health._get_dir_metrics_cached(tmp_path / str(index)) == (1, 0.5)
    assert len(health._dir_metrics_cache) <= 64


async def test_concurrent_status_requests_share_one_gpu_inventory(tmp_path, monkeypatch):
    app, request = status_case(tmp_path, monkeypatch)
    started, release = asyncio.Event(), threading.Event()
    loop, calls = asyncio.get_running_loop(), []
    def detect(*, allow_torch_import):
        assert allow_torch_import is False
        calls.append(True)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return True, "NVIDIA synthetic probe", 1
    monkeypatch.setattr(health, "detect_gpu_capability", detect)
    tasks = [asyncio.create_task(health.system_status(request())) for _ in range(3)]
    try:
        await asyncio.wait_for(started.wait(), 3)
        for _ in range(8):
            await asyncio.sleep(0)
        await asyncio.to_thread(lambda: None)
        release.set()
        results = await asyncio.gather(*tasks)
        assert all(result.hardware.gpu_name == "NVIDIA synthetic probe" for result in results)
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        owner = getattr(app.state, "telemetry_jobs", None)
        if owner:
            await owner.aclose()


async def test_disconnected_status_reader_drains_its_owned_engine_probe(tmp_path, monkeypatch):
    from fastapi import HTTPException

    app, request = status_case(tmp_path, monkeypatch)
    monkeypatch.setattr(health, "detect_gpu_capability", lambda **kwargs: (False, "N/A", None))
    started, finished, disconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def blocked(url):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
    async def closed():
        return disconnected.is_set()
    monkeypatch.setattr(health, "_probe_gpt_sovits", blocked)
    reader = request()
    monkeypatch.setattr(reader, "is_disconnected", closed)
    operation = asyncio.create_task(health.system_status(reader))
    try:
        await asyncio.wait_for(started.wait(), 3)
        disconnected.set()
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(operation, 2)
        assert error.value.status_code == 499
        assert finished.is_set(), "the disconnected request left an engine health read alive"
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        owner = getattr(app.state, "telemetry_jobs", None)
        if owner:
            await owner.aclose()


def test_slow_gpu_inventory_gets_a_full_cache_lifetime(monkeypatch):
    clock, calls = [0.0], []
    monkeypatch.setattr(health, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(health, "_gpu_telemetry_cache", None)
    def detect(*, allow_torch_import):
        assert allow_torch_import is False
        calls.append(True)
        clock[0] += 70
        return True, "NVIDIA synthetic", 1
    monkeypatch.setattr(health, "detect_gpu_capability", detect)
    assert health._get_gpu_telemetry_cached() == (True, "NVIDIA synthetic")
    assert health._get_gpu_telemetry_cached() == (True, "NVIDIA synthetic")
    assert calls == [True]


async def test_shared_failure_is_retryable_and_one_cancelled_reader_does_not_cancel_the_job():
    from galgame2voice.services.telemetry_jobs import TelemetryJobs

    owner = TelemetryJobs()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def fail():
        calls.append(True)
        started.set()
        await release.wait()
        raise OSError("synthetic probe failure")
    readers = [asyncio.create_task(owner.run("probe", fail)) for _ in range(3)]
    try:
        await started.wait()
        readers[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await readers[0]
        release.set()
        results = await asyncio.gather(*readers[1:], return_exceptions=True)
        assert all(isinstance(result, OSError) for result in results) and len(calls) == 1
        async def recovered():
            calls.append(True)
            return "available"
        assert await owner.run("probe", recovered) == "available"
        assert len(calls) == 2
        await owner.aclose()
        with pytest.raises(asyncio.CancelledError):
            await owner.run("late", recovered)
    finally:
        release.set()
        await asyncio.gather(*readers, return_exceptions=True)
        await owner.aclose()


async def test_lifespan_closes_shared_telemetry_before_other_services(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from galgame2voice import main

    monkeypatch.setattr(main, "_init_logging_and_safety", lambda settings: None)
    monkeypatch.setattr(main, "_init_directories", lambda settings: None)
    monkeypatch.setattr(main, "_init_database_and_characters", AsyncMock())
    monkeypatch.setattr(main, "_init_gpt_sovits_client", AsyncMock())
    monkeypatch.setattr(main, "_start_telegram_bg", lambda settings: None)
    started, closed = asyncio.Event(), asyncio.Event()
    async def blocked():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    async def idle_cleanup(**kwargs):
        await asyncio.Event().wait()
    async def shutdown(settings, cleanup, telegram):
        assert closed.is_set(), "executor shutdown started before telemetry ownership was released"
        cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)
    monkeypatch.setattr(main, "_audio_cleanup_loop", idle_cleanup)
    monkeypatch.setattr(main, "_shutdown_services", shutdown)
    app = main.create_app()
    async with main.lifespan(app):
        reader = asyncio.create_task(app.state.telemetry_jobs.run("owned", blocked))
        await asyncio.wait_for(started.wait(), 3)
    assert closed.is_set()
    with pytest.raises(asyncio.CancelledError):
        await reader


async def test_database_metadata_check_keeps_the_event_loop_responsive(tmp_path, monkeypatch):
    app, request = status_case(tmp_path, monkeypatch)
    monkeypatch.setattr(health, "detect_gpu_capability", lambda **kwargs: (False, "N/A", None))
    loop = asyncio.get_running_loop()
    settings = health.get_settings()
    original = settings.db_path
    class Metadata:
        name = original.name
        def relative_to(self, parent):
            return original.relative_to(parent)
        def exists(self):
            responsive = threading.Event()
            loop.call_soon_threadsafe(responsive.set)
            assert responsive.wait(3), "database metadata check blocked the event loop"
            return False
    settings.db_path = Metadata()
    try:
        result = await health.system_status(request())
        assert result.database.status == "initializing"
    finally:
        owner = getattr(app.state, "telemetry_jobs", None)
        if owner:
            await owner.aclose()
