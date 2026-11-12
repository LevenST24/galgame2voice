"""
Route-level tests for structured diagnostics returned by POST /api/providers/test.
"""

import json
from pathlib import Path
import re

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database.session import init_db
from galgame2voice.main import create_app
from galgame2voice.utils.error_diagnostics import (
    EMPTY_REPLY_CONTENT_FILTERED,
    ConfigurationSelectionError,
    EmptyReplyError,
    StreamIncompleteError,
    StreamTruncatedError,
    chat_error_payload,
)
from scripts.package_release import should_include

_CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


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


@pytest.mark.asyncio
async def test_provider_test_ssrf_and_credential_mismatch_return_structured_diagnostics(app_client):
    # Scenario A: private/loopback address rejected when private endpoints are disabled
    resp_ssrf = await app_client.post(
        "/api/providers/test",
        json={
            "id": "custom",
            "api_key": "dummy-local-key",
            "base_url": "http://127.0.0.1:11434/v1",
        },
    )
    assert resp_ssrf.status_code == 400
    data_ssrf = resp_ssrf.json()
    assert "detail" in data_ssrf and "允许私网 LLM 端点" in data_ssrf["detail"]
    assert data_ssrf.get("success") is False
    assert data_ssrf.get("status_code") == 400
    assert data_ssrf.get("diagnostic_code") == "PRIVATE_ENDPOINT_BLOCKED"
    assert data_ssrf.get("error_code") == "PRIVATE_ENDPOINT_BLOCKED"
    assert data_ssrf.get("error_title") and _CJK_RE.search(data_ssrf["error_title"])
    assert data_ssrf.get("summary_zh") and _CJK_RE.search(data_ssrf["summary_zh"])
    assert data_ssrf.get("diagnostic") and _CJK_RE.search(data_ssrf["diagnostic"])
    assert data_ssrf.get("action_zh") and _CJK_RE.search(data_ssrf["action_zh"])
    assert data_ssrf.get("retryable") is False

    # Scenario B: stored OpenAI secret + masked key + custom base_url rejected
    save_resp = await app_client.post(
        "/api/providers",
        json={
            "id": "openai",
            "api_key": "sk-real-secret-key-12345678",
            "api_base_url": "https://api.openai.com/v1",
        },
    )
    assert save_resp.status_code == 200
    masked_key = save_resp.json()["provider"]["api_key"]
    assert masked_key.startswith("sk-****")

    resp_mismatch = await app_client.post(
        "/api/providers/test",
        json={
            "id": "openai",
            "api_key": masked_key,
            "base_url": "https://api.example.com/v1",
        },
    )
    assert resp_mismatch.status_code == 400
    data_mismatch = resp_mismatch.json()
    assert "detail" in data_mismatch and "Cannot test custom base_url with stored API key" in data_mismatch["detail"]
    assert data_mismatch.get("success") is False
    assert data_mismatch.get("status_code") == 400
    assert data_mismatch.get("diagnostic_code") == "CREDENTIAL_HOST_MISMATCH"
    assert data_mismatch.get("error_code") == "CREDENTIAL_HOST_MISMATCH"
    assert data_mismatch.get("error_title") and _CJK_RE.search(data_mismatch["error_title"])
    assert data_mismatch.get("summary_zh") and _CJK_RE.search(data_mismatch["summary_zh"])
    assert data_mismatch.get("diagnostic") and _CJK_RE.search(data_mismatch["diagnostic"])
    assert data_mismatch.get("action_zh") and _CJK_RE.search(data_mismatch["action_zh"])
    assert data_mismatch.get("retryable") is False

    # Scenario C: invalid scheme URL rejected with INVALID_BASE_URL while preserving detail
    resp_invalid_url = await app_client.post(
        "/api/providers/test",
        json={
            "id": "custom",
            "api_key": "sk-explicit-key-12345",
            "base_url": "ftp://api.example.com/v1",
        },
    )
    assert resp_invalid_url.status_code == 400
    data_invalid_url = resp_invalid_url.json()
    assert "detail" in data_invalid_url and data_invalid_url["detail"]
    assert data_invalid_url.get("diagnostic_code") == "INVALID_BASE_URL"
    assert data_invalid_url.get("error_code") == "INVALID_BASE_URL"
    assert data_invalid_url.get("summary_zh") and _CJK_RE.search(data_invalid_url["summary_zh"])
    assert data_invalid_url.get("action_zh") and _CJK_RE.search(data_invalid_url["action_zh"])
    assert data_invalid_url.get("retryable") is False


