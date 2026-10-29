"""Portable payload checks prevent accidentally bundling FFmpeg twice."""

import hashlib
from pathlib import Path
import zipfile

import pytest

from scripts.verify_windows_portable import verify_archive


def make_portable(root: Path, *, duplicate: bool) -> Path:
    static = root / "galgame2voice/static"
    built = root / "frontend/dist"
    for folder in (static / "assets", built / "assets", root / "docs"):
        folder.mkdir(parents=True)
    for folder in (static, built):
        (folder / "index.html").write_text("<html>public app</html>")
        (folder / "assets/app.js").write_text("// public bundle")
    (root / "docs/WINDOWS_PORTABLE.md").write_text("portable guide")
    files = {
        "Galgame2Voice.exe": b"synthetic executable",
        "tools/ffmpeg.exe": b"synthetic ffmpeg",
        "_internal/python312.dll": b"synthetic runtime",
        "启动.bat": b"launcher",
        "数据恢复.bat": b"recovery",
        "LICENSE": b"public license",
        "使用说明.md": b"portable guide",
        "galgame2voice/static/index.html": (static / "index.html").read_bytes(),
        "galgame2voice/static/assets/app.js": (static / "assets/app.js").read_bytes(),
    }
    if duplicate:
        files["_internal/imageio_ffmpeg/binaries/ffmpeg-win64.exe"] = files["tools/ffmpeg.exe"]
    files.update({f"{folder}/.keep": b"" for folder in ("data", "audio", "logs", "characters")})
    archive = root / "portable.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, content in files.items():
            bundle.writestr("Galgame2Voice/" + name, content)
    archive.with_suffix(".zip.sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
    return archive


def test_portable_payload_keeps_the_required_ffmpeg(tmp_path):
    archive = make_portable(tmp_path, duplicate=False)
    assert verify_archive(archive, tmp_path)["no_user_secrets_or_models"]


def test_portable_payload_rejects_a_second_ffmpeg_binary(tmp_path):
    archive = make_portable(tmp_path, duplicate=True)
    with pytest.raises(ValueError, match="FFmpeg"):
        verify_archive(archive, tmp_path)
