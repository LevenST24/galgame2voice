"""
Unit and Integration Tests for System Version & One-Click GitHub Update (Milestone System Update).
Covers:
- GET /api/system/version (local check & remote fetch check)
- GET /api/system/update/check (alias endpoint)
- POST /api/system/update/apply (safety guards, detached HEAD, uncommitted changes, git pull, diff handling)
- Frontend rebuild trigger on frontend changes or force flag
- CharacterManager sync_with_db integration on successful update
- Robust error handling: git missing, timeout, detached HEAD, uncommitted changes, network errors
- Console authentication enforcement
"""

import asyncio
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.main import create_app
from galgame2voice.routers.system import (
    SystemUpdateRequest,
    SystemUpdateResponse,
    SystemVersionResponse,
    _apply_update_sync,
    _check_version_sync,
    _rebuild_frontend_sync,
    _run_git_cmd,
)


@pytest.fixture
def test_app():
    """Provides a fresh FastAPI app instance for testing."""
    return create_app()


# ============================================================================
# 1. Version Check & Inspection Tests
# ============================================================================

class TestSystemVersionCheck:
    """Verifies GET /api/system/version and GET /api/system/update/check."""

    @pytest.mark.asyncio
    async def test_get_version_local_real_repo(self, test_app):
        """Verifies GET /api/system/version?check_remote=false against the actual repository."""
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/system/version?check_remote=false")
            assert resp.status_code == 200
            data = resp.json()
            assert "current_version" in data
            assert data["current_version"] != "unknown"
            assert "latest_version" in data
            assert data["has_update"] is False
            assert data["behind_count"] == 0
            assert "remote_url" in data
            assert isinstance(data["commits_log"], list)
            assert data["current_branch"] in ("main", "master", "HEAD")
            assert data["error"] is None

    @pytest.mark.asyncio
    async def test_get_version_alias_endpoint(self, test_app):
        """Verifies GET /api/system/update/check alias works and returns SystemVersionResponse schema."""
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Mock the internal check function to avoid live network latency during fast tests
            mock_resp = SystemVersionResponse(
                current_version="a1b2c3d",
                latest_version="a1b2c3d",
                has_update=False,
                behind_count=0,
                remote_url="https://github.com/LevenST24/galgame2voice.git",
                commits_log=[],
                current_branch="main",
                commit_date="2026-09-13 14:00:00 +0800",
                commit_message="test commit",
                error=None,
            )
            with patch("galgame2voice.routers.system._check_version_sync", return_value=mock_resp):
                resp = await client.get("/api/system/update/check")
                assert resp.status_code == 200
                data = resp.json()
                assert data["current_version"] == "a1b2c3d"
                assert data["latest_version"] == "a1b2c3d"
                assert data["has_update"] is False
                assert data["behind_count"] == 0
                assert data["current_branch"] == "main"

    @pytest.mark.asyncio
    async def test_get_version_detects_pending_updates(self, test_app):
        """Verifies pending remote updates are parsed into has_update=True and commits_log."""
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            mock_resp = SystemVersionResponse(
                current_version="a1b2c3d",
                latest_version="f9e8d7c",
                has_update=True,
                behind_count=2,
                remote_url="https://github.com/LevenST24/galgame2voice.git",
                commits_log=[
                    "f9e8d7c feat(update): add one-click updater (2026-09-13)",
                    "e8d7c6b fix(ui): polish settings modal (2026-09-13)",
                ],
                current_branch="main",
                commit_date="2026-09-13 12:00:00 +0800",
                commit_message="older commit",
                error=None,
            )
            with patch("galgame2voice.routers.system._check_version_sync", return_value=mock_resp):
                resp = await client.get("/api/system/version?check_remote=true")
                assert resp.status_code == 200
                data = resp.json()
                assert data["has_update"] is True
                assert data["behind_count"] == 2
                assert data["latest_version"] == "f9e8d7c"
                assert len(data["commits_log"]) == 2
                assert "feat(update)" in data["commits_log"][0]

    def test_check_version_sync_not_a_git_repo(self, tmp_path):
        """Verifies _check_version_sync returns graceful unknown payload for non-git directories."""
        res = _check_version_sync(tmp_path, check_remote=True)
        assert res.current_version == "unknown"
        assert res.latest_version == "unknown"
        assert res.has_update is False
        assert res.behind_count == 0
        assert "未检测到有效 Git 仓库" in (res.error or "")

    def test_check_version_sync_remote_fetch_failure(self, tmp_path):
        """Verifies _check_version_sync handles remote fetch network failures gracefully without crashing."""
        def fake_git(args, cwd, timeout=30.0, env_overrides=None):
            if args[0] == "rev-parse" and args[1] == "--is-inside-work-tree":
                return 0, "true", ""
            if args[0] == "rev-parse" and args[1] == "--short" and args[2] == "HEAD":
                return 0, "1234567", ""
            if args[0] == "rev-parse" and args[1] == "--abbrev-ref":
                return 0, "main", ""
            if args[0] == "log":
                return 0, "sample message", ""
            if args[0] == "remote":
                return 0, "https://github.com/LevenST24/galgame2voice.git", ""
            if args[0] == "fetch":
                return 128, "", "fatal: unable to access 'https://github.com/': Could not resolve host"
            return 0, "", ""

        with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git):
            res = _check_version_sync(tmp_path, check_remote=True)
            assert res.current_version == "1234567"
            assert res.has_update is False
            assert res.behind_count == 0
            assert "远程更新检查失败" in (res.error or "")


