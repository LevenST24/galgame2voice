"""
Route-level tests for structured diagnostics returned by POST /api/providers/test.
"""

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database.session import init_db
from galgame2voice.main import create_app


@pytest.fixture
async def app_client(isolate_test_database):
    await init_db(isolate_test_database)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _patch_upstream(monkeypatch, handler):
    orig_get = httpx.AsyncClient.get
    orig_post = httpx.AsyncClient.post

    def _is_internal(url_str: str) -> bool:
        return (
            not url_str.startswith("http://") and not url_str.startswith("https://")
        ) or url_str.startswith("http://test")

    async def mocked_get(self_client, url, *args, **kwargs):
        url_str = str(url)
        if _is_internal(url_str):
            return await orig_get(self_client, url, *args, **kwargs)
        return await handler("GET", url_str, kwargs)

    async def mocked_post(self_client, url, *args, **kwargs):
        url_str = str(url)
        if _is_internal(url_str):
            return await orig_post(self_client, url, *args, **kwargs)
        return await handler("POST", url_str, kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", mocked_get)
    monkeypatch.setattr(httpx.AsyncClient, "post", mocked_post)


@pytest.mark.asyncio
async def test_provider_test_402_insufficient_balance(app_client, monkeypatch):
    async def upstream(method: str, url: str, _kwargs: dict):
        return httpx.Response(
            402,
            json={"error": {"message": "Insufficient Balance"}},
            request=httpx.Request(method, url),
        )

    _patch_upstream(monkeypatch, upstream)

    resp = await app_client.post(
        "/api/providers/test",
        json={
            "id": "deepseek",
            "api_key": "sk-deepseek-test-key-12345",
            "base_url": "https://api.deepseek.com",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["status_code"] == 402
    assert data["error_code"] == "QUOTA_EXCEEDED"
    assert data["error_title"] and "余额" in data["error_title"]
    assert data["diagnostic"] and ("充值" in data["diagnostic"] or "账单" in data["diagnostic"])
    assert data["error"] == data["error_title"]


@pytest.mark.asyncio
async def test_provider_test_429_rate_limit_not_misclassified_by_28401(app_client, monkeypatch):
    async def upstream(method: str, url: str, _kwargs: dict):
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": (
                        "Rate limit reached for gpt-4o on tokens per min (TPM): "
                        "Limit 30000, Used 28401, Requested 2100."
                    )
                }
            },
            request=httpx.Request(method, url),
        )

    _patch_upstream(monkeypatch, upstream)

    resp = await app_client.post(
        "/api/providers/test",
        json={
            "id": "openai",
            "api_key": "sk-openai-test-key-12345",
            "base_url": "https://api.openai.com/v1",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["status_code"] == 429
    assert data["error_code"] in ("OPENAI_RATE_LIMIT", "RATE_LIMIT", "OPENAI_QUOTA_EXCEEDED", "QUOTA_EXCEEDED")
    assert data["error_title"] and "429" in data["error_title"]
    assert "401" not in data["error_title"]
    assert "验证失败" not in data["error_title"]
    assert data["diagnostic"]


@pytest.mark.asyncio
async def test_provider_test_401_auth_failed_openai_and_anthropic(app_client, monkeypatch):
    async def upstream(method: str, url: str, _kwargs: dict):
        return httpx.Response(
            401,
            text="Unauthorized",
            request=httpx.Request(method, url),
        )

    _patch_upstream(monkeypatch, upstream)

    resp_openai = await app_client.post(
        "/api/providers/test",
        json={
            "id": "openai",
            "api_key": "sk-invalid-openai-key",
            "base_url": "https://api.openai.com/v1",
        },
    )
    assert resp_openai.status_code == 200
    data_openai = resp_openai.json()
    assert data_openai["success"] is False
    assert data_openai["status_code"] == 401
    assert data_openai["error_code"] == "OPENAI_AUTH_FAILED"
    assert data_openai["error_title"] and "验证失败" in data_openai["error_title"]
    assert data_openai["diagnostic"] and "密钥" in data_openai["diagnostic"]

    resp_anthropic = await app_client.post(
        "/api/providers/test",
        json={
            "id": "anthropic",
            "api_key": "sk-ant-invalid-key",
            "base_url": "https://api.anthropic.com/v1",
        },
    )
    assert resp_anthropic.status_code == 200
    data_anthropic = resp_anthropic.json()
    assert data_anthropic["success"] is False
    assert data_anthropic["status_code"] == 401
    assert data_anthropic["error_code"] == "ANTHROPIC_AUTH_FAILED"
    assert data_anthropic["error_title"] and "验证失败" in data_anthropic["error_title"]
    assert data_anthropic["diagnostic"] and "密钥" in data_anthropic["diagnostic"]


@pytest.mark.asyncio
async def test_provider_test_timeout_and_connect_error_preserve_transport_diagnosis(app_client, monkeypatch):
    async def timeout_upstream(method: str, url: str, _kwargs: dict):
        raise httpx.ReadTimeout("timed out", request=httpx.Request(method, url))

    _patch_upstream(monkeypatch, timeout_upstream)

    resp_timeout = await app_client.post(
        "/api/providers/test",
        json={
            "id": "openai",
            "api_key": "sk-openai-timeout-key",
            "base_url": "https://api.openai.com/v1",
        },
    )
    assert resp_timeout.status_code == 200
    data_timeout = resp_timeout.json()
    assert data_timeout["success"] is False
    assert data_timeout["error_code"] == "NETWORK_TIMEOUT"
    assert data_timeout["status_code"] == 408
    assert data_timeout["error_title"] and "超时" in data_timeout["error_title"]
    assert data_timeout["diagnostic"] and ("代理" in data_timeout["diagnostic"] or "网络" in data_timeout["diagnostic"])

    async def tls_upstream(method: str, url: str, _kwargs: dict):
        raise httpx.ConnectError("TLS handshake failed", request=httpx.Request(method, url))

    _patch_upstream(monkeypatch, tls_upstream)

    resp_tls = await app_client.post(
        "/api/providers/test",
        json={
            "id": "openai",
            "api_key": "sk-openai-tls-key",
            "base_url": "https://api.openai.com/v1",
        },
    )
    assert resp_tls.status_code == 200
    data_tls = resp_tls.json()
    assert data_tls["success"] is False
    assert data_tls["error_code"] == "NETWORK_CONNECTION_FAILED"
    assert data_tls["error_title"] and "连接失败" in data_tls["error_title"]
