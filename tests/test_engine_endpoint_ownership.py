"""Status and restart operations must refer to the configured engine endpoint."""

import ast
import asyncio
import threading
from pathlib import Path
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
    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
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
        await asyncio.wait_for(complete.wait(), timeout=10.0)
        return {"status": "starting"}

    monkeypatch.setattr(health, "_restart_local_engine", restart)
    task = asyncio.create_task(health.restart_sovits_endpoint(None))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10.0)
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
        await asyncio.wait_for(finish_probe.wait(), timeout=10.0)
        return SimpleNamespace(status="reachable")

    async def restart(*args):
        restarting.set()
        await asyncio.wait_for(finish_restart.wait(), timeout=10.0)
        return {"status": "starting"}

    monkeypatch.setattr(health, "_probe_gpt_sovits", probe)
    monkeypatch.setattr(health, "_restart_local_engine", restart)
    status_task = asyncio.create_task(health.engine_status_endpoint())
    restart_task = None
    try:
        await asyncio.wait_for(probing.wait(), timeout=10.0)
        restart_task = asyncio.create_task(health.restart_sovits_endpoint(None))
        await asyncio.wait_for(restarting.wait(), timeout=10.0)
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
            await asyncio.wait_for(finish_read.wait(), timeout=10.0)
            return "旧进程的启动错误"
        return await to_thread(function, *args, **kwargs)

    async def restart(*args):
        restarting.set()
        await asyncio.wait_for(finish_restart.wait(), timeout=10.0)
        return {"status": "starting"}

    monkeypatch.setattr(health.asyncio, "to_thread", delayed_diagnosis)
    monkeypatch.setattr(health, "_restart_local_engine", restart)
    status_task = asyncio.create_task(health.engine_status_endpoint())
    restart_task = None
    try:
        await asyncio.wait_for(reading.wait(), timeout=10.0)
        restart_task = asyncio.create_task(health.restart_sovits_endpoint(None))
        await asyncio.wait_for(restarting.wait(), timeout=10.0)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["port_busy", "spawn_error"])
async def test_restart_abort_after_stopping_owned_process_clears_launch_and_does_not_report_failed(
    owned_engine, monkeypatch, tmp_path, failure_mode
):
    endpoint, proc = owned_engine
    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
    pid_file = tmp_path / "gptsovits.pid"
    pid_file.write_text(str(proc.pid), encoding="utf-8")

    def terminate(pid):
        assert pid == proc.pid
        proc.poll.return_value = 1

    monkeypatch.setattr(run_server, "terminate_process_tree", terminate)
    proc.wait = MagicMock(return_value=1)

    monkeypatch.setattr(health, "_apply_sovits_precision_config", AsyncMock(return_value=("cpu", False, "test")))

    sleep = asyncio.sleep

    async def fast_sleep(delay):
        await sleep(0 if delay == 0.25 else delay)

    monkeypatch.setattr(health.asyncio, "sleep", fast_sleep)

    if failure_mode == "port_busy":
        monkeypatch.setattr(run_server, "is_port_in_use", lambda port, host=None: True)
        with pytest.raises(HTTPException) as caught:
            await health.restart_sovits_endpoint(None)
        assert caught.value.status_code == 409
    else:
        monkeypatch.setattr(run_server, "is_port_in_use", lambda port, host=None: False)
        monkeypatch.setattr(run_server, "_spawn_sovits_process", MagicMock(side_effect=RuntimeError("Spawn failed")))
        with pytest.raises(HTTPException) as caught:
            await health.restart_sovits_endpoint(None)
        assert caught.value.status_code == 500

    status = await health.engine_status_endpoint()
    assert status["state"] == "unavailable"
    assert "退出" not in status["message"]
    assert run_server._SPAWNED_SOVITS_PROC is None
    assert run_server._SPAWNED_SOVITS_LAUNCH is None
    assert run_server._SPAWNED_SOVITS_STARTED_AT is None
    assert not pid_file.exists()


