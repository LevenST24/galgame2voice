"""
Route-level tests for structured diagnostics returned by POST /api/providers/test.
"""

import json
from pathlib import Path
import re
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter
from galgame2voice.adapters.llm.xai_adapter import XAILLMAdapter
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
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        yield client


def _patch_upstream(monkeypatch, handler):
    orig_get = httpx.AsyncClient.get
    orig_post = httpx.AsyncClient.post

    def _is_internal(url_str: str) -> bool:
        return (
            not url_str.startswith("http://") and not url_str.startswith("https://")
        ) or url_str.startswith("http://127.0.0.1:8080")

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


def _patch_upstream_response(monkeypatch, status_code: int, **response_kwargs):
    async def upstream(method: str, url: str, _kwargs: dict):
        return httpx.Response(status_code, **response_kwargs, request=httpx.Request(method, url))

    _patch_upstream(monkeypatch, upstream)


def _patch_upstream_error(monkeypatch, exc_cls, message: str):
    async def upstream(method: str, url: str, _kwargs: dict):
        raise exc_cls(message, request=httpx.Request(method, url))

    _patch_upstream(monkeypatch, upstream)


async def _post_provider_test(app_client: AsyncClient, provider_id: str, api_key: str, base_url: str):
    return await app_client.post(
        "/api/providers/test",
        json={"id": provider_id, "api_key": api_key, "base_url": base_url},
    )


