"""Probe launcher addresses without contacting an existing app or model."""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts import desktop_instance, run_server


@pytest.mark.parametrize("host,family", [("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)])
def test_dynamic_launcher_port_is_bindable_on_requested_address(host, family):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((host, 0))
    except OSError:
        pytest.skip(f"Local {host} binding is unavailable")
    port = run_server.find_available_port(0, host=host)
    assert 1 <= port <= 65535
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.bind((host, port))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows SO_REUSEADDR semantics")
@pytest.mark.parametrize("host,family", [("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)])
def test_windows_port_probe_does_not_reuse_an_existing_listener(monkeypatch, host, family):
    owner = None
    try:
        owner = socket.socket(family, socket.SOCK_STREAM)
        owner.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        owner.bind((host, 0))
        owner.listen()
    except OSError:
        if owner is not None:
            owner.close()
        pytest.skip(f"Local {host} binding is unavailable")
    port = owner.getsockname()[1]
    real_socket = socket.socket

    class RestrictedProbe:
        def __init__(self, *args):
            self.inner = real_socket(*args)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.inner.close()

        def setsockopt(self, *args):
            return self.inner.setsockopt(*args)

        def close(self):
            self.inner.close()

        def listen(self, backlog):
            self.inner.listen(backlog)

        def bind(self, address):
            # Never probe the user's other fixed ports in this test.
            if address[1] not in (0, port):
                raise OSError("Synthetic occupied fallback")
            return self.inner.bind(address)

        def getsockname(self):
            return self.inner.getsockname()

    monkeypatch.setattr(run_server, "socket", SimpleNamespace(
        AF_INET=socket.AF_INET, AF_INET6=socket.AF_INET6, SOCK_STREAM=socket.SOCK_STREAM,
        SOL_SOCKET=socket.SOL_SOCKET, SO_REUSEADDR=socket.SO_REUSEADDR,
        SO_EXCLUSIVEADDRUSE=socket.SO_EXCLUSIVEADDRUSE, socket=RestrictedProbe,
    ))
    try:
        selected = run_server.find_available_port(port, host=host)
        assert selected != port
        with socket.create_connection((host, port), timeout=1):
            pass
    finally:
        owner.close()


@pytest.mark.parametrize("host,probe_host,url_host", [
    ("127.0.0.1", "127.0.0.1", "127.0.0.1"),
    ("0.0.0.0", "127.0.0.1", "127.0.0.1"),
    ("::1", "::1", "[::1]"),
    ("::", "::1", "[::1]"),
])
def test_browser_probe_and_url_match_bind_address(monkeypatch, host, probe_host, url_host):
    connection = MagicMock()
    connection.getresponse.return_value.status = 200
    connection.getresponse.return_value.read.return_value = b'{"app":"galgame2voice","status":"ok"}'
    connect = MagicMock(return_value=connection)
    browser = MagicMock()
    monkeypatch.setattr(http.client, "HTTPConnection", connect)
    monkeypatch.setattr(run_server.webbrowser, "open", browser)
    monkeypatch.setattr(run_server.time, "sleep", lambda _: None)

    class SynchronousThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(run_server.threading, "Thread", SynchronousThread)
    run_server.auto_open_browser(18080, host=host)
    connect.assert_called_once_with(probe_host, 18080, timeout=0.2)
    browser.assert_called_once_with(f"http://{url_host}:18080/")


def test_existing_ipv6_wildcard_instance_uses_ipv6_loopback(tmp_path, monkeypatch):
    desktop_instance.write_instance_address(tmp_path, "::", 18080)
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = b'{"app":"galgame2voice","status":"ok"}'
    monkeypatch.setattr(desktop_instance.urllib.request, "build_opener", lambda *args: opener)
    browser = MagicMock()
    monkeypatch.setattr(desktop_instance.webbrowser, "open", browser)
    desktop_instance.open_existing_instance(tmp_path, no_browser=False)
    opener.open.assert_called_once_with("http://[::1]:18080/api/health", timeout=0.5)
    browser.assert_called_once_with("http://[::1]:18080/")


@pytest.mark.parametrize("bind_host", ["::1", "::"])
def test_browser_waiter_connects_to_a_real_ipv6_only_loopback_server(monkeypatch, bind_host):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append((self.path, self.headers.get("Host")))
            body = b'{"app":"galgame2voice","status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    try:
        server = Server(("::1", 0), Handler)
    except OSError:
        pytest.skip("Local IPv6 loopback is unavailable")
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    opened = threading.Event()
    urls = []

    def open_browser(url):
        urls.append(url)
        opened.set()

    monkeypatch.setattr(run_server.webbrowser, "open", open_browser)
    try:
        run_server.auto_open_browser(server.server_port, host=bind_host)
        assert opened.wait(timeout=5), "Browser was not opened after the IPv6 health response"
        assert urls == [f"http://[::1]:{server.server_port}/"]
        assert observed == [("/api/health", f"[::1]:{server.server_port}")]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.requires_piped_subprocess
@pytest.mark.requires_process_termination
@pytest.mark.parametrize("host,family", [("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)])
def test_source_launcher_serves_with_automatic_port(tmp_path, host, family):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((host, 0))
    except OSError:
        pytest.skip(f"Local {host} loopback is unavailable")
    for name in ("data", "audio", "logs"):
        (tmp_path / name).mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update(GALGAME2VOICE_PROJECT_ROOT=str(tmp_path), GALGAME2VOICE_AUTH_DISABLED="1",
               DATA_DIR_NAME=str(tmp_path / "data"), AUDIO_DIR_NAME=str(tmp_path / "audio"),
               LOGS_DIR_NAME=str(tmp_path / "logs"), GALGAME2VOICE_RATE_LIMIT_DISABLED="1")
    command = ("import sys; from pathlib import Path; import scripts.run_server as launcher; "
               "launcher.PROJECT_ROOT=Path(sys.argv[1]); "
               "launcher.main(['--host',sys.argv[2],'--port','0','--no-engine','--no-browser'])")
    log_path = tmp_path / "launcher.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-c", command, str(tmp_path), host], env=env,
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            port_file = tmp_path / "data" / "active_port.txt"
            response_data = None
            while time.monotonic() < deadline and process.poll() is None:
                if port_file.is_file():
                    port = int(port_file.read_text(encoding="utf-8"))
                    assert port > 0
                    try:
                        connection = http.client.HTTPConnection(host, port, timeout=0.5)
                        try:
                            connection.request("GET", "/api/health")
                            response = connection.getresponse()
                            if response.status == 200:
                                response_data = json.loads(response.read())
                                break
                        finally:
                            connection.close()
                    except OSError:
                        pass
                time.sleep(0.1)
            assert response_data and response_data.get("app") == "galgame2voice", log_path.read_text(encoding="utf-8")
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
