"""Portable data paths, asset boundaries and safe frozen launcher behavior."""

import builtins
import hashlib
import io
from pathlib import Path
import re
import runpy
import subprocess
import sys
from unittest.mock import MagicMock
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient
import yaml

from galgame2voice.config import Settings
from galgame2voice.main import create_app
from galgame2voice import runtime_paths
from galgame2voice.routers import system
from scripts import build_windows_portable, run_server, smoke_windows_portable


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
    for folder in (static / "assets", static / "characters", source / "data", source / "characters", source / "docs"):
        folder.mkdir(parents=True, exist_ok=True)
    (static / "index.html").write_text("<html>application</html>")
    (static / "assets" / "app.js").write_text("console.log('app')")
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


@pytest.mark.asyncio
async def test_frozen_version_uses_release_version_without_inspecting_parent_git(monkeypatch):
    transport = ASGITransport(app=create_app())  # built before sys.frozen flips, so cached settings stay untouched
    monkeypatch.setattr(system.sys, "frozen", True, raising=False)
    spawn = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", spawn)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        resp = await client.get("/api/system/version?check_remote=false")
    assert resp.status_code == 200
    data = resp.json()
    assert data["current_version"] == system.__version__
    assert data["current_branch"] == "portable"
    assert not data["has_update"]
    spawn.assert_not_called()


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