# ============================================================================
# 2. Update Application & Safety Guard Tests
# ============================================================================

class TestSystemUpdateApply:
    """Verifies POST /api/system/update/apply safety checks, pull execution, and post-tasks."""

    @pytest.mark.asyncio
    async def test_apply_update_detached_head_guard(self, test_app):
        """Verifies update is safely blocked if git is in detached HEAD state."""
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            def fake_git(args, cwd, timeout=30.0, env_overrides=None):
                if args[0] == "rev-parse" and args[1] == "--is-inside-work-tree":
                    return 0, "true", ""
                if args[0] == "rev-parse" and args[1] == "--short":
                    return 0, "eb94d85", ""
                if args[0] == "rev-parse" and args[1] == "--abbrev-ref":
                    return 0, "HEAD", ""  # Detached HEAD!
                return 0, "", ""

            with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git):
                resp = await client.post("/api/system/update/apply")
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is False
                assert data["error"] == "Detached HEAD"
                assert "Detached HEAD" in data["output"]

    @pytest.mark.asyncio
    async def test_apply_update_uncommitted_changes_guard(self, test_app):
        """Verifies update is safely blocked if local tracked files have uncommitted changes."""
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            def fake_git(args, cwd, timeout=30.0, env_overrides=None):
                if args[0] == "rev-parse" and args[1] == "--is-inside-work-tree":
                    return 0, "true", ""
                if args[0] == "rev-parse" and args[1] == "--short":
                    return 0, "eb94d85", ""
                if args[0] == "rev-parse" and args[1] == "--abbrev-ref":
                    return 0, "main", ""
                if args[0] == "status" and "--porcelain" in args:
                    return 0, "M galgame2voice/main.py\n M frontend/src/main.js", ""
                return 0, "", ""

            with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git):
                resp = await client.post("/api/system/update/apply")
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is False
                assert data["error"] == "Uncommitted changes in local workspace"
                assert "存在未提交的代码修改" in data["output"]
                assert "galgame2voice/main.py" in data["output"]

    @pytest.mark.asyncio
    async def test_apply_update_pull_failure_handled(self, test_app):
        """Verifies git pull network failure or merge conflict is safely captured with diagnostic output."""
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            def fake_git(args, cwd, timeout=30.0, env_overrides=None):
                if args[0] == "rev-parse" and args[1] == "--is-inside-work-tree":
                    return 0, "true", ""
                if args[0] == "rev-parse" and args[1] == "--short":
                    return 0, "eb94d85", ""
                if args[0] == "rev-parse" and args[1] == "--abbrev-ref":
                    return 0, "main", ""
                if args[0] == "status":
                    return 0, "", ""
                if args[0] == "rev-parse" and args[1] == "HEAD":
                    return 0, "eb94d8522043c72c1ed5c3111b138ef8eee6dcc6", ""
                if args[0] == "pull":
                    return 1, "", "fatal: connection timed out after 60000 milliseconds"
                return 0, "", ""

            with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git):
                resp = await client.post("/api/system/update/apply")
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is False
                assert "connection timed out" in data["output"]
                assert data["rebuilt_frontend"] is False
                assert data["restart_required"] is False

    @pytest.mark.asyncio
    async def test_apply_update_success_already_up_to_date(self, test_app):
        """Verifies update succeeding when already up to date."""
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            commit_hash = "eb94d8522043c72c1ed5c3111b138ef8eee6dcc6"
            def fake_git(args, cwd, timeout=30.0, env_overrides=None):
                if args[0] == "rev-parse" and args[1] == "--is-inside-work-tree":
                    return 0, "true", ""
                if args[0] == "rev-parse" and args[1] == "--short":
                    return 0, "eb94d85", ""
                if args[0] == "rev-parse" and args[1] == "--abbrev-ref":
                    return 0, "main", ""
                if args[0] == "status":
                    return 0, "", ""
                if args[0] == "rev-parse" and args[1] == "HEAD":
                    return 0, commit_hash, ""
                if args[0] == "pull":
                    return 0, "Already up to date.", ""
                return 0, "", ""

            with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git):
                resp = await client.post("/api/system/update/apply")
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is True
                assert data["rebuilt_frontend"] is False
                assert data["restart_required"] is False
                assert "Already up to date" in data["output"]
                assert data["current_version"] == "eb94d85"
                assert data["previous_version"] == "eb94d85"

    @pytest.mark.asyncio
    async def test_apply_update_triggers_frontend_rebuild_and_char_sync(self, test_app):
        """Verifies frontend changes trigger npm run deploy and character package sync runs."""
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            before_hash = "1111111111111111111111111111111111111111"
            after_hash = "2222222222222222222222222222222222222222"
            calls = []

            def fake_git(args, cwd, timeout=30.0, env_overrides=None):
                calls.append(args[0])
                if args[0] == "rev-parse" and args[1] == "--is-inside-work-tree":
                    return 0, "true", ""
                if args[0] == "rev-parse" and args[1] == "--short":
                    return 0, "2222222" if before_hash in str(calls) else "1111111", ""
                if args[0] == "rev-parse" and args[1] == "--abbrev-ref":
                    return 0, "main", ""
                if args[0] == "status":
                    return 0, "", ""
                if args[0] == "rev-parse" and args[1] == "HEAD":
                    return 0, after_hash if "pull" in calls else before_hash, ""
                if args[0] == "pull":
                    return 0, "Updating 1111111..2222222\nFast-forward", ""
                if args[0] == "diff":
                    return 0, "frontend/src/settings.js\ngalgame2voice/main.py\ncharacters/character_packages/sakura.json", ""
                return 0, "", ""

            mock_char_mgr = MagicMock()
            mock_char_mgr.discover_characters.return_value = ["sakura"]
            mock_char_mgr.sync_with_db = AsyncMock(return_value=1)

            with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git), \
                 patch("galgame2voice.routers.system._rebuild_frontend_sync", return_value=(True, "Vite deployed 3 assets")) as mock_rebuild, \
                 patch("galgame2voice.services.character_manager.get_character_manager", return_value=mock_char_mgr):

                resp = await client.post("/api/system/update/apply")
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is True
                assert data["rebuilt_frontend"] is True
                assert data["restart_required"] is True  # Because galgame2voice/main.py changed
                assert mock_rebuild.called
                assert mock_char_mgr.sync_with_db.called
                assert "角色设定包" in data["output"]
                assert "前端静态产物编译并部署成功" in data["output"]

    @pytest.mark.asyncio
    async def test_apply_update_force_rebuild_flag(self, test_app):
        """Verifies force_rebuild_frontend=True forces frontend rebuild even if only docs changed."""
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            before_hash = "1111111111111111111111111111111111111111"
            after_hash = "2222222222222222222222222222222222222222"
            calls = []

            def fake_git(args, cwd, timeout=30.0, env_overrides=None):
                calls.append(args[0])
                if args[0] == "rev-parse" and args[1] == "--is-inside-work-tree":
                    return 0, "true", ""
                if args[0] == "rev-parse" and args[1] == "--short":
                    return 0, "2222222" if "pull" in calls else "1111111", ""
                if args[0] == "rev-parse" and args[1] == "--abbrev-ref":
                    return 0, "main", ""
                if args[0] == "status":
                    return 0, "", ""
                if args[0] == "rev-parse" and args[1] == "HEAD":
                    return 0, after_hash if "pull" in calls else before_hash, ""
                if args[0] == "pull":
                    return 0, "Updating 1111111..2222222", ""
                if args[0] == "diff":
                    return 0, "README.md", ""  # Only docs changed!
                return 0, "", ""

            with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git), \
                 patch("galgame2voice.routers.system._rebuild_frontend_sync", return_value=(True, "deployed")) as mock_rebuild:

                payload = {"force_rebuild_frontend": True}
                resp = await client.post("/api/system/update/apply", json=payload)
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is True
                assert data["rebuilt_frontend"] is True
                assert data["restart_required"] is False
                assert mock_rebuild.called

    @pytest.mark.asyncio
    async def test_apply_update_auto_restores_static_artifacts(self, test_app):
        """Verifies that changes solely in galgame2voice/static/ are auto-reverted so deploy artifacts do not block updates."""
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            checkout_calls = []

            def fake_git(args, cwd, timeout=30.0, env_overrides=None):
                if args[0] == "rev-parse" and args[1] == "--is-inside-work-tree":
                    return 0, "true", ""
                if args[0] == "rev-parse" and args[1] == "--short":
                    return 0, "eb94d85", ""
                if args[0] == "rev-parse" and args[1] == "--abbrev-ref" and len(args) > 2 and args[2] == "@{u}":
                    return 0, "origin/main", ""
                if args[0] == "rev-parse" and args[1] == "--abbrev-ref":
                    return 0, "main", ""
                if args[0] == "status":
                    # Simulate modified build artifacts from a previous npm run deploy
                    return 0, " M galgame2voice/static/index.html\n D galgame2voice/static/assets/old.js", ""
                if args[0] == "checkout" and "galgame2voice/static" in args:
                    checkout_calls.append(args)
                    return 0, "Updated 2 paths", ""
                if args[0] == "rev-parse" and args[1] == "HEAD":
                    return 0, "eb94d8522043c72c1ed5c3111b138ef8eee6dcc6", ""
                if args[0] == "pull":
                    return 0, "Already up to date.", ""
                return 0, "", ""

            with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git):
                resp = await client.post("/api/system/update/apply")
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is True
                assert len(checkout_calls) == 1
                assert "checkout" in checkout_calls[0]
                assert "galgame2voice/static" in checkout_calls[0]

    @pytest.mark.asyncio
    async def test_apply_update_with_stash_changes(self, test_app):
        """Verifies stash_changes=True executes git stash when uncommitted non-static changes exist."""
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            stash_called = False

            def fake_git(args, cwd, timeout=30.0, env_overrides=None):
                nonlocal stash_called
                if args[0] == "rev-parse" and args[1] == "--is-inside-work-tree":
                    return 0, "true", ""
                if args[0] == "rev-parse" and args[1] == "--short":
                    return 0, "eb94d85", ""
                if args[0] == "rev-parse" and args[1] == "--abbrev-ref":
                    return 0, "main", ""
                if args[0] == "status":
                    return 0, " M galgame2voice/main.py", ""
                if args[0] == "stash":
                    stash_called = True
                    return 0, "Saved working directory", ""
                if args[0] == "rev-parse" and args[1] == "HEAD":
                    return 0, "eb94d8522043c72c1ed5c3111b138ef8eee6dcc6", ""
                if args[0] == "pull":
                    return 0, "Already up to date.", ""
                return 0, "", ""

            with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git):
                resp = await client.post("/api/system/update/apply", json={"stash_changes": True})
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is True
                assert stash_called is True

    @pytest.mark.asyncio
    async def test_apply_update_with_discard_local_changes(self, test_app):
        """Verifies discard_local_changes=True executes git reset --hard HEAD."""
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            reset_called = False

            def fake_git(args, cwd, timeout=30.0, env_overrides=None):
                nonlocal reset_called
                if args[0] == "rev-parse" and args[1] == "--is-inside-work-tree":
                    return 0, "true", ""
                if args[0] == "rev-parse" and args[1] == "--short":
                    return 0, "eb94d85", ""
                if args[0] == "rev-parse" and args[1] == "--abbrev-ref":
                    return 0, "main", ""
                if args[0] == "status":
                    return 0, " M galgame2voice/main.py", ""
                if args[0] == "reset" and "--hard" in args:
                    reset_called = True
                    return 0, "HEAD is now at eb94d85", ""
                if args[0] == "rev-parse" and args[1] == "HEAD":
                    return 0, "eb94d8522043c72c1ed5c3111b138ef8eee6dcc6", ""
                if args[0] == "pull":
                    return 0, "Already up to date.", ""
                return 0, "", ""

            with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git):
                resp = await client.post("/api/system/update/apply", json={"discard_local_changes": True})
                assert resp.status_code == 200
                data = resp.json()
                assert data["success"] is True
                assert reset_called is True


