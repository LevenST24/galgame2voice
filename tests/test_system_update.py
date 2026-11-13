"""
Tests for the system version endpoint and the retired in-app update endpoints.

- GET /api/system/update/check and POST /api/system/update/apply no longer exist
- GET /api/system/version without check_remote reads the running version only:
  no HTTP client, no subprocess
- GET /api/system/version?check_remote=true against a stubbed GitHub Releases API:
  newer / same / older / differently formatted tags, release_url validation, every failure mode
- Console authentication on /api/system/version
"""

import asyncio
import subprocess
import sys

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import galgame2voice
from galgame2voice.main import create_app
from galgame2voice.routers import system

RELEASES_URL = "https://github.com/LevenST24/galgame2voice/releases"
LATEST_RELEASE_API = "https://api.github.com/repos/LevenST24/galgame2voice/releases/latest"
NETWORK_ERROR = "无法连接 GitHub Releases，请检查网络或直接前往下载页查看。"
RATE_LIMIT_ERROR = "GitHub 接口请求受限，请稍后再试或直接前往下载页查看。"
UNAVAILABLE_ERROR = "暂时无法获取 GitHub Releases 版本信息，请直接前往下载页查看。"


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def github(monkeypatch):
    """Answer the GitHub request made by the version route from a handler instead of the network.

    Call github(handler) to install the handler; the requests it received are in github.requests.
    """

    def serve(handler):
        def record_and_handle(request):
            serve.requests.append(request)
            return handler(request)

        monkeypatch.setattr(
            system,
            "create_async_client",
            lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(record_and_handle), **kwargs),
        )

    serve.requests = []
    return serve


async def get(app, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        return await client.get(path)


def release(tag, html_url=None, **extra):
    return httpx.Response(
        200,
        json={"tag_name": tag, "html_url": html_url or f"{RELEASES_URL}/tag/{tag}", **extra},
    )


def refuse(request):
    raise httpx.ConnectTimeout("timed out", request=request)


def local_only(error=None):
    return {
        "current_version": galgame2voice.__version__,
        "current_branch": "source",
        "remote_url": RELEASES_URL,
        "release_url": RELEASES_URL,
        "latest_version": None,
        "published_at": None,
        "has_update": False,
        "error": error,
    }


@pytest.mark.asyncio
async def test_update_check_and_apply_routes_are_gone(app):
    assert (await get(app, "/api/system/update/check")).status_code == 404
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        response = await client.post("/api/system/update/apply", json={"discard_local_changes": True})
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/system/version", "/api/system/version?check_remote=false"])
async def test_local_version_makes_no_network_or_subprocess_calls(app, monkeypatch, path):
    def forbidden(*args, **kwargs):
        raise AssertionError("the local version read must not reach for the network or spawn a process")

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(system, "create_async_client", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)

    response = await get(app, path)

    assert response.status_code == 200
    assert response.json() == local_only()


@pytest.mark.asyncio
async def test_newer_release_is_reported_with_its_release_page(app, github):
    github(lambda request: release("v99.0.0", published_at="2026-11-10T08:00:00Z"))

    response = await get(app, "/api/system/version?check_remote=true")

    assert response.status_code == 200
    assert [str(request.url) for request in github.requests] == [LATEST_RELEASE_API]
    assert github.requests[0].headers["accept"] == "application/vnd.github+json"
    assert response.json() == {
        **local_only(),
        "latest_version": "99.0.0",
        "release_url": f"{RELEASES_URL}/tag/v99.0.0",
        "published_at": "2026-11-10T08:00:00Z",
        "has_update": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tag", "has_update"),
    [
        (f"v{galgame2voice.__version__}", False),
        ("0.0.1", False),
        ("V99.0.0", True),
        ("99.0.0", True),
        ("v10.0.0", True),  # compared as numbers: as text "10" sorts before "3"
    ],
)
async def test_release_tag_is_compared_with_the_running_version(app, github, tag, has_update):
    github(lambda request: release(tag))

    data = (await get(app, "/api/system/version?check_remote=true")).json()

    assert data["has_update"] is has_update
    assert data["latest_version"] == tag.lstrip("vV")
    assert data["error"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "html_url",
    [
        "https://evil.example.com/LevenST24/galgame2voice/releases/tag/v99.1.0",
        "https://github.com/LevenST24/galgame2voice-fork/releases/tag/v99.1.0",
        "javascript:alert(1)",
        None,
    ],
)
async def test_release_page_outside_this_repository_falls_back_to_the_releases_page(app, github, html_url):
    github(lambda request: httpx.Response(200, json={"tag_name": "v99.1.0", "html_url": html_url}))

    data = (await get(app, "/api/system/version?check_remote=true")).json()

    assert data["has_update"] is True
    assert data["release_url"] == RELEASES_URL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "error"),
    [
        (refuse, NETWORK_ERROR),
        (lambda request: httpx.Response(403, json={"message": "API rate limit exceeded"}), RATE_LIMIT_ERROR),
        (lambda request: httpx.Response(429), RATE_LIMIT_ERROR),
        (lambda request: httpx.Response(404, json={"message": "Not Found"}), UNAVAILABLE_ERROR),
        (lambda request: httpx.Response(502, text="<html>Bad Gateway</html>"), UNAVAILABLE_ERROR),
        (lambda request: httpx.Response(200, text="<html>captive portal</html>"), UNAVAILABLE_ERROR),
        (lambda request: httpx.Response(200, json={"name": "no tag"}), UNAVAILABLE_ERROR),
        (lambda request: httpx.Response(200, json={"tag_name": "nightly"}), UNAVAILABLE_ERROR),
        (lambda request: httpx.Response(200, json=["not", "a", "release"]), UNAVAILABLE_ERROR),
        (lambda request: httpx.Response(200, json={"tag_name": 3}), UNAVAILABLE_ERROR),
    ],
)
async def test_github_failures_come_back_as_a_chinese_error_not_a_500(app, github, handler, error):
    github(handler)

    response = await get(app, "/api/system/version?check_remote=true")

    assert response.status_code == 200
    assert response.json() == local_only(error)


@pytest.mark.asyncio
async def test_http_client_that_cannot_be_built_comes_back_as_a_chinese_error_not_a_500(app, monkeypatch):
    def broken_client(**kwargs):
        raise FileNotFoundError("no CA bundle")

    monkeypatch.setattr(system, "create_async_client", broken_client)

    response = await get(app, "/api/system/version?check_remote=true")

    assert response.status_code == 200
    assert response.json() == local_only(NETWORK_ERROR)


@pytest.mark.asyncio
async def test_version_requires_the_console_token_when_auth_is_enabled(monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_AUTH_DISABLED", "0")
    monkeypatch.setenv("GALGAME2VOICE_CONSOLE_TOKEN", "secret-test-token-1234")
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/system/version")).status_code == 401

        headers = {"Authorization": "Bearer secret-test-token-1234"}
        response = await client.get("/api/system/version?check_remote=false", headers=headers)
        assert response.status_code == 200
        assert response.json()["current_version"] == galgame2voice.__version__