def test_smoke_cli_prints_chinese_failure_log_with_cp1252_stdio(tmp_path, monkeypatch):
    archive = tmp_path / "portable.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Galgame2Voice/tools/ffmpeg.exe", b"fake ffmpeg")
    archive.with_suffix(".zip.sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
    log_text = "启动失败：中文诊断日志\n"

    def fake_run(command, **kwargs):
        install = Path(command[0]).parent
        if "--check-only" in command and (install / "tools/ffmpeg.disabled").exists():
            (install / "logs").mkdir()
            (install / "logs/launcher_error.log").write_text("missing ffmpeg.exe", encoding="utf-8")
            return subprocess.CompletedProcess(command, 1, b"", b"")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def failed_launch(*args, **kwargs):
        kwargs["stdout"].write(log_text.encode("utf-8"))
        return MagicMock(poll=MagicMock(return_value=1))

    monkeypatch.setattr(smoke_windows_portable.subprocess, "run", fake_run)
    monkeypatch.setattr(smoke_windows_portable.subprocess, "Popen", failed_launch)
    monkeypatch.setattr(smoke_windows_portable.urllib.request, "build_opener", MagicMock())
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setattr(sys, "argv", [str(smoke_windows_portable.__file__), str(archive),
                                      "--scratch", str(tmp_path / "scratch")])
    output = io.BytesIO()
    errors = io.BytesIO()
    with monkeypatch.context() as stdio_patch:
        stdout = io.TextIOWrapper(output, encoding="cp1252")
        stderr = io.TextIOWrapper(errors, encoding="cp1252")
        stdio_patch.setattr(sys, "stdout", stdout)
        stdio_patch.setattr(sys, "stderr", stderr)
        with pytest.raises(RuntimeError, match="Portable application exited early"):
            runpy.run_path(smoke_windows_portable.__file__, run_name="__main__")
        stdout.flush()
        assert stdout.encoding == stderr.encoding == "utf-8"
    assert log_text.rstrip("\n") in output.getvalue().decode("utf-8").splitlines()


def test_smoke_browser_retries_timeout_with_new_profile(tmp_path, monkeypatch, capsys):
    calls = []
    partial_stdout = b"<html>partial output\xff"
    partial_stderr = b"Chrome startup diagnostic"

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=partial_stdout, stderr=partial_stderr)
        return subprocess.CompletedProcess(command, 0, b'<div id="advRoot"><div class="adv-stage is-home"><div class="adv-title-screen"></div></div></div>', b"")

    monkeypatch.setattr(smoke_windows_portable.subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    smoke_windows_portable.check_browser(Path("chrome.exe"), "http://127.0.0.1:1234", {}, tmp_path, tmp_path)
    assert len(calls) == 2
    profiles = [next(arg for arg in command if arg.startswith("--user-data-dir=")) for command, _ in calls]
    assert profiles[0] != profiles[1]
    assert all(kwargs["timeout"] == 60 and kwargs["capture_output"] for _, kwargs in calls)
    assert (tmp_path / "browser-attempt-1-stdout.log").read_bytes() == partial_stdout
    assert (tmp_path / "browser-attempt-1-stderr.log").read_bytes() == partial_stderr
    assert "60" in capsys.readouterr().out


@pytest.mark.parametrize("output", [b"partial diagnostic", None])
def test_smoke_browser_reports_both_timeouts(tmp_path, monkeypatch, capsys, output):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=output, stderr=output)

    monkeypatch.setattr(smoke_windows_portable.subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    with pytest.raises(RuntimeError, match="60.*" + re.escape(str(tmp_path))) as failure:
        smoke_windows_portable.check_browser(Path("chrome.exe"), "http://127.0.0.1:1234", {}, tmp_path, tmp_path)
    assert isinstance(failure.value.__cause__, subprocess.TimeoutExpired)
    assert len(calls) == 2
    for attempt in (1, 2):
        for stream in ("stdout", "stderr"):
            assert (tmp_path / f"browser-attempt-{attempt}-{stream}.log").read_bytes() == (output or b"")
    assert len(capsys.readouterr().out.splitlines()) == 2


@pytest.mark.parametrize(("returncode", "dom"), [(0, b"<html></html>"), (1, "支持语音输入与朗读回复".encode("utf-8"))])
def test_smoke_browser_preserves_failed_render_diagnostics(tmp_path, monkeypatch, returncode, dom):
    run = MagicMock(return_value=subprocess.CompletedProcess([], returncode, dom, b"render error"))
    monkeypatch.setattr(smoke_windows_portable.subprocess, "run", run)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    with pytest.raises(RuntimeError, match="The real browser did not initialize the portable chat interface"):
        smoke_windows_portable.check_browser(Path("chrome.exe"), "http://127.0.0.1:1234", {}, tmp_path, tmp_path)
    run.assert_called_once()
    assert (tmp_path / "browser-rendered.html").read_bytes() == dom
    assert (tmp_path / "browser-errors.log").read_bytes() == b"render error"


def test_smoke_startup_only_preserves_diagnostics_on_failure(tmp_path, monkeypatch):
    archive = tmp_path / "portable.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Galgame2Voice/Galgame2Voice.exe", b"fake exe")
        bundle.writestr("Galgame2Voice/tools/ffmpeg.exe", b"fake ffmpeg")
    archive.with_suffix(".zip.sha256").write_text(
        hashlib.sha256(archive.read_bytes()).hexdigest(), encoding="utf-8"
    )
    scratch = tmp_path / "scratch"
    marker = "startup diagnostic marker: missing runtime dependency\n"

    def failing_run(command, **kwargs):
        install = Path(command[0]).parent
        (install / "logs").mkdir(parents=True, exist_ok=True)
        (install / "logs" / "launcher_error.log").write_text(marker, encoding="utf-8")
        return subprocess.CompletedProcess(command, 1, b"", b"dependency failure")

    monkeypatch.setattr(smoke_windows_portable.subprocess, "run", failing_run)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setenv("SystemRoot", str(tmp_path))

    with pytest.raises(RuntimeError, match="Portable dependency check failed"):
        smoke_windows_portable.smoke_test(archive, scratch, startup_only=True)

    assert [entry.name for entry in scratch.iterdir()] == ["diagnostics"]
    preserved = scratch / "diagnostics" / "launcher_error.log"
    assert preserved.is_file()
    assert marker in preserved.read_text(encoding="utf-8")


