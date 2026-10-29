"""Status and restart operations must refer to the configured engine endpoint."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
import pytest

from galgame2voice.config import get_settings
from galgame2voice.routers import health
from galgame2voice.services.engine_runtime import EngineLaunch
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.sovits_endpoint import parse_sovits_endpoint
from scripts import run_server


@pytest.mark.asyncio
@pytest.mark.parametrize('client_target', ['owned', 'target', 'unrelated'])
@pytest.mark.parametrize('spawn_fails', [False, True])
async def test_restart_invalidates_only_the_engines_it_stops_or_starts(owned_engine, monkeypatch, client_target, spawn_fails):
    endpoint, proc = owned_engine
    target = parse_sovits_endpoint(f'http://127.0.0.1:{endpoint.port + 1}', source='db')
    addresses = {'owned': endpoint.base_url, 'target': target.base_url, 'unrelated': 'http://unrelated.test:9880'}
    client = GptSovitsClient(base_url=addresses[client_target])
    profile = object()
    client.active_profile = profile
    client.current_gpt_weights = 'loaded.ckpt'
    client.current_sovits_weights = 'loaded.pth'
    client.current_refer_audio = 'reference.wav'
    monkeypatch.setattr('galgame2voice.services.gpt_sovits_client.get_gpt_sovits_client', lambda: client)
    monkeypatch.setattr(health, 'resolve_effective_sovits_endpoint', AsyncMock(return_value=target))
    monkeypatch.setattr(health, '_apply_sovits_precision_config', AsyncMock(return_value=('cpu', False, 'test')))
    monkeypatch.setattr(run_server, 'is_port_in_use', lambda *args: False)

    def terminate(pid):
        assert pid == proc.pid
        if client_target != 'unrelated':
            assert client.active_profile is None
            assert not client.models_unchanged()

    next_process = MagicMock()
    next_process.poll.return_value = None
    spawn = MagicMock(return_value=next_process, side_effect=RuntimeError('Synthetic spawn failure') if spawn_fails else None)
    monkeypatch.setattr(run_server, 'terminate_process_tree', terminate)
    monkeypatch.setattr(run_server, '_spawn_sovits_process', spawn)
    if spawn_fails:
        with pytest.raises(HTTPException) as caught:
            await health.restart_sovits_endpoint(None)
        assert caught.value.status_code == 500
    else:
        assert (await health.restart_sovits_endpoint(None))['status'] == 'starting'
    if client_target == 'unrelated':
        assert client.active_profile is profile
        assert client.models_unchanged()
    else:
        assert client.current_gpt_weights is None
        assert client.current_sovits_weights is None
        assert client.current_refer_audio is None


@pytest.mark.asyncio
async def test_rejected_restart_preserves_confirmed_models(owned_engine, monkeypatch):
    endpoint, _ = owned_engine
    client = GptSovitsClient(base_url=endpoint.base_url)
    profile = object()
    client.active_profile = profile
    monkeypatch.setattr('galgame2voice.services.gpt_sovits_client.get_gpt_sovits_client', lambda: client)
    monkeypatch.setattr(run_server, 'is_port_in_use', lambda *args: True)
    monkeypatch.setattr(run_server, 'get_managed_engine_launch', lambda *args: None)
    with pytest.raises(HTTPException) as caught:
        await health.restart_sovits_endpoint(None)
    assert caught.value.status_code == 409
    assert client.active_profile is profile
    assert client.models_unchanged()


@pytest.fixture
def owned_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "project_root", tmp_path)
    (tmp_path / "data").mkdir()
    directory = tmp_path / "engine"
    directory.mkdir()
    (directory / "api_v2.py").write_text("# synthetic entrypoint, never executed")
    (tmp_path / "data/sovits_dir.txt").write_text(str(directory))
    endpoint = parse_sovits_endpoint("http://127.0.0.1:19230", source="db")
    proc = MagicMock(pid=1234)
    proc.poll.return_value = None
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", EngineLaunch(proc, endpoint.host, endpoint.port, "cpu", False))
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", None)
    monkeypatch.setattr(health, "_engine_restart_lock", threading.Lock())
    monkeypatch.setattr(health, "_engine_restart_target", None, raising=False)

    async def resolve():
        return endpoint

    async def probe(*args):
        return SimpleNamespace(status="unreachable")

    monkeypatch.setattr(health, "resolve_effective_sovits_endpoint", resolve)
    monkeypatch.setattr(health, "_probe_gpt_sovits", probe)
    return endpoint, proc


@pytest.mark.asyncio
@pytest.mark.parametrize("exited", [False, True])
async def test_old_endpoint_process_and_logs_do_not_describe_the_new_address(owned_engine, monkeypatch, exited):
    endpoint, proc = owned_engine
    proc.poll.return_value = 1 if exited else None
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", EngineLaunch(proc, endpoint.host, endpoint.port + 1, "cpu", False))
    diagnose = MagicMock(return_value="旧引擎日志中的错误")
    monkeypatch.setattr(health, "engine_failure_message", diagnose)
    result = await health.engine_status_endpoint()
    assert result["state"] == "unavailable"
    assert result["ready"] is False
    assert result["base_url"] == endpoint.base_url
    diagnose.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_missing_or_replaced_launch_record_cannot_prove_loading(owned_engine, monkeypatch, missing):
    endpoint, _ = owned_engine
    if missing:
        monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", None)
    else:
        replacement = MagicMock()
        replacement.poll.return_value = None
        monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", replacement)
    result = await health.engine_status_endpoint()
    assert result["state"] == "unavailable"
    assert result["base_url"] == endpoint.base_url


@pytest.mark.asyncio
async def test_another_endpoint_restart_does_not_hide_a_ready_service(owned_engine, monkeypatch):
    entered, complete = asyncio.Event(), asyncio.Event()

    async def restart(*args):
        entered.set()
        await complete.wait()
        return {"status": "starting"}

    monkeypatch.setattr(health, "_restart_local_engine", restart)
    task = asyncio.create_task(health.restart_sovits_endpoint(None))
    try:
        await entered.wait()
        endpoint = parse_sovits_endpoint("https://synthetic.invalid:9443/voice", source="db")

        async def resolve():
            return endpoint

        async def probe(url):
            assert url == endpoint.base_url
            return SimpleNamespace(status="reachable")

        monkeypatch.setattr(health, "resolve_effective_sovits_endpoint", resolve)
        monkeypatch.setattr(health, "_probe_gpt_sovits", probe)
        result = await health.engine_status_endpoint()
        assert result["state"] == "ready"
        assert result["base_url"] == endpoint.base_url
    finally:
        complete.set()
        await task


@pytest.mark.asyncio
async def test_restart_started_during_a_probe_invalidates_its_ready_result(owned_engine, monkeypatch):
    probing, finish_probe, restarting, finish_restart = (asyncio.Event() for _ in range(4))

    async def probe(*args):
        probing.set()
        await finish_probe.wait()
        return SimpleNamespace(status="reachable")

    async def restart(*args):
        restarting.set()
        await finish_restart.wait()
        return {"status": "starting"}

    monkeypatch.setattr(health, "_probe_gpt_sovits", probe)
    monkeypatch.setattr(health, "_restart_local_engine", restart)
    status_task = asyncio.create_task(health.engine_status_endpoint())
    restart_task = None
    try:
        await probing.wait()
        restart_task = asyncio.create_task(health.restart_sovits_endpoint(None))
        await restarting.wait()
        finish_probe.set()
        result = await status_task
        assert result["state"] == "starting"
        assert result["ready"] is False
    finally:
        finish_probe.set()
        finish_restart.set()
        await status_task
        if restart_task:
            await restart_task


@pytest.mark.asyncio
async def test_new_launch_during_a_probe_requires_a_fresh_read(owned_engine, monkeypatch):
    endpoint, _ = owned_engine

    async def probe(*args):
        replacement = MagicMock()
        replacement.poll.return_value = None
        monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", replacement)
        monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", EngineLaunch(replacement, endpoint.host, endpoint.port, "cpu", False))
        return SimpleNamespace(status="reachable")

    monkeypatch.setattr(health, "_probe_gpt_sovits", probe)
    result = await health.engine_status_endpoint()
    assert result["state"] == "checking"
    assert result["ready"] is False
    assert result["base_url"] == endpoint.base_url


@pytest.mark.asyncio
async def test_restart_during_log_diagnosis_invalidates_the_old_failure(owned_engine, monkeypatch):
    _, proc = owned_engine
    proc.poll.return_value = 1
    reading, finish_read, restarting, finish_restart = (asyncio.Event() for _ in range(4))
    to_thread = asyncio.to_thread

    async def delayed_diagnosis(function, *args, **kwargs):
        if function is health.engine_failure_message:
            reading.set()
            await finish_read.wait()
            return "旧进程的启动错误"
        return await to_thread(function, *args, **kwargs)

    async def restart(*args):
        restarting.set()
        await finish_restart.wait()
        return {"status": "starting"}

    monkeypatch.setattr(health.asyncio, "to_thread", delayed_diagnosis)
    monkeypatch.setattr(health, "_restart_local_engine", restart)
    status_task = asyncio.create_task(health.engine_status_endpoint())
    restart_task = None
    try:
        await reading.wait()
        restart_task = asyncio.create_task(health.restart_sovits_endpoint(None))
        await restarting.wait()
        finish_read.set()
        result = await status_task
        assert result["state"] == "starting"
        assert "旧进程" not in result["message"]
    finally:
        finish_read.set()
        finish_restart.set()
        await status_task
        if restart_task:
            await restart_task


@pytest.mark.asyncio
async def test_loading_age_belongs_to_the_matching_launch(owned_engine, monkeypatch):
    endpoint, proc = owned_engine
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 0.0)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", EngineLaunch(
        proc, endpoint.host, endpoint.port, "cpu", False, run_server.time.monotonic()))
    result = await health.engine_status_endpoint()
    assert result["state"] == "starting"


@pytest.mark.asyncio
async def test_moving_to_a_free_port_still_restarts_the_owned_process(owned_engine, monkeypatch):
    endpoint, proc = owned_engine
    next_endpoint = parse_sovits_endpoint(f"http://127.0.0.1:{endpoint.port + 1}", source="db")

    async def resolve():
        return next_endpoint

    async def precision(*args):
        return "cpu", False, "request"

    monkeypatch.setattr(health, "resolve_effective_sovits_endpoint", resolve)
    monkeypatch.setattr(health, "_apply_sovits_precision_config", precision)
    monkeypatch.setattr(run_server, "is_port_in_use", lambda *args: False)
    terminate = MagicMock()
    next_proc = MagicMock()
    next_proc.poll.return_value = None
    spawn = MagicMock(return_value=next_proc)
    monkeypatch.setattr(run_server, "terminate_process_tree", terminate)
    monkeypatch.setattr(run_server, "_spawn_sovits_process", spawn)
    result = await health.restart_sovits_endpoint(None)
    assert result["status"] == "starting"
    terminate.assert_called_once_with(proc.pid)
    assert spawn.call_args.args[1:3] == (next_endpoint.host, next_endpoint.port)


@pytest.mark.asyncio
async def test_busy_new_port_preserves_the_owned_engine_at_the_old_address(owned_engine, monkeypatch):
    old_endpoint, proc = owned_engine
    new_endpoint = parse_sovits_endpoint(f"http://127.0.0.1:{old_endpoint.port + 1}", source="db")

    async def resolve():
        return new_endpoint

    precision = MagicMock()

    async def apply_precision(*args):
        precision()
        return "cpu", False, "request"

    sleep = asyncio.sleep

    async def fast_sleep(delay):
        await sleep(0 if delay == 0.25 else delay)

    monkeypatch.setattr(health, "resolve_effective_sovits_endpoint", resolve)
    monkeypatch.setattr(health, "_apply_sovits_precision_config", apply_precision)
    monkeypatch.setattr(health.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(run_server, "is_port_in_use", lambda *args: True)
    terminate, spawn = MagicMock(), MagicMock()
    monkeypatch.setattr(run_server, "terminate_process_tree", terminate)
    monkeypatch.setattr(run_server, "_spawn_sovits_process", spawn)
    with pytest.raises(HTTPException) as caught:
        await health.restart_sovits_endpoint(None)
    assert caught.value.status_code == 409
    terminate.assert_not_called()
    precision.assert_not_called()
    spawn.assert_not_called()
    proc.wait.assert_not_called()
