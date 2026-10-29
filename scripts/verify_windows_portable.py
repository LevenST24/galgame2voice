"""Verify a Windows release checksum, shipped assets and absence of user data."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PREFIX = "Galgame2Voice/"


def verify_archive(archive: Path, project_root: Path = PROJECT_ROOT) -> dict:
    """Check the final ZIP without executing it or reading installed user data."""
    digest = hashlib.sha256()
    with archive.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    checksum = digest.hexdigest()
    declared = archive.with_suffix(".zip.sha256").read_text(encoding="utf-8").split()
    if not declared or declared[0].lower() != checksum:
        raise ValueError("Release checksum does not match the ZIP")

    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.namelist()
        folded = [name.casefold() for name in entries]
        if len(folded) != len(set(folded)):
            raise ValueError("Duplicate Windows ZIP entries")
        for name in entries:
            relative = PurePosixPath(name)
            if not name.startswith(PREFIX) or "\\" in name or ":" in name or ".." in relative.parts:
                raise ValueError(f"Unsafe release path: {name}")
            if relative.name.casefold() in {".env", ".master_key", ".console_token"} or relative.suffix.lower() in {".db", ".ckpt", ".pth"}:
                raise ValueError(f"User data or model included in release: {name}")
        files = {info.filename for info in bundle.infolist() if not info.is_dir()}
        ffmpeg_binaries = {name for name in files if PurePosixPath(name).name.lower().startswith("ffmpeg")
                           and PurePosixPath(name).suffix.lower() == ".exe"}
        if ffmpeg_binaries != {PREFIX + "tools/ffmpeg.exe"}:
            raise ValueError("FFmpeg must be bundled exactly once, at tools/ffmpeg.exe")
        required = {"Galgame2Voice.exe", "启动.bat", "数据恢复.bat", "tools/ffmpeg.exe", "LICENSE", "使用说明.md"}
        if missing := {PREFIX + name for name in required} - files:
            raise ValueError(f"Required portable files missing: {sorted(missing)}")
        if not any(name.startswith(PREFIX + "_internal/") for name in files):
            raise ValueError("Bundled runtime is missing")
        for folder in ("data", "audio", "logs", "characters"):
            actual = {name for name in files if name.casefold().startswith(f"{PREFIX}{folder}/".casefold())}
            if actual != {f"{PREFIX}{folder}/.keep"}:
                raise ValueError(f"Release {folder}/ must contain only .keep")

        static = project_root / "galgame2voice" / "static"
        built = project_root / "frontend" / "dist"
        expected = {"index.html"}
        expected.update(path.relative_to(static).as_posix() for path in (static / "assets").rglob("*") if path.is_file())
        shipped = {name.removeprefix(PREFIX + "galgame2voice/static/") for name in files
                   if name.startswith(PREFIX + "galgame2voice/static/")}
        if shipped != expected:
            raise ValueError("ZIP static assets differ from the deployed frontend")
        for relative in expected:
            content = (static / relative).read_bytes()
            if content != bundle.read(PREFIX + "galgame2voice/static/" + relative):
                raise ValueError(f"ZIP frontend content differs: {relative}")
            if content != (built / relative).read_bytes():
                raise ValueError(f"Deployed frontend differs from Vite build: {relative}")
        if bundle.read(PREFIX + "使用说明.md") != (project_root / "docs" / "WINDOWS_PORTABLE.md").read_bytes():
            raise ValueError("Portable guide differs from the current documentation")
    return {"archive": str(archive.resolve()), "bytes": archive.stat().st_size, "sha256": checksum,
            "frontend_matches_zip_and_dist": True, "empty_user_folders": True, "no_user_secrets_or_models": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify_archive(args.archive), ensure_ascii=False, indent=2))
