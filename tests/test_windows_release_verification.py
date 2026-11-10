"""A release must match the built UI and must not ship installed user data."""

import hashlib
import zipfile

import pytest

from scripts.verify_windows_portable import PREFIX, verify_archive


@pytest.fixture
def release(tmp_path):
    files = {name: b"fixture" for name in (
        "Galgame2Voice.exe", "启动.bat", "数据恢复.bat", "tools/ffmpeg.exe", "LICENSE", "_internal/python312.dll",
    )}
    files.update({f"{folder}/.keep": b"" for folder in ("data", "audio", "logs", "characters")})
    for relative in ("index.html", "assets/app.js", "assets/app.css"):
        content = relative.encode()
        deployed = tmp_path / "galgame2voice/static" / relative
        deployed.parent.mkdir(parents=True, exist_ok=True)
        deployed.write_bytes(content)
        built = tmp_path / "frontend/dist" / relative
        built.parent.mkdir(parents=True, exist_ok=True)
        built.write_bytes(content)
        files["galgame2voice/static/" + relative] = content
    guide = tmp_path / "docs/WINDOWS_PORTABLE.md"
    guide.parent.mkdir()
    guide.write_bytes(b"public guide")
    files["使用说明.md"] = guide.read_bytes()
    archive = tmp_path / "portable.zip"

    def write(extra=None):
        with zipfile.ZipFile(archive, "w") as bundle:
            for name, content in {**files, **(extra or {})}.items():
                bundle.writestr(PREFIX + name, content)
        archive.with_suffix(".zip.sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
        return archive

    return tmp_path, write


def test_current_release_matches_and_keeps_legacy_player(release):
    root, write = release
    result = verify_archive(write(), root)
    assert result["frontend_matches_zip_and_dist"] is True
    assert result["no_user_secrets_or_models"] is True


@pytest.mark.parametrize("name", [".ENV", "data/.MASTER_KEY", "logs/user-note.txt", "characters/model.ckpt", "../outside"])
def test_rejects_runtime_data_secrets_models_and_unsafe_paths(release, name):
    root, write = release
    with pytest.raises(ValueError):
        verify_archive(write({name: b"private fixture"}), root)


def test_rejects_stale_hashed_frontend_in_zip(release):
    root, write = release
    with pytest.raises(ValueError, match="assets differ"):
        verify_archive(write({"galgame2voice/static/assets/old.js": b"old build"}), root)


def test_rejects_changed_frontend_content_even_with_valid_checksum(release):
    root, write = release
    with pytest.raises(ValueError, match="content differs"):
        verify_archive(write({"galgame2voice/static/assets/app.js": b"wrong build"}), root)


def test_rejects_deployed_build_drift(release):
    root, write = release
    archive = write()
    (root / "frontend/dist/index.html").write_bytes(b"new build not deployed")
    with pytest.raises(ValueError, match="Vite build"):
        verify_archive(archive, root)


def test_rejects_invalid_checksum(release):
    root, write = release
    archive = write()
    archive.with_suffix(".zip.sha256").write_text("0" * 64)
    with pytest.raises(ValueError, match="checksum"):
        verify_archive(archive, root)


def test_rejects_case_collisions_on_windows(release):
    root, write = release
    with pytest.raises(ValueError, match="Duplicate"):
        verify_archive(write({"LICENSE": b"license", "license": b"ambiguous"}), root)


def test_verifies_static_assets_without_vite_dist_when_opted_out(tmp_path):
    project_root = tmp_path / "repo"
    static = project_root / "galgame2voice" / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_bytes(b"<html>portable</html>")
    (static / "assets" / "app.js").write_bytes(b"console.log('portable');")
    guide = project_root / "docs" / "WINDOWS_PORTABLE.md"
    guide.parent.mkdir(parents=True)
    guide.write_bytes(b"portable guide")
    assert not (project_root / "frontend" / "dist").exists()

    base_files = {
        "Galgame2Voice.exe": b"exe",
        "启动.bat": b"bat",
        "数据恢复.bat": b"recover",
        "tools/ffmpeg.exe": b"ffmpeg",
        "LICENSE": b"mit",
        "_internal/python312.dll": b"py312",
        "使用说明.md": guide.read_bytes(),
        "galgame2voice/static/index.html": (static / "index.html").read_bytes(),
        "galgame2voice/static/assets/app.js": (static / "assets" / "app.js").read_bytes(),
        **{f"{folder}/.keep": b"" for folder in ("data", "audio", "logs", "characters")},
    }

    def build_zip(filename: str, overrides: dict[str, bytes] | None = None):
        target = tmp_path / filename
        merged = {**base_files, **(overrides or {})}
        with zipfile.ZipFile(target, "w") as bundle:
            for rel, payload in merged.items():
                bundle.writestr(PREFIX + rel, payload)
        target.with_suffix(".zip.sha256").write_text(
            hashlib.sha256(target.read_bytes()).hexdigest(), encoding="utf-8"
        )
        return target

    archive = build_zip("valid.zip")
    with pytest.raises(FileNotFoundError):
        verify_archive(archive, project_root)

    result = verify_archive(archive, project_root, require_vite_dist=False)
    assert result["frontend_matches_zip_and_dist"] is False
    assert result["frontend_matches_deployed_static"] is True
    assert result["no_user_secrets_or_models"] is True

    tampered = build_zip("tampered.zip", {"galgame2voice/static/assets/app.js": b"tampered();"})
    with pytest.raises(ValueError, match=r"ZIP frontend content differs: assets/app\.js"):
        verify_archive(tampered, project_root, require_vite_dist=False)


@pytest.mark.parametrize("placeholder_entry", ["_internal/placeholder.txt", "_internal/python3.dll"])
def test_rejects_missing_python_runtime_dll(release, placeholder_entry):
    root, write = release
    archive = write()
    rewritten = root / "missing_python_dll.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(rewritten, "w") as target:
        for item in source.infolist():
            if item.filename == PREFIX + "_internal/python312.dll":
                continue
            target.writestr(item.filename, source.read(item.filename))
        target.writestr(PREFIX + placeholder_entry, b"placeholder")
    rewritten.with_suffix(".zip.sha256").write_text(
        hashlib.sha256(rewritten.read_bytes()).hexdigest(), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Bundled Python runtime DLL is missing"):
        verify_archive(rewritten, root)
    with pytest.raises(ValueError, match="Bundled Python runtime DLL is missing"):
        verify_archive(rewritten, root, require_vite_dist=False)


@pytest.mark.parametrize("missing_name", ["Galgame2Voice.exe", "启动.bat", "数据恢复.bat", "tools/ffmpeg.exe"])
def test_rejects_missing_required_portable_files(release, missing_name):
    root, write = release
    archive = write()
    incomplete = root / "incomplete.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(incomplete, "w") as target:
        for item in source.infolist():
            if item.filename == PREFIX + missing_name:
                continue
            target.writestr(item.filename, source.read(item.filename))
    incomplete.with_suffix(".zip.sha256").write_text(
        hashlib.sha256(incomplete.read_bytes()).hexdigest(), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        verify_archive(incomplete, root, require_vite_dist=False)
