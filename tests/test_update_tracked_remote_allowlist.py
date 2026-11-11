"""The update allowlist must check the remote that is actually fetched and pulled.

A branch can track a remote other than ``origin``. The update check and the
one-click update both fetch from the branch's upstream remote, so an allowed
``origin`` must not let them pull from an untrusted remote.
"""

import subprocess
from pathlib import Path

import pytest

from galgame2voice.routers.system import _apply_update_sync, _check_version_sync

ALLOWED_ORIGIN = "https://github.com/LevenST24/galgame2voice.git"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo_tracking_untrusted_remote(tmp_path, monkeypatch):
    monkeypatch.delenv("GALGAME2VOICE_UPDATE_ALLOWED_REMOTES", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q", "-b", "main")
    (project / "README.md").write_text("local\n", encoding="utf-8")
    _git(project, "add", "README.md")
    _git(project, "commit", "-q", "-m", "local")

    untrusted = tmp_path / "untrusted.git"
    _git(tmp_path, "clone", "-q", "--bare", str(project), str(untrusted))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(untrusted), str(work))
    (work / "README.md").write_text("untrusted\n", encoding="utf-8")
    _git(work, "commit", "-q", "-am", "untrusted change")
    _git(work, "push", "-q", "origin", "main")

    _git(project, "remote", "add", "origin", ALLOWED_ORIGIN)
    _git(project, "remote", "add", "fork", str(untrusted))
    _git(project, "fetch", "-q", "fork")
    _git(project, "branch", "-q", "--set-upstream-to", "fork/main")
    return project


def test_apply_update_refuses_untrusted_upstream_remote(repo_tracking_untrusted_remote, tmp_path):
    project = repo_tracking_untrusted_remote
    head_before = _git(project, "rev-parse", "HEAD")

    result, changed = _apply_update_sync(project, data_dir=tmp_path / "data")

    assert result.success is False
    assert "fork" in (result.error or "")
    assert changed == []
    assert _git(project, "rev-parse", "HEAD") == head_before
    assert (project / "README.md").read_text(encoding="utf-8") == "local\n"


def test_update_check_skips_untrusted_upstream_remote(repo_tracking_untrusted_remote, tmp_path):
    project = repo_tracking_untrusted_remote

    result = _check_version_sync(project, check_remote=True, data_dir=tmp_path / "data")

    assert result.has_update is False
    assert result.behind_count == 0
    assert "fork" in (result.error or "")