@pytest.mark.asyncio
async def test_managed_engine_launch_matches_localhost_loopback_alias_and_allows_restart(owned_engine, monkeypatch):
    endpoint, proc = owned_engine
    launch = EngineLaunch(proc, "127.0.0.1", 9880, "cuda", False)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", launch)

    localhost_endpoint = parse_sovits_endpoint("http://localhost:9880", source="db")
    assert localhost_endpoint.is_local is True

    matched = run_server.get_managed_engine_launch(localhost_endpoint)
    assert matched is launch
    assert run_server.get_managed_engine_precision(localhost_endpoint) == "FP32"

    monkeypatch.setattr(health, "resolve_effective_sovits_endpoint", AsyncMock(return_value=localhost_endpoint))
    monkeypatch.setattr(health, "_apply_sovits_precision_config", AsyncMock(return_value=("cuda", False, "test")))

    in_use = True
    monkeypatch.setattr(run_server, "is_port_in_use", lambda port, host=None: in_use)

    terminated_pids = []

    def terminate(pid):
        nonlocal in_use
        terminated_pids.append(pid)
        proc.poll.return_value = 1
        in_use = False

    monkeypatch.setattr(run_server, "terminate_process_tree", terminate)
    proc.wait = MagicMock(return_value=0)

    next_proc = MagicMock(pid=5678)
    next_proc.poll.return_value = None
    spawn = MagicMock(return_value=next_proc)
    monkeypatch.setattr(run_server, "_spawn_sovits_process", spawn)

    result = await health.restart_sovits_endpoint(None)
    assert result["status"] == "starting"
    assert terminated_pids == [proc.pid]
    assert spawn.call_count == 1
    assert spawn.call_args.args[1:3] == (localhost_endpoint.host, localhost_endpoint.port)


def test_managed_engine_launch_does_not_match_ipv6_loopback_to_ipv4_launch(owned_engine, monkeypatch):
    endpoint, proc = owned_engine
    launch = EngineLaunch(proc, "127.0.0.1", 9880, "cuda", False)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", launch)

    ipv6_endpoint = parse_sovits_endpoint("http://[::1]:9880", source="db")
    assert ipv6_endpoint.is_local is True

    assert run_server.get_managed_engine_launch(ipv6_endpoint) is None
    assert run_server.get_managed_engine_precision(ipv6_endpoint) is None


@pytest.mark.parametrize(
    ("ep_url", "launch_host", "launch_port", "expected_match"),
    [
        ("http://localhost:9880", "127.0.0.1", 9880, True),
        ("http://127.0.0.1:9880", "localhost", 9880, True),
        ("http://localhost:9880", "localhost", 9880, True),
        ("http://127.0.0.1:9880", "127.0.0.1", 9880, True),
        ("http://localhost:9880", "0.0.0.0", 9880, True),
        ("http://127.0.0.1:9880", "0.0.0.0", 9880, True),
        ("http://LOCALHOST:9880", "127.0.0.1", 9880, True),
        ("http://localhost:9880", "  127.0.0.1  ", 9880, True),
        ("http://127.0.0.1:9880", "::1", 9880, False),
        ("http://localhost:9880", "::1", 9880, False),
        ("http://[::1]:9880", "localhost", 9880, False),
        ("http://localhost:9880", "127.0.0.1", 9881, False),
        ("http://[::1]:9880", "127.0.0.1", 9880, False),
        ("http://[::1]:9880", "::1", 9880, True),
        ("http://[::1]:9880", "[::1]", 9880, True),
        ("http://[::1]:9880", "0.0.0.0", 9880, False),
    ],
)
def test_managed_engine_launch_host_matching_matrix(
    owned_engine, monkeypatch, ep_url, launch_host, launch_port, expected_match
):
    _, proc = owned_engine
    launch = EngineLaunch(proc, launch_host, launch_port, "cpu", False)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", launch)

    endpoint = parse_sovits_endpoint(ep_url, source="db")
    matched = run_server.get_managed_engine_launch(endpoint)
    if expected_match:
        assert matched is launch
    else:
        assert matched is None