@pytest.mark.asyncio
async def test_provider_test_402_insufficient_balance(app_client, monkeypatch):
    _patch_upstream_response(monkeypatch, 402, json={"error": {"message": "Insufficient Balance"}})

    resp = await _post_provider_test(
        app_client, "deepseek", "sk-deepseek-test-key-12345", "https://api.deepseek.com"
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
    _patch_upstream_response(
        monkeypatch,
        429,
        json={
            "error": {
                "message": (
                    "Rate limit reached for gpt-4o on tokens per min (TPM): "
                    "Limit 30000, Used 28401, Requested 2100."
                )
            }
        },
    )

    resp = await _post_provider_test(
        app_client, "openai", "sk-openai-test-key-12345", "https://api.openai.com/v1"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["status_code"] == 429
    assert data["error_code"] in ("OPENAI_RATE_LIMIT", "RATE_LIMIT", "OPENAI_QUOTA_EXCEEDED", "QUOTA_EXCEEDED")
    assert data["error_title"] and "频率超限" in data["error_title"]
    assert "401" not in data["error_title"]
    assert "验证失败" not in data["error_title"]
    assert data["diagnostic"]


@pytest.mark.asyncio
async def test_provider_test_401_auth_failed_openai_and_anthropic(app_client, monkeypatch):
    _patch_upstream_response(monkeypatch, 401, text="Unauthorized")

    resp_openai = await _post_provider_test(
        app_client, "openai", "sk-invalid-openai-key", "https://api.openai.com/v1"
    )
    assert resp_openai.status_code == 200
    data_openai = resp_openai.json()
    assert data_openai["success"] is False
    assert data_openai["status_code"] == 401
    assert data_openai["error_code"] == "OPENAI_AUTH_FAILED"
    assert data_openai["error_title"] and "验证失败" in data_openai["error_title"]
    assert data_openai["diagnostic"] and "密钥" in data_openai["diagnostic"]

    resp_anthropic = await _post_provider_test(
        app_client, "anthropic", "sk-ant-invalid-key", "https://api.anthropic.com/v1"
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
    _patch_upstream_error(monkeypatch, httpx.ReadTimeout, "timed out")

    resp_timeout = await _post_provider_test(
        app_client, "openai", "sk-openai-timeout-key", "https://api.openai.com/v1"
    )
    assert resp_timeout.status_code == 200
    data_timeout = resp_timeout.json()
    assert data_timeout["success"] is False
    assert data_timeout["error_code"] == "NETWORK_TIMEOUT"
    assert data_timeout["status_code"] == 408
    assert data_timeout["error_title"] and "超时" in data_timeout["error_title"]
    assert data_timeout["diagnostic"] and ("代理" in data_timeout["diagnostic"] or "网络" in data_timeout["diagnostic"])

    _patch_upstream_error(monkeypatch, httpx.ConnectError, "TLS handshake failed")

    resp_tls = await _post_provider_test(
        app_client, "openai", "sk-openai-tls-key", "https://api.openai.com/v1"
    )
    assert resp_tls.status_code == 200
    data_tls = resp_tls.json()
    assert data_tls["success"] is False
    assert data_tls["error_code"] == "NETWORK_CONNECTION_FAILED"
    assert data_tls["error_title"] and "连接失败" in data_tls["error_title"]


@pytest.mark.asyncio
async def test_provider_test_ssrf_and_credential_mismatch_return_structured_diagnostics(app_client):
    # Scenario A: private/loopback address rejected when private endpoints are disabled
    resp_ssrf = await _post_provider_test(app_client, "custom", "dummy-local-key", "http://127.0.0.1:11434/v1")
    assert resp_ssrf.status_code == 400
    data_ssrf = resp_ssrf.json()
    assert "detail" in data_ssrf and "允许连接本机或局域网的模型服务" in data_ssrf["detail"]
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

    resp_mismatch = await _post_provider_test(app_client, "openai", masked_key, "https://api.example.com/v1")
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
    resp_invalid_url = await _post_provider_test(
        app_client, "custom", "sk-explicit-key-12345", "ftp://api.example.com/v1"
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

    for item in builtin:
        assert set(item.keys()) == {
            "id",
            "name",
            "default_base_url",
            "default_chat_model",
            "default_stt_model",
            "default_reasoning_effort",
            "requires_api_key",
            "preset_models",
            "description",
        }
        if item["id"] == "custom":
            assert item["requires_api_key"] is False
        else:
            assert item["requires_api_key"] is True

        assert item["default_reasoning_effort"] == {
            "gemini": "low", "openai": "none", "deepseek": "none", "anthropic": "none",
            "xai": "low", "glm": "none", "moonshot": "low",
        }.get(item["id"])

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

            _patch_upstream_response(monkeypatch, status_code, text=body)
            resp = await _post_provider_test(
                app_client, sample["provider_id"], "sk-contract-test-key-12345", sample["base_url"]
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

            _patch_upstream_error(monkeypatch, httpx.ReadTimeout, msg)
            resp = await _post_provider_test(
                app_client, sample["provider_id"], "sk-contract-timeout-key-12345", sample["base_url"]
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
            resp = await _post_provider_test(app_client, sample["provider_id"], "dummy-key", sample["base_url"])
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
            resp = await _post_provider_test(
                app_client, sample["provider_id"], "sk-****7766", sample["test_base_url"]
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


class TestOpenAICompatibleTestConnection:
    """Verifies HTTP 400 auth error interception without falling through to chat completions."""

    @pytest.mark.asyncio
    async def test_test_connection_http_400_xai_auth_error_no_fallthrough(self):
        """
        xAI returns HTTP 400 with 'Incorrect API key provided'.
        Must classify as auth failure and NOT fall through to /chat/completions.
        """
        adapter = XAILLMAdapter(api_key="xai-invalid-key")
        mock_resp = httpx.Response(
            status_code=400,
            text='{"code":"invalid-argument","error":"Incorrect API key provided. You can obtain an API key from https://console.x.ai."}',
            request=httpx.Request("GET", "https://api.x.ai/v1/models"),
        )

        chat_completions_called = False

        async def mock_get(client, url, *args, **kwargs):
            return mock_resp

        async def mock_post(client, url, *args, **kwargs):
            nonlocal chat_completions_called
            if "chat/completions" in str(url):
                chat_completions_called = True
            return httpx.Response(status_code=200, json={}, request=httpx.Request("POST", str(url)))

        with patch.object(httpx.AsyncClient, "get", mock_get), \
             patch.object(httpx.AsyncClient, "post", mock_post):
            result = await adapter.test_connection()
            assert result.success is False
            assert "Authentication failed (400)" in result.message
            assert result.diagnostic is not None
            assert "xai" in result.diagnostic.lower() or "console.x.ai" in result.diagnostic.lower()
            assert chat_completions_called is False

    @pytest.mark.asyncio
    async def test_test_connection_http_400_gemini_auth_error_no_fallthrough(self):
        """
        Gemini returns HTTP 400 with 'Please pass a valid API key'.
        Must classify as auth failure without falling through to chat completions.
        """
        adapter = OpenAICompatibleLLMAdapter(
            api_key="bad-gemini-key",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            provider_id="gemini",
        )
        mock_resp = httpx.Response(
            status_code=400,
            text='{"error":{"code":400,"message":"Please pass a valid API key","status":"INVALID_ARGUMENT"}}',
            request=httpx.Request("GET", "https://generativelanguage.googleapis.com/v1beta/openai/models"),
        )

        chat_called = False

        async def mock_get(client, url, *args, **kwargs):
            return mock_resp

        async def mock_post(client, url, *args, **kwargs):
            nonlocal chat_called
            chat_called = True
            return httpx.Response(status_code=200, json={}, request=httpx.Request("POST", str(url)))

        with patch.object(httpx.AsyncClient, "get", mock_get), \
             patch.object(httpx.AsyncClient, "post", mock_post):
            result = await adapter.test_connection()
            assert result.success is False
            assert "Authentication failed (400)" in result.message
            assert result.diagnostic is not None
            assert "aistudio.google.com" in result.diagnostic
            assert chat_called is False

    @pytest.mark.asyncio
    async def test_test_connection_chat_fallback_returns_probed_model_when_models_unsupported(self):
        """When /models is unsupported (e.g. 404) and /chat/completions succeeds, models includes the verified model."""
        adapter = OpenAICompatibleLLMAdapter(
            api_key="sk-relay-valid-key",
            base_url="https://relay.example.com/v1",
            provider_id="custom",
        )
        probed_payloads = []

        async def mock_get(client, url, *args, **kwargs):
            return httpx.Response(status_code=404, text="Not Found", request=httpx.Request("GET", str(url)))

        async def mock_post(client, url, *args, **kwargs):
            probed_payloads.append((str(url), kwargs.get("json")))
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"content": "ok"}}]},
                request=httpx.Request("POST", str(url)),
            )

        with patch("galgame2voice.security.url_guard.assert_llm_url_safe", new_callable=AsyncMock), \
             patch.object(httpx.AsyncClient, "get", mock_get), \
             patch.object(httpx.AsyncClient, "post", mock_post):
            result = await adapter.test_connection(model="relay-custom-model")

        assert result.success is True
        assert result.models == ["relay-custom-model"]
        assert len(probed_payloads) == 1
        assert probed_payloads[0][0].endswith("/chat/completions")
        assert probed_payloads[0][1]["model"] == "relay-custom-model"

    @pytest.mark.asyncio
    async def test_test_connection_empty_models_list_falls_back_to_chat_probe(self):
        """When /models returns 200 with an empty data list, fall back to /chat/completions to verify the model."""
        adapter = OpenAICompatibleLLMAdapter(
            api_key="sk-relay-valid-key",
            base_url="https://relay.example.com/v1",
            provider_id="custom",
        )
        chat_calls = []

        async def mock_get(client, url, *args, **kwargs):
            return httpx.Response(status_code=200, json={"data": []}, request=httpx.Request("GET", str(url)))

        async def mock_post(client, url, *args, **kwargs):
            chat_calls.append(kwargs.get("json"))
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"content": "ok"}}]},
                request=httpx.Request("POST", str(url)),
            )

        with patch("galgame2voice.security.url_guard.assert_llm_url_safe", new_callable=AsyncMock), \
             patch.object(httpx.AsyncClient, "get", mock_get), \
             patch.object(httpx.AsyncClient, "post", mock_post):
            result = await adapter.test_connection(model="relay-custom-model")

        assert result.success is True
        assert result.models == ["relay-custom-model"]
        assert len(chat_calls) == 1
        assert chat_calls[0]["model"] == "relay-custom-model"

    @pytest.mark.asyncio
    async def test_test_connection_empty_models_list_reports_chat_probe_failure(self):
        """When /models returns 200 empty and /chat/completions rejects the model, test_connection fails."""
        adapter = OpenAICompatibleLLMAdapter(
            api_key="sk-relay-valid-key",
            base_url="https://relay.example.com/v1",
            provider_id="custom",
        )

        async def mock_get(client, url, *args, **kwargs):
            return httpx.Response(status_code=200, json={"data": []}, request=httpx.Request("GET", str(url)))

        async def mock_post(client, url, *args, **kwargs):
            return httpx.Response(
                status_code=404,
                text='{"error":{"message":"The model `bad-model` does not exist"}}',
                request=httpx.Request("POST", str(url)),
            )

        with patch("galgame2voice.security.url_guard.assert_llm_url_safe", new_callable=AsyncMock), \
             patch.object(httpx.AsyncClient, "get", mock_get), \
             patch.object(httpx.AsyncClient, "post", mock_post):
            result = await adapter.test_connection(model="bad-model")

        assert result.success is False


