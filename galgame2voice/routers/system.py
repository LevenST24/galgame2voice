"""
System Version and One-Click GitHub Update Router for galgame2voice.
Provides endpoints for version inspection, remote update checks,
and safe one-click pulling from GitHub with automatic frontend rebuild
and character package synchronization.
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from galgame2voice.config import get_settings
from galgame2voice.security.auth import require_auth
from galgame2voice.utils.logger import sanitize_error_detail

logger = logging.getLogger("galgame2voice.routers.system")
router = APIRouter(prefix="/api/system", tags=["System & Update"])


# ============================================================================
# Pydantic Schemas
# ============================================================================

class SystemVersionResponse(BaseModel):
    """System version, git commit state, and remote update availability telemetry."""
    current_version: str = Field(..., description="Current local commit short hash (e.g. eb94d85)")
    latest_version: str = Field(..., description="Latest remote origin commit short hash")
    has_update: bool = Field(default=False, description="Whether newer commits exist on remote")
    behind_count: int = Field(default=0, description="Number of commits local HEAD is behind remote")
    remote_url: str = Field(default="", description="Configured git remote repository URL")
    commits_log: List[str] = Field(default_factory=list, description="List of pending update commit descriptions")
    current_branch: str = Field(default="main", description="Current git branch name")
    commit_date: Optional[str] = Field(default=None, description="Current commit ISO date string")
    commit_message: Optional[str] = Field(default=None, description="Current commit subject message")
    error: Optional[str] = Field(default=None, description="Error or diagnostic detail if remote check failed")


class SystemUpdateRequest(BaseModel):
    """Optional payload for system update execution."""
    force_rebuild_frontend: bool = Field(
        default=False,
        description="Whether to force rebuilding frontend static assets even if no frontend files changed",
    )
    stash_changes: bool = Field(
        default=False,
        description="Whether to git stash uncommitted changes before pulling",
    )
    discard_local_changes: bool = Field(
        default=False,
        description="Whether to discard uncommitted changes before pulling",
    )


class SystemUpdateResponse(BaseModel):
    """Result of one-click update operation from GitHub."""
    success: bool = Field(..., description="Whether git pull and subsequent tasks succeeded")
    rebuilt_frontend: bool = Field(default=False, description="Whether frontend static assets were rebuilt")
    restart_required: bool = Field(default=False, description="Whether backend restart is recommended")
    output: str = Field(..., description="Detailed execution log and status messages")
    current_version: str = Field(..., description="Commit short hash after update")
    previous_version: str = Field(..., description="Commit short hash before update")
    error: Optional[str] = Field(default=None, description="Error detail if operation failed")


# ============================================================================
# Git Execution Helpers (Synchronous & Async Thread-Bound)
# ============================================================================

def _run_git_cmd(
    args: List[str],
    cwd: Path,
    timeout: float = 30.0,
    env_overrides: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """
    Executes a git command safely with non-interactive terminal flags
    and strict timeout guard.
    """
    git_bin = shutil.which("git")
    if not git_bin:
        return -1, "", "系统未找到 Git 可执行程序，请确认已在操作系统中安装 Git 并将其添加至 PATH 环境变量。"

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    if env_overrides:
        env.update(env_overrides)

    try:
        proc = subprocess.run(
            [git_bin, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"Git 操作执行超时 ({timeout:.1f}s)，请检查网络连接或 GitHub 连通性。"
    except FileNotFoundError:
        return -1, "", "Git 程序未找到，请检查环境配置。"
    except Exception as exc:
        return -1, "", f"执行 Git 命令异常: {sanitize_error_detail(exc)}"


def _rebuild_frontend_sync(project_root: Path, timeout: float = 120.0) -> Tuple[bool, str]:
    """
    Executes `npm run deploy` inside the frontend/ directory
    to compile assets and deploy to galgame2voice/static/.
    Safely sanitizes unicode characters to prevent Windows console UnicodeEncodeErrors.
    """
    frontend_dir = project_root / "frontend"
    if not frontend_dir.is_dir():
        return False, f"未找到前端源码目录: {frontend_dir}"

    npm_bin = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm_bin:
        return False, "未找到 npm 程序，无法自动构建前端产物，请在安装 Node.js 后手动在 frontend 目录执行 npm run deploy。"

    try:
        proc = subprocess.run(
            [npm_bin, "run", "deploy"],
            cwd=str(frontend_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=(sys.platform == "win32"),
        )
        combined = f"{proc.stdout}\n{proc.stderr}".strip()
        # Sanitize terminal colors and Vite unicode checkmark (\u2713) to prevent UnicodeEncodeError on GBK consoles
        sanitized = re.sub(r"\x1b\[[0-9;]*[mK]", "", combined)
        sanitized = sanitized.replace("\u2713", "[OK]").replace("\u2717", "[FAIL]")
        if proc.returncode == 0:
            return True, sanitized
        return False, f"npm run deploy 执行失败 (退出码 {proc.returncode}):\n{sanitized}"
    except subprocess.TimeoutExpired:
        return False, f"前端构建超时 ({timeout:.1f}s)。"
    except Exception as exc:
        return False, f"前端构建发生异常: {sanitize_error_detail(exc)}"


def _get_remote_and_branch(project_root: Path) -> Tuple[str, str]:
    """
    Determines the remote name (default 'origin') and remote branch (default 'main').
    Queries the upstream tracking branch of current HEAD via @{u}.
    If detached or untracked, defaults to ('origin', 'main').
    """
    rc, upstream, _ = _run_git_cmd(["rev-parse", "--abbrev-ref", "@{u}"], cwd=project_root)
    if rc == 0 and upstream and "/" in upstream:
        parts = upstream.split("/", 1)
        return parts[0], parts[1]

    # Verify if 'origin' is a valid remote
    rc, remotes, _ = _run_git_cmd(["remote"], cwd=project_root)
    remote_name = "origin" if (rc == 0 and "origin" in remotes.split()) else "origin"
    return remote_name, "main"


def _check_version_sync(project_root: Path, check_remote: bool = True) -> SystemVersionResponse:
    """
    Synchronous git inspection logic resolving local HEAD, commit metadata,
    and remote upstream status.
    """
    # 1. Verify working directory is a git repository
    rc, out, err = _run_git_cmd(["rev-parse", "--is-inside-work-tree"], cwd=project_root, timeout=5.0)
    if rc != 0 or out != "true":
        return SystemVersionResponse(
            current_version="unknown",
            latest_version="unknown",
            has_update=False,
            behind_count=0,
            remote_url="",
            commits_log=[],
            current_branch="unknown",
            error="当前项目目录未检测到有效 Git 仓库 (.git 不存在或未初始化)。",
        )

    # 2. Query local commit information
    rc, cur_short, _ = _run_git_cmd(["rev-parse", "--short", "HEAD"], cwd=project_root)
    if rc != 0 or not cur_short:
        cur_short = "unknown"

    rc, branch, _ = _run_git_cmd(["rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root)
    if rc != 0 or not branch:
        branch = "unknown"

    rc, commit_date, _ = _run_git_cmd(["log", "-1", "--format=%cd", "--date=iso"], cwd=project_root)
    if rc != 0:
        commit_date = None

    rc, commit_msg, _ = _run_git_cmd(["log", "-1", "--format=%s"], cwd=project_root)
    if rc != 0:
        commit_msg = None

    # Query remote URL
    rc, remote_url, _ = _run_git_cmd(["remote", "get-url", "origin"], cwd=project_root)
    if rc != 0 or not remote_url:
        rc, remote_url, _ = _run_git_cmd(["config", "--get", "remote.origin.url"], cwd=project_root)
        if rc != 0:
            remote_url = ""

    # If remote check is disabled, return local snapshot immediately
    if not check_remote:
        return SystemVersionResponse(
            current_version=cur_short,
            latest_version=cur_short,
            has_update=False,
            behind_count=0,
            remote_url=remote_url,
            commits_log=[],
            current_branch=branch,
            commit_date=commit_date,
            commit_message=commit_msg,
        )

    # 3. Check remote origin status
    remote_name, remote_branch = _get_remote_and_branch(project_root)

    # Fetch origin using refspec to update both FETCH_HEAD and remote-tracking branch
    refspec = f"+refs/heads/{remote_branch}:refs/remotes/{remote_name}/{remote_branch}"
    fetch_rc, fetch_out, fetch_err = _run_git_cmd(
        ["fetch", remote_name, refspec],
        cwd=project_root,
        timeout=20.0,
    )

    if fetch_rc != 0:
        # Fallback to plain fetch
        fetch_rc, fetch_out, fetch_err = _run_git_cmd(
            ["fetch", remote_name, remote_branch],
            cwd=project_root,
            timeout=20.0,
        )

    if fetch_rc != 0:
        err_msg = fetch_err or fetch_out or "无法连接到 GitHub 远程仓库"
        return SystemVersionResponse(
            current_version=cur_short,
            latest_version=cur_short,
            has_update=False,
            behind_count=0,
            remote_url=remote_url,
            commits_log=[],
            current_branch=branch,
            commit_date=commit_date,
            commit_message=commit_msg,
            error=f"远程更新检查失败: {err_msg}",
        )

    # Remote latest commit
    rc, remote_short, _ = _run_git_cmd(["rev-parse", "--short", f"{remote_name}/{remote_branch}"], cwd=project_root)
    if rc != 0 or not remote_short:
        rc, remote_short, _ = _run_git_cmd(["rev-parse", "--short", "FETCH_HEAD"], cwd=project_root)
        if rc != 0 or not remote_short:
            remote_short = cur_short

    # Count commits behind
    rc, count_str, _ = _run_git_cmd(["rev-list", f"HEAD..{remote_name}/{remote_branch}", "--count"], cwd=project_root)
    if rc != 0 or not count_str.isdigit():
        rc, count_str, _ = _run_git_cmd(["rev-list", "HEAD..FETCH_HEAD", "--count"], cwd=project_root)

    behind_count = int(count_str) if (count_str and count_str.isdigit()) else 0
    has_update = behind_count > 0

    commits_log: List[str] = []
    if has_update:
        # Retrieve readable log of new commits
        rc, log_out, _ = _run_git_cmd(
            ["log", f"HEAD..{remote_name}/{remote_branch}", "--pretty=format:%h %s (%cd)", "--date=short", "-n", "20"],
            cwd=project_root,
        )
        if rc != 0 or not log_out:
            rc, log_out, _ = _run_git_cmd(
                ["log", "HEAD..FETCH_HEAD", "--pretty=format:%h %s (%cd)", "--date=short", "-n", "20"],
                cwd=project_root,
            )
        if rc == 0 and log_out:
            commits_log = [line.strip() for line in log_out.splitlines() if line.strip()]

    return SystemVersionResponse(
        current_version=cur_short,
        latest_version=remote_short,
        has_update=has_update,
        behind_count=behind_count,
        remote_url=remote_url,
        commits_log=commits_log,
        current_branch=branch,
        commit_date=commit_date,
        commit_message=commit_msg,
        error=None,
    )


def _apply_update_sync(
    project_root: Path,
    force_rebuild_frontend: bool = False,
    stash_changes: bool = False,
    discard_local_changes: bool = False,
) -> Tuple[SystemUpdateResponse, List[str]]:
    """
    Synchronous git pull execution with pre-flight safety validations
    (detached HEAD, uncommitted modifications).
    Automatically restores local generated build artifacts in galgame2voice/static/
    to prevent self-lockout update loops.
    Returns (response_object, list_of_changed_files).
    """
    # 1. Verify git repo
    rc, out, err = _run_git_cmd(["rev-parse", "--is-inside-work-tree"], cwd=project_root, timeout=5.0)
    if rc != 0 or out != "true":
        msg = "更新失败: 当前项目目录不是有效 Git 仓库。"
        return SystemUpdateResponse(
            success=False,
            rebuilt_frontend=False,
            restart_required=False,
            output=msg,
            current_version="unknown",
            previous_version="unknown",
            error=msg,
        ), []

    # Record current short commit before checks
    _, before_short, _ = _run_git_cmd(["rev-parse", "--short", "HEAD"], cwd=project_root)
    if not before_short:
        before_short = "unknown"

    # 2. Check for detached HEAD
    rc, branch, _ = _run_git_cmd(["rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root)
    if branch == "HEAD" or rc != 0:
        msg = "当前 Git 仓库处于游离分支 (Detached HEAD) 状态，无法自动拉取。请先签出具体分支 (如 git checkout main)。"
        return SystemUpdateResponse(
            success=False,
            rebuilt_frontend=False,
            restart_required=False,
            output=msg,
            current_version=before_short,
            previous_version=before_short,
            error="Detached HEAD",
        ), []

    # 3. Check for uncommitted tracked changes
    rc, status_out, _ = _run_git_cmd(["status", "--porcelain", "-uno"], cwd=project_root)
    if status_out.strip():
        # Parse modified tracked files
        status_lines = [line.strip() for line in status_out.splitlines() if line.strip()]
        modified_files: List[str] = []
        for line in status_lines:
            # git status --porcelain format: XY PATH or XY "PATH"
            content = line[2:].strip()
            parts = content.split(" -> ")
            modified_files.append(parts[-1].strip('"'))

        # Check if ALL modified files are inside galgame2voice/static/ (build artifacts from npm run deploy)
        only_static = bool(modified_files) and all(
            f.startswith("galgame2voice/static/")
            or f.startswith("galgame2voice\\static\\")
            or f in ("galgame2voice/static", "galgame2voice\\static")
            for f in modified_files
        )

        if only_static:
            # Safely restore galgame2voice/static/ so that local build artifacts do not block git pull!
            logger.info("Automatically reverting local build artifacts in galgame2voice/static/ before git pull")
            _run_git_cmd(["checkout", "HEAD", "--", "galgame2voice/static"], cwd=project_root)
        elif discard_local_changes:
            logger.warning("Discarding local uncommitted modifications per discard_local_changes flag")
            _run_git_cmd(["reset", "--hard", "HEAD"], cwd=project_root)
        elif stash_changes:
            logger.info("Stashing local uncommitted modifications per stash_changes flag")
            _run_git_cmd(["stash", "push", "-m", "auto-stash before webui update"], cwd=project_root)
        else:
            non_static_files = [
                f for f in modified_files
                if not (f.startswith("galgame2voice/static/") or f.startswith("galgame2voice\\static\\"))
            ]
            msg = (
                "检测到本地工作区存在未提交的代码修改，为防止覆盖或产生合并冲突，"
                f"请先提交 (git commit) 或暂存 (git stash) 后再尝试更新：\n" +
                "\n".join(f" - {f}" for f in (non_static_files or modified_files))
            )
            return SystemUpdateResponse(
                success=False,
                rebuilt_frontend=False,
                restart_required=False,
                output=msg,
                current_version=before_short,
                previous_version=before_short,
                error="Uncommitted changes in local workspace",
            ), []

    # 4. Record current commit before pull
    _, before_full, _ = _run_git_cmd(["rev-parse", "HEAD"], cwd=project_root)
    _, before_short, _ = _run_git_cmd(["rev-parse", "--short", "HEAD"], cwd=project_root)
    remote_name, remote_branch = _get_remote_and_branch(project_root)

    # 5. Execute git pull
    pull_rc, pull_out, pull_err = _run_git_cmd(
        ["pull", remote_name, remote_branch],
        cwd=project_root,
        timeout=60.0,
    )

    if pull_rc != 0:
        err_msg = pull_err or pull_out or "Git pull 异常退出"
        msg = f"Git 拉取更新失败 (退出码 {pull_rc}):\n{err_msg}"
        return SystemUpdateResponse(
            success=False,
            rebuilt_frontend=False,
            restart_required=False,
            output=msg,
            current_version=before_short,
            previous_version=before_short,
            error=err_msg,
        ), []

    # 6. Record commit after pull
    _, after_full, _ = _run_git_cmd(["rev-parse", "HEAD"], cwd=project_root)
    _, after_short, _ = _run_git_cmd(["rev-parse", "--short", "HEAD"], cwd=project_root)

    output_lines = [f"[Git Pull 成功] {pull_out}"]

    # 7. Check diff between before and after
    changed_files: List[str] = []
    if before_full and after_full and before_full != after_full:
        _, diff_out, _ = _run_git_cmd(["diff", "--name-only", before_full, after_full], cwd=project_root)
        changed_files = [line.strip() for line in diff_out.splitlines() if line.strip()]
        output_lines.append(f"共变更 {len(changed_files)} 个文件。")

    # Check whether frontend files or dependencies changed
    frontend_changed = any(f.startswith("frontend/") for f in changed_files)
    should_rebuild = frontend_changed or force_rebuild_frontend

    rebuilt_frontend = False
    if should_rebuild:
        output_lines.append("\n[前端更新] 检测到前端资源变动，正在执行静态产物重新编译与部署 (npm run deploy)...")
        build_ok, build_out = _rebuild_frontend_sync(project_root)
        rebuilt_frontend = build_ok
        if build_ok:
            output_lines.append(f"前端静态产物编译并部署成功。\n{build_out}")
        else:
            output_lines.append(f"前端静态产物构建失败 (建议手动排查):\n{build_out}")
    else:
        output_lines.append("\n[前端状态] 前端资源无变更，跳过前端重新构建。")

    # Determine whether backend restart is recommended
    backend_changed = any(
        f.endswith(".py") or f in ("requirements.txt", "requirements-dev.txt", "pyproject.toml")
        for f in changed_files
    )
    restart_required = backend_changed

    if restart_required:
        output_lines.append("\n[提示] 检测到 Python 后端或依赖文件发生变动，建议重启服务以加载最新代码。")
    elif before_full == after_full:
        output_lines.append("\n[状态] 当前已是最新版本，无代码变动。")
    else:
        output_lines.append("\n[状态] 更新已完成，刷新 Web 页面即可体验最新功能。")

    return SystemUpdateResponse(
        success=True,
        rebuilt_frontend=rebuilt_frontend,
        restart_required=restart_required,
        output="\n".join(output_lines),
        current_version=after_short,
        previous_version=before_short,
        error=None,
    ), changed_files


# ============================================================================
# API Endpoints
# ============================================================================

@router.get(
    "/version",
    response_model=SystemVersionResponse,
    summary="Get System Version & Check Updates",
    description="Inspects local git repository commit info and optionally queries GitHub remote for pending updates.",
)
async def get_system_version(
    check_remote: bool = Query(
        default=True,
        description="Whether to fetch remote origin to check for pending updates",
    )
):
    """Returns local git commit metadata and pending updates comparison."""
    settings = get_settings()
    return await asyncio.to_thread(_check_version_sync, settings.project_root, check_remote)


@router.get(
    "/update/check",
    response_model=SystemVersionResponse,
    summary="Check Updates from GitHub (Alias)",
    description="Alias endpoint for checking pending updates from GitHub remote.",
)
async def check_system_update():
    """Convenient alias explicitly checking for remote updates."""
    settings = get_settings()
    return await asyncio.to_thread(_check_version_sync, settings.project_root, True)


@router.post(
    "/update/apply",
    response_model=SystemUpdateResponse,
    summary="Safely Pull and Apply Updates from GitHub",
    description="Pulls latest commits from origin/main, validates repo safety, rebuilds frontend if needed, and syncs characters.",
)
async def apply_system_update(payload: Optional[SystemUpdateRequest] = None):
    """Executes safe pull from GitHub, rebuilds frontend, and syncs characters with SQLite DB."""
    settings = get_settings()
    force_rebuild = payload.force_rebuild_frontend if payload else False
    stash_changes = payload.stash_changes if payload else False
    discard_local_changes = payload.discard_local_changes if payload else False

    # Execute git pull and frontend rebuild in worker thread
    res, changed_files = await asyncio.to_thread(
        _apply_update_sync,
        settings.project_root,
        force_rebuild,
        stash_changes,
        discard_local_changes,
    )

    # If update succeeded, automatically synchronize character packages with DB
    if res.success:
        try:
            from galgame2voice.services.character_manager import get_character_manager
            from galgame2voice.database.session import get_db
            char_mgr = get_character_manager()
            char_mgr.discover_characters()
            async with get_db(settings.db_path) as conn:
                synced = await char_mgr.sync_with_db(conn)
                if synced > 0:
                    res.output += f"\n[角色设定同步] 成功同步 {synced} 个角色设定包至数据库。"
        except Exception as exc:
            logger.warning("Character packages sync after update: %s", exc)
            res.output += f"\n[角色设定同步提示] 角色同步跳过: {sanitize_error_detail(exc)}"

    return res
