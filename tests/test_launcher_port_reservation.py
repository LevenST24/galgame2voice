"""The launcher must retain its chosen port until the server takes ownership."""

import os
import shutil
import socket
from unittest.mock import MagicMock

import pytest
import uvicorn

from scripts import desktop_entry, run_server


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
    for key in (
        "GALGAME2VOICE_PORT", "PORT", "GALGAME_PORT",
        "GALGAME2VOICE_HOST", "HOST", "GALGAME_HOST",
    ):
        if key not in os.environ:
            monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
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
    for key in (
        "GALGAME2VOICE_PORT", "PORT", "GALGAME_PORT",
        "GALGAME2VOICE_HOST", "HOST", "GALGAME_HOST",
    ):
        if key not in os.environ:
            monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
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


def test_low_disk_hint(tmp_path, monkeypatch):
    for free, hit in ((10 * 1024 * 1024, True), (10 * 1024**3, False)):
        monkeypatch.setattr(shutil, "disk_usage", lambda _p, f=free: MagicMock(free=f))
        assert bool(hint := run_server.low_disk_hint(tmp_path)) is hit
        assert not hit or ("磁盘" in hint and "清理" in hint and "10 MB" in hint)
    monkeypatch.setattr(shutil, "disk_usage", MagicMock(side_effect=OSError("no disk")))
    assert run_server.low_disk_hint(tmp_path) is None


def test_full_disk_startup_reports_disk_space(tmp_path, monkeypatch, capsys, request):
    request.addfinalizer(run_server.get_settings.cache_clear)
    for key in ("GALGAME2VOICE_PORT", "PORT", "GALGAME_PORT", "GALGAME2VOICE_HOST", "HOST", "GALGAME_HOST"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: MagicMock(free=10 * 1024 * 1024))
    for name, fn in (("check_python_environment", lambda: True), ("run_hardware_diagnostics", dict),
                     ("setup_windows_job_object", lambda: None), ("setup_signal_handlers", lambda: None),
                     ("setup_console_close_handler", lambda: None), ("disable_console_quick_edit", lambda: None),
                     ("cleanup_subprocesses", lambda: None), ("atexit", MagicMock())):
        monkeypatch.setattr(run_server, name, fn)
    monkeypatch.setattr(run_server.sys, "frozen", True, raising=False)
    monkeypatch.setattr(run_server.desktop_instance, "write_instance_address", MagicMock(side_effect=OSError(28, "full")))
    srv = MagicMock(return_value=MagicMock(started=False))
    monkeypatch.setattr(uvicorn, "Config", MagicMock())
    monkeypatch.setattr(uvicorn, "Server", srv)
    with pytest.raises(SystemExit) as exc:
        run_server.main(["--no-engine", "--no-browser", "--host", "127.0.0.1", "--port", "0"])
    out = capsys.readouterr().out
    assert exc.value.code == 3 and srv.return_value.run.called and "[启动失败]" in out and "磁盘" in out and "清理" in out
    assert "数据库损坏" not in out and "数据恢复.bat" not in out
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(desktop_entry, "get_install_root", lambda: tmp_path)
    monkeypatch.setattr(desktop_entry.sys, "frozen", False, raising=False)
    monkeypatch.setattr(desktop_entry.sys, "argv", ["Galgame2Voice.exe"])
    monkeypatch.setattr(run_server, "main", MagicMock(side_effect=OSError(28, "No space left")))
    assert desktop_entry.main() == 1
    assert "磁盘" in (e := capsys.readouterr().out) and "清理" in e and "解压到有写入权限的目录" not in e