@pytest.mark.asyncio
async def test_pasted_chat_completions_suffix_is_dropped(monkeypatch):
    """接口地址照教程粘成完整的 …/v1/chat/completions 时，测试连通性和聊天都请求到正确的地址。"""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-chat"}]})
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
            headers={"Content-Type": "text/event-stream"},
        )

    monkeypatch.setattr(
        "galgame2voice.adapters.llm.openai_adapter.create_async_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
    )
    adapter = OpenAICompatibleLLMAdapter(
        api_key="sk-test", base_url="https://api.deepseek.com/v1/Chat/Completions/", default_model="deepseek-chat"
    )
    assert (await adapter.test_connection()).success is True
    tokens = [t async for t in adapter.stream_chat([{"role": "user", "content": "hi"}], model="deepseek-chat")]
    assert tokens == ["ok"]
    assert set(seen) == {"https://api.deepseek.com/v1/models", "https://api.deepseek.com/v1/chat/completions"}


@pytest.mark.asyncio
async def test_provider_test_fails_for_webpage_or_error_json(app_client, monkeypatch):
    """接口地址填成网页，或中转站用 200 返回错误 JSON 时，「测试连通性」不再显示通过。"""
    from galgame2voice.utils.error_diagnostics import WEBPAGE_ENDPOINT_ACTION_ZH

    monkeypatch.setattr("galgame2voice.security.url_guard.validate_llm_base_url", lambda *_a, **_kw: (True, ""))
    monkeypatch.setattr("galgame2voice.security.url_guard.assert_llm_url_safe", AsyncMock())
    page = "<!doctype html><html><body>Console</body></html>"
    _patch_upstream_response(monkeypatch, 200, text=page, headers={"Content-Type": "text/html; charset=utf-8"})
    for provider_id, base_url in (("deepseek", "https://chat.deepseek.com"), ("anthropic", "https://claude.ai")):
        data = (await _post_provider_test(app_client, provider_id, "sk-test-webpage-key-12345", base_url)).json()
        assert (data["success"], data["error_code"], data["action_zh"]) == (
            False, "INVALID_BASE_URL", WEBPAGE_ENDPOINT_ACTION_ZH
        ), provider_id

    _patch_upstream_response(monkeypatch, 200, json={"code": 401, "msg": "Invalid token"})
    data = (await _post_provider_test(app_client, "custom", "sk-test-key-12345", "https://relay.example.com/v1")).json()
    assert data["success"] is False


