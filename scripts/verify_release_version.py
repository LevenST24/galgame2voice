"""Fail a release build whose version metadata, Git tag and CHANGELOG.md disagree."""

import argparse
import json
from pathlib import Path
import re

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _find(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else None


def verify_release_version(project_root: Path = PROJECT_ROOT, *, release_tag: str | None = None) -> str:
    """Return the version shared by pyproject.toml, the package and the frontend, or raise ValueError.

    With a release tag, the tag must be ``v<version>`` and CHANGELOG.md must have a ``## [<version>]`` section.
    """
    pyproject = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    package_init = (project_root / "galgame2voice" / "__init__.py").read_text(encoding="utf-8")
    package_json = json.loads((project_root / "frontend" / "package.json").read_text(encoding="utf-8"))

    # tomllib is missing on Python 3.10; only the [project] table counts, other tables may have a `version` too.
    project_table = _find(r"^\[project\]\n(.*?)(?=^\[|\Z)", pyproject) or ""
    versions = {
        "pyproject.toml": _find(r'^version\s*=\s*"([^"]+)"', project_table),
        "galgame2voice/__init__.py": _find(r'^__version__\s*=\s*"([^"]+)"', package_init),
        "frontend/package.json": package_json.get("version"),
    }
    version = versions["pyproject.toml"]
    if not version or set(versions.values()) != {version}:
        found = ", ".join(f"{name}={value!r}" for name, value in versions.items())
        raise ValueError(f"Version mismatch: {found}")

    if release_tag:
        if release_tag != f"v{version}":
            raise ValueError(f"Release tag mismatch: expected 'v{version}', got {release_tag!r}")
        changelog = (project_root / "CHANGELOG.md").read_text(encoding="utf-8")
        if not re.search(rf"^## \[{re.escape(version)}\]", changelog, re.MULTILINE):
            raise ValueError(f"CHANGELOG.md has no '## [{version}]' section")
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tag", help="Git tag being released, for example v3.1.3")
    args = parser.parse_args()
    print(f"Release version verified: {verify_release_version(release_tag=args.release_tag)}")
