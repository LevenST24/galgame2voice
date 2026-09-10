"""
Galgame2Voice Release Packaging Script.
Bundles the application into a clean, distributable ZIP package ready for distribution or GitHub Releases.
Excludes local environment data, databases, caches, and sensitive files.
"""

import os
import sys
import zipfile
import hashlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Files and directories strictly excluded from release bundles
EXCLUDE_DIRS = {
    ".git",
    ".github",
    ".venv",
    "venv",
    "ENV",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "temp_cache_mem",
    "dist",
    "build",
    "galgame2voice.egg-info",
}

EXCLUDE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".db",
    ".db-journal",
    ".db-wal",
    ".db-shm",
    ".log",
    ".pid",
}

EXCLUDE_EXACT_FILES = {
    ".env",
    "galgame2voice.db",
    "galgame2voice.pid",
    "gptsovits.pid",
}


def should_include(rel_path: Path) -> bool:
    parts = rel_path.parts
    # Check directory exclusions
    for p in parts[:-1]:
        if p in EXCLUDE_DIRS:
            return False
    filename = parts[-1]
    if filename in EXCLUDE_DIRS or filename in EXCLUDE_EXACT_FILES:
        return False
    if any(filename.endswith(ext) for ext in EXCLUDE_EXTENSIONS):
        return False

    # Don't bundle frontend source node_modules if inside frontend
    if "node_modules" in parts:
        return False

    # Exclude dynamic runtime files inside data/
    if len(parts) >= 2 and parts[0] == "data":
        if filename.endswith(".db") or filename.endswith(".txt"):
            return False

    # Exclude dynamic runtime files inside logs/ and audio/
    if len(parts) >= 2:
        if parts[0] == "logs":
            return False
        if parts[0] == "audio":
            # Exclude dynamic generated wav files and caches, but preserve bundled character reference audios
            if "references" in parts or filename.endswith(".ogg") or filename.endswith(".keep"):
                return True
            return False

    # Ensure characters/ folder is bundled with manifests, prompts, and reference audios,
    # while excluding large .ckpt / .pth binary model weights.
    if len(parts) >= 2 and parts[0] == "characters":
        if filename.endswith(".ckpt") or filename.endswith(".pth"):
            abs_p = PROJECT_ROOT / rel_path
            try:
                if abs_p.is_file() and abs_p.stat().st_size > 1024 * 1024:
                    return False
                elif not abs_p.is_file():
                    return False
            except Exception:
                return False
        return True

    return True


def build_release_zip(version: str = "2.0.0") -> Path:
    static_dir = PROJECT_ROOT / "galgame2voice" / "static"
    if not (static_dir / "index.html").exists():
        print("[ERROR] galgame2voice/static/index.html is missing! Build the frontend first.")
        sys.exit(1)

    dist_dir = PROJECT_ROOT / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    zip_filename = dist_dir / f"galgame2voice-v{version}.zip"
    print(f"[1/3] Packaging project files into {zip_filename.name}...")

    total_files = 0
    with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zf:
        # Walk all project files
        for root, dirs, files in os.walk(PROJECT_ROOT):
            # Prune excluded dirs in place
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

            for file in files:
                abs_path = Path(root) / file
                rel_path = abs_path.relative_to(PROJECT_ROOT)

                if should_include(rel_path):
                    # Write file into zip under top-level 'galgame2voice' folder for clean extraction
                    archive_name = Path(f"galgame2voice-v{version}") / rel_path
                    zf.write(abs_path, str(archive_name))
                    total_files += 1

        # Add empty placeholder runtime directories
        for empty_dir in ["logs", "data", "audio", "characters"]:
            zf.writestr(f"galgame2voice-v{version}/{empty_dir}/.keep", "")

    file_size_mb = zip_filename.stat().st_size / (1024 * 1024)
    print(f"      [OK] Packaged {total_files} files ({file_size_mb:.2f} MB)")

    print("[2/3] Calculating SHA-256 checksum...")
    sha256 = hashlib.sha256(zip_filename.read_bytes()).hexdigest()
    checksum_file = zip_filename.with_suffix(".zip.sha256")
    checksum_file.write_text(f"{sha256}  {zip_filename.name}\n", encoding="utf-8")
    print(f"      [OK] SHA-256: {sha256}")

    print("[3/3] Release package ready!")
    print(f"      Archive:  {zip_filename}")
    print(f"      Checksum: {checksum_file}")
    return zip_filename


if __name__ == "__main__":
    v = "2.0.0"
    if len(sys.argv) > 1:
        v = sys.argv[1].lstrip("v")
    build_release_zip(v)
