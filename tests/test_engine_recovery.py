"""Owned engine restart, truthful status and cancellation-safe recovery."""

import asyncio
import os
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException
import pytest

from galgame2voice.config import get_settings
from galgame2voice.routers import health
from galgame2voice.services.engine_recovery import engine_failure_message
from galgame2voice.services.engine_runtime import EngineLaunch
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
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", None)
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
    ("FileNotFoundError: private-file.ckpt", "角色与会话"),
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


def test_engine_failure_ignores_errors_from_previous_launch(tmp_path):
    log = tmp_path / "engine.log"
    log.write_text("CUDA out of memory\n=== Galgame2Voice engine start ===\nFileNotFoundError: missing.ckpt\n")
    assert "缺少模型或配置文件" in engine_failure_message(log)


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
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", EngineLaunch(proc, "127.0.0.1", 19230, "cpu", False))
    async def probe(*args):
        return SimpleNamespace(status="reachable" if reachable else "unreachable")
    monkeypatch.setattr(health, "_probe_gpt_sovits", probe)
    result = await health.engine_status_endpoint()
    assert result["state"] == state
    assert result["ready"] == reachable


@pytest.mark.asyncio
async def test_live_but_unresponsive_engine_does_not_claim_loading_forever(engine, monkeypatch):
    proc = MagicMock()
    proc.poll.return_value = None
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_LAUNCH", EngineLaunch(proc, "127.0.0.1", 19230, "cpu", False, 10.0))
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", 10.0, raising=False)
    monkeypatch.setattr(run_server.time, "monotonic", lambda: 250.0)

    async def probe(*args):
        return SimpleNamespace(status="unreachable")

    monkeypatch.setattr(health, "_probe_gpt_sovits", probe)
    result = await health.engine_status_endpoint()
    assert result["state"] == "loading_slow"
    assert result["ready"] is False
    assert "尚未确认就绪" in result["message"]
    assert "gpt_sovits.log" in result["message"]


@pytest.mark.requires_piped_subprocess
def test_engine_startup_output_is_visible_before_process_exit(engine, monkeypatch):
    root, directory = engine
    (root / "logs").mkdir()
    (directory / "api_v2.py").write_text("import time\nprint('engine-started')\ntime.sleep(20)\n")
    monkeypatch.setattr(run_server, "PROJECT_ROOT", root)
    monkeypatch.setattr(run_server, "find_engine_python", lambda _: Path(sys.executable))
    monkeypatch.setattr(run_server, "write_sovits_yaml_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_server, "build_gpt_sovits_env", lambda *args, **kwargs: os.environ.copy())
    monkeypatch.setattr(run_server, "setup_windows_job_object", lambda: None)
    monkeypatch.setattr(run_server, "assign_process_to_job", lambda _: False)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_STARTED_AT", None)
    proc = run_server._spawn_sovits_process(directory, "127.0.0.1", 19230, False)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "engine-started" in (root / "logs/gpt_sovits.log").read_text(encoding="utf-8"):
                break
            time.sleep(0.05)
        else:
            pytest.fail("Engine startup output stayed buffered while the process was alive")
        assert proc.poll() is None
        assert run_server._SPAWNED_SOVITS_STARTED_AT is not None
        assert run_server._SPAWNED_SOVITS_LAUNCH.process is proc
        assert run_server._SPAWNED_SOVITS_LAUNCH.port == 19230
        assert run_server._SPAWNED_SOVITS_LAUNCH.started_at == run_server._SPAWNED_SOVITS_STARTED_AT
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_engine_numba_cache_uses_writable_application_data(engine, monkeypatch):
    root, directory = engine
    monkeypatch.setattr(run_server, "PROJECT_ROOT", root)
    monkeypatch.delenv("NUMBA_CACHE_DIR", raising=False)
    environment = run_server.build_gpt_sovits_env(directory)
    cache = Path(environment["NUMBA_CACHE_DIR"])
    assert cache.is_relative_to(root / "data")
    assert not cache.is_relative_to(directory)
    assert cache.is_dir()
    (cache / "write-probe").write_text("ok")
    for key in ("TEMP", "TMP", "TMPDIR"):
        temporary = Path(environment[key])
        assert temporary.is_relative_to(root / "data")
        assert not temporary.is_relative_to(directory)
        assert temporary.is_dir()


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
    for key in ("GALGAME_PORT", "PORT", "GALGAME_HOST", "HOST"):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    def failed(*args, **kwargs):
        raise SystemExit(3)
    monkeypatch.setattr(uvicorn, "Config", MagicMock())
    server = MagicMock()
    server.run.side_effect = failed
    monkeypatch.setattr(uvicorn, "Server", lambda config: server)
    with pytest.raises(SystemExit) as error:
        run_server.main(["--no-engine", "--no-browser", "--host", "127.0.0.1", "--port", "0"])
    assert error.value.code == 3


def test_engine_guidance_quotes_only_names_the_settings_page_shows():
    """Guidance tells the user which tab, button or option to use, quoting its name in 「」.

    The tab was renamed from 语音与推理 to 语音服务, and messages kept the old name. Each
    quoted name (each step of 「设置中心 → 语音服务」) must be text the page shows.
    """
    repo = Path(__file__).resolve().parents[1]
    page = re.sub(r"<!--.*?-->", " ", (repo / "frontend" / "index.html").read_text(encoding="utf-8"), flags=re.S)
    visible = re.sub(r"<[^>]+>", " ", page)
    sources = [repo / "galgame2voice" / "routers" / "health.py", repo / "galgame2voice" / "routers" / "common.py",
               repo / "galgame2voice" / "services" / "engine_recovery.py",
               repo / "galgame2voice" / "services" / "sovits_installation.py"]
    quoted = {step.strip() for source in sources
              for name in re.findall(r"「([^」]+)」", source.read_text(encoding="utf-8"))
              for step in name.split("→")}
    # 「文档」 is the Windows Documents folder, not part of the page.
    assert sorted(name for name in quoted - {"文档"} if name not in visible) == []
