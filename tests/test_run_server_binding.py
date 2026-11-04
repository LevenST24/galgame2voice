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


def test_public_bind_without_auth_fails_safety_guard_even_with_early_cache_warmup(
    isolated_launcher_env, monkeypatch
):
    """Early get_settings() warmup must not cause 0.0.0.0 without auth to bypass security check."""
    # Step 1: Simulate early cache warmup (e.g. from early import or ensure_gpt_sovits_running)
    early_settings = get_settings()
    assert early_settings.host == "127.0.0.1"

    # Mock socket reservation so we never bind real 0.0.0.0
    mock_socket = MagicMock()
    mock_socket.getsockname.return_value = ("0.0.0.0", 8080)
    monkeypatch.setattr(run_server, "reserve_available_socket", lambda port, host=None: mock_socket)

    # Intercept uvicorn.Server.run to execute actual lifespan safety guard
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

    mock_socket = MagicMock()
    mock_socket.getsockname.return_value = ("0.0.0.0", 8080)
    monkeypatch.setattr(run_server, "reserve_available_socket", lambda port, host=None: mock_socket)

    captured = {}

    def simulate_uvicorn_run(*args, **kwargs):
        settings = get_settings()
        captured["settings"] = settings
        _init_logging_and_safety(settings)

    server = MagicMock(started=True)
    server.run.side_effect = simulate_uvicorn_run
    monkeypatch.setattr(uvicorn, "Config", MagicMock())
    monkeypatch.setattr(uvicorn, "Server", lambda config: server)

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
    mock_socket = MagicMock()
    mock_socket.getsockname.return_value = ("127.0.0.1", 8080)
    monkeypatch.setattr(run_server, "reserve_available_socket", lambda port, host=None: mock_socket)

    captured = {}

    def simulate_uvicorn_run(*args, **kwargs):
        settings = get_settings()
        captured["settings"] = settings
        _init_logging_and_safety(settings)

    server = MagicMock(started=True)
    server.run.side_effect = simulate_uvicorn_run
    monkeypatch.setattr(uvicorn, "Config", MagicMock())
    monkeypatch.setattr(uvicorn, "Server", lambda config: server)

    with pytest.raises(SystemExit) as exc:
        run_server.main(["--no-engine", "--no-browser", "--host", "127.0.0.1", "--port", "8080"])

    assert exc.value.code == 0
    assert captured["settings"].host == "127.0.0.1"


def test_non_loopback_with_auth_enabled_passes_safety_check(
    isolated_launcher_env, monkeypatch
):
    """Non-loopback host (0.0.0.0) with auth enabled (GALGAME2VOICE_AUTH_DISABLED=0) is allowed."""
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "0")

    mock_socket = MagicMock()
    mock_socket.getsockname.return_value = ("0.0.0.0", 8080)
    monkeypatch.setattr(run_server, "reserve_available_socket", lambda port, host=None: mock_socket)

    captured = {}

    def simulate_uvicorn_run(*args, **kwargs):
        settings = get_settings()
        captured["settings"] = settings
        _init_logging_and_safety(settings)

    server = MagicMock(started=True)
    server.run.side_effect = simulate_uvicorn_run
    monkeypatch.setattr(uvicorn, "Config", MagicMock())
    monkeypatch.setattr(uvicorn, "Server", lambda config: server)

    with pytest.raises(SystemExit) as exc:
        run_server.main(["--no-engine", "--no-browser", "--host", "0.0.0.0", "--port", "8080"])

    assert exc.value.code == 0
    assert captured["settings"].host == "0.0.0.0"


def test_fallback_port_is_reflected_in_settings(
    isolated_launcher_env, monkeypatch
):
    """When preferred port is occupied, the resolved fallback port is visible in Settings."""
    mock_socket = MagicMock()
    # Preferred was 8080, but fallback resolved to 8081
    mock_socket.getsockname.return_value = ("127.0.0.1", 8081)
    monkeypatch.setattr(run_server, "reserve_available_socket", lambda port, host=None: mock_socket)

    captured = {}

    def simulate_uvicorn_run(*args, **kwargs):
        settings = get_settings()
        captured["settings"] = settings
        _init_logging_and_safety(settings)

    server = MagicMock(started=True)
    server.run.side_effect = simulate_uvicorn_run
    monkeypatch.setattr(uvicorn, "Config", MagicMock())
    monkeypatch.setattr(uvicorn, "Server", lambda config: server)

    with pytest.raises(SystemExit) as exc:
        run_server.main(["--no-engine", "--no-browser", "--host", "127.0.0.1", "--port", "8080"])

    assert exc.value.code == 0
    assert captured["settings"].port == 8081
    assert os.environ["GALGAME2VOICE_PORT"] == "8081"
    assert os.environ["PORT"] == "8081"
    assert os.environ["GALGAME_PORT"] == "8081"
