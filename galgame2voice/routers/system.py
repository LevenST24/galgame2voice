"""
System version endpoint for galgame2voice.

Reports the running package version and, on request, asks the official GitHub
Releases whether a newer release exists. It never spawns subprocesses and never
touches local files.
"""

from __future__ import annotations

import logging
import re
import sys

import httpx
from fastapi import APIRouter, Query
from pydantic import BaseModel

from galgame2voice import __version__
from galgame2voice.utils.http_client import create_async_client

logger = logging.getLogger("galgame2voice.routers.system")
router = APIRouter(prefix="/api/system", tags=["System & Update"])

GITHUB_RELEASES_URL = "https://github.com/LevenST24/galgame2voice/releases"
GITHUB_LATEST_RELEASE_API = "https://api.github.com/repos/LevenST24/galgame2voice/releases/latest"
_RELEASE_URL_PREFIX = "https://github.com/LevenST24/galgame2voice/"


class SystemVersionResponse(BaseModel):
    current_version: str
    current_branch: str
    remote_url: str = GITHUB_RELEASES_URL
    release_url: str = GITHUB_RELEASES_URL
    latest_version: str | None = None
    published_at: str | None = None
    has_update: bool = False
    error: str | None = None


def _parse_version(text: str) -> tuple[int, int, int] | None:
    match = re.match(r"[vV]?(\d+)\.(\d+)\.(\d+)", text.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


@router.get(
    "/version",
    response_model=SystemVersionResponse,
    summary="Get System Version & Check GitHub Releases",
    description="Returns the local version and optionally queries GitHub Releases for a newer official release.",
)
async def get_system_version(
    check_remote: bool = Query(default=False),
) -> SystemVersionResponse:
    local = SystemVersionResponse(
        current_version=__version__,
        current_branch="portable" if getattr(sys, "frozen", False) else "source",
    )
    if not check_remote:
        return local

    try:
        async with create_async_client(timeout=10.0) as client:
            resp = await client.get(
                GITHUB_LATEST_RELEASE_API,
                headers={"Accept": "application/vnd.github+json"},
            )
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("GitHub Releases version check failed: %s", exc)
        return local.model_copy(update={"error": "无法连接 GitHub Releases，请检查网络或直接前往下载页查看。"})
    if resp.status_code in (403, 429):
        return local.model_copy(update={"error": "GitHub 接口请求受限，请稍后再试或直接前往下载页查看。"})

    unavailable = local.model_copy(update={"error": "暂时无法获取 GitHub Releases 版本信息，请直接前往下载页查看。"})
    try:
        release = resp.json()
    except ValueError:
        return unavailable
    tag = release.get("tag_name") if isinstance(release, dict) else None
    latest = _parse_version(tag) if isinstance(tag, str) else None
    if latest is None:
        return unavailable

    html_url = release.get("html_url")
    return local.model_copy(
        update={
            "latest_version": tag.strip().lstrip("vV"),
            "release_url": html_url
            if isinstance(html_url, str) and html_url.startswith(_RELEASE_URL_PREFIX)
            else GITHUB_RELEASES_URL,
            "published_at": release.get("published_at"),
            "has_update": latest > _parse_version(__version__),
        }
    )
