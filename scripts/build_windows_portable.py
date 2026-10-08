"""Build a Windows x64 ZIP containing the interpreter, dependencies and FFmpeg."""

import hashlib
import importlib.metadata
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from scripts.package_release import read_project_version  # noqa: E402


def copy_runtime_licenses(destination: Path) -> None:
    """Carry the licenses supplied by the build environment alongside binaries."""
    destination.mkdir(parents=True, exist_ok=True)
    for distribution in importlib.metadata.distributions():
        name = re.sub(r"[^a-zA-Z0-9_.-]", "_", distribution.metadata.get("Name", "package"))
        for relative in distribution.files or ():
            if "license" not in str(relative).lower() and "copying" not in relative.name.lower():
                continue
            source = Path(distribution.locate_file(relative))
            if source.is_file():
                folder = destination / f"{name}-{distribution.version}"
                folder.mkdir(exist_ok=True)
                shutil.copy2(source, folder / relative.name)
    for source in Path(sys.base_prefix).glob("LICENSE*"):
        if source.is_file():
            shutil.copy2(source, destination / f"Python-{source.name}")


def assemble_portable_files(folder: Path, ffmpeg_binary: Path) -> None:
    """Only copy distributable application assets; never take user runtime data."""
    static_source = PROJECT_ROOT / "galgame2voice" / "static"
    static_target = folder / "galgame2voice" / "static"
    static_target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(static_source / "index.html", static_target / "index.html")
    for name in ("assets", "js"):
        if (static_source / name).is_dir():
            shutil.copytree(static_source / name, static_target / name, dirs_exist_ok=True)
    for name in ("LICENSE", "SECURITY.md", ".env.example"):
        shutil.copy2(PROJECT_ROOT / name, folder / name)
    shutil.copy2(PROJECT_ROOT / "docs" / "WINDOWS_PORTABLE.md", folder / "使用说明.md")
    for name in ("data", "audio", "logs", "characters", "tools"):
        (folder / name).mkdir(exist_ok=True)
    shutil.copy2(ffmpeg_binary, folder / "tools" / "ffmpeg.exe")
    (folder / "启动.bat").write_text(
        '@echo off\nchcp 65001 >nul 2>&1\ncd /d "%~dp0"\n'
        '"%~dp0Galgame2Voice.exe" %*\nif errorlevel 1 pause\n',
        encoding="utf-8", newline="\r\n",
    )
    (folder / "数据恢复.bat").write_text(
        '@echo off\nchcp 65001 >nul 2>&1\ncd /d "%~dp0"\n'
        '"%~dp0Galgame2Voice.exe" --restore-database\npause\n',
        encoding="utf-8", newline="\r\n",
    )


def build_portable() -> Path:
    if sys.platform != "win32" or sys.maxsize <= 2**32:
        raise RuntimeError("Build the Windows portable package with 64-bit Python on Windows")
    import imageio_ffmpeg
    from imageio_ffmpeg import binaries
    ffmpeg_candidates = list(Path(binaries.__file__).parent.glob("ffmpeg*.exe"))
    if len(ffmpeg_candidates) != 1:
        raise RuntimeError("The locked imageio-ffmpeg Windows wheel must supply exactly one FFmpeg binary")

    staging = (PROJECT_ROOT / "build" / "portable").resolve()
    staging.relative_to((PROJECT_ROOT / "build").resolve())
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--console",
        "--name", "Galgame2Voice", "--distpath", str(staging / "app"),
        "--workpath", str(staging / "work"), "--specpath", str(staging / "spec"),
        "--paths", str(PROJECT_ROOT), "--collect-submodules", "galgame2voice",
        "--collect-submodules", "uvicorn", "--hidden-import", "scripts.run_server",
        "--hidden-import", "tkinter", "--hidden-import", "anyio._backends._asyncio",
        "--exclude-module", "pytest", "--exclude-module", "torch",
        str(PROJECT_ROOT / "scripts" / "desktop_entry.py"),
    ]
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    folder = staging / "app" / "Galgame2Voice"
    assemble_portable_files(folder, ffmpeg_candidates[0])
    copy_runtime_licenses(folder / "THIRD_PARTY_LICENSES")
    for source in (PROJECT_ROOT / "docs" / "licenses").glob("FFmpeg-*.txt"):
        shutil.copy2(source, folder / "THIRD_PARTY_LICENSES" / source.name)
    for argument, name in (("-version", "FFmpeg-build.txt"), ("-L", "FFmpeg-notice.txt")):
        result = subprocess.run(
            [str(ffmpeg_candidates[0]), argument], check=True, capture_output=True, timeout=15,
        )
        (folder / "THIRD_PARTY_LICENSES" / name).write_bytes(result.stdout + result.stderr)
    for source in Path(binaries.__file__).parent.glob("*.md"):
        shutil.copy2(source, folder / "THIRD_PARTY_LICENSES" / f"FFmpeg-{source.name}")
    (folder / "THIRD_PARTY_LICENSES" / "FFmpeg-source.txt").write_text(
        f"Bundled imageio-ffmpeg version: {imageio_ffmpeg.__version__}\n"
        f"FFmpeg binary: {ffmpeg_candidates[0].name}\n"
        "Binary/build provenance: https://github.com/imageio/imageio-binaries/tree/master/ffmpeg\n"
        "FFmpeg source and license: https://ffmpeg.org/download.html and https://ffmpeg.org/legal.html\n"
        "This is a separate executable invoked by the application.\n", encoding="utf-8",
    )

    output = PROJECT_ROOT / "dist"
    output.mkdir(exist_ok=True)
    archive = output / f"galgame2voice-v{read_project_version()}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for root, directories, filenames in os.walk(folder):
            directories.sort()
            for filename in sorted(filenames):
                source = Path(root) / filename
                bundle.write(source, Path("Galgame2Voice") / source.relative_to(folder))
        for name in ("data", "audio", "logs", "characters"):
            bundle.writestr(f"Galgame2Voice/{name}/.keep", "")
    with archive.open("rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        checksum = digest.hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")
    print(f"Windows portable archive: {archive}")
    return archive


if __name__ == "__main__":
    build_portable()
