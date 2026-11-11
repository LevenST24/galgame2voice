"""The release workflow must refuse to build when version metadata, the tag and the changelog disagree."""

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SCRIPT = REPO_ROOT / "scripts" / "verify_release_version.py"
CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n## [3.2.0] - 2026-11-12\n\n## [3.1.2] - 2026-11-04\n"


def _verify(*args, **kwargs):
    # Imported per call so that a missing script fails each test instead of breaking collection.
    from scripts.verify_release_version import verify_release_version

    return verify_release_version(*args, **kwargs)


def _make_project(root: Path, *, pyproject="3.2.0", package_init="3.2.0", frontend="3.2.0", changelog=CHANGELOG) -> Path:
    (root / "galgame2voice").mkdir()
    (root / "frontend").mkdir()
    (root / "pyproject.toml").write_text(
        f'[build-system]\nrequires = ["setuptools"]\n\n[project]\nname = "demo"\nversion = "{pyproject}"\n',
        encoding="utf-8",
    )
    (root / "galgame2voice" / "__init__.py").write_text(f'__version__ = "{package_init}"\n', encoding="utf-8")
    (root / "frontend" / "package.json").write_text(json.dumps({"version": frontend}), encoding="utf-8")
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("pyproject", "package_init", "frontend"),
    [("3.2.1", "3.2.0", "3.2.0"), ("3.2.0", "3.1.2", "3.2.0"), ("3.2.0", "3.2.0", "3.1.2")],
)
def test_the_three_version_files_must_agree(tmp_path, pyproject, package_init, frontend):
    root = _make_project(tmp_path, pyproject=pyproject, package_init=package_init, frontend=frontend)

    with pytest.raises(ValueError, match="Version mismatch") as error:
        _verify(root)

    assert f"pyproject.toml={pyproject!r}" in str(error.value)
    assert f"galgame2voice/__init__.py={package_init!r}" in str(error.value)
    assert f"frontend/package.json={frontend!r}" in str(error.value)


def test_only_the_project_table_of_pyproject_counts(tmp_path):
    root = _make_project(tmp_path)
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        '[tool.first]\nversion = "0.0.1"\n\n' + pyproject.read_text(encoding="utf-8") + '\n[tool.last]\nversion = "9.9.9"\n',
        encoding="utf-8",
    )

    assert _verify(root) == "3.2.0"


@pytest.mark.parametrize("tag", ["v3.2.1", "3.2.0", "v3.2.0-rc1"])
def test_release_tag_must_be_v_plus_the_version(tmp_path, tag):
    root = _make_project(tmp_path)

    with pytest.raises(ValueError, match="Release tag mismatch"):
        _verify(root, release_tag=tag)


def test_release_needs_its_own_changelog_section(tmp_path):
    # "3.2.0-rc1" and "3.2.00" start like "3.2.0" but are other releases.
    changelog = "# Changelog\n\n## [Unreleased]\n\n## [3.2.0-rc1] - 2026-11-10\n\n## [3.2.00] - 2026-11-09\n"
    root = _make_project(tmp_path, changelog=changelog)

    with pytest.raises(ValueError, match=r"CHANGELOG\.md has no '## \[3\.2\.0\]' section"):
        _verify(root, release_tag="v3.2.0")
    # A branch run of the workflow has no tag, so only the version files are compared.
    assert _verify(root) == "3.2.0"


def test_matching_tag_passes_with_or_without_a_changelog_date(tmp_path):
    root = _make_project(tmp_path)
    assert _verify(root, release_tag="v3.2.0") == "3.2.0"

    (root / "CHANGELOG.md").write_text("## [Unreleased]\n\n## [3.2.0]\n", encoding="utf-8")
    assert _verify(root, release_tag="v3.2.0") == "3.2.0"


def test_command_line_fails_for_a_wrong_tag_and_passes_without_one():
    def run(*args):
        return subprocess.run([sys.executable, str(GATE_SCRIPT), *args], capture_output=True, text=True)

    untagged = run()
    assert untagged.returncode == 0, untagged.stderr
    assert "Release version verified" in untagged.stdout

    wrong_tag = run("--release-tag", "v0.0.0")
    assert wrong_tag.returncode != 0
    assert "Release tag mismatch" in wrong_tag.stderr


def _workflow(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _action_refs(workflow: dict) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                action, _, ref = step["uses"].partition("@")
                refs.setdefault(action, set()).add(ref)
    return refs


def _step_index(steps: list[dict], command: str) -> int:
    for index, step in enumerate(steps):
        if command in step.get("run", ""):
            return index
    raise AssertionError(f"no step runs {command!r}")


def test_release_workflow_pins_actions_to_the_commits_ci_uses():
    ci_refs = _action_refs(_workflow("ci.yml"))

    for action, refs in _action_refs(_workflow("release.yml")).items():
        assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs), f"{action} is not pinned to a commit: {refs}"
        if action in ci_refs:
            assert refs <= ci_refs[action], f"{action}: release.yml pins {refs}, ci.yml pins {ci_refs[action]}"


@pytest.mark.parametrize("job", ["build-release", "build-windows-portable"])
def test_release_jobs_verify_the_version_before_building_the_frontend(job):
    steps = _workflow("release.yml")["jobs"][job]["steps"]

    gate = _step_index(steps, "scripts/verify_release_version.py")

    assert gate < _step_index(steps, "npm run deploy")
    assert "--release-tag" in steps[gate]["run"]
    assert "github.ref_type" in str(steps[gate])
    assert "github.ref_name" in str(steps[gate])


def test_build_release_runs_the_frontend_tests_before_building_the_bundle():
    steps = _workflow("release.yml")["jobs"]["build-release"]["steps"]

    script = steps[_step_index(steps, "npm run deploy")]["run"]

    assert "npm test" in script
    assert script.index("npm ci") < script.index("npm test") < script.index("npm run deploy")
