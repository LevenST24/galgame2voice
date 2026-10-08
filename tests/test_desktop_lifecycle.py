"""Portable instance ownership and real Windows job shutdown regressions."""

import json
import io
import os
import subprocess
import sys
import time
from unittest.mock import MagicMock

import psutil
import pytest

from scripts import desktop_entry, desktop_instance, run_server


def test_cleanup_never_kills_pid_read_from_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", None)
    monkeypatch.setattr(run_server, "_WINDOWS_JOB_HANDLE", None)
    (tmp_path / "data").mkdir()
    (tmp_path / "galgame2voice.pid").write_text(str(os.getpid()))
    (tmp_path / "gptsovits.pid").write_text(str(os.getpid()))
    terminate = MagicMock()
    monkeypatch.setattr(run_server, "terminate_process_tree", terminate)
    run_server.cleanup_subprocesses()
    terminate.assert_not_called()
    assert not (tmp_path / "gptsovits.pid").exists()


def test_cleanup_preserves_other_launchers_tracking_files(tmp_path, monkeypatch):
    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", None)
    monkeypatch.setattr(run_server, "_WINDOWS_JOB_HANDLE", None)
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
    (tmp_path / "data").mkdir()
    files = [tmp_path / "galgame2voice.pid", tmp_path / "gptsovits.pid", tmp_path / "data" / "active_port.txt"]
    for file in files:
        file.write_text("99999999")
    run_server.cleanup_subprocesses()
    assert all(file.read_text() == "99999999" for file in files)


def test_cleanup_enumerates_live_tree_before_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(run_server, "_WINDOWS_JOB_HANDLE", None)
    proc = MagicMock(pid=12345)
    proc.poll.return_value = None
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    events = []
    monkeypatch.setattr(run_server, "terminate_process_tree", lambda pid: events.append(("tree", pid)))
    proc.wait.side_effect = lambda **kwargs: events.append(("wait", kwargs["timeout"]))
    run_server.cleanup_subprocesses()
    assert events == [("tree", 12345), ("wait", 3)]
    assert run_server._SPAWNED_SOVITS_PROC is None
    # A second cleanup cannot act on a reused PID.
    run_server.cleanup_subprocesses()
    assert len(events) == 2


def test_cleanup_ignores_exited_process_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(run_server, "_WINDOWS_JOB_HANDLE", None)
    proc = MagicMock()
    proc.poll.return_value = 0
    monkeypatch.setattr(run_server, "_SPAWNED_SOVITS_PROC", proc)
    terminate = MagicMock()
    monkeypatch.setattr(run_server, "terminate_process_tree", terminate)
    run_server.cleanup_subprocesses()
    terminate.assert_not_called()


def test_existing_instance_uses_actual_port_without_browser(tmp_path, monkeypatch, capsys):
    desktop_instance.write_instance_address(tmp_path, "0.0.0.0", 18080)
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = b'{"app":"galgame2voice","status":"ok"}'
    monkeypatch.setattr(desktop_instance.urllib.request, "build_opener", lambda *args: opener)
    browser = MagicMock()
    monkeypatch.setattr(desktop_instance.webbrowser, "open", browser)
    desktop_instance.open_existing_instance(tmp_path, no_browser=True)
    opener.open.assert_called_once_with("http://127.0.0.1:18080/api/health", timeout=0.5)
    browser.assert_not_called()
    assert "18080" in capsys.readouterr().out


def test_existing_instance_opens_browser_when_requested(tmp_path, monkeypatch):
    desktop_instance.write_instance_address(tmp_path, "::1", 18080)
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = b'{"app":"galgame2voice","status":"ok"}'
    monkeypatch.setattr(desktop_instance.urllib.request, "build_opener", lambda *args: opener)
    browser = MagicMock()
    monkeypatch.setattr(desktop_instance.webbrowser, "open", browser)
    desktop_instance.open_existing_instance(tmp_path, no_browser=False)
    browser.assert_called_once_with("http://[::1]:18080/")


