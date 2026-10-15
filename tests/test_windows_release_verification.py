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
    for relative in ("index.html", "assets/app.js", "assets/app.css", "js/audio_player.js"):
        content = relative.encode()
        deployed = tmp_path / "galgame2voice/static" / relative
        deployed.parent.mkdir(parents=True, exist_ok=True)
        deployed.write_bytes(content)
        if not relative.startswith("js/"):
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
