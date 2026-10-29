"""Dependency-recovery commands must target the interpreter that failed."""

import builtins
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from unittest.mock import MagicMock
import venv

import pytest

from scripts import run_server


@pytest.mark.parametrize("has_requirements", [True, False])
def test_dependency_recovery_quotes_project_interpreter_and_targets_same_environment(
    tmp_path, monkeypatch, capsys, has_requirements,
):
    root = tmp_path / "project with spaces"
    root.mkdir()
    requirements = root / "requirements.txt"
    if has_requirements:
        requirements.write_text("cryptography\n", encoding="utf-8")
    python = str(root / ".venv" / "Scripts" / "python.exe")
    monkeypatch.setattr(run_server, "PROJECT_ROOT", root)
    monkeypatch.setattr(run_server.sys, "executable", python)
    monkeypatch.setattr(run_server.sys, "frozen", False, raising=False)
    original_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "cryptography":
            raise ImportError("synthetic missing dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    install = MagicMock(side_effect=OSError("synthetic install failure"))
    monkeypatch.setattr(run_server.subprocess, "run", install)
    assert not run_server.check_python_environment()
    output = capsys.readouterr().out
    if has_requirements:
        command = [python, "-m", "pip", "install", "-r", str(requirements)]
        install.assert_called_once_with(command, check=True)
    else:
        command = [python, "-m", "pip", "install", "cryptography"]
        install.assert_not_called()
    quoted = subprocess.list2cmdline(command) if sys.platform == "win32" else shlex.join(command)
    assert quoted in output
    assert "py -m pip" not in output


@pytest.mark.parametrize("dependency_visible", [True, False])
def test_successful_installer_is_verified_in_the_active_interpreter(tmp_path, monkeypatch, capsys, dependency_visible):
    (tmp_path / "requirements.txt").write_text("cryptography\n", encoding="utf-8")
    monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(run_server.sys, "frozen", False, raising=False)
    original_import = builtins.__import__
    installed = False

    def mock_import(name, *args, **kwargs):
        if name == "cryptography":
            if installed and dependency_visible:
                return object()
            raise ImportError("synthetic missing dependency")
        return original_import(name, *args, **kwargs)

    def mock_install(*args, **kwargs):
        nonlocal installed
        installed = True

    monkeypatch.setattr(builtins, "__import__", mock_import)
    monkeypatch.setattr(run_server.subprocess, "run", mock_install)
    assert run_server.check_python_environment() is dependency_visible
    output = capsys.readouterr().out
    if dependency_visible:
        assert "自动安装成功" in output
    else:
        assert "仍无法" in output and "cryptography" in output
        assert "自动安装成功" not in output


@pytest.mark.requires_piped_subprocess
@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell recovery command")
def test_printed_powershell_recovery_command_runs_in_the_project_environment(tmp_path, monkeypatch, capsys):
    root = tmp_path / "O'Brien $ project !"
    root.mkdir()
    requirements = root / "requirements.txt"
    requirements.write_text("synthetic-only\n", encoding="utf-8")
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    python = root / ".venv" / "Scripts" / "python.exe"
    pip = root / ".venv" / "Lib" / "site-packages" / "pip"
    pip.mkdir()
    (pip / "__init__.py").write_text("", encoding="utf-8")
    (pip / "__main__.py").write_text(
        "import json, sys\nprint('SYNTHETIC_PIP_ARGS=' + json.dumps(sys.argv[1:]))\n", encoding="utf-8"
    )
    original_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "cryptography":
            raise ImportError("synthetic missing dependency")
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as mocked:
        mocked.setattr(run_server, "PROJECT_ROOT", root)
        mocked.setattr(run_server.sys, "executable", str(python))
        mocked.setattr(run_server.sys, "frozen", False, raising=False)
        mocked.setattr(builtins, "__import__", missing)
        mocked.setattr(run_server.subprocess, "run", MagicMock(side_effect=OSError("synthetic failure")))
        assert not run_server.check_python_environment()
    lines = capsys.readouterr().out.splitlines()
    command = next((line.strip().removeprefix("PowerShell: ") for line in lines
                    if line.strip().startswith("PowerShell: ") and "pip" in line), None)
    cmd_command = next((line.strip().removeprefix("命令提示符: ") for line in lines
                        if line.strip().startswith("命令提示符: ") and "pip" in line), None)
    assert command is not None, lines
    assert cmd_command is not None, lines
    (tmp_path / "recovery-command.txt").write_text(command, encoding="utf-8")
    system32 = Path(os.environ.get("SystemRoot") or os.environ["SYSTEMROOT"]) / "System32"
    powershell = system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        pytest.skip("Windows PowerShell is not installed")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    # The isolated release runner deliberately passes a minimal environment.
    # Windows PowerShell needs PATHEXT to invoke native executables.
    env.setdefault("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert "SYNTHETIC_PIP_ARGS=" + json.dumps(["install", "-r", str(requirements)]) in result.stdout, (
        command, result.stdout, result.stderr
    )
    driver = tmp_path / "recovery.cmd"
    driver.write_text(f"@echo off\r\n{cmd_command}\r\nexit /b %errorlevel%\r\n", encoding="utf-8")
    cmd_result = subprocess.run([str(system32 / "cmd.exe"), "/d", "/v:off", "/c", str(driver)],
                                cwd=root, env=env, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=30, check=False)
    assert cmd_result.returncode == 0, cmd_result.stderr
    assert "SYNTHETIC_PIP_ARGS=" + json.dumps(["install", "-r", str(requirements)]) in cmd_result.stdout
