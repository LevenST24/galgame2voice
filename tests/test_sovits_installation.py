"""Remembered engine directories, relocation and the settings API's failure boundaries."""

import shutil
import asyncio
import threading
from unittest.mock import MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from galgame2voice.config import get_settings
from galgame2voice.main import create_app
from galgame2voice.routers import health, voice
from galgame2voice.services import sovits_installation as installation
from galgame2voice.utils.path_guard import get_authorized_roots
from scripts import run_server


def make_engine(path):
    path.mkdir(parents=True)
    (path / "api_v2.py").write_text("# fake engine for path checks\n")
    (path / "runtime").mkdir()
    name = "python.exe" if installation.sys.platform == "win32" else "python"
    (path / "runtime" / name).write_bytes(b"test interpreter marker, never executed")
    return path


def test_relative_hint_survives_relocation_and_different_cwd(tmp_path, monkeypatch):
    original = tmp_path / "original 中文"
    root = original / "G2V"
    root.mkdir(parents=True)
    engine = make_engine(original / "my voice engine !")
    installation.save_sovits_directory(root, f'"{engine}"')
    assert (root / "data" / "sovits_dir.txt").read_text() == "../my voice engine !" or (
        root / "data" / "sovits_dir.txt"
    ).read_text() == "..\\my voice engine !"
    relocated = tmp_path / "relocated 中文"
    shutil.copytree(original, relocated)
    monkeypatch.chdir(tmp_path)
    expected = relocated / engine.name
    assert installation.read_sovits_directory(relocated / "G2V") == expected
    assert health._resolve_sovits_directory(relocated / "G2V") == expected
    monkeypatch.setattr(run_server, "PROJECT_ROOT", relocated / "G2V")
    assert run_server.find_gpt_sovits_directory() == expected


def test_bom_quoted_hint_resolves_against_install_not_cwd(tmp_path, monkeypatch):
    engine = make_engine(tmp_path / "engine")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sovits_dir.txt").write_text('\ufeff"engine"', encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)
    assert installation.read_sovits_directory(tmp_path) == engine


@pytest.mark.parametrize("value", ["", " \n ", "invalid\x00folder", "missing", "data"])
def test_invalid_selection_preserves_saved_directory(tmp_path, value):
    make_engine(tmp_path / "engine")
    installation.save_sovits_directory(tmp_path, "engine")
    before = (tmp_path / "data" / "sovits_dir.txt").read_bytes()
    with pytest.raises(ValueError):
        installation.save_sovits_directory(tmp_path, value)
    assert (tmp_path / "data" / "sovits_dir.txt").read_bytes() == before


def test_failed_atomic_write_preserves_previous_hint_and_cleans_temp(tmp_path, monkeypatch):
    make_engine(tmp_path / "engine")
    installation.save_sovits_directory(tmp_path, "engine")
    before = (tmp_path / "data" / "sovits_dir.txt").read_bytes()
    other = make_engine(tmp_path / "other engine")
    def fail(*args):
        raise PermissionError("read-only test destination")
    monkeypatch.setattr(installation.os, "replace", fail)
    with pytest.raises(PermissionError):
        installation.save_sovits_directory(tmp_path, str(other))
    assert (tmp_path / "data" / "sovits_dir.txt").read_bytes() == before
    assert list((tmp_path / "data").glob(".sovits-dir-*.tmp")) == []


def test_portable_rejects_engine_without_runtime(tmp_path, monkeypatch):
    (tmp_path / "api_v2.py").write_text("# source-only engine")
    monkeypatch.setattr(installation.sys, "frozen", True, raising=False)
    with pytest.raises(ValueError, match="runtime"):
        installation.save_sovits_directory(tmp_path, str(tmp_path))
    assert not (tmp_path / "data" / "sovits_dir.txt").exists()


@pytest.mark.asyncio
async def test_directory_api_feeds_model_scan_and_authorized_roots(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "project_root", root)
    engine = make_engine(tmp_path / "outside-selected-engine")
    for folder, filename in (("GPT_weights", "user.ckpt"), ("SoVITS_weights", "user.pth")):
        (engine / folder).mkdir()
        (engine / folder / filename).write_bytes(b"test model marker")
    voice._scan_cache["result"] = (9999999999, {"stale": True})
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        empty = await client.get("/api/system/sovits-directory")
        assert empty.json()["directory"] == ""
        result = await client.put("/api/system/sovits-directory", json={"directory": str(engine)})
        assert result.status_code == 200
        assert result.json()["valid"] and result.json()["runtime_available"]
        assert (await client.get("/api/system/sovits-directory")).json()["directory"] == str(engine)
        assert engine in get_authorized_roots()
        scan = await client.get("/api/voice/scan-models")
        assert str(engine / "GPT_weights" / "user.ckpt") in [item["path"] for item in scan.json()["gpt_weights"]]
        assert str(engine / "SoVITS_weights" / "user.pth") in [item["path"] for item in scan.json()["sovits_weights"]]
        invalid = await client.put("/api/system/sovits-directory", json={"directory": str(tmp_path)})
        assert invalid.status_code == 400
        assert installation.read_sovits_directory(root) == engine


@pytest.mark.asyncio
async def test_directory_api_requires_authentication(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "project_root", tmp_path)
    monkeypatch.setattr("galgame2voice.security.auth.is_auth_disabled", lambda: False)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        assert (await client.get("/api/system/sovits-directory")).status_code == 401
        assert (await client.put("/api/system/sovits-directory", json={"directory": str(tmp_path)})).status_code == 401
    assert not (tmp_path / "data" / "sovits_dir.txt").exists()


@pytest.mark.asyncio
async def test_native_directory_picker_destroys_window_on_failure(monkeypatch):
    tkinter = pytest.importorskip("tkinter")
    from tkinter import filedialog
    root = MagicMock()
    monkeypatch.setattr(tkinter, "Tk", lambda: root)
    picker = MagicMock(side_effect=RuntimeError("dialog failed"))
    monkeypatch.setattr(filedialog, "askdirectory", picker)
    result = await voice.open_native_file_dialog(voice.BrowseFileRequest(file_type="directory"))
    assert result == {"selected_path": ""}
    picker.assert_called_once()
    root.destroy.assert_called_once()


@pytest.mark.asyncio
async def test_saving_directory_discards_inflight_scan_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "project_root", tmp_path)
    monkeypatch.setattr(voice, "_scan_cache", {})
    started = threading.Event()
    release = threading.Event()
    def scan_old_directory():
        started.set()
        assert release.wait(5)
        return {"gpt_weights": [{"path": "old"}]}
    monkeypatch.setattr(voice, "_scan_models_sync", scan_old_directory)
    pending = asyncio.create_task(voice.scan_discovered_models())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        engine = make_engine(tmp_path / "new engine")
        await health.save_sovits_directory_endpoint(health.SovitsDirectoryPayload(directory=str(engine)))
    finally:
        release.set()
        await pending
    assert "result" not in voice._scan_cache
