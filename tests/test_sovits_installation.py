"""Remembered engine directories, relocation and the settings API's failure boundaries."""

import shutil
import asyncio
from pathlib import Path
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


@pytest.mark.asyncio
async def test_nested_engine_directory_names_the_only_child(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "project_root", root)
    inner = tmp_path / "GPT-SoVITS" / "GPT-SoVITS"
    inner.mkdir(parents=True)
    (inner / "api_v2.py").write_text("# marker\n", encoding="utf-8")
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        result = await client.put("/api/system/sovits-directory", json={"directory": str(inner.parent)})
    detail = result.json()["detail"]
    assert result.status_code == 400 and "请再进入「GPT-SoVITS」" in detail
    assert installation.read_sovits_directory(root) is None


def test_valid_directory_message_names_the_tab_that_starts_the_engine(tmp_path):
    # The tab was renamed from 状态诊断 to 运行状态; this message kept the old name.
    make_engine(tmp_path / "engine")
    installation.save_sovits_directory(tmp_path, "engine")
    assert "「运行状态」" in installation.inspect_sovits_directory(tmp_path)["message"]


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
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8080") as client:
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


def test_launcher_discovery_writes_relative_hint_for_sibling_and_survives_copy(tmp_path, monkeypatch):
    old_parent = tmp_path / "old_parent"
    old_g2v = old_parent / "Galgame2Voice"
    old_engine = make_engine(old_parent / "GPT-SoVITS")
    old_g2v.mkdir(parents=True)

    monkeypatch.setattr(run_server, "PROJECT_ROOT", old_g2v)
    monkeypatch.delenv("GPT_SOVITS_DIR", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_DATA_ROOT", raising=False)

    discovered = run_server.find_gpt_sovits_directory()
    assert discovered == old_engine.resolve()

    cache_file = old_g2v / "data" / "sovits_dir.txt"
    assert cache_file.is_file(), "data/sovits_dir.txt should have been created"
    saved_text = cache_file.read_text(encoding="utf-8").strip()

    # Must be relative path, not absolute
    assert not Path(saved_text).is_absolute(), f"Expected relative path, got absolute: {saved_text}"
    assert saved_text in ("..\\GPT-SoVITS", "../GPT-SoVITS")

    # Now copy old_parent to new_parent (old_parent remains on disk!)
    new_parent = tmp_path / "new_parent"
    shutil.copytree(old_parent, new_parent)
    new_g2v = new_parent / "Galgame2Voice"
    new_engine = new_parent / "GPT-SoVITS"

    # inspect_sovits_directory on new_g2v must resolve to new_engine, NOT old_engine
    inspected = installation.inspect_sovits_directory(new_g2v)
    assert inspected["valid"] is True
    assert Path(inspected["directory"]).resolve() == new_engine.resolve()
    assert Path(inspected["directory"]).resolve() != old_engine.resolve()

    # find_gpt_sovits_directory with PROJECT_ROOT = new_g2v must resolve to new_engine, NOT old_engine
    monkeypatch.setattr(run_server, "PROJECT_ROOT", new_g2v)
    new_discovered = run_server.find_gpt_sovits_directory()
    assert new_discovered.resolve() == new_engine.resolve()
    assert new_discovered.resolve() != old_engine.resolve()

    # Also verify moving/renaming old_parent (old_parent removed)
    moved_parent = tmp_path / "moved_parent"
    shutil.move(str(old_parent), str(moved_parent))
    moved_g2v = moved_parent / "Galgame2Voice"
    moved_engine = moved_parent / "GPT-SoVITS"

    inspected_moved = installation.inspect_sovits_directory(moved_g2v)
    assert inspected_moved["valid"] is True
    assert Path(inspected_moved["directory"]).resolve() == moved_engine.resolve()

    monkeypatch.setattr(run_server, "PROJECT_ROOT", moved_g2v)
    assert run_server.find_gpt_sovits_directory().resolve() == moved_engine.resolve()


def test_write_sovits_directory_hint_external_path(tmp_path):
    root = tmp_path / "app"
    external = tmp_path.parent / "completely_external_engine"
    external.mkdir(parents=True, exist_ok=True)
    stored = installation.write_sovits_directory_hint(root, external)
    assert Path(stored).is_absolute()
    assert (root / "data" / "sovits_dir.txt").read_text(encoding="utf-8") == str(external.resolve())
    assert installation.read_sovits_directory(root) == external.resolve()


def test_write_sovits_directory_hint_strips_quotes_and_survives_relocation(tmp_path):
    parent = tmp_path / "bundle"
    root = parent / "Galgame2Voice"
    engine = make_engine(parent / "GPT-SoVITS")
    root.mkdir(parents=True)

    quoted_input = f'  "{engine.resolve()}"  '
    stored = installation.write_sovits_directory_hint(root, quoted_input)
    assert not Path(stored).is_absolute()
    assert stored in ("..\\GPT-SoVITS", "../GPT-SoVITS")

    new_parent = tmp_path / "moved_bundle"
    shutil.move(str(parent), str(new_parent))
    new_root = new_parent / "Galgame2Voice"
    new_engine = new_parent / "GPT-SoVITS"

    resolved = installation.read_sovits_directory(new_root)
    assert resolved == new_engine.resolve()


def test_launcher_discovery_migrates_legacy_absolute_hint_for_sibling(tmp_path, monkeypatch):
    parent = tmp_path / "old_bundle"
    g2v = parent / "Galgame2Voice"
    engine = make_engine(parent / "GPT-SoVITS")
    g2v.mkdir(parents=True)
    (g2v / "data").mkdir()

    # Pre-populate with legacy hardcoded absolute path
    cache_file = g2v / "data" / "sovits_dir.txt"
    cache_file.write_text(str(engine.resolve()), encoding="utf-8")

    monkeypatch.setattr(run_server, "PROJECT_ROOT", g2v)
    monkeypatch.delenv("GPT_SOVITS_DIR", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_DATA_ROOT", raising=False)

    discovered = run_server.find_gpt_sovits_directory()
    assert discovered == engine.resolve()

    # Launcher should have self-healed the legacy absolute cache to a relative hint
    migrated_text = cache_file.read_text(encoding="utf-8").strip()
    assert not Path(migrated_text).is_absolute(), f"Expected relative path after migration, got {migrated_text}"
    assert migrated_text in ("..\\GPT-SoVITS", "../GPT-SoVITS")

    # Verify that copying this bundle now survives without referencing old_bundle
    new_parent = tmp_path / "new_bundle"
    shutil.copytree(parent, new_parent)
    new_g2v = new_parent / "Galgame2Voice"
    new_engine = new_parent / "GPT-SoVITS"

    inspected = installation.inspect_sovits_directory(new_g2v)
    assert inspected["valid"] is True
    assert Path(inspected["directory"]).resolve() == new_engine.resolve()
    assert Path(inspected["directory"]).resolve() != engine.resolve()


def test_launcher_discovery_handles_quoted_env_override(tmp_path, monkeypatch):
    parent = tmp_path / "env_test"
    g2v = parent / "Galgame2Voice"
    engine = make_engine(parent / "Custom-SoVITS")
    g2v.mkdir(parents=True)

    monkeypatch.setattr(run_server, "PROJECT_ROOT", g2v)
    monkeypatch.setenv("GPT_SOVITS_DIR", f'  "{engine.resolve()}"  ')
    monkeypatch.delenv("GALGAME2VOICE_DATA_ROOT", raising=False)

    discovered = run_server.find_gpt_sovits_directory()
    assert discovered == engine.resolve()

    cache_file = g2v / "data" / "sovits_dir.txt"
    assert cache_file.is_file()
    saved_text = cache_file.read_text(encoding="utf-8").strip()
    assert not Path(saved_text).is_absolute()


def test_find_gpt_sovits_directory_prefers_hint_in_configured_project_root(tmp_path, monkeypatch):
    stale = make_engine(tmp_path / "stale-engine")
    chosen = make_engine(tmp_path / "chosen-engine")
    install = tmp_path / "install"
    custom = tmp_path / "custom-data"
    installation.save_sovits_directory(install, str(stale))
    installation.save_sovits_directory(custom, str(chosen))
    monkeypatch.setattr(run_server, "PROJECT_ROOT", install)
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(custom))
    get_settings.cache_clear()
    assert Path(get_settings().project_root) != run_server._DEFAULT_PROJECT_ROOT
    monkeypatch.delenv("GPT_SOVITS_DIR", raising=False)
    monkeypatch.delenv("GALGAME2VOICE_DATA_ROOT", raising=False)
    found = run_server.find_gpt_sovits_directory()
    assert found == chosen.resolve()
    assert installation.read_sovits_directory(install) == stale.resolve()
    assert installation.read_sovits_directory(custom) == chosen.resolve()

    # Legacy absolute hint in configured root rewrites back to configured root, not install root.
    (custom / "data" / "sovits_dir.txt").write_text(str(chosen.resolve()), encoding="utf-8")
    assert run_server.find_gpt_sovits_directory() == chosen.resolve()
    assert not Path((custom / "data" / "sovits_dir.txt").read_text(encoding="utf-8").strip()).is_absolute()
    assert installation.read_sovits_directory(install) == stale.resolve()

