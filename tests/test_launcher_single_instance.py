"""A second source launch of the same install reuses the running service."""

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts import desktop_instance, run_server


def _health_server(host="127.0.0.1", app="galgame2voice"):
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

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    server = Server((host, 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _write_runtime_files(root, pid, port, host="127.0.0.1"):
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "galgame2voice.pid").write_text(str(pid), encoding="utf-8")
    (root / "data" / "active_port.txt").write_text(str(port), encoding="utf-8")
    desktop_instance.write_instance_address(root, host, port)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_second_launch_reuses_the_running_service_and_keeps_its_files(
    tmp_path, monkeypatch, capsys, host
):
    try:
        server = _health_server(host=host)
    except OSError:
        pytest.skip(f"Local {host} loopback is unavailable")
    port = server.server_address[1]
    other_live_pid = 424242
    _write_runtime_files(tmp_path, other_live_pid, port, host=host)

    env_keys = (
        "GALGAME2VOICE_PORT",
        "PORT",
        "GALGAME_PORT",
        "GALGAME2VOICE_HOST",
        "HOST",
        "GALGAME_HOST",
        "GALGAME2VOICE_PROFILE",
    )
    for key in env_keys:
        if key not in os.environ:
            monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)

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

    first = desktop_instance.DesktopInstance(tmp_path)
    assert first.acquire()
    try:
        with pytest.raises(SystemExit) as exc:
            run_server.main(["--no-browser", "--profile", "--port", str(port)])
    finally:
        first.close()
        server.shutdown()
        server.server_close()

    expected_host = f"[{host}]" if ":" in host else host
    out = capsys.readouterr().out
    assert exc.value.code == 0
    assert f"http://{expected_host}:{port}/" in out
    assert "性能追踪" not in out
    assert all(key not in os.environ for key in env_keys)
    assert (tmp_path / "galgame2voice.pid").read_text(encoding="utf-8") == str(other_live_pid)
    assert (tmp_path / "data" / "active_port.txt").read_text(encoding="utf-8") == str(port)


def test_second_launch_during_startup_window_does_not_start_duplicate(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_server, "_resolve_project_root", lambda: tmp_path)
    monkeypatch.setattr(desktop_instance, "sleep", lambda _delay: None)
    monkeypatch.setattr(desktop_instance, "monotonic", iter([0.0, 0.0, 9.0]).__next__)
    monkeypatch.setattr(
        run_server,
        "sync_runtime_address",
        lambda **kwargs: pytest.fail("must not run"),
    )

    first = desktop_instance.DesktopInstance(tmp_path)
    assert first.acquire()
    try:
        with pytest.raises(SystemExit) as exc:
            run_server.main(["--no-browser", "--port", "8080"])
    finally:
        first.close()

    assert exc.value.code == 0
    assert "正在准备网页" in capsys.readouterr().out
    assert first.acquire()
    first.close()


def test_open_existing_instance_ignores_non_http_service(tmp_path, monkeypatch, capsys):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve_non_http():
        conn, _ = listener.accept()
        with conn:
            conn.recv(1024)
            conn.sendall(b"NOT_HTTP\r\n")

    worker = threading.Thread(target=serve_non_http, daemon=True)
    worker.start()
    desktop_instance.write_instance_address(tmp_path, "127.0.0.1", port)
    monkeypatch.setattr(desktop_instance, "sleep", lambda _delay: None)
    monkeypatch.setattr(desktop_instance, "monotonic", iter([0.0, 0.0, 9.0]).__next__)
    try:
        desktop_instance.open_existing_instance(tmp_path, no_browser=True)
    finally:
        worker.join(timeout=2)
        listener.close()

    assert "正在准备网页" in capsys.readouterr().out
