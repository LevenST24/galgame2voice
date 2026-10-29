"""Inspect source archives built from a workspace containing synthetic private data."""

import hashlib
import os
from pathlib import Path
import zipfile

import pytest

from scripts import package_release


PUBLIC = {
    "README.md": "public guide",
    ".env.example": "PUBLIC_EXAMPLE=1",
    "requirements.txt": "fastapi",
    "启动.bat": "@echo off",
    "galgame2voice/main.py": "# application",
    "galgame2voice/static/index.html": "<html>application</html>",
    "galgame2voice/static/assets/index-public.js": "// bundle",
    "galgame2voice/static/assets/index-public.css": "body{}",
    "frontend/package.json": "{}",
    "frontend/src/main.js": "// source",
    "scripts/run_server.py": "# launcher",
    "scripts/dev_standins_replies.json": "{}",
    "tests/test_example.py": "# synthetic regression",
    "docs/licenses/FFmpeg-LICENSE.txt": "public license",
}
PRIVATE = [
    ".env", ".env.local", ".env.production", ".aws/credentials", ".codex/config.toml",
    ".claude/settings.json", ".ssh/id_ed25519", "private-notes.md", "export.json",
    "data/settings.json", "data/precision.json", "data/.master_key", "data/.console_token",
    "data/engine_cache/tts_infer.yaml", "data/engine_cache/bootstrap/api_v2-private.py",
    "characters/Demo/manifest.json", "characters/Demo/system_prompt.txt",
    "characters/Demo/portrait.webp", "characters/Demo/tiny.ckpt", "characters/Demo/tiny.pth",
    "models/tiny.ckpt", "audio/references/Demo/voice.ogg", "audio/local.ogg",
    "skins/Demo/theme.json", "backgrounds/Demo/background.webp", "logs/output.txt",
    "galgame2voice/static/characters/Demo/manifest.json",
    "galgame2voice/static/assets/characters/Demo/portrait.webp",
    "frontend/.env.local", "frontend/node_modules/demo/index.js",
    "frontend/dist/assets/old.js", "scripts/local-config.json", "scripts/.aws/config",
    "tests/.env.production", ".local/verification/private.md",
]


def populate(root, files):
    for name, content in files.items():
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")


def test_archive_contains_program_and_excludes_local_data(tmp_path, monkeypatch):
    populate(tmp_path, {**PUBLIC, **dict.fromkeys(PRIVATE, "PRIVATE_SENTINEL")})
    monkeypatch.setattr(package_release, "PROJECT_ROOT", tmp_path)
    archive = package_release.build_release_zip("test-clean")
    prefix = "galgame2voice-vtest-clean/"
    with zipfile.ZipFile(archive) as bundle:
        files = {name.removeprefix(prefix) for name in bundle.namelist()}
        assert set(PUBLIC) <= files
        assert files == set(PUBLIC) | {f"{name}/.keep" for name in ("audio", "data", "logs", "characters")}
        assert all(b"PRIVATE_SENTINEL" not in bundle.read(name) for name in bundle.namelist())
    assert archive.with_suffix(".zip.sha256").read_text().split()[0] == hashlib.sha256(archive.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", PRIVATE)
def test_private_paths_are_not_release_inputs(name):
    assert not package_release.should_include(Path(name))


def test_symlinked_file_and_directory_are_not_followed(tmp_path, monkeypatch):
    root, external = tmp_path / "project", tmp_path / "private"
    populate(root, PUBLIC)
    populate(external, {"secret.py": "PRIVATE_SENTINEL"})
    try:
        (root / "scripts/linked.py").symlink_to(external / "secret.py")
        (root / "galgame2voice/linked").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit creating symbolic links")
    monkeypatch.setattr(package_release, "PROJECT_ROOT", root)
    archive = package_release.build_release_zip("test-link")
    with zipfile.ZipFile(archive) as bundle:
        assert not any("linked" in name for name in bundle.namelist())


def test_failed_rebuild_preserves_previous_archive(tmp_path, monkeypatch):
    populate(tmp_path, PUBLIC)
    monkeypatch.setattr(package_release, "PROJECT_ROOT", tmp_path)
    archive = package_release.build_release_zip("test-atomic")
    original = archive.read_bytes()
    checksum = archive.with_suffix(".zip.sha256").read_bytes()

    def failed_write(*args, **kwargs):
        raise OSError("synthetic interrupted build")

    monkeypatch.setattr(zipfile.ZipFile, "write", failed_write)
    with pytest.raises(OSError):
        package_release.build_release_zip("test-atomic")
    assert archive.read_bytes() == original
    assert archive.with_suffix(".zip.sha256").read_bytes() == checksum


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_windows_junction_is_not_followed(tmp_path, monkeypatch):
    import _winapi

    root, external = tmp_path / "project", tmp_path / "private"
    populate(root, PUBLIC)
    populate(external, {"secret.py": "PRIVATE_SENTINEL"})
    junction = root / "galgame2voice/linked"
    try:
        _winapi.CreateJunction(str(external), str(junction))
    except (OSError, AttributeError):
        pytest.skip("Host does not permit creating Windows junctions")
    try:
        monkeypatch.setattr(package_release, "PROJECT_ROOT", root)
        archive = package_release.build_release_zip("test-junction")
        with zipfile.ZipFile(archive) as bundle:
            assert not any("linked" in name for name in bundle.namelist())
    finally:
        assert junction.resolve() == external.resolve()
        junction.rmdir()


@pytest.mark.parametrize("version", ["../outside", "bad/name", "bad\\name", "", "."])
def test_invalid_version_cannot_select_an_output_path(tmp_path, monkeypatch, version):
    monkeypatch.setattr(package_release, "PROJECT_ROOT", tmp_path)
    with pytest.raises(ValueError):
        package_release.build_release_zip(version)
