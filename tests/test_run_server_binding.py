"""Tests for launcher host and port binding consistency and safety checks.

Ensures that CLI-specified host and port are strictly synchronized to all
environment variable aliases and Pydantic Settings cache, preventing
unauthenticated public network exposure or binding mismatch.
"""

import os
from unittest.mock import MagicMock
import pytest
import uvicorn

from galgame2voice.config import get_settings
from galgame2voice.main import _init_logging_and_safety
from scripts import run_server
import scripts.run_server as rs


@pytest.fixture
def isolated_launcher_env(tmp_path, monkeypatch):
    """Sets up an isolated environment for run_server tests without real network/services."""
    for name in ("data", "logs", "audio"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(run_server, "check_python_environment", lambda: True)
    monkeypatch.setattr(run_server, "run_hardware_diagnostics", lambda: {})
    monkeypatch.setattr(run_server, "setup_windows_job_object", lambda: None)
    monkeypatch.setattr(run_server, "setup_signal_handlers", lambda: None)
    monkeypatch.setattr(run_server, "cleanup_subprocesses", lambda: None)
    monkeypatch.setattr(run_server, "auto_open_browser", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_server.atexit, "register", lambda *args: None)

    # Clean and isolate environment aliases with complete monkeypatch rollback registration.
    # If a key does not exist initially, setenv("") first so monkeypatch records NOTSET,
    # ensuring direct os.environ writes in run_server are fully rolled back upon undo.
    env_keys = (
        "GALGAME2VOICE_HOST",
        "HOST",
        "GALGAME_HOST",
        "GALGAME2VOICE_PORT",
        "PORT",
        "GALGAME_PORT",
        "GALGAME2VOICE_AUTH_DISABLED",
        "AUTH_DISABLED",
        "GALGAME2VOICE_PROJECT_ROOT",
        "PROJECT_ROOT",
    )
    for key in env_keys:
        if key not in os.environ:
            monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    try:
        yield tmp_path
    finally:
        import logging
        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            if isinstance(handler, logging.FileHandler):
                handler.close()
                root_logger.removeHandler(handler)
        get_settings.cache_clear()


def _setup_mock_uvicorn(monkeypatch, sockname):
    """Mock socket reservation and uvicorn.Server.run to capture settings and lifespan safety."""
    mock_socket = MagicMock()
    mock_socket.getsockname.return_value = sockname
    monkeypatch.setattr(run_server, "reserve_available_socket", lambda port, host=None: mock_socket)

    captured = {}

    def simulate_uvicorn_run(*args, **kwargs):
        settings = get_settings()
        captured["settings"] = settings
        try:
            _init_logging_and_safety(settings)
        except Exception as exc:
            captured["safety_error"] = exc
            raise

    server = MagicMock(started=True)
    server.run.side_effect = simulate_uvicorn_run
    monkeypatch.setattr(uvicorn, "Config", MagicMock())
    monkeypatch.setattr(uvicorn, "Server", lambda config: server)
    return captured


def test_public_bind_without_auth_fails_safety_guard_even_with_early_cache_warmup(
    isolated_launcher_env, monkeypatch
):
    """Early get_settings() warmup must not cause 0.0.0.0 without auth to bypass security check."""
    # Step 1: Simulate early cache warmup (e.g. from early import or ensure_gpt_sovits_running)
    early_settings = get_settings()
    assert early_settings.host == "127.0.0.1"

    captured = _setup_mock_uvicorn(monkeypatch, ("0.0.0.0", 8080))

    # Step 2: Launch with --host 0.0.0.0 and default auth (auth_disabled=True)
    # The safety guard must reject the startup with exit code 1 and a fatal error.
    with pytest.raises(SystemExit) as exc:
        run_server.main(["--no-engine", "--no-browser", "--host", "0.0.0.0", "--port", "8080"])

    assert exc.value.code == 1, "Expected fatal security rejection exit code 1"
    assert "safety_error" in captured, "Expected _init_logging_and_safety to raise error"
    assert "FATAL: Application is configured to listen on '0.0.0.0' with auth_disabled=True" in str(
        captured["safety_error"]
    )
    assert captured["settings"].host == "0.0.0.0"


def test_cli_host_overrides_stale_high_priority_env_alias(
    isolated_launcher_env, monkeypatch
):
    """CLI --host 0.0.0.0 must override high-priority GALGAME2VOICE_HOST=127.0.0.1 and sync to Settings."""
    monkeypatch.setenv("GALGAME2VOICE_HOST", "127.0.0.1")
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("GALGAME_HOST", "127.0.0.1")
    # Enable auth so the non-loopback bind is legally allowed
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "0")

    captured = _setup_mock_uvicorn(monkeypatch, ("0.0.0.0", 8080))

    with pytest.raises(SystemExit) as exc:
        run_server.main(["--no-engine", "--no-browser", "--host", "0.0.0.0", "--port", "8080"])

    assert exc.value.code == 0
    assert captured["settings"].host == "0.0.0.0"
    assert os.environ["GALGAME2VOICE_HOST"] == "0.0.0.0"
    assert os.environ["HOST"] == "0.0.0.0"
    assert os.environ["GALGAME_HOST"] == "0.0.0.0"


def test_loopback_default_bind_passes_safety_check(
    isolated_launcher_env, monkeypatch
):
    """Default loopback host (127.0.0.1) with auth_disabled passes safety checks cleanly."""
    captured = _setup_mock_uvicorn(monkeypatch, ("127.0.0.1", 8080))

    with pytest.raises(SystemExit) as exc:
        run_server.main(["--no-engine", "--no-browser", "--host", "127.0.0.1", "--port", "8080"])

    assert exc.value.code == 0
    assert captured["settings"].host == "127.0.0.1"


