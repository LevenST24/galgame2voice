"""
System Version and One-Click GitHub Update Router for galgame2voice.
Provides endpoints for version inspection, remote update checks,
and safe one-click pulling from GitHub with automatic frontend rebuild
and character package synchronization.
"""

import asyncio
from datetime import datetime
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import zipfile

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from galgame2voice.config import get_settings
from galgame2voice.utils.hardware import release_system_memory
from galgame2voice.utils.logger import sanitize_error_detail

logger = logging.getLogger("galgame2voice.routers.system")
router = APIRouter(prefix="/api/system", tags=["System & Update"])

# Supply-chain hardening: one-click update executes code freshly pulled from
# `origin` (npm build etc.), so the remote URL must be pinned to the upstream
# repository. Operators can override/extend via a comma-separated list in the
# GALGAME2VOICE_UPDATE_ALLOWED_REMOTES environment variable.
_DEFAULT_ALLOWED_REMOTES = (
    "https://github.com/LevenST24/galgame2voice",
    "https://github.com/LevenST24/galgame2voice.git",
    "git@github.com:LevenST24/galgame2voice",
    "git@github.com:LevenST24/galgame2voice.git",
    "ssh://git@github.com/LevenST24/galgame2voice",
    "ssh://git@github.com/LevenST24/galgame2voice.git",
)


def _get_allowed_remotes() -> List[str]:
    raw = os.getenv("GALGAME2VOICE_UPDATE_ALLOWED_REMOTES", "").strip()
    if raw:
        return [entry.strip() for entry in raw.split(",") if entry.strip()]
    return list(_DEFAULT_ALLOWED_REMOTES)


def _is_allowed_remote(url: str) -> bool:
    normalized = (url or "").strip().rstrip("/")
    return any(normalized == allowed.rstrip("/") for allowed in _get_allowed_remotes())


def _verify_remote_url_allowed(project_root: Path) -> Optional[str]:
    """Returns an error message if `origin` is not an allowed update source, else None."""
    rc, remote_url, err = _run_git_cmd(["remote", "get-url", "origin"], cwd=project_root)
    if rc != 0 or not remote_url:
        return f"无法读取 git remote origin 地址: {err or 'remote 不存在'}"
    if not _is_allowed_remote(remote_url):
        logger.warning("Blocked one-click update from untrusted remote: %s", remote_url)
        return (
            f"安全拦截: 当前 git remote origin 不在允许的更新源白名单内 ({remote_url})。"
            "如需更换更新源, 请设置 GALGAME2VOICE_UPDATE_ALLOWED_REMOTES 环境变量。"
        )
    return None


# Serializes all git fetch/pull/update operations to prevent concurrent
# repository lock contention.
_GIT_OP_LOCK = asyncio.Lock()


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
        description="Whether to discard local modifications with git reset --hard HEAD",
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
            # --ignore-scripts blocks malicious lifecycle hooks (pre/postinstall
            # style) from freshly pulled code during the build.
            [npm_bin, "--ignore-scripts", "run", "deploy"],
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
            try:
                release_system_memory()
            except Exception as mem_err:
                logger.debug("Failed releasing system memory after npm deploy: %s", mem_err)
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