@pytest.mark.asyncio
async def test_get_providers_includes_builtin_presets_without_breaking_legacy_presets_contract(app_client):
    legacy_contract = json.loads((_CONTRACTS_DIR / "provider_presets_v1.json").read_text(encoding="utf-8"))

    resp = await app_client.get("/api/providers")
    assert resp.status_code == 200
    data = resp.json()

    # Legacy "presets" must remain byte-for-byte structurally identical to v1 contract
    assert data["presets"] == legacy_contract["presets"]

    assert "builtin_presets" in data
    builtin = data["builtin_presets"]
    assert isinstance(builtin, list) and len(builtin) == 10

    expected_ids = [p["id"] for p in legacy_contract["presets"]]
    assert [p["id"] for p in builtin] == expected_ids

    stt_supported_ids = {"openai", "qwen", "siliconflow", "custom"}
    for item in builtin:
        assert set(item.keys()) == {
            "id",
            "name",
            "default_base_url",
            "default_chat_model",
            "default_stt_model",
            "default_reasoning_effort",
            "requires_api_key",
            "supports_stt",
            "preset_models",
            "description",
        }
        if item["id"] == "custom":
            assert item["requires_api_key"] is False
        else:
            assert item["requires_api_key"] is True

        assert item["supports_stt"] is (item["id"] in stt_supported_ids)
        if item["id"] == "gemini":
            assert item["default_reasoning_effort"] == "low"
        else:
            assert item["default_reasoning_effort"] is None

    # Mutating returned preset_models must not mutate global PROVIDER_PRESETS
    from galgame2voice.adapters import registry

    first_builtin = registry.list_builtin_provider_presets()
    first_builtin[0]["preset_models"].append("mutated-model")
    second_builtin = registry.list_builtin_provider_presets()
    assert "mutated-model" not in second_builtin[0]["preset_models"]


