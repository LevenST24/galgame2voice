"""Build a source release from explicit program paths, excluding all local runtime data."""

import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def read_project_version() -> str:
    """Single source of truth for the release version: pyproject.toml."""
    pyproject = PROJECT_ROOT / "pyproject.toml"
    try:
        import tomllib

        with open(pyproject, "rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except Exception:
        pass
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE)
    if match:
        return match.group(1)
    raise RuntimeError(f"Could not determine version from {pyproject}")


# Adding a new distributable root/file type requires an explicit decision here.
# Assets, saved configuration, engine caches and workspace tooling are not inputs.
ROOT_FILES = {
    "README.md", "使用说明.md", "LICENSE", "SECURITY.md", "CHANGELOG.md", "AGENTS.md",
    "pyproject.toml", "uv.lock", "requirements.txt", "requirements-dev.txt", "pytest.ini",
    "启动.bat", "run.sh", "Dockerfile", "docker-compose.yml",
    ".env.example", ".dockerignore", ".gitignore", ".gitattributes",
}
SOURCE_ROOTS = {"galgame2voice", "frontend", "scripts", "docs", "tests"}
EXCLUDE_DIRS = {
    "data", "audio", "logs", "characters", "backgrounds", "skins", "models",
    "node_modules", "__pycache__", "dist", "build", "venv", "env", "ENV",
    "temp_audio", "temp_cache_mem", "temp_test", "tmp_check",
}
PUBLIC_ASSET_TYPES = {".js", ".css", ".svg", ".woff", ".woff2"}
CODE_TYPES = {".py", ".js", ".mjs", ".css", ".html", ".md"}


def should_include(rel_path: Path) -> bool:
    """Pure manifest decision; never inspect model sizes or follow filesystem links."""
    parts = rel_path.parts
    if not parts or rel_path.is_absolute() or rel_path.drive or ".." in parts:
        return False
    if len(parts) == 1:
        return parts[0] in ROOT_FILES
    if parts[0] not in SOURCE_ROOTS:
        return False
    if any(part.startswith(".") or part in EXCLUDE_DIRS
           or part.startswith(("pytest-of-", "g2v_cap_")) for part in parts):
        return False
    filename = parts[-1]
    if filename.startswith(("_probe_", "temp_", "debug_", "scratch_")):
        return False
    suffix = rel_path.suffix.lower()
    root = parts[0]
    if root == "galgame2voice":
        if len(parts) > 2 and parts[1] == "static":
            return (parts[2:] == ("index.html",)
                    or (parts[2] == "assets" and suffix in PUBLIC_ASSET_TYPES))
        return suffix == ".py"
    if root == "scripts":
        return suffix == ".py" or parts[1:] == ("dev_standins_replies.json",)
    if root == "docs":
        return suffix == ".md" or (parts[1] == "licenses" and suffix == ".txt")
    if root == "frontend":
        return (suffix in CODE_TYPES
                or (len(parts) == 2 and filename in ("package.json", "package-lock.json"))
                or (parts[1] == "src" and suffix in PUBLIC_ASSET_TYPES))
    return suffix in {".py", ".js", ".mjs", ".json"}


def _is_link(path: Path) -> bool:
    """Junctions/reparse points must also be pruned on Windows."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _iter_release_files():
    for root, dirs, files in os.walk(PROJECT_ROOT, followlinks=False):
        folder = Path(root)
        dirs[:] = sorted(name for name in dirs
                         if not name.startswith(".") and name not in EXCLUDE_DIRS
                         and not _is_link(folder / name)
                         and ((folder == PROJECT_ROOT and name in SOURCE_ROOTS)
                              or folder != PROJECT_ROOT))
        for filename in sorted(files):
            source = folder / filename
            relative = source.relative_to(PROJECT_ROOT)
            if should_include(relative) and not _is_link(source) and source.is_file():
                yield source, relative


def build_release_zip(version: str | None = None) -> Path:
    """Write a complete archive and checksum before replacing an earlier build."""
    version = read_project_version() if version is None else version
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise ValueError("Release version must be a filename-safe identifier")
    static_dir = PROJECT_ROOT / "galgame2voice" / "static"
    if not (static_dir / "index.html").is_file():
        raise RuntimeError("galgame2voice/static/index.html is missing; build the frontend first")

    dist_dir = PROJECT_ROOT / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    archive = dist_dir / f"galgame2voice-v{version}.zip"
    checksum = archive.with_suffix(".zip.sha256")
    with tempfile.NamedTemporaryFile(dir=dist_dir, prefix=".g2v-", suffix=".zip", delete=False) as handle:
        pending = Path(handle.name)
    pending_checksum = pending.with_suffix(".sha256")
    total = 0
    included = set()
    try:
        with zipfile.ZipFile(pending, "w", zipfile.ZIP_DEFLATED) as bundle:
            for source, relative in _iter_release_files():
                bundle.write(source, f"galgame2voice-v{version}/{relative.as_posix()}")
                included.add(relative.as_posix())
                total += 1
            if "galgame2voice/static/index.html" not in included:
                raise RuntimeError("Frontend entrypoint must be a regular file inside the project")
            for name in ("logs", "data", "audio", "characters"):
                bundle.writestr(f"galgame2voice-v{version}/{name}/.keep", "")
        digest = hashlib.sha256()
        with pending.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        pending_checksum.write_text(f"{digest.hexdigest()}  {archive.name}\n", encoding="utf-8")
        pending.replace(archive)
        pending_checksum.replace(checksum)
    finally:
        pending.unlink(missing_ok=True)
        pending_checksum.unlink(missing_ok=True)
    print(f"Source release: {archive} ({total} files, {archive.stat().st_size / 1048576:.2f} MB)")
    print(f"Checksum: {checksum}")
    return archive


if __name__ == "__main__":
    build_release_zip(sys.argv[1].lstrip("v") if len(sys.argv) > 1 else None)