def test_smoke_startup_only_stops_after_bounded_health_checks(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "portable.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Galgame2Voice/Galgame2Voice.exe", b"fake exe")
        bundle.writestr("Galgame2Voice/启动.bat", b"@echo off")
        bundle.writestr("Galgame2Voice/tools/ffmpeg.exe", b"fake ffmpeg")
    archive.with_suffix(".zip.sha256").write_text(
        hashlib.sha256(archive.read_bytes()).hexdigest(), encoding="utf-8"
    )
    scratch = tmp_path / "scratch"
    launched_ports: list[str] = []

    def fake_run(command, **kwargs):
        first = Path(command[0])
        install = first.parent if first.name == "Galgame2Voice.exe" else None
        if install and "--check-only" in command and (install / "tools" / "ffmpeg.disabled").exists():
            (install / "logs").mkdir(parents=True, exist_ok=True)
            (install / "logs" / "launcher_error.log").write_text("missing ffmpeg.exe", encoding="utf-8")
            return subprocess.CompletedProcess(command, 1, b"", b"")
        if install and "--restore-database" in command:
            return subprocess.CompletedProcess(command, 1, "程序仍在运行".encode("utf-8"), b"")
        if install and "--port" in command and command[command.index("--port") + 1] == "1":
            return subprocess.CompletedProcess(command, 0, launched_ports[-1].encode("ascii"), b"")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    process = MagicMock()
    process.poll.side_effect = lambda: None if not process.terminate.called else 0

    def fake_popen(command, **kwargs):
        install = Path(command[0]).parent
        port = command[command.index("--port") + 1]
        launched_ports.append(port)
        (install / "data").mkdir(parents=True, exist_ok=True)
        (install / "data" / "desktop_instance.json").write_text(f'{{"port": {port}}}', encoding="utf-8")
        kwargs["stdout"].write(b"portable startup ok\n")
        return process

    class _FakeResponse(io.BytesIO):
        def __init__(self, body: bytes, content_type: str = "application/json", status: int = 200):
            super().__init__(body)
            self.status = status
            self.headers = {"Content-Type": content_type}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    class _FakeOpener:
        def open(self, url, timeout=5):
            target = url if isinstance(url, str) else url.full_url
            if target.endswith("/api/health"):
                return _FakeResponse(b'{"status":"ok"}')
            if target.endswith("/"):
                return _FakeResponse(b'<html><script src="/static/assets/app.js"></script></html>', "text/html")
            if target.endswith("/static/assets/app.js"):
                return _FakeResponse(b"console.log('portable bundle');" * 5, "application/javascript")
            raise AssertionError(f"Unexpected post-startup request in startup_only mode: {target}")

    monkeypatch.setattr(smoke_windows_portable.subprocess, "run", fake_run)
    monkeypatch.setattr(smoke_windows_portable.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(smoke_windows_portable.urllib.request, "build_opener", lambda *_: _FakeOpener())
    monkeypatch.setattr(
        smoke_windows_portable,
        "check_browser",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("browser check ran in startup_only mode")),
    )
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "argv",
        [str(smoke_windows_portable.__file__), str(archive), "--scratch", str(scratch), "--startup-only"],
    )

    runpy.run_path(smoke_windows_portable.__file__, run_name="__main__")

    process.terminate.assert_called_once()
    process.wait.assert_called_once()
    assert "Portable startup check passed" in capsys.readouterr().out
    assert list(scratch.iterdir()) == []


def test_portable_premerge_workflow_contract():
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "portable-premerge.yml"
    raw = workflow_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw)

    triggers = parsed.get("on") or parsed.get(True)
    assert isinstance(triggers, dict)
    assert "workflow_dispatch" in triggers
    pr_trigger = triggers.get("pull_request")
    assert isinstance(pr_trigger, dict)
    paths = pr_trigger.get("paths")
    assert isinstance(paths, list) and paths
    for expected_path in (
        "scripts/build_windows_portable.py",
        "scripts/verify_windows_portable.py",
        "scripts/smoke_windows_portable.py",
        "scripts/desktop_entry.py",
        "scripts/run_server.py",
        "galgame2voice/runtime_paths.py",
        "galgame2voice/database/**",
    ):
        assert expected_path in paths

    job = parsed["jobs"]["portable-startup-check"]
    assert job["runs-on"] == "windows-latest"
    assert job["timeout-minutes"] == 20

    assert "scripts/build_windows_portable.py" in raw
    assert "scripts/verify_windows_portable.py" in raw and "--skip-vite-dist" in raw
    assert "scripts/smoke_windows_portable.py" in raw and "--startup-only" in raw
    assert "softprops/action-gh-release" not in raw
    assert ".local/portable-premerge-smoke/diagnostics" in raw