# ============================================================================
# 3. Low-Level Subprocess & Error Path Tests
# ============================================================================

class TestSubprocessAndErrorHandling:
    """Verifies low-level subprocess guards (missing git, timeout, etc.)."""

    def test_run_git_cmd_git_not_installed(self, tmp_path):
        """Verifies _run_git_cmd handles missing git binary gracefully."""
        with patch("shutil.which", return_value=None):
            rc, out, err = _run_git_cmd(["status"], cwd=tmp_path)
            assert rc == -1
            assert "未找到 Git 可执行程序" in err

    def test_run_git_cmd_timeout_expired(self, tmp_path):
        """Verifies _run_git_cmd converts subprocess.TimeoutExpired to clear error."""
        with patch("shutil.which", return_value="git"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=5.0)):
            rc, out, err = _run_git_cmd(["fetch"], cwd=tmp_path, timeout=5.0)
            assert rc == -1
            assert "超时" in err

    def test_rebuild_frontend_npm_not_found(self, tmp_path):
        """Verifies _rebuild_frontend_sync handles missing npm binary gracefully."""
        frontend_dir = tmp_path / "frontend"
        frontend_dir.mkdir(parents=True, exist_ok=True)
        with patch("shutil.which", return_value=None):
            ok, log = _rebuild_frontend_sync(tmp_path)
            assert ok is False
            assert "未找到 npm 程序" in log

    def test_rebuild_frontend_missing_dir(self, tmp_path):
        """Verifies _rebuild_frontend_sync handles missing frontend/ directory gracefully."""
        ok, log = _rebuild_frontend_sync(tmp_path)
        assert ok is False
        assert "未找到前端源码目录" in log

    def test_rebuild_frontend_unicode_sanitization(self, tmp_path):
        """Verifies _rebuild_frontend_sync replaces checkmarks with [OK] to prevent Windows GBK UnicodeEncodeErrors."""
        frontend_dir = tmp_path / "frontend"
        frontend_dir.mkdir(parents=True, exist_ok=True)
        fake_stdout = "\x1b[32m\u2713\x1b[39m built in 500ms\n\u2717 failed module"
        mock_proc = MagicMock(returncode=0, stdout=fake_stdout, stderr="")
        with patch("shutil.which", return_value="npm"), \
             patch("subprocess.run", return_value=mock_proc):
            ok, log = _rebuild_frontend_sync(tmp_path)
            assert ok is True
            assert "[OK]" in log
            assert "[FAIL]" in log
            assert "\u2713" not in log

    def test_get_remote_and_branch_resolution(self, tmp_path):
        """Verifies _get_remote_and_branch resolves tracking branch or falls back safely."""
        from galgame2voice.routers.system import _get_remote_and_branch

        # 1. When tracking origin/main
        with patch("galgame2voice.routers.system._run_git_cmd", return_value=(0, "origin/main", "")):
            remote, branch = _get_remote_and_branch(tmp_path)
            assert remote == "origin"
            assert branch == "main"

        # 2. When tracking upstream/release-1.0
        with patch("galgame2voice.routers.system._run_git_cmd", return_value=(0, "upstream/release-1.0", "")):
            remote, branch = _get_remote_and_branch(tmp_path)
            assert remote == "upstream"
            assert branch == "release-1.0"

        # 3. When untracked/detached
        with patch("galgame2voice.routers.system._run_git_cmd", return_value=(1, "", "fatal")):
            remote, branch = _get_remote_and_branch(tmp_path)
            assert remote == "origin"
            assert branch == "main"


# ============================================================================
# 4. Console Auth Enforcement Tests
# ============================================================================

class TestConsoleAuthOnSystemEndpoints:
    """Verifies that console token authentication applies when enabled."""

    @pytest.mark.asyncio
    async def test_auth_enforced_when_enabled(self, monkeypatch):
        """Verifies 401 when console authentication is active and no token provided."""
        monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "0")
        monkeypatch.setenv("GALGAME2VOICE_CONSOLE_TOKEN", "secret-test-token-1234")

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Unauthenticated request -> 401
            resp = await client.get("/api/system/version")
            assert resp.status_code == 401

            # 2. Authenticated request with Bearer -> 200
            headers = {"Authorization": "Bearer secret-test-token-1234"}
            resp = await client.get("/api/system/version?check_remote=false", headers=headers)
            assert resp.status_code == 200
            assert "current_version" in resp.json()