def _inspect_local_git_repo(project_root: Path) -> Optional[Tuple[str, str, Optional[str], Optional[str], str]]:
    """Validates git work tree and retrieves (cur_short, branch, commit_date, commit_msg, remote_url)."""
    rc, out, _ = _run_git_cmd(["rev-parse", "--is-inside-work-tree"], cwd=project_root, timeout=5.0)
    if rc != 0 or out != "true":
        return None

    rc, cur_short, _ = _run_git_cmd(["rev-parse", "--short", "HEAD"], cwd=project_root)
    cur_short = cur_short if (rc == 0 and cur_short) else "unknown"

    rc, branch, _ = _run_git_cmd(["rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root)
    branch = branch if (rc == 0 and branch) else "unknown"

    rc, commit_date, _ = _run_git_cmd(["log", "-1", "--format=%cd", "--date=iso"], cwd=project_root)
    commit_date = commit_date if rc == 0 else None

    rc, commit_msg, _ = _run_git_cmd(["log", "-1", "--format=%s"], cwd=project_root)
    commit_msg = commit_msg if rc == 0 else None

    rc, remote_url, _ = _run_git_cmd(["remote", "get-url", "origin"], cwd=project_root)
    if rc != 0 or not remote_url:
        rc, remote_url, _ = _run_git_cmd(["config", "--get", "remote.origin.url"], cwd=project_root)
        if rc != 0:
            remote_url = ""

    return cur_short, branch, commit_date, commit_msg, remote_url


def _query_remote_git_status(
    project_root: Path,
    cur_short: str,
    remote_url: str,
) -> Tuple[str, bool, int, List[str], Optional[str]]:
    """Fetches remote tracking ref and queries (remote_short, has_update, behind_count, commits_log, error)."""
    remote_name, remote_branch = _get_remote_and_branch(project_root)

    # Supply-chain guard: never fetch from an untrusted remote
    if remote_name == "origin" and not _is_allowed_remote(remote_url):
        logger.warning("Blocked remote update check from untrusted remote: %s", remote_url)
        return cur_short, False, 0, [], "安全拦截: git remote origin 不在允许的更新源白名单内，已跳过远端检查。"

    refspec = f"+refs/heads/{remote_branch}:refs/remotes/{remote_name}/{remote_branch}"
    fetch_rc, fetch_out, fetch_err = _run_git_cmd(
        ["fetch", remote_name, refspec],
        cwd=project_root,
        timeout=20.0,
    )
    if fetch_rc != 0:
        fetch_rc, fetch_out, fetch_err = _run_git_cmd(
            ["fetch", remote_name, remote_branch],
            cwd=project_root,
            timeout=20.0,
        )

    if fetch_rc != 0:
        err_msg = fetch_err or fetch_out or "无法连接到 GitHub 远程仓库"
        return cur_short, False, 0, [], f"远程更新检查失败: {err_msg}"

    rc, remote_short, _ = _run_git_cmd(["rev-parse", "--short", f"{remote_name}/{remote_branch}"], cwd=project_root)
    if rc != 0 or not remote_short:
        rc, remote_short, _ = _run_git_cmd(["rev-parse", "--short", "FETCH_HEAD"], cwd=project_root)
        if rc != 0 or not remote_short:
            remote_short = cur_short

    rc, count_str, _ = _run_git_cmd(["rev-list", f"HEAD..{remote_name}/{remote_branch}", "--count"], cwd=project_root)
    if rc != 0 or not count_str.isdigit():
        rc, count_str, _ = _run_git_cmd(["rev-list", "HEAD..FETCH_HEAD", "--count"], cwd=project_root)

    behind_count = int(count_str) if (count_str and count_str.isdigit()) else 0
    has_update = behind_count > 0

    commits_log: List[str] = []
    if has_update:
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

    return remote_short, has_update, behind_count, commits_log, None


def _check_version_sync(project_root: Path, check_remote: bool = True) -> SystemVersionResponse:
    """
    Synchronous git inspection logic resolving local HEAD, commit metadata,
    and remote upstream status.
    """
    local_info = _inspect_local_git_repo(project_root)
    if not local_info:
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

    cur_short, branch, commit_date, commit_msg, remote_url = local_info

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

    remote_short, has_update, behind_count, commits_log, err = _query_remote_git_status(
        project_root, cur_short, remote_url
    )

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
        error=err,
    )


def _create_pre_update_backup(project_root: Path, modified_files: List[str]) -> Optional[Path]:
    """Zips uncommitted/untracked files to data/backups before git operations to prevent any data loss."""
    try:
        backup_dir = project_root / "data" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_zip = backup_dir / f"pre_update_backup_{ts}.zip"
        with zipfile.ZipFile(backup_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for rel_str in modified_files:
                file_path = project_root / rel_str
                if file_path.is_file():
                    zf.write(file_path, arcname=rel_str)
        # Keep only the latest 5 pre-update backups
        backups = sorted(backup_dir.glob("pre_update_backup_*.zip"), key=lambda p: p.stat().st_mtime)
        while len(backups) > 5:
            backups.pop(0).unlink(missing_ok=True)
        logger.info("Saved pre-update workspace snapshot to %s", backup_zip)
        return backup_zip
    except Exception as exc:
        logger.warning("Failed creating pre-update backup: %s", exc)
        return None


def _verify_update_preflight(project_root: Path) -> Tuple[Optional[str], Optional[SystemUpdateResponse]]:
    """Checks git repo validity, allowed remote source, and detached HEAD state."""
    rc, out, _ = _run_git_cmd(["rev-parse", "--is-inside-work-tree"], cwd=project_root, timeout=5.0)
    if rc != 0 or out != "true":
        msg = "更新失败: 当前项目目录不是有效 Git 仓库。"
        return None, SystemUpdateResponse(
            success=False,
            rebuilt_frontend=False,
            restart_required=False,
            output=msg,
            current_version="unknown",
            previous_version="unknown",
            error=msg,
        )

    remote_err = _verify_remote_url_allowed(project_root)
    if remote_err:
        return None, SystemUpdateResponse(
            success=False,
            rebuilt_frontend=False,
            restart_required=False,
            output=remote_err,
            current_version="unknown",
            previous_version="unknown",
            error=remote_err,
        )

    _, before_short, _ = _run_git_cmd(["rev-parse", "--short", "HEAD"], cwd=project_root)
    before_short = before_short or "unknown"

    rc, branch, _ = _run_git_cmd(["rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root)
    if branch == "HEAD" or rc != 0:
        msg = "当前 Git 仓库处于游离分支 (Detached HEAD) 状态，无法自动拉取。请先签出具体分支 (如 git checkout main)。"
        return before_short, SystemUpdateResponse(
            success=False,
            rebuilt_frontend=False,
            restart_required=False,
            output=msg,
            current_version=before_short,
            previous_version=before_short,
            error="Detached HEAD",
        )

    return before_short, None


def _handle_uncommitted_modifications(
    project_root: Path,
    before_short: str,
    stash_changes: bool,
    discard_local_changes: bool,
) -> Optional[SystemUpdateResponse]:
    """Inspects porcelain status and safely handles local uncommitted modifications."""
    rc, status_out, _ = _run_git_cmd(["status", "--porcelain"], cwd=project_root)
    if not status_out.strip():
        return None

    status_lines = [line.strip() for line in status_out.splitlines() if line.strip()]
    modified_files: List[str] = []
    for line in status_lines:
        content = line[2:].strip()
        parts = content.split(" -> ")
        modified_files.append(parts[-1].strip('"'))

    backup_zip_path = _create_pre_update_backup(project_root, modified_files)

    known_build_prefixes = (
        "galgame2voice/static/assets/index-",
        "galgame2voice\\static\\assets\\index-",
        "galgame2voice/static/index.html",
        "galgame2voice\\static\\index.html",
    )
    only_known_build_artifacts = bool(modified_files) and all(
        f.startswith(known_build_prefixes) for f in modified_files
    )

    if discard_local_changes:
        logger.warning("Discarding local uncommitted modifications per discard_local_changes flag")
        _run_git_cmd(["reset", "--hard", "HEAD"], cwd=project_root)
    elif stash_changes:
        logger.info("Stashing local uncommitted modifications per stash_changes flag")
        _run_git_cmd(["stash", "push", "-u", "-m", "auto-stash before webui update"], cwd=project_root)
    elif only_known_build_artifacts or all(f.startswith(("galgame2voice/static/", "galgame2voice\\static\\")) for f in modified_files):
        logger.info("Restoring tracked build artifacts before git pull (backup saved to %s)", backup_zip_path)
        _run_git_cmd(["checkout", "HEAD", "--", "galgame2voice/static"], cwd=project_root)
        _run_git_cmd(["clean", "-fd", "--", "galgame2voice/static"], cwd=project_root)
    else:
        msg = (
            "检测到本地工作区存在未提交的代码修改或未跟踪文件。为防止覆盖，系统已自动创建安全备份归档。\n"
            f"备份位置: {backup_zip_path or 'data/backups/'}\n"
            "请先提交 (git commit) 或开启暂存选项 (stash_changes) 后再尝试更新：\n" +
            "\n".join(f" - {f}" for f in modified_files[:10])
        )
        return SystemUpdateResponse(
            success=False,
            rebuilt_frontend=False,
            restart_required=False,
            output=msg,
            current_version=before_short,
            previous_version=before_short,
            error="Uncommitted changes in local workspace",
        )
    return None


def _execute_update_pull_and_build(
    project_root: Path,
    before_short: str,
    force_rebuild_frontend: bool,
) -> Tuple[SystemUpdateResponse, List[str]]:
    """Executes git pull, inspects changed files, and runs conditional frontend rebuild."""
    _, before_full, _ = _run_git_cmd(["rev-parse", "HEAD"], cwd=project_root)
    remote_name, remote_branch = _get_remote_and_branch(project_root)

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

    _, after_full, _ = _run_git_cmd(["rev-parse", "HEAD"], cwd=project_root)
    _, after_short, _ = _run_git_cmd(["rev-parse", "--short", "HEAD"], cwd=project_root)

    output_lines = [f"[Git Pull 成功] {pull_out}"]

    changed_files: List[str] = []
    if before_full and after_full and before_full != after_full:
        _, diff_out, _ = _run_git_cmd(["diff", "--name-only", before_full, after_full], cwd=project_root)
        changed_files = [line.strip() for line in diff_out.splitlines() if line.strip()]
        output_lines.append(f"共变更 {len(changed_files)} 个文件。")

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


def _apply_update_sync(
    project_root: Path,
    force_rebuild_frontend: bool = False,
    stash_changes: bool = False,
    discard_local_changes: bool = False,
) -> Tuple[SystemUpdateResponse, List[str]]:
    """
    Synchronous git pull execution with pre-flight safety validations
    (detached HEAD, uncommitted modifications).
    Automatically creates pre-update backup archive and safely handles tracked build artifacts.
    Never executes destructive git reset --hard or git clean -fd.
    Returns (response_object, list_of_changed_files).
    """
    before_short, preflight_err = _verify_update_preflight(project_root)
    if preflight_err:
        return preflight_err, []

    err_resp = _handle_uncommitted_modifications(
        project_root, before_short, stash_changes, discard_local_changes
    )
    if err_resp:
        return err_resp, []

    return _execute_update_pull_and_build(project_root, before_short, force_rebuild_frontend)


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
        default=False,
        description="Whether to fetch remote origin to check for pending updates",
    )
) -> SystemVersionResponse:
    """Returns local git commit metadata and pending updates comparison."""
    settings = get_settings()
    async with _GIT_OP_LOCK:
        return await asyncio.to_thread(_check_version_sync, settings.project_root, check_remote)


@router.get(
    "/update/check",
    response_model=SystemVersionResponse,
    summary="Check Updates from GitHub (Alias)",
    description="Alias endpoint for checking pending updates from GitHub remote.",
)
async def check_system_update() -> SystemVersionResponse:
    """Convenient alias explicitly checking for remote updates."""
    settings = get_settings()
    async with _GIT_OP_LOCK:
        return await asyncio.to_thread(_check_version_sync, settings.project_root, True)


@router.post(
    "/update/apply",
    response_model=SystemUpdateResponse,
    summary="Safely Pull and Apply Updates from GitHub",
    description="Pulls latest commits from origin/main, validates repo safety, rebuilds frontend if needed, and syncs characters.",
)
async def apply_system_update(payload: Optional[SystemUpdateRequest] = None) -> SystemUpdateResponse:
    """Executes safe pull from GitHub, rebuilds frontend, and syncs characters with SQLite DB."""
    settings = get_settings()
    force_rebuild = payload.force_rebuild_frontend if payload else False
    stash_changes = payload.stash_changes if payload else False
    discard_local_changes = payload.discard_local_changes if payload else False

    # Execute git pull and frontend rebuild in worker thread (serialized to
    # avoid concurrent git operations racing on the repository lock)
    async with _GIT_OP_LOCK:
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
