"""Portable instance ownership and real Windows job shutdown regressions."""

import json
import io
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import psutil
import pytest

from scripts import desktop_entry, desktop_instance, run_server
from scripts.run_server import is_port_in_use

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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


class TestProcessTerminationAndPortReleaseWindows:
    """Verifies clean Windows process reclamation and socket re-binding."""

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="Windows Job Object process reclamation and WSAEADDRINUSE-free port rebinding are Windows-specific",
    )
    def test_subprocess_server_spawn_terminate_and_port_rebind(self):
        """
        Spawn uvicorn in a subprocess on an ephemeral port, verify health response,
        terminate the process, and verify the port is immediately re-bindable without WSAEADDRINUSE.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            test_port = reservation.getsockname()[1]
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "galgame2voice.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(test_port),
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            # Poll /api/health until responsive
            ready = False
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 30.0:
                time.sleep(0.5)
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{test_port}/api/health", timeout=1.0) as resp:
                        if resp.status == 200:
                            ready = True
                            break
                except Exception:
                    pass

            assert ready, f"Uvicorn server failed to respond on port {test_port} within 30s"
            assert is_port_in_use(test_port), f"Port {test_port} must be marked in use while running"

        finally:
            # Terminate server
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)

        # Verify process is dead
        assert proc.poll() is not None, "Process failed to terminate"

        # Verify the port is reclaimed by the kernel and re-bindable.
        # On Windows, socket handle cleanup after TerminateProcess is asynchronous,
        # so retry within a bounded window instead of demanding instantaneous rebind.
        deadline = time.perf_counter() + 10.0
        last_error: OSError | None = None
        while time.perf_counter() < deadline:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                # Bind without SO_REUSEADDR to strictly verify kernel port reclamation
                test_sock.bind(("127.0.0.1", test_port))
                test_sock.listen(1)
                last_error = None
                break
            except OSError as e:
                last_error = e
                time.sleep(0.25)
            finally:
                test_sock.close()

        if last_error is not None:
            pytest.fail(
                f"Port {test_port} was not released within 10s of process exit: {last_error}"
            )


class TestProcessTreeTermination:
    """Empirically test process tree killing (/F /T /PID) on Windows."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill testing requires Windows OS")
    @pytest.mark.requires_process_termination
    def test_nested_process_tree_termination(self, tmp_path):
        """
        Empirically test that `taskkill /f /t /pid <root_pid>` kills the entire
        hierarchy (Parent -> Child -> Grandchild) and releases all bound ports.
        """
        ready_path = tmp_path / "owned-tree-ready.json"
        child_code = f"""
import json, subprocess, time, sys, socket
from pathlib import Path
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(('127.0.0.1', 0))
listener.listen(1)
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
ready = Path({str(ready_path)!r})
temporary = ready.with_suffix('.tmp')
temporary.write_text(json.dumps({{'ports': [int(sys.argv[1]), listener.getsockname()[1]],
                                'grandchild': grandchild.pid}}), encoding='utf-8')
temporary.replace(ready)
while True:
    time.sleep(0.5)
"""
        worker_code = f"""
import subprocess, time, sys, socket
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(('127.0.0.1', 0))
listener.listen(1)
child = subprocess.Popen([sys.executable, '-c', {child_code!r}, str(listener.getsockname()[1])])
while True:
    time.sleep(0.5)
"""
        parent = subprocess.Popen([sys.executable, "-c", worker_code],
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        root = psutil.Process(parent.pid)
        root_created = root.create_time()
        owned = {(root.pid, root_created)}

        def running(identity):
            try:
                process = psutil.Process(identity[0])
                return process.create_time() == identity[1] and process.is_running()
            except psutil.NoSuchProcess:
                return False

        def is_port_listening(port):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                return s.connect_ex(("127.0.0.1", port)) == 0

        try:
            deadline = time.monotonic() + 10
            while not ready_path.is_file() and time.monotonic() < deadline:
                assert parent.poll() is None, "The owned parent exited before readiness"
                time.sleep(0.05)
            assert ready_path.is_file(), "The owned process tree did not report readiness"
            ready = json.loads(ready_path.read_text(encoding="utf-8"))
            descendants = root.children(recursive=True)
            owned.update((process.pid, process.create_time()) for process in descendants)
            assert ready["grandchild"] in {pid for pid, _ in owned}
            assert all(is_port_listening(port) for port in ready["ports"])
            result = subprocess.run(["taskkill", "/f", "/t", "/pid", str(parent.pid)],
                                    capture_output=True, text=True, timeout=10)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and any(running(identity) for identity in owned):
                time.sleep(0.05)
            # Windows can report "no running instance" for an already-exited
            # descendant and return 255 despite cleaning the complete tree.
            # Accept that only if every captured process and listener is gone.
            survivors = [identity for identity in owned if running(identity)]
            assert not survivors, f"Owned processes survived taskkill: {survivors}; {result.stderr}"
            assert not any(is_port_listening(port) for port in ready["ports"]), result.stderr
            parent.wait(timeout=5)
        finally:
            if running((root.pid, root_created)):
                owned.update((process.pid, process.create_time()) for process in root.children(recursive=True))
            # Clean only this test's process identities, even after setup/assertion failure.
            for identity in owned:
                if running(identity):
                    try:
                        psutil.Process(identity[0]).kill()
                    except psutil.NoSuchProcess:
                        pass
            try:
                parent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                parent.kill()
                parent.wait(timeout=5)
