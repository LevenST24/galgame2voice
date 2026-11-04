"""
Contract and regression tests for update preservation:
1. Untracked custom static files/directories are preserved and not deleted by default update.
2. Pre-update backup captures full contents of untracked directories/files (including non-ASCII and spaces).
3. Backup failure aborts destructive actions (both discard_local_changes and default artifact cleanup).
4. Rebuildable artifacts are safely auto-restored when backup succeeds.
5. Frozen portable message includes backgrounds and .env.
"""

import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from galgame2voice.routers.system import (
    _handle_uncommitted_modifications,
    _verify_update_preflight,
)


def _init_synthetic_repo(repo_dir: Path) -> Path:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/LevenST24/galgame2voice.git"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    # create initial tracked files
    static_dir = repo_dir / "galgame2voice" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "index.html").write_text("<html>initial</html>", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True, capture_output=True)
    return repo_dir


def test_default_update_preserves_unknown_static_files(tmp_path: Path):
    """
    Verify that an untracked custom directory under static/ (e.g. static/custom/theme.css)
    and non-ASCII files are NOT wiped by default update, abort with Chinese guidance,
    and backup zip contains the exact file bytes.
    """
    repo = _init_synthetic_repo(tmp_path / "repo")
    custom_dir = repo / "galgame2voice" / "static" / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    custom_file = custom_dir / "theme.css"
    custom_file.write_text("/* custom user theme */", encoding="utf-8")

    unicode_file = custom_dir / "测试 样式.css"
    unicode_file.write_text("/* unicode theme content */", encoding="utf-8")

    # Call _handle_uncommitted_modifications directly with default flags (discard=False, stash=False)
    res = _handle_uncommitted_modifications(
        project_root=repo,
        before_short="1234567",
        stash_changes=False,
        discard_local_changes=False,
    )

    # 1. Update must abort (return non-None failure response)
    assert res is not None, "Expected update to abort due to uncommitted unknown static files"
    assert res.success is False
    assert "galgame2voice/static/custom/theme.css" in res.output
    assert "中止默认更新" in res.output or "未提交" in res.output or "未确认" in res.output

    # 2. Crucial: The unknown custom files must NOT have been deleted from disk
    assert custom_file.exists(), "Untracked static/custom/theme.css must not be deleted by default update!"
    assert custom_file.read_text(encoding="utf-8") == "/* custom user theme */"
    assert unicode_file.exists(), "Untracked static/custom/测试 样式.css must not be deleted by default update!"
    assert unicode_file.read_text(encoding="utf-8") == "/* unicode theme content */"

    # 3. Pre-update backup must have archived the untracked files
    backup_dir = repo / "data" / "backups"
    zips = list(backup_dir.glob("pre_update_backup_*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0], "r") as zf:
        namelist = zf.namelist()
        norm_to_exact = {n.replace("\\", "/"): n for n in namelist}
        expected_theme = "galgame2voice/static/custom/theme.css"
        expected_unicode = "galgame2voice/static/custom/测试 样式.css"

        assert expected_theme in norm_to_exact, f"Missing {expected_theme} in zip namelist: {namelist}"
        assert expected_unicode in norm_to_exact, f"Missing {expected_unicode} in zip namelist: {namelist}"

        assert zf.read(norm_to_exact[expected_theme]) == b"/* custom user theme */"
        assert zf.read(norm_to_exact[expected_unicode]) == "/* unicode theme content */".encode("utf-8")


def test_backup_failure_aborts_destructive_discard(tmp_path: Path):
    """
    Verify that if pre-update backup fails, destructive discard_local_changes
    aborts immediately and does NOT execute git reset or delete files.
    """
    repo = _init_synthetic_repo(tmp_path / "repo")
    custom_dir = repo / "galgame2voice" / "static" / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    custom_file = custom_dir / "theme.css"
    custom_file.write_text("/* important custom theme */", encoding="utf-8")

    with patch("galgame2voice.routers.system._create_pre_update_backup", return_value=None), \
         patch("galgame2voice.routers.system._run_git_cmd") as mock_git:

        # Let status --porcelain return the file
        mock_git.return_value = (0, "?? galgame2voice/static/custom/theme.css\n", "")

        res = _handle_uncommitted_modifications(
            project_root=repo,
            before_short="1234567",
            stash_changes=False,
            discard_local_changes=True,
        )

        assert res is not None
        assert res.success is False
        assert res.error == "Pre-update backup failed"
        assert "安全备份创建失败" in res.output

        # Verify git reset was NOT called
        for call_args in mock_git.call_args_list:
            cmd = call_args[0][0]
            assert not ("reset" in cmd and "--hard" in cmd), "git reset --hard must not be called when backup fails!"


def test_backup_failure_aborts_default_artifact_cleanup(tmp_path: Path):
    """
    Rework RED test:
    When only tracked static/index.html is modified, but pre-update backup fails
    (e.g. disk full or unwritable backup dir, returning None), default update
    must FAIL CLOSED: abort without running checkout or clean, and preserve
    the modified bytes on disk.
    """
    repo = _init_synthetic_repo(tmp_path / "repo")
    static_file = repo / "galgame2voice" / "static" / "index.html"
    modified_text = "<html>locally customized index.html</html>"
    static_file.write_text(modified_text, encoding="utf-8")

    with patch("galgame2voice.routers.system._create_pre_update_backup", return_value=None):
        res = _handle_uncommitted_modifications(
            project_root=repo,
            before_short="1234567",
            stash_changes=False,
            discard_local_changes=False,
        )

        # 1. Must fail closed (abort)
        assert res is not None, "Expected default update to abort when backup fails"
        assert res.success is False
        assert res.error == "Pre-update backup failed"
        assert "安全备份创建失败" in res.output

        # 2. Must preserve original modified bytes on disk (not checked out back to initial)
        assert static_file.read_text(encoding="utf-8") == modified_text, (
            "Modified bytes of index.html were overwritten despite backup failure!"
        )


def test_rebuildable_artifacts_auto_restored(tmp_path: Path):
    """
    Verify that when only rebuildable build artifacts are modified/added and backup succeeds,
    the updater safely restores static and returns None (allowing pull to proceed).
    """
    repo = _init_synthetic_repo(tmp_path / "repo")
    static_dir = repo / "galgame2voice" / "static"
    # modify index.html
    (static_dir / "index.html").write_text("<html>rebuilt locally</html>", encoding="utf-8")
    # create new vite index artifact
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / "index-AbCd1234.js").write_text("// bundled js", encoding="utf-8")

    res = _handle_uncommitted_modifications(
        project_root=repo,
        before_short="1234567",
        stash_changes=False,
        discard_local_changes=False,
    )

    # Should be auto-restored so update can proceed
    assert res is None, "Only rebuildable build artifacts should be auto-restored without aborting"
    # index.html should be checked out back to initial commit
    assert (static_dir / "index.html").read_text(encoding="utf-8") == "<html>initial</html>"
    # untracked build artifact should be cleaned
    assert not (assets_dir / "index-AbCd1234.js").exists()


def test_system_frozen_message_includes_backgrounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    Verify that in frozen environment (Windows portable), the error message prompts
    migrating backgrounds (if exists) and .env.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _, resp = _verify_update_preflight(tmp_path)
    assert resp is not None
    assert resp.success is False
    assert "backgrounds（若存在）" in resp.output
    assert ".env" in resp.output
