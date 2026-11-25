"""The launcher only calls a busy engine port a running GPT-SoVITS when GPT-SoVITS answers there."""

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from galgame2voice.services.sovits_endpoint import parse_sovits_endpoint
from scripts import run_server


def _serve(status_for_control: int, *, redirect_to: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server naming
            is_control = self.path.endswith("/control")
            self.send_response(status_for_control if is_control else (200 if redirect_to else 404))
            if is_control and redirect_to:
                self.send_header("Location", redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize(("status", "expected"), [(400, "engine"), (200, "engine"), (404, "other")])
def test_probe_tells_engine_from_other_http_servers(status, expected):
    server = _serve(status)
    try:
        port = server.server_address[1]
        assert run_server._probe_engine_identity(f"http://127.0.0.1:{port}") == expected
    finally:
        server.shutdown()
        server.server_close()


def test_probe_reports_a_silent_listener_as_unknown():
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(4)
    try:
        port = silent.getsockname()[1]
        assert run_server._probe_engine_identity(f"http://127.0.0.1:{port}", timeout=0.3) == "unknown"
    finally:
        silent.close()


def test_probe_reports_a_non_http_listener_as_other():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    def answer_garbage():
        conn, _ = listener.accept()
        conn.recv(1024)
        conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
        conn.close()

    worker = threading.Thread(target=answer_garbage, daemon=True)
    worker.start()
    try:
        assert run_server._probe_engine_identity(f"http://127.0.0.1:{port}") == "other"
    finally:
        worker.join(timeout=5)
        listener.close()


def test_launcher_does_not_call_an_unrelated_busy_port_a_running_engine(monkeypatch, capsys):
    server = _serve(404)
    port = server.server_address[1]
    endpoint = parse_sovits_endpoint(f"http://127.0.0.1:{port}", source="env")
    monkeypatch.setattr(
        "galgame2voice.services.sovits_endpoint.resolve_effective_sovits_endpoint_sync", lambda: endpoint
    )
    spawn = []
    monkeypatch.setattr(run_server, "find_gpt_sovits_directory", lambda: spawn.append(1))
    try:
        run_server.ensure_gpt_sovits_running()
    finally:
        server.shutdown()
        server.server_close()
    out = capsys.readouterr().out
    assert "语音引擎已在运行" not in out
    assert f"端口 {port} 已被别的程序占用" in out
    assert not spawn


def test_probe_does_not_follow_redirects_to_200():
    server = _serve(302, redirect_to="/login")
    try:
        assert run_server._probe_engine_identity(f"http://127.0.0.1:{server.server_address[1]}") == "other"
    finally:
        server.shutdown()
        server.server_close()


def test_launcher_probes_non_local_endpoint_without_http_proxy(monkeypatch, capsys):
    engine, proxy = _serve(400), _serve(502)
    endpoint = parse_sovits_endpoint(f"http://127.0.0.1:{engine.server_address[1]}/sovits", source="db")
    assert not endpoint.is_local
    for name in ("HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_address[1]}")
    monkeypatch.setattr(
        "galgame2voice.services.sovits_endpoint.resolve_effective_sovits_endpoint_sync", lambda: endpoint
    )
    try:
        run_server.ensure_gpt_sovits_running()
    finally:
        for srv in (engine, proxy):
            srv.shutdown()
            srv.server_close()
    out = capsys.readouterr().out
    assert "语音引擎已在运行" in out and "已被别的程序占用" not in out
