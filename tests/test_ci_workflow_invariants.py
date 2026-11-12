"""GitHub Actions workflows keep the required checks, cache dependencies, and do not repeat headless browser runs."""

import fnmatch
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# Branch protection on main listens for these job names. The matrix expands
# `lint-and-test` into the two checks `lint-and-test (3.10)` and `lint-and-test (3.12)`.
REQUIRED_CI_JOBS = ("lint-and-test", "test-linux", "dependency-audit", "frontend-build", "docker-build")
UV_CACHE_WORKFLOWS = ("ci.yml", "portable-premerge.yml", "release.yml")
NODE_CACHE_WORKFLOWS = ("ci.yml", "release.yml")

HEADLESS_GLOB = "tests/test_*_browser.py"
HEADLESS_IGNORE = '--ignore-glob="tests/test_*_browser.py"'
PURE_PYTHON_BROWSER_TESTS = (
    "tests/test_browser_autostart.py",
    "tests/test_portable_browser_validation.py",
)
BUILD_PUSH_ACTION = "docker/build-push-action@471d1dc4e07e5cdedd4c2171150001c434f0b7a4"
FIXED_SLEEP = re.compile(r"(?<![A-Za-z0-9_])(?:sleep[ \t]+\d+|Start-Sleep)\b", re.IGNORECASE)
PYTEST_COMMAND = re.compile(r"(?<![\w-])pytest(?![\w-])")
UNCACHED_DOCKER_BUILD = re.compile(r"(?<![\w-])docker\s+build\s+\.(?:\s|$)")


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _workflow_paths() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def _iter_steps(workflow: dict):
    for job_name, job in workflow["jobs"].items():
        for index, step in enumerate(job.get("steps") or []):
            yield job_name, index, step


def _condition(step: dict) -> str:
    return " ".join(str(step.get("if", "")).split())


def _pytest_lines(script: str) -> list[str]:
    lines = []
    for raw in script.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if PYTEST_COMMAND.search(stripped):
            lines.append(stripped)
    return lines


def _uses_values(workflow: dict) -> list[str]:
    found = []
    for job in workflow["jobs"].values():
        if isinstance(job, dict) and "uses" in job:
            found.append(job["uses"])
        for step in (job or {}).get("steps") or []:
            if "uses" in step:
                found.append(step["uses"])
    return found


def test_ci_jobs_keep_the_branch_protection_check_names():
    jobs = _workflow("ci.yml")["jobs"]

    missing = [name for name in REQUIRED_CI_JOBS if name not in jobs]
    assert missing == []
    matrix = jobs["lint-and-test"]["strategy"]["matrix"]["python-version"]
    assert matrix == ["3.10", "3.12"]
    # Windows keeps a full Python run on 3.12; Linux keeps its own full run.
    assert jobs["lint-and-test"]["runs-on"] == "windows-latest"
    assert jobs["test-linux"]["runs-on"] == "ubuntu-latest"


def test_workflow_actions_are_pinned_to_full_commit_shas():
    for path in _workflow_paths():
        for uses in _uses_values(_workflow(path.name)):
            action, separator, ref = uses.rpartition("@")
            assert separator and action, f"{path.name} has no action pin: {uses}"
            assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{path.name} is not pinned to a commit: {uses}"


def test_frontend_tests_run_only_on_the_312_windows_leg():
    steps = _workflow("ci.yml")["jobs"]["lint-and-test"]["steps"]
    expected = "matrix.python-version == '3.12'"

    for name in ("Setup Node.js", "Run frontend tests"):
        matches = [step for step in steps if step.get("name") == name]
        assert len(matches) == 1, name
        assert _condition(matches[0]) == expected, name