@pytest.mark.asyncio
async def test_provider_test_passes_when_only_models_is_a_webpage(app_client, monkeypatch):
    """中转站的 /models 是网页、/chat/completions 能用时，「测试连通性」照样通过。"""
    monkeypatch.setattr("galgame2voice.security.url_guard.validate_llm_base_url", lambda *_a, **_kw: (True, ""))
    monkeypatch.setattr("galgame2voice.security.url_guard.assert_llm_url_safe", AsyncMock())

    async def relay(method, url, _kwargs):
        if method == "GET" and url.endswith("/models"):
            return httpx.Response(
                200, text="<!doctype html><html></html>", headers={"Content-Type": "text/html"},
                request=httpx.Request(method, url),
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]}, request=httpx.Request(method, url))

    _patch_upstream(monkeypatch, relay)
    data = (await _post_provider_test(app_client, "custom", "sk-relay-valid-key-12345", "https://relay.example.com/v1")).json()
    assert data["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_url", "punct"),
    [
        ("https：//api.deepseek.com", "："),
        ("https://api。deepseek。com", "。"),
        ("https://api.deepseek.com／v1", "／"),
    ],
)
async def test_provider_url_with_fullwidth_punctuation_rejected(app_client, bad_url, punct):
    expected = f"接口地址里有中文标点「{punct}」，请切换到英文输入法后重新输入"
    save_resp = await app_client.post(
        "/api/providers",
        json={"id": "deepseek", "api_base_url": bad_url, "api_key": "sk-deepseek-test-12345"},
    )
    assert save_resp.status_code == 400
    assert save_resp.json()["detail"] == expected
    assert (await app_client.get("/api/providers/deepseek")).json()["provider"]["api_base_url"] == "https://api.deepseek.com"
    test_resp = await _post_provider_test(app_client, "deepseek", "sk-deepseek-test-12345", bad_url)
    assert test_resp.status_code == 400
    assert test_resp.json()["detail"] == expected