@pytest.mark.asyncio
async def test_provider_diagnostics_v1_contract_samples(app_client, monkeypatch):
    assert should_include(Path("tests/contracts/provider_diagnostics_v1.json")) is True

    contract_file = _CONTRACTS_DIR / "provider_diagnostics_v1.json"
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    assert contract["schema_version"] == "provider_diagnostics_v1"
    samples = contract["samples"]
    assert len(samples) >= 8

    for sample in samples:
        kind = sample["kind"]
        expected = sample["expected"]
        assert _CJK_RE.search(expected["summary_zh"]), f"{sample['id']} summary_zh must be Chinese"
        assert _CJK_RE.search(expected["action_zh"]), f"{sample['id']} action_zh must be Chinese"

        if kind == "upstream_http":
            status_code = sample["upstream_status"]
            body = sample["upstream_body"]

            async def upstream_http(method: str, url: str, _kwargs: dict, sc=status_code, text=body):
                return httpx.Response(sc, text=text, request=httpx.Request(method, url))

            _patch_upstream(monkeypatch, upstream_http)
            resp = await app_client.post(
                "/api/providers/test",
                json={
                    "id": sample["provider_id"],
                    "api_key": "sk-contract-test-key-12345",
                    "base_url": sample["base_url"],
                },
            )
            assert resp.status_code == expected["provider_test_http_status"], sample["id"]
            payload = resp.json()
            assert payload["diagnostic_code"] == expected["diagnostic_code"], sample["id"]
            assert payload["retryable"] is expected["retryable"], sample["id"]
            assert payload["summary_zh"] == expected["summary_zh"], sample["id"]
            assert payload["action_zh"] == expected["action_zh"], sample["id"]
            assert payload["error_code"] == expected["legacy_error_code"], sample["id"]
            assert payload["error"] == payload["error_title"] == expected["summary_zh"], sample["id"]

            chat_err = RuntimeError(f"API returned status {status_code}: {body}")
            chat_payload = chat_error_payload(chat_err, provider_id=sample["provider_id"], api_key_configured=True)
            assert chat_payload["diagnostic_code"] == expected["diagnostic_code"], sample["id"]
            assert chat_payload["retryable"] is expected["retryable"], sample["id"]
            assert chat_payload["summary_zh"] == expected["summary_zh"], sample["id"]
            assert chat_payload["action_zh"] == expected["action_zh"], sample["id"]
            assert chat_payload["error_code"] == expected["legacy_error_code"], sample["id"]
            assert expected["summary_zh"] in chat_payload["error"], sample["id"]

        elif kind == "transport_timeout":
            msg = sample["exception_message"]

            async def upstream_timeout(method: str, url: str, _kwargs: dict, err_msg=msg):
                raise httpx.ReadTimeout(err_msg, request=httpx.Request(method, url))

            _patch_upstream(monkeypatch, upstream_timeout)
            resp = await app_client.post(
                "/api/providers/test",
                json={
                    "id": sample["provider_id"],
                    "api_key": "sk-contract-timeout-key-12345",
                    "base_url": sample["base_url"],
                },
            )
            assert resp.status_code == expected["provider_test_http_status"], sample["id"]
            payload = resp.json()
            assert payload["diagnostic_code"] == expected["diagnostic_code"], sample["id"]
            assert payload["retryable"] is expected["retryable"], sample["id"]
            assert payload["summary_zh"] == expected["summary_zh"], sample["id"]
            assert payload["action_zh"] == expected["action_zh"], sample["id"]
            assert payload["error_code"] == expected["legacy_error_code"], sample["id"]

            timeout_exc = httpx.ReadTimeout(msg, request=httpx.Request("POST", sample["base_url"]))
            for key_configured in (True, False):
                chat_payload = chat_error_payload(
                    timeout_exc, provider_id=sample["provider_id"], api_key_configured=key_configured
                )
                assert chat_payload["diagnostic_code"] == expected["diagnostic_code"], sample["id"]
                assert chat_payload["retryable"] is expected["retryable"], sample["id"]
                assert chat_payload["summary_zh"] == expected["summary_zh"], sample["id"]
                assert chat_payload["action_zh"] == expected["action_zh"], sample["id"]
                assert chat_payload["error_code"] == expected["legacy_error_code"], sample["id"]

        elif kind == "preflight_private_endpoint":
            resp = await app_client.post(
                "/api/providers/test",
                json={
                    "id": sample["provider_id"],
                    "api_key": "dummy-key",
                    "base_url": sample["base_url"],
                },
            )
            assert resp.status_code == expected["provider_test_http_status"], sample["id"]
            payload = resp.json()
            assert "detail" in payload and payload["detail"], sample["id"]
            assert payload["diagnostic_code"] == expected["diagnostic_code"], sample["id"]
            assert payload["retryable"] is expected["retryable"], sample["id"]
            assert payload["summary_zh"] == expected["summary_zh"], sample["id"]
            assert payload["action_zh"] == expected["action_zh"], sample["id"]
            assert payload["error_code"] == expected["legacy_error_code"], sample["id"]

        elif kind == "preflight_credential_mismatch":
            await app_client.post(
                "/api/providers",
                json={
                    "id": sample["provider_id"],
                    "api_key": "sk-stored-secret-key-99887766",
                    "api_base_url": sample["stored_base_url"],
                },
            )
            resp = await app_client.post(
                "/api/providers/test",
                json={
                    "id": sample["provider_id"],
                    "api_key": "sk-****7766",
                    "base_url": sample["test_base_url"],
                },
            )
            assert resp.status_code == expected["provider_test_http_status"], sample["id"]
            payload = resp.json()
            assert "detail" in payload and payload["detail"], sample["id"]
            assert payload["diagnostic_code"] == expected["diagnostic_code"], sample["id"]
            assert payload["retryable"] is expected["retryable"], sample["id"]
            assert payload["summary_zh"] == expected["summary_zh"], sample["id"]
            assert payload["action_zh"] == expected["action_zh"], sample["id"]
            assert payload["error_code"] == expected["legacy_error_code"], sample["id"]

        elif kind == "chat_empty_reply":
            exc = EmptyReplyError(stopped_on_length=sample["stopped_on_length"])
            chat_payload = chat_error_payload(exc)
            assert chat_payload["diagnostic_code"] == expected["diagnostic_code"], sample["id"]
            assert chat_payload["retryable"] is expected["retryable"], sample["id"]
            assert chat_payload["summary_zh"] == expected["summary_zh"], sample["id"]
            assert chat_payload["action_zh"] == expected["action_zh"], sample["id"]
            assert chat_payload["error_code"] == expected["legacy_error_code"], sample["id"]
            assert chat_payload["kind"] == "empty_reply", sample["id"]
            assert chat_payload["error"] == str(exc), sample["id"]

        elif kind == "chat_stream_incomplete":
            exc = StreamIncompleteError()
            chat_payload = chat_error_payload(exc)
            assert chat_payload["diagnostic_code"] == expected["diagnostic_code"], sample["id"]
            assert chat_payload["retryable"] is expected["retryable"], sample["id"]
            assert chat_payload["summary_zh"] == expected["summary_zh"], sample["id"]
            assert chat_payload["action_zh"] == expected["action_zh"], sample["id"]
            assert chat_payload["error_code"] == expected["legacy_error_code"], sample["id"]
            assert chat_payload["error"] == str(exc), sample["id"]
        else:
            pytest.fail(f"Unhandled contract sample kind: {kind}")

    # Verify remaining chat_error_payload branches (content-filtered EmptyReplyError, StreamTruncatedError & ConfigurationSelectionError)
    empty_cf = chat_error_payload(EmptyReplyError(EMPTY_REPLY_CONTENT_FILTERED))
    assert empty_cf["diagnostic_code"] == "EMPTY_REPLY_CONTENT_FILTERED"
    assert empty_cf["error_code"] == "empty_reply"
    assert empty_cf["kind"] == "empty_reply"
    assert empty_cf["retryable"] is False
    assert _CJK_RE.search(empty_cf["summary_zh"]) and _CJK_RE.search(empty_cf["action_zh"])

    trunc_len = chat_error_payload(StreamTruncatedError(stopped_on_length=True))
    assert trunc_len["diagnostic_code"] == "STREAM_TRUNCATED_ON_LENGTH"
    assert trunc_len["error_code"] == "stream_truncated_on_length"
    assert trunc_len["retryable"] is False
    assert _CJK_RE.search(trunc_len["summary_zh"]) and _CJK_RE.search(trunc_len["action_zh"])

    trunc_cf = chat_error_payload(StreamTruncatedError(stopped_on_length=False))
    assert trunc_cf["diagnostic_code"] == "STREAM_TRUNCATED_CONTENT_FILTERED"
    assert trunc_cf["error_code"] == "stream_truncated_content_filtered"
    assert trunc_cf["retryable"] is False
    assert _CJK_RE.search(trunc_cf["summary_zh"]) and _CJK_RE.search(trunc_cf["action_zh"])

    cfg_err = chat_error_payload(
        ConfigurationSelectionError("指定的模型配置已不存在", error_code="provider_not_found", status_code=404)
    )
    assert cfg_err["diagnostic_code"] == "PROVIDER_NOT_FOUND"
    assert cfg_err["error_code"] == "provider_not_found"
    assert cfg_err["retryable"] is False
    assert _CJK_RE.search(cfg_err["summary_zh"]) and _CJK_RE.search(cfg_err["action_zh"])