@pytest.mark.asyncio
async def test_managed_engine_launch_matches_localhost_when_process_crashed_for_diagnostics(owned_engine, monkeypatch):
    endpoint, proc = owned_engine
    proc.poll.return_value = 1
    launch = EngineLaunch(proc, "127.0.0.1", 9880, "cuda", False)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", launch)

    localhost_endpoint = parse_sovits_endpoint("http://localhost:9880", source="db")
    matched = run_server.get_managed_engine_launch(localhost_endpoint)
    assert matched is launch
    assert run_server.get_managed_engine_precision(localhost_endpoint) is None

    monkeypatch.setattr(health, "resolve_effective_sovits_endpoint", AsyncMock(return_value=localhost_endpoint))
    diagnose = MagicMock(return_value="显存不足 (CUDA out of memory)")
    monkeypatch.setattr(health, "engine_failure_message", diagnose)

    status = await health.engine_status_endpoint()
    assert status["state"] == "failed"
    assert "显存不足 (CUDA out of memory)" in status["message"]
    diagnose.assert_called_once()


def test_is_matching_managed_host_rejects_empty_or_none():
    assert run_server._is_matching_managed_host(None, None) is False
    assert run_server._is_matching_managed_host("", "") is False
    assert run_server._is_matching_managed_host("127.0.0.1", None) is False
    assert run_server._is_matching_managed_host(None, "127.0.0.1") is False
    assert run_server._is_matching_managed_host("   ", "   ") is False


@pytest.mark.asyncio
async def test_saving_remote_sovits_url_unlinks_pid_under_settings_project_root_not_launcher_root(
    tmp_path, monkeypatch
):
    from galgame2voice.routers import config as config_router

    monkeypatch.delenv("GALGAME2VOICE_GPT_SOVITS_BASE_URL", raising=False)
    monkeypatch.delenv("GPT_SOVITS_BASE_URL", raising=False)

    launcher_root = tmp_path / "launcher"
    custom_root = tmp_path / "custom_project_root"
    launcher_root.mkdir()
    custom_root.mkdir()
    (launcher_root / "gptsovits.pid").write_text("4321", encoding="utf-8")
    (custom_root / "gptsovits.pid").write_text("1234", encoding="utf-8")

    monkeypatch.setattr(get_settings(), "project_root", custom_root)
    monkeypatch.setattr(run_server, "PROJECT_ROOT", launcher_root)

    proc = MagicMock(pid=1234)
    proc.poll.return_value = None
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    monkeypatch.setattr(
        run_server,
        "_SPAWNED_SOVITS_LAUNCH",
        EngineLaunch(proc, "127.0.0.1", 9880, "cpu", False, started_at=1.0),
    )
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 1.0)
    monkeypatch.setattr(health, "_engine_restart_lock", threading.Lock())

    def terminate(pid, timeout=2.0):
        assert pid == 1234
        proc.poll.return_value = 0

    monkeypatch.setattr(run_server, "terminate_process_tree", terminate)

    result = await config_router._apply_sovits_url_update("http://192.168.1.50:9880")
    assert result == {"base_url": "http://192.168.1.50:9880", "source": "db"}
    assert not (custom_root / "gptsovits.pid").exists()
    assert (launcher_root / "gptsovits.pid").exists()


