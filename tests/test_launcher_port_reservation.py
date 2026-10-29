"""The launcher must retain its chosen port until the server takes ownership."""

import os
import socket
from unittest.mock import MagicMock

import pytest
import uvicorn

from scripts import run_server


def test_chosen_port_cannot_be_taken_before_uvicorn_starts(tmp_path, monkeypatch):
    for name in ("data", "logs", "audio"):
        (tmp_path / name).mkdir(exist_ok=True)
    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(run_server, "check_python_environment", lambda: True)
    monkeypatch.setattr(run_server, "run_hardware_diagnostics", lambda: {})
    monkeypatch.setattr(run_server, "setup_windows_job_object", lambda: None)
    monkeypatch.setattr(run_server, "setup_signal_handlers", lambda: None)
    monkeypatch.setattr(run_server, "cleanup_subprocesses", lambda: None)
    monkeypatch.setattr(run_server.atexit, "register", lambda *args: None)
    for key in ("GALGAME_PORT", "PORT", "GALGAME_HOST", "HOST"):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    checked = []

    def assert_reserved():
        port = int(os.environ["GALGAME_PORT"])
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as contender:
            contender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                contender.bind(("127.0.0.1", port))
        checked.append(port)

    # Old and new Uvicorn entry points are stubbed: this test never runs a
    # service and only attempts to bind the launcher's own ephemeral port.
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: assert_reserved())
    monkeypatch.setattr(uvicorn, "Config", MagicMock())
    server = MagicMock(started=True)
    server.run.side_effect = lambda **kwargs: assert_reserved()
    monkeypatch.setattr(uvicorn, "Server", lambda config: server)
    with pytest.raises(SystemExit) as exc:
        run_server.main(["--no-engine", "--no-browser", "--host", "127.0.0.1", "--port", "0"])
    assert exc.value.code == 0
    assert len(checked) == 1
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as released:
        released.bind(("127.0.0.1", checked[0]))


@pytest.mark.parametrize("raises", [True, False])
def test_port_is_released_if_server_startup_fails(tmp_path, monkeypatch, raises):
    for name in ("data", "logs", "audio"):
        (tmp_path / name).mkdir(exist_ok=True)
    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(run_server, "check_python_environment", lambda: True)
    monkeypatch.setattr(run_server, "run_hardware_diagnostics", lambda: {})
    monkeypatch.setattr(run_server, "setup_windows_job_object", lambda: None)
    monkeypatch.setattr(run_server, "setup_signal_handlers", lambda: None)
    monkeypatch.setattr(run_server, "cleanup_subprocesses", lambda: None)
    monkeypatch.setattr(run_server.atexit, "register", lambda *args: None)
    for key in ("GALGAME_PORT", "PORT", "GALGAME_HOST", "HOST"):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    ports = []

    def fail_startup(*, sockets):
        ports.append(sockets[0].getsockname()[1])
        if raises:
            raise SystemExit(3)

    monkeypatch.setattr(uvicorn, "Config", MagicMock())
    server = MagicMock(started=False)
    server.run.side_effect = fail_startup
    monkeypatch.setattr(uvicorn, "Server", lambda config: server)
    with pytest.raises(SystemExit) as exc:
        run_server.main(["--no-engine", "--no-browser", "--host", "127.0.0.1", "--port", "0"])
    assert exc.value.code == 3
    assert len(ports) == 1
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as released:
        released.bind(("127.0.0.1", ports[0]))
