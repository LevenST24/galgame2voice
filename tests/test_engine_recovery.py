"""Owned engine restart, truthful status and cancellation-safe recovery."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException
import pytest

from galgame2voice.config import get_settings
from galgame2voice.routers import health
from galgame2voice.services.engine_recovery import engine_failure_message
from galgame2voice.services.sovits_endpoint import parse_sovits_endpoint
from scripts import run_server


@pytest.fixture
def engine(tmp_path, monkeypatch):
    root = tmp_path
    monkeypatch.setattr(get_settings(), "project_root", root)
    (root / "data").mkdir()
    directory = root / "engine"
    directory.mkdir()
    (directory / "api_v2.py").write_text("# test marker, never run")
    (root / "data" / "sovits_dir.txt").write_text(str(directory))
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", None)
    monkeypatch.setattr(run_server, "is_port_in_use", lambda *args: False)
    async def resolve():
        return parse_sovits_endpoint("http://127.0.0.1:19230", source="db")
    monkeypatch.setattr(health, "resolve_effective_sovits_endpoint", resolve)
    async def precision(*args):
        return "cpu", False, "request"
    monkeypatch.setattr(health, "_apply_sovits_precision_config", precision)
    return root, directory


@pytest.mark.parametrize("log, keyword", [
    ("RuntimeError: CUDA out of memory", "CPU"),
    ("ModuleNotFoundError: no module named torch", "runtime"),
    ("DLL load failed", "runtime"),
    ("FileNotFoundError: private-file.ckpt", "会话设置"),
    ("PermissionError: access is denied", "文档"),
    ("Unexpected internal exception sk-secret-value", "文字聊天"),
])
def test_engine_log_guidance_hides_raw_errors(tmp_path, log, keyword):
    file = tmp_path / "engine.log"
    file.write_text(log)
    message = engine_failure_message(file)
    assert keyword in message
    assert "sk-secret-value" not in message
    assert "private-file.ckpt" not in message


@pytest.mark.asyncio
async def test_restart_starts_without_trusting_a_stale_pid(engine, monkeypatch):
    root, directory = engine
    (root / "gptsovits.pid").write_text("7777")
    terminate = MagicMock()
    monkeypatch.setattr(run_server, "terminate_process_tree", terminate)
    proc = MagicMock(pid=12345)
    proc.poll.return_value = None
    monkeypatch.setattr(run_server, "_spawn_sovits_process", MagicMock(return_value=proc))
    result = await health.restart_sovits_endpoint(None)
    assert result["status"] == "starting" and result["ready"] is False
    terminate.assert_not_called()


@pytest.mark.asyncio
async def test_restart_does_not_touch_external_process_or_config(engine, monkeypatch):
    monkeypatch.setattr(run_server, "is_port_in_use", lambda *args: True)
    spawn = MagicMock()
    monkeypatch.setattr(run_server, "_spawn_sovits_process", spawn)
    changed = False
    async def precision(*args):
        nonlocal changed
        changed = True
    monkeypatch.setattr(health, "_apply_sovits_precision_config", precision)
    with pytest.raises(HTTPException) as error:
        await health.restart_sovits_endpoint(None)
    assert error.value.status_code == 409 and "其他窗口" in error.value.detail
    assert not changed
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_restart_stops_only_owned_live_tree(engine, monkeypatch):
    proc = MagicMock(pid=1234)
    proc.poll.return_value = None
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    terminate = MagicMock()
    monkeypatch.setattr(run_server, "terminate_process_tree", terminate)
    next_proc = MagicMock(pid=1235)
    next_proc.poll.return_value = None
    monkeypatch.setattr(run_server, "_spawn_sovits_process", MagicMock(return_value=next_proc))
    await health.restart_sovits_endpoint(None)
    terminate.assert_called_once_with(1234)
    proc.wait.assert_called_once_with(timeout=3)


@pytest.mark.asyncio
async def test_invalid_directory_preserves_running_engine(engine, monkeypatch):
    root, directory = engine
    (directory / "api_v2.py").unlink()
    terminate = MagicMock()
    monkeypatch.setattr(run_server, "terminate_process_tree", terminate)
    with pytest.raises(HTTPException) as error:
        await health.restart_sovits_endpoint(None)
    assert error.value.status_code == 400 and "重新选择" in error.value.detail
    terminate.assert_not_called()


@pytest.mark.asyncio
async def test_early_engine_exit_is_reported_as_failure(engine, monkeypatch):
    root, directory = engine
    (root / "logs").mkdir()
    (root / "logs" / "gpt_sovits.log").write_text("ModuleNotFoundError: no module named torch")
    proc = MagicMock(pid=1234)
    proc.poll.return_value = 1
    monkeypatch.setattr(run_server, "_spawn_sovits_process", MagicMock(return_value=proc))
    with pytest.raises(HTTPException) as error:
        await health.restart_sovits_endpoint(None)
    assert error.value.status_code == 502 and "完整解压" in error.value.detail


@pytest.mark.asyncio
async def test_restarts_are_serial_even_after_browser_disconnect(engine, monkeypatch):
    entered = asyncio.Event()
    complete = asyncio.Event()
    async def restart(*args):
        entered.set()
        await complete.wait()
        return {"status": "starting"}
    monkeypatch.setattr(health, "_restart_local_engine", restart)
    first = asyncio.create_task(health.restart_sovits_endpoint(None))
    try:
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(HTTPException) as error:
            await health.restart_sovits_endpoint(None)
        assert error.value.status_code == 409
    finally:
        complete.set()
        # Let the shielded operation and its release callback finish.
        for _ in range(3):
            await asyncio.sleep(0)
    assert not health._engine_restart_lock.locked()
    assert (await health.restart_sovits_endpoint(None))["status"] == "starting"


@pytest.mark.asyncio
@pytest.mark.parametrize("poll, reachable, state", [(None, False, "starting"), (1, False, "failed"), (None, True, "ready")])
async def test_status_uses_reachability_and_process_liveness(engine, monkeypatch, poll, reachable, state):
    proc = MagicMock()
    proc.poll.return_value = poll
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    async def probe(*args):
        return SimpleNamespace(status="reachable" if reachable else "unreachable")
    monkeypatch.setattr(health, "_probe_gpt_sovits", probe)
    result = await health.engine_status_endpoint()
    assert result["state"] == state
    assert result["ready"] == reachable


def test_failed_application_startup_keeps_nonzero_exit(engine, monkeypatch):
    import uvicorn
    root, directory = engine
    monkeypatch.setattr(run_server, "PROJECT_ROOT", root)
    monkeypatch.setattr(run_server, "_WINDOWS_JOB_HANDLE", None)
    monkeypatch.setattr(run_server, "setup_windows_job_object", lambda: None)
    monkeypatch.setattr(run_server, "setup_signal_handlers", lambda: None)
    monkeypatch.setattr(run_server.atexit, "register", lambda *args: None)
    monkeypatch.setattr(run_server, "check_python_environment", lambda: True)
    monkeypatch.setattr(run_server, "run_hardware_diagnostics", lambda: {})
    monkeypatch.setattr(run_server, "find_available_port", lambda *args, **kwargs: 18080)
    for key in ("GALGAME_PORT", "PORT", "GALGAME_HOST", "HOST"):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    def failed(*args, **kwargs):
        raise SystemExit(3)
    monkeypatch.setattr(uvicorn, "run", failed)
    with pytest.raises(SystemExit) as error:
        run_server.main(["--no-engine", "--no-browser", "--host", "127.0.0.1", "--port", "18080"])
    assert error.value.code == 3