@pytest.mark.asyncio
async def test_restart_local_engine_uses_settings_project_root_for_pid_cache_and_logs(
    tmp_path, monkeypatch
):
    import sys

    launcher_root = tmp_path / "launcher"
    custom_root = tmp_path / "custom_project_root"
    launcher_root.mkdir()
    (custom_root / "data").mkdir(parents=True)

    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    (engine_dir / "api_v2.py").write_text("# synthetic entrypoint, never executed", encoding="utf-8")
    (custom_root / "data" / "sovits_dir.txt").write_text(str(engine_dir), encoding="utf-8")

    (launcher_root / "gptsovits.pid").write_text("9999", encoding="utf-8")
    (custom_root / "gptsovits.pid").write_text("1234", encoding="utf-8")

    monkeypatch.setattr(get_settings(), "project_root", custom_root)
    monkeypatch.setattr(run_server, "PROJECT_ROOT", launcher_root)

    endpoint = parse_sovits_endpoint("http://127.0.0.1:19230", source="db")
    old_proc = MagicMock(pid=1234)
    old_proc.poll.return_value = None
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", old_proc)
    monkeypatch.setattr(
        run_server,
        "_SPAWNED_SOVITS_LAUNCH",
        EngineLaunch(old_proc, endpoint.host, endpoint.port, "cpu", False),
    )
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 1.0)
    monkeypatch.setattr(health, "_engine_restart_lock", threading.Lock())
    monkeypatch.setattr(health, "_engine_restart_target", None, raising=False)
    monkeypatch.setattr(health, "resolve_effective_sovits_endpoint", AsyncMock(return_value=endpoint))
    monkeypatch.setattr(health, "_apply_sovits_precision_config", AsyncMock(return_value=("cpu", False, "test")))
    monkeypatch.setattr(run_server, "is_port_in_use", lambda *args: False)

    def terminate(pid, timeout=2.0):
        assert pid == 1234
        old_proc.poll.return_value = 0

    monkeypatch.setattr(run_server, "terminate_process_tree", terminate)
    monkeypatch.setattr(run_server, "find_engine_python", lambda _: tmp_path / "python.exe")
    monkeypatch.setattr(run_server, "write_sovits_yaml_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_server, "setup_windows_job_object", lambda: None)
    monkeypatch.setattr(run_server, "assign_process_to_job", lambda _: False)

    new_proc = MagicMock(pid=5678)
    new_proc.poll.return_value = None
    monkeypatch.setattr(run_server.subprocess, "Popen", MagicMock(return_value=new_proc))
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    result = await health.restart_sovits_endpoint(None)
    assert result["status"] == "starting"
    assert result["pid"] == 5678
    assert (custom_root / "gptsovits.pid").read_text(encoding="utf-8") == "5678"
    assert (custom_root / "data" / "engine_cache" / "numba").is_dir()
    assert (custom_root / "data" / "engine_cache" / "temp").is_dir()
    assert (custom_root / "logs" / "gpt_sovits.log").is_file()
    assert (launcher_root / "gptsovits.pid").read_text(encoding="utf-8") == "9999"
    assert not (launcher_root / "data" / "engine_cache").exists()
    assert not (launcher_root / "logs" / "gpt_sovits.log").exists()


def test_find_custom_voice_ref_audio_does_not_fallback_to_launcher_project_root(tmp_path, monkeypatch):
    import sqlite3

    launcher_root = tmp_path / "launcher"
    custom_root = tmp_path / "custom_root"
    (launcher_root / "audio").mkdir(parents=True)
    (custom_root / "data").mkdir(parents=True)
    ghost = launcher_root / "audio" / "ghost_ref.wav"
    ghost.write_bytes(b"not-a-real-wav")

    db_file = custom_root / "data" / "galgame2voice.db"
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            "CREATE TABLE settings (id INTEGER PRIMARY KEY, active_voice_profile_id INTEGER)"
        )
        conn.execute(
            """
            CREATE TABLE voice_profiles (
                id INTEGER PRIMARY KEY,
                ref_audio_path TEXT,
                prompt_text TEXT,
                prompt_lang TEXT,
                is_default INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO settings (id, active_voice_profile_id) VALUES (1, 1)"
        )
        conn.execute(
            """
            INSERT INTO voice_profiles (id, ref_audio_path, prompt_text, prompt_lang, is_default)
            VALUES (1, 'audio/ghost_ref.wav', 'テスト', 'ja', 0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    from galgame2voice.utils import audio_spec

    monkeypatch.setattr(run_server, "PROJECT_ROOT", launcher_root)
    monkeypatch.setattr(audio_spec, "_PROJECT_ROOT", launcher_root)
    monkeypatch.chdir(launcher_root)
    settings = get_settings()
    # The autouse fixture points DATA_DIR_NAME at an absolute scratch directory.
    # Pin the data folder name so db_path stays under the configured project root.
    monkeypatch.setattr(settings, "project_root", custom_root)
    monkeypatch.setattr(settings, "data_dir_name", "data")
    assert settings.db_path == db_file

    ref_file, _, _ = run_server._find_custom_voice_ref_audio()
    assert ref_file is None

    custom_audio = custom_root / "audio" / "ghost_ref.wav"
    custom_audio.parent.mkdir(parents=True)
    custom_audio.write_bytes(b"not-a-real-wav")
    ref_file, _, _ = run_server._find_custom_voice_ref_audio()
    assert ref_file == custom_audio.resolve()


def test_calibrate_and_cleanup_use_configured_project_root_and_clear_supervisor_launch(tmp_path, monkeypatch):
    launcher_root = tmp_path / "launcher"
    custom_root = tmp_path / "custom_root"
    launcher_root.mkdir()
    custom_root.mkdir()
    (launcher_root / "gptsovits.pid").write_text("1111", encoding="utf-8")
    (custom_root / "gptsovits.pid").write_text("2222", encoding="utf-8")

    monkeypatch.setattr(get_settings(), "project_root", custom_root)
    monkeypatch.setattr(run_server, "PROJECT_ROOT", launcher_root)
    monkeypatch.setattr(run_server, "_WINDOWS_JOB_HANDLE", None)

    sovits_dir = tmp_path / "engine"
    run_server.calibrate_engine_precision(sovits_dir, True, lambda: 0.25, lambda _: None)
    assert (custom_root / "data" / "precision.json").is_file()
    assert not (launcher_root / "data" / "precision.json").exists()

    proc = MagicMock()
    proc.poll.return_value = 0
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", object())
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 123.0)
    run_server.cleanup_subprocesses()
    assert run_server._SPAWNED_SOVITS_LAUNCH is None
    assert run_server._SPAWNED_SOVITS_STARTED_AT is None
    assert not (custom_root / "gptsovits.pid").exists()
    assert (launcher_root / "gptsovits.pid").read_text(encoding="utf-8") == "1111"


def _is_scripts_module(name: str) -> bool:
    return name == "scripts" or name.startswith("scripts.")


def _dynamic_scripts_import(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    is_import_module = (
        isinstance(func, ast.Name) and func.id in {"__import__", "import_module"}
    ) or (
        isinstance(func, ast.Attribute)
        and func.attr == "import_module"
        and isinstance(func.value, ast.Name)
        and func.value.id == "importlib"
    )
    if not is_import_module:
        return False
    candidates = list(node.args[:1]) + [kw.value for kw in node.keywords if kw.arg == "name"]
    return any(
        isinstance(arg, ast.Constant) and isinstance(arg.value, str) and _is_scripts_module(arg.value)
        for arg in candidates
    )


def _scripts_reverse_import_lines(source: str) -> list[int]:
    tree = ast.parse(source)
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_is_scripts_module(alias.name) for alias in node.names):
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and _is_scripts_module(node.module):
                lines.append(node.lineno)
        elif _dynamic_scripts_import(node):
            lines.append(node.lineno)
    return lines


def test_galgame2voice_package_has_no_reverse_imports_from_scripts():
    package_root = Path(__file__).resolve().parents[1] / "galgame2voice"
    offenders: list[str] = []
    for py_file in sorted(package_root.rglob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        for line in _scripts_reverse_import_lines(source):
            relative = py_file.relative_to(package_root.parent)
            offenders.append(f"{relative}:{line}")
    assert offenders == [], f"galgame2voice must not import scripts: {offenders}"


def test_scripts_import_guard_rejects_static_and_dynamic_imports():
    assert _scripts_reverse_import_lines("import scripts.run_server\n") == [1]
    assert _scripts_reverse_import_lines("from scripts.run_server import foo\n") == [1]
    assert _scripts_reverse_import_lines("import scripts\n") == [1]
    assert _scripts_reverse_import_lines('import importlib\nimportlib.import_module("scripts.foo")\n') == [2]
    assert _scripts_reverse_import_lines('import importlib\nimportlib.import_module("scripts")\n') == [2]
    assert _scripts_reverse_import_lines('__import__("scripts.run_server")\n') == [1]
    benign = 'note = """\nimport scripts.run_server\n"""\n'
    assert _scripts_reverse_import_lines(benign) == []