def test_python_310_skips_headless_browser_files_only():
    for relative in PURE_PYTHON_BROWSER_TESTS:
        assert (REPO_ROOT / relative).is_file()
        assert not fnmatch.fnmatch(relative, HEADLESS_GLOB)
    headless = sorted((REPO_ROOT / "tests").glob("test_*_browser.py"))
    assert headless
    for path in headless:
        assert fnmatch.fnmatch(f"tests/{path.name}", HEADLESS_GLOB)

    job = _workflow("ci.yml")["jobs"]["lint-and-test"]
    matches = [
        item
        for item in job["steps"]
        if _condition(item) == "matrix.python-version != '3.12'" and _pytest_lines(item.get("run") or "")
    ]
    assert len(matches) == 1
    step = matches[0]
    lines = _pytest_lines(step["run"])
    assert lines
    assert all(HEADLESS_IGNORE in line for line in lines), lines
    for relative in PURE_PYTHON_BROWSER_TESTS:
        assert Path(relative).name not in step["run"]


def test_windows_312_and_linux_still_run_headless_browser_files():
    ci = _workflow("ci.yml")["jobs"]
    windows = next(
        item
        for item in ci["lint-and-test"]["steps"]
        if _condition(item) == "matrix.python-version == '3.12'" and _pytest_lines(item.get("run") or "")
    )
    linux_steps = [item for item in ci["test-linux"]["steps"] if _pytest_lines(item.get("run") or "")]
    assert linux_steps

    for step in (windows, *linux_steps):
        assert HEADLESS_IGNORE not in step["run"]
        assert "--ignore-glob" not in step["run"]


def test_setup_uv_enables_the_dependency_cache():
    missing = []
    for name in UV_CACHE_WORKFLOWS:
        for job_name, index, step in _iter_steps(_workflow(name)):
            if not str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
                continue
            if (step.get("with") or {}).get("enable-cache") is not True:
                missing.append(f"{name}:{job_name}:step{index}")
    assert missing == []


def test_setup_node_caches_the_frontend_lockfile():
    missing = []
    for name in NODE_CACHE_WORKFLOWS:
        for job_name, index, step in _iter_steps(_workflow(name)):
            if not str(step.get("uses", "")).startswith("actions/setup-node@"):
                continue
            options = step.get("with") or {}
            if options.get("cache") != "npm" or options.get("cache-dependency-path") != "frontend/package-lock.json":
                missing.append(f"{name}:{job_name}:step{index}")
    assert missing == []


def test_docker_build_keeps_the_dockerfile_check_and_uses_gha_cache():
    steps = _workflow("ci.yml")["jobs"]["docker-build"]["steps"]
    problems = []
    actions = [step for step in steps if str(step.get("uses", "")).startswith("docker/build-push-action@")]
    if len(actions) != 1:
        problems.append(f"expected one docker/build-push-action, found {len(actions)}")
    else:
        action = actions[0]
        if action["uses"] != BUILD_PUSH_ACTION:
            problems.append(f"unexpected build-push pin: {action['uses']}")
        options = action.get("with") or {}
        if options.get("cache-from") != "type=gha":
            problems.append(f"cache-from={options.get('cache-from')!r}")
        if options.get("cache-to") != "type=gha,mode=max":
            problems.append(f"cache-to={options.get('cache-to')!r}")
        if options.get("push") is not False:
            problems.append(f"push={options.get('push')!r}")
        if options.get("load") is not True:
            problems.append(f"load={options.get('load')!r}")
        # The docker exporter used by load: true cannot carry BuildKit's default attestation.
        if options.get("provenance") is not False:
            problems.append(f"provenance={options.get('provenance')!r}")
    if not any("docker build --check ." in (step.get("run") or "") for step in steps):
        problems.append("missing docker build --check .")
    for step in steps:
        for raw in (step.get("run") or "").splitlines():
            stripped = raw.strip()
            if stripped.startswith("#"):
                continue
            if UNCACHED_DOCKER_BUILD.search(stripped):
                problems.append(f"uncached docker build: {stripped}")
    assert problems == []


def test_workflow_run_scripts_do_not_use_a_fixed_sleep():
    offenders = []
    for path in _workflow_paths():
        for job_name, index, step in _iter_steps(_workflow(path.name)):
            script = step.get("run")
            if not isinstance(script, str):
                continue
            if FIXED_SLEEP.search(script):
                offenders.append(f"{path.name}:{job_name}:step{index}")
    assert offenders == []
