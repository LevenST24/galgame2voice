"""A second source launch of the same install reuses the running service."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts import run_server


def _health_server(app="galgame2voice"):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server naming
            body = json.dumps({"status": "ok", "app": app}).encode()
            self.send_response(200 if self.path == "/api/health" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def other_live_pid():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _write_runtime_files(root, pid, port):
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "galgame2voice.pid").write_text(str(pid), encoding="utf-8")
    (root / "data" / "active_port.txt").write_text(str(port), encoding="utf-8")


def test_finds_a_live_instance_that_answers_health(tmp_path, other_live_pid):
    server = _health_server()
    try:
        _write_runtime_files(tmp_path, other_live_pid, server.server_address[1])
        assert run_server.find_running_instance(tmp_path) == f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("case", ["own_pid", "other_app", "nothing_listening", "no_files"])
def test_does_not_trust_stale_or_foreign_runtime_files(tmp_path, other_live_pid, case):
    server = _health_server(app="something-else" if case == "other_app" else "galgame2voice")
    port = server.server_address[1]
    if case == "nothing_listening":
        server.shutdown()
        server.server_close()
    try:
        if case != "no_files":
            _write_runtime_files(tmp_path, os.getpid() if case == "own_pid" else other_live_pid, port)
        assert run_server.find_running_instance(tmp_path) is None
    finally:
        if case != "nothing_listening":
            server.shutdown()
            server.server_close()


def test_second_launch_reuses_the_running_service_and_keeps_its_files(
    tmp_path, other_live_pid, monkeypatch, capsys
):
    server = _health_server()
    port = server.server_address[1]
    _write_runtime_files(tmp_path, other_live_pid, port)
    monkeypatch.setattr(run_server, "_resolve_project_root", lambda: tmp_path)
    monkeypatch.setattr(run_server, "check_python_environment", lambda: True)
    monkeypatch.setattr(run_server, "run_hardware_diagnostics", lambda: {})
    monkeypatch.setattr(run_server, "setup_windows_job_object", lambda: None)
    monkeypatch.setattr(run_server, "setup_signal_handlers", lambda: None)
    monkeypatch.setattr(run_server.atexit, "register", lambda *args: None)

    def must_not_start(*args, **kwargs):
        raise AssertionError("a second server must not start")

    monkeypatch.setattr(run_server, "ensure_gpt_sovits_running", must_not_start)
    monkeypatch.setattr(run_server, "reserve_available_socket", must_not_start)
    try:
        with pytest.raises(SystemExit) as exc:
            run_server.main(["--no-browser", "--port", str(port)])
    finally:
        server.shutdown()
        server.server_close()

    assert exc.value.code == 0
    assert f"http://127.0.0.1:{port}/" in capsys.readouterr().out
    assert (tmp_path / "galgame2voice.pid").read_text(encoding="utf-8") == str(other_live_pid)
    assert (tmp_path / "data" / "active_port.txt").read_text(encoding="utf-8") == str(port)