def test_fallback_port_is_reflected_in_settings(
    isolated_launcher_env, monkeypatch
):
    """When preferred port is occupied, the resolved fallback port is visible in Settings."""
    captured = _setup_mock_uvicorn(monkeypatch, ("127.0.0.1", 8081))

    with pytest.raises(SystemExit) as exc:
        run_server.main(["--no-engine", "--no-browser", "--host", "127.0.0.1", "--port", "8080"])

    assert exc.value.code == 0
    assert captured["settings"].port == 8081
    assert os.environ["GALGAME2VOICE_PORT"] == "8081"
    assert os.environ["PORT"] == "8081"
    assert os.environ["GALGAME_PORT"] == "8081"


class TestServerLauncherCLI:
    @pytest.fixture(autouse=True)
    def _isolate_launcher_cli_env(self, monkeypatch):
        for key in (
            "GALGAME2VOICE_PORT", "PORT", "GALGAME_PORT",
            "GALGAME2VOICE_HOST", "HOST", "GALGAME_HOST",
            "GALGAME_NO_BROWSER",
        ):
            if key not in os.environ:
                monkeypatch.setenv(key, "")
            monkeypatch.delenv(key)

    def test_parse_args_defaults(self):
        args = rs.parse_args([])
        assert args.host == "127.0.0.1"
        assert args.port == 8080
        assert args.no_browser is False
        assert args.check_only is False

    def test_parse_args_custom_values(self):
        args = rs.parse_args(["--host", "0.0.0.0", "--port", "9090", "--no-browser", "--check-only"])
        assert args.host == "0.0.0.0"
        assert args.port == 9090
        assert args.no_browser is True
        assert args.check_only is True

    def test_parse_args_env_overrides(self, monkeypatch):
        monkeypatch.setenv("GALGAME_PORT", "9999")
        monkeypatch.setenv("GALGAME_HOST", "0.0.0.0")
        monkeypatch.setenv("GALGAME_NO_BROWSER", "1")
        args = rs.parse_args([])
        assert args.port == 9999
        assert args.host == "0.0.0.0"
        assert args.no_browser is True

    def test_parse_args_reads_host_and_port_from_dotenv(self, tmp_path, monkeypatch):
        # .env.example documents GALGAME2VOICE_HOST/PORT; Settings reads them from
        # .env, so the launcher must bind there too instead of forcing 8080.
        (tmp_path / ".env").write_text(
            "galgame2voice_port=9123\nGALGAME2VOICE_HOST=127.0.0.2\n", encoding="utf-8",
        )
        # Both the working-directory and program-folder lookups point at tmp_path.
        import galgame2voice.config as config_module
        monkeypatch.setattr(config_module, "get_install_root", lambda: tmp_path)
        monkeypatch.chdir(tmp_path)
        args = rs.parse_args([])
        assert args.port == 9123
        assert args.host == "127.0.0.2"

        # A real environment variable still outranks the file, as in Settings.
        monkeypatch.setenv("PORT", "9200")
        assert rs.parse_args([]).port == 9200

    def test_parse_args_ignores_unusable_dotenv_port(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text("GALGAME2VOICE_PORT=abc\n", encoding="utf-8")
        import galgame2voice.config as config_module
        monkeypatch.setattr(config_module, "get_install_root", lambda: tmp_path)
        monkeypatch.chdir(tmp_path)
        assert rs.parse_args([]).port == 8080

    def test_parse_args_env_alias_precedence(self, monkeypatch):
        # GALGAME2VOICE_PORT takes precedence over PORT and GALGAME_PORT
        monkeypatch.setenv("GALGAME_PORT", "9999")
        monkeypatch.setenv("PORT", "8888")
        monkeypatch.setenv("GALGAME2VOICE_PORT", "7777")
        args = rs.parse_args([])
        assert args.port == 7777

        # PORT takes precedence over GALGAME_PORT when GALGAME2VOICE_PORT is unset
        monkeypatch.delenv("GALGAME2VOICE_PORT")
        args = rs.parse_args([])
        assert args.port == 8888

        # GALGAME2VOICE_HOST takes precedence over HOST and GALGAME_HOST
        monkeypatch.setenv("GALGAME_HOST", "192.168.1.1")
        monkeypatch.setenv("HOST", "10.0.0.1")
        monkeypatch.setenv("GALGAME2VOICE_HOST", "172.16.0.1")
        args = rs.parse_args([])
        assert args.host == "172.16.0.1"

        # HOST takes precedence over GALGAME_HOST when GALGAME2VOICE_HOST is unset
        monkeypatch.delenv("GALGAME2VOICE_HOST")
        args = rs.parse_args([])
        assert args.host == "10.0.0.1"

    def test_main_check_only_success(self, monkeypatch):
        monkeypatch.setattr(rs, "check_python_environment", lambda: True)
        monkeypatch.setattr(rs, "run_hardware_diagnostics", lambda: {"has_nvidia": False})

        with pytest.raises(SystemExit) as exc_info:
            rs.main(["--check-only"])
        assert exc_info.value.code == 0

    def test_main_check_only_env_failure(self, monkeypatch):
        monkeypatch.setattr(rs, "check_python_environment", lambda: False)

        with pytest.raises(SystemExit) as exc_info:
            rs.main(["--check-only"])
        assert exc_info.value.code == 1
