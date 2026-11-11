"""Pre-update backups belong to the configured data directory.

The one-click update zips uncommitted files before it stashes, discards or
restores them. The zip must go to the data directory the app was started
with (``settings.data_dir``), like the database snapshots, and not to a
hard-coded ``<project>/data`` folder.
"""

from pathlib import Path
from unittest.mock import patch

from galgame2voice.routers.system import _apply_update_sync


def _fake_git(args, cwd, timeout=30.0, env_overrides=None):
    if args[:2] == ["remote", "get-url"]:
        return 0, "https://github.com/LevenST24/galgame2voice.git", ""
    if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
        return 0, "true", ""
    if args[:2] == ["rev-parse", "--short"]:
        return 0, "1234567", ""
    if args == ["rev-parse", "--abbrev-ref", "@{u}"]:
        return 0, "origin/main", ""
    if args[:2] == ["rev-parse", "--abbrev-ref"]:
        return 0, "main", ""
    if args[0] == "status":
        return 0, " M notes.txt", ""
    if args == ["rev-parse", "HEAD"]:
        return 0, "1" * 40, ""
    if args[0] == "pull":
        return 0, "Already up to date.", ""
    return 0, "", ""


def test_pre_update_backup_goes_to_configured_data_dir(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_text("local edit\n", encoding="utf-8")
    data_dir = tmp_path / "custom-data"

    with patch("galgame2voice.routers.system._run_git_cmd", side_effect=_fake_git):
        result, _ = _apply_update_sync(project, stash_changes=True, data_dir=data_dir)

    assert result.success is True
    assert len(list((data_dir / "backups").glob("pre_update_backup_*.zip"))) == 1
    assert not (project / "data").exists()
