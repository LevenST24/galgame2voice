"""Portable data paths, asset boundaries and safe frozen launcher behavior."""

import builtins
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from galgame2voice.config import Settings
from galgame2voice import runtime_paths
from galgame2voice.routers import system
from scripts import build_windows_portable, run_server


def test_source_install_root_is_project():
    assert runtime_paths.get_install_root() == Path(__file__).resolve().parents[1]


def test_frozen_paths_keep_user_data_beside_executable(tmp_path, monkeypatch):
    root = tmp_path / "便携 ! folder"
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_paths.sys, "executable", str(root / "Galgame2Voice.exe"))
    monkeypatch.delenv("GALGAME2VOICE_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    settings = Settings(data_dir_name="data", audio_dir_name="audio", logs_dir_name="logs")
    assert settings.project_root == root
    assert settings.db_path == root / "data" / "galgame2voice.db"
    assert settings.static_dir == root / "galgame2voice" / "static"
    assert settings.audio_dir == root / "audio"


def test_portable_assets_do_not_copy_user_secrets_or_characters(tmp_path, monkeypatch):
    source = tmp_path / "source"
    static = source / "galgame2voice" / "static"
    for folder in (static / "assets", static / "js", static / "characters", source / "data", source / "characters", source / "docs"):
        folder.mkdir(parents=True, exist_ok=True)
    (static / "index.html").write_text("<html>application</html>")
    (static / "assets" / "app.js").write_text("console.log('app')")
    (static / "js" / "audio_player.js").write_text("export const player = {}")
    (static / "characters" / "private.png").write_bytes(b"private portrait")
    (source / "characters" / "private.json").write_text("private character")
    (source / "data" / ".master_key").write_bytes(b"private key")
    (source / ".env").write_text("private key")
    for name in ("LICENSE", "SECURITY.md", ".env.example"):
        (source / name).write_text("public template")
    (source / "docs" / "WINDOWS_PORTABLE.md").write_text("public guide")
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"test binary")
    target = tmp_path / "package"
    target.mkdir()
    monkeypatch.setattr(build_windows_portable, "PROJECT_ROOT", source)
    build_windows_portable.assemble_portable_files(target, ffmpeg)
    assert (target / "galgame2voice" / "static" / "assets" / "app.js").is_file()
    assert (target / "tools" / "ffmpeg.exe").read_bytes() == b"test binary"
    assert list((target / "data").iterdir()) == []
    assert list((target / "characters").iterdir()) == []
    assert not (target / ".env").exists()
    assert not (target / "galgame2voice" / "static" / "characters").exists()
    assert '"%~dp0Galgame2Voice.exe" %*' in (target / "启动.bat").read_text(encoding="utf-8")


def test_frozen_missing_dependencies_never_spawn_executable_as_pip(monkeypatch, capsys):
    original_import = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == "cryptography":
            raise ImportError("missing packaged dependency")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", missing)
    monkeypatch.setattr(run_server.sys, "frozen", True, raising=False)
    spawn = MagicMock()
    monkeypatch.setattr(run_server.subprocess, "run", spawn)
    assert not run_server.check_python_environment()
    spawn.assert_not_called()
    assert "_internal" in capsys.readouterr().out


def test_frozen_engine_requires_its_own_python(tmp_path, monkeypatch):
    monkeypatch.setattr(run_server.sys, "frozen", True, raising=False)
    spawn = MagicMock()
    monkeypatch.setattr(run_server.subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError, match="runtime/python.exe"):
        run_server._spawn_sovits_process(tmp_path, "127.0.0.1", 9880, False)
    spawn.assert_not_called()


def test_launcher_can_open_first_setup_without_managing_engine():
    assert run_server.parse_args(["--no-engine", "--no-browser"]).no_engine


def test_frozen_version_uses_release_version_without_inspecting_parent_git(tmp_path, monkeypatch):
    monkeypatch.setattr(system.sys, "frozen", True, raising=False)
    git = MagicMock()
    monkeypatch.setattr(system, "_run_git_cmd", git)
    response = system._check_version_sync(tmp_path, check_remote=True)
    assert response.current_version == system.__version__
    assert response.current_branch == "portable"
    assert not response.has_update
    git.assert_not_called()


def test_frozen_update_never_mutates_git_even_when_forced(tmp_path, monkeypatch):
    monkeypatch.setattr(system.sys, "frozen", True, raising=False)
    git = MagicMock()
    monkeypatch.setattr(system, "_run_git_cmd", git)
    response, changed = system._apply_update_sync(tmp_path, True, True, True)
    assert not response.success
    assert "使用说明" in response.output
    assert changed == []
    git.assert_not_called()


def test_launcher_failure_writes_diagnostic_log(tmp_path, monkeypatch, capsys):
    from scripts import desktop_entry
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(desktop_entry, "get_install_root", lambda: tmp_path)
    monkeypatch.setattr(desktop_entry.sys, "argv", ["Galgame2Voice.exe"])
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    def fail():
        raise RuntimeError("startup diagnostic marker")
    monkeypatch.setattr(run_server, "main", fail)
    assert desktop_entry.main() == 1
    text = (tmp_path / "logs" / "launcher_error.log").read_text(encoding="utf-8")
    assert "Traceback" in text and "startup diagnostic marker" in text
    assert "launcher_error.log" in capsys.readouterr().out
