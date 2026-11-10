"""Portable build, release verification and update smoke must cover backgrounds/."""

import ast
import hashlib
from pathlib import Path
import zipfile

import pytest

from scripts import build_windows_portable
from scripts.verify_windows_portable import PREFIX, verify_archive

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_project_and_archive(root: Path, runtime_entries: dict[str, bytes]) -> Path:
    static = root / "galgame2voice/static"
    built = root / "frontend/dist"
    for folder in (static / "assets", built / "assets", root / "docs"):
        folder.mkdir(parents=True, exist_ok=True)
    for folder in (static, built):
        (folder / "index.html").write_bytes(b"<html>portable</html>")
        (folder / "assets/app.js").write_bytes(b"console.log('portable')")
    guide = root / "docs/WINDOWS_PORTABLE.md"
    guide.write_bytes(b"portable guide")

    files = {
        "Galgame2Voice.exe": b"exe",
        "启动.bat": b"bat",
        "数据恢复.bat": b"restore",
        "tools/ffmpeg.exe": b"ffmpeg",
        "LICENSE": b"license",
        "使用说明.md": guide.read_bytes(),
        "_internal/python312.dll": b"dll",
        "galgame2voice/static/index.html": (static / "index.html").read_bytes(),
        "galgame2voice/static/assets/app.js": (static / "assets/app.js").read_bytes(),
        **runtime_entries,
    }
    archive = root / "portable.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(PREFIX + name, content)
    archive.with_suffix(".zip.sha256").write_text(
        hashlib.sha256(archive.read_bytes()).hexdigest(),
        encoding="utf-8",
    )
    return archive


def test_verify_archive_requires_empty_backgrounds_directory(tmp_path):
    without_backgrounds = {
        f"{folder}/.keep": b"" for folder in ("data", "audio", "logs", "characters")
    }
    missing_root = tmp_path / "missing"
    missing_archive = _write_project_and_archive(missing_root, without_backgrounds)
    with pytest.raises(ValueError, match=r"Release backgrounds/ must contain only \.keep"):
        verify_archive(missing_archive, missing_root)

    valid_entries = {**without_backgrounds, "backgrounds/.keep": b""}
    valid_root = tmp_path / "valid"
    valid_archive = _write_project_and_archive(valid_root, valid_entries)
    result = verify_archive(valid_archive, valid_root)
    assert result["empty_user_folders"] is True

    dirty_entries = {**valid_entries, "backgrounds/custom.png": b"residual-bg"}
    dirty_root = tmp_path / "dirty"
    dirty_archive = _write_project_and_archive(dirty_root, dirty_entries)
    with pytest.raises(ValueError, match=r"Release backgrounds/ must contain only \.keep"):
        verify_archive(dirty_archive, dirty_root)


def test_build_portable_creates_and_zips_empty_backgrounds_directory(tmp_path, monkeypatch):
    source = tmp_path / "source"
    static = source / "galgame2voice/static"
    built = source / "frontend/dist"
    for folder in (
        static / "assets",
        built / "assets",
        source / "docs",
        source / "backgrounds" / "private_work",
    ):
        folder.mkdir(parents=True, exist_ok=True)
    for folder in (static, built):
        (folder / "index.html").write_text("<html>portable</html>", encoding="utf-8")
        (folder / "assets/app.js").write_text("console.log('portable')", encoding="utf-8")
    (source / "backgrounds" / "private_work" / "room.png").write_bytes(b"private-bg")
    for name in ("LICENSE", "SECURITY.md", ".env.example"):
        (source / name).write_text("public", encoding="utf-8")
    (source / "docs/WINDOWS_PORTABLE.md").write_text("public guide", encoding="utf-8")

    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"ffmpeg-bin")
    dist_dir = tmp_path / "Galgame2Voice"
    (dist_dir / "_internal").mkdir(parents=True)
    (dist_dir / "Galgame2Voice.exe").write_bytes(b"exe")
    (dist_dir / "_internal/python312.dll").write_bytes(b"dll")

    monkeypatch.setattr(build_windows_portable, "PROJECT_ROOT", source)
    build_windows_portable.assemble_portable_files(dist_dir, ffmpeg)

    assert (dist_dir / "backgrounds").is_dir()
    assert list((dist_dir / "backgrounds").iterdir()) == []
    assert "backgrounds" in build_windows_portable.PORTABLE_EMPTY_RUNTIME_DIRS

    archive = tmp_path / "portable.zip"
    build_windows_portable.write_zip(dist_dir, archive)
    archive.with_suffix(".zip.sha256").write_text(
        hashlib.sha256(archive.read_bytes()).hexdigest(),
        encoding="utf-8",
    )

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert "Galgame2Voice/backgrounds/.keep" in names
        assert not any("private_work" in name or name.endswith(".png") for name in names)
        extracted = tmp_path / "extracted"
        bundle.extractall(extracted)
    assert (extracted / "Galgame2Voice" / "backgrounds").is_dir()
    assert verify_archive(archive, source)["empty_user_folders"] is True


def test_smoke_portable_update_relocation_covers_backgrounds():
    guide_text = (PROJECT_ROOT / "docs" / "WINDOWS_PORTABLE.md").read_text(encoding="utf-8")
    for folder in ("data", "audio", "characters", "backgrounds"):
        assert f"`{folder}`" in guide_text

    smoke_source = (PROJECT_ROOT / "scripts" / "smoke_windows_portable.py").read_text(encoding="utf-8")
    module = ast.parse(smoke_source)
    smoke_fn = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "smoke_test"
    )

    seeded_dirs = None
    copied_dirs = None
    verified_dirs = None
    for node in ast.walk(smoke_fn):
        if not isinstance(node, ast.For) or not isinstance(node.iter, ast.Tuple):
            continue
        if not all(isinstance(elt, ast.Constant) and isinstance(elt.value, str) for elt in node.iter.elts):
            continue
        values = tuple(elt.value for elt in node.iter.elts)
        body_src = "\n".join(ast.unparse(stmt) for stmt in node.body)
        if "install" in body_src and "user-note.txt" in body_src and "write_text" in body_src:
            seeded_dirs = values
        elif "shutil.copytree" in body_src and "install" in body_src and "updated" in body_src:
            copied_dirs = values
        elif "updated" in body_src and "user-note.txt" in body_src and "read_text" in body_src:
            verified_dirs = values

    assert seeded_dirs == ("audio", "characters", "backgrounds")
    assert copied_dirs == ("data", "audio", "characters", "backgrounds")
    assert verified_dirs == ("audio", "characters", "backgrounds")