@pytest.mark.parametrize("metadata", [
    {"host": "example.com", "port": 8080},
    {"host": "127.0.0.1", "port": "8080"},
    {"host": "127.0.0.1", "port": 0},
])
def test_existing_instance_rejects_invalid_addresses(tmp_path, monkeypatch, metadata):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "desktop_instance.json").write_text(json.dumps(metadata))
    browser = MagicMock()
    opener = MagicMock()
    monkeypatch.setattr(desktop_instance.webbrowser, "open", browser)
    monkeypatch.setattr(desktop_instance.urllib.request, "build_opener", lambda *args: opener)
    monkeypatch.setattr(desktop_instance, "sleep", lambda delay: None)
    clock = iter([0, 0, 2])
    monkeypatch.setattr(desktop_instance, "monotonic", lambda: next(clock))
    desktop_instance.open_existing_instance(tmp_path, no_browser=False, timeout=1)
    browser.assert_not_called()
    opener.open.assert_not_called()


def test_duplicate_entry_never_imports_or_runs_server(tmp_path, monkeypatch):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "ffmpeg.exe").write_bytes(b"marker")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(desktop_entry, "get_install_root", lambda: tmp_path)
    monkeypatch.setattr(desktop_entry.sys, "frozen", True, raising=False)
    monkeypatch.setattr(desktop_entry.sys, "argv", ["Galgame2Voice.exe", "--no-browser"])
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    mutex = MagicMock()
    mutex.acquire.return_value = False
    monkeypatch.setattr(desktop_instance, "DesktopInstance", lambda root: mutex)
    reopen = MagicMock()
    monkeypatch.setattr(desktop_instance, "open_existing_instance", reopen)
    run = MagicMock()
    monkeypatch.setattr(run_server, "main", run)
    assert desktop_entry.main() == 0
    reopen.assert_called_once_with(tmp_path, no_browser=True)
    run.assert_not_called()
    mutex.close.assert_called_once()


def test_entry_handles_chinese_output_in_ascii_locale(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    output = io.TextIOWrapper(buffer, encoding="ascii")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(desktop_entry, "get_install_root", lambda: tmp_path)
    monkeypatch.setattr(desktop_entry.sys, "argv", ["Galgame2Voice.exe"])
    monkeypatch.setattr(desktop_entry.sys, "stdout", output)
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(run_server, "main", lambda: print("程序已在运行"))
    assert desktop_entry.main() == 0
    output.flush()
    assert buffer.getvalue().decode("utf-8").strip() == "程序已在运行"
    output.detach()


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows named mutexes")
def test_native_mutex_is_scoped_to_install_and_released(tmp_path):
    first = desktop_instance.DesktopInstance(tmp_path)
    second = desktop_instance.DesktopInstance(tmp_path)
    other = desktop_instance.DesktopInstance(tmp_path / "other")
    try:
        assert first.acquire()
        assert not second.acquire()
        assert other.acquire()
        second.close()
        first.close()
        assert second.acquire()
    finally:
        first.close()
        second.close()
        other.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows job objects")
@pytest.mark.requires_process_termination
def test_native_job_close_kills_child_and_descendant(tmp_path, monkeypatch):
    from galgame2voice.utils.windows_runtime import kernel32
    monkeypatch.setattr(run_server, "_WINDOWS_JOB_HANDLE", None)
    handle = run_server.setup_windows_job_object()
    assert handle
    marker = tmp_path / "descendant.pid"
    child_code = (
        "import pathlib, subprocess, sys, time; sys.stdin.readline(); "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"pathlib.Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    child = subprocess.Popen([sys.executable, "-c", child_code], stdin=subprocess.PIPE)
    descendant = None
    try:
        assert run_server.assign_process_to_job(child)
        child.stdin.write(b"ready\n")
        child.stdin.flush()
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        descendant = int(marker.read_text())
        assert psutil.pid_exists(descendant)
        assert kernel32().CloseHandle(handle)
        handle = None
        run_server._WINDOWS_JOB_HANDLE = None
        child.wait(timeout=5)
        deadline = time.monotonic() + 5
        while psutil.pid_exists(descendant) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not psutil.pid_exists(descendant)
    finally:
        if handle:
            kernel32().CloseHandle(handle)
        run_server._WINDOWS_JOB_HANDLE = None
        child.stdin.close()
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        if descendant and psutil.pid_exists(descendant):
            psutil.Process(descendant).kill()
