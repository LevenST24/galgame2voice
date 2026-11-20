"""Transport failures must not invent a refused port or hide a DNS failure."""

import http.client
import httpx
import pytest

from galgame2voice.utils.error_diagnostics import (
    chat_error_payload,
    diagnose_llm_error,
    format_provider_error,
)


@pytest.mark.parametrize("failure", [
    httpx.ConnectError(""),
    httpx.ConnectError("TLS handshake failed"),
    "ConnectError: ",
    "ConnectError: All connection attempts failed",
])
def test_unspecified_connection_failure_does_not_claim_the_port_is_closed(failure):
    result = diagnose_llm_error(failure, status_code=502)
    assert result.error_code == "NETWORK_CONNECTION_FAILED"
    assert "代理" in result.guidance
    assert "端口未开放" not in result.guidance


@pytest.mark.parametrize("message", [
    "[WinError 10061] No connection could be made",
    "Connection refused",
])
def test_explicit_refusal_keeps_its_specific_diagnosis(message):
    result = diagnose_llm_error(httpx.ConnectError(message), status_code=502)
    assert result.error_code == "CONNECTION_REFUSED"


def test_dns_connect_error_keeps_the_dns_diagnosis():
    result = diagnose_llm_error(httpx.ConnectError("getaddrinfo failed"), status_code=502)
    assert result.error_code == "DNS_ERROR"


def test_model_not_found_versus_endpoint_404_and_overload_and_local_timeout():
    import httpcore
    from galgame2voice.utils.error_diagnostics import chat_error_payload

    # 1. Explicit model_not_found -> 模型名称提示
    explicit_missing_model = RuntimeError('API returned status 404: {"error": {"code": "model_not_found", "message": "The model `xyz` does not exist."}}')
    payload_missing = chat_error_payload(explicit_missing_model, provider_id="openai")
    assert payload_missing["error_code"] == "MODEL_NOT_FOUND"
    assert "模型名称不存在" in payload_missing["error"]

    # 2. 404 without model -> 接口地址 / Base URL 提示
    html_404 = RuntimeError("API returned status 404: <html><body>404 Not Found</body></html>")
    payload_404 = chat_error_payload(html_404, provider_id="custom")
    assert payload_404["error_code"] == "ENDPOINT_NOT_FOUND"
    assert "接口地址" in payload_404["error"] or "Base URL" in payload_404["error"]
    assert "模型名称不存在" not in payload_404["error"]

    # 3. 503 overloaded -> 服务繁忙提示，不让用户修改配置
    overload_503 = RuntimeError('API returned status 503: {"error":{"message":"The model is currently overloaded. Please try again."}}')
    payload_503 = chat_error_payload(overload_503, provider_id="openai")
    assert payload_503["error_code"] == "SERVER_OVERLOADED"
    assert "繁忙" in payload_503["error"] or "过载" in payload_503["error"]
    assert "模型名称不存在" not in payload_503["error"]
    assert "404" not in payload_503["error"]
    assert "503" in payload_503["error"]

    # 4. RuntimeError wrapping httpx.ReadTimeout pointing to 127.0.0.1 -> 本机模型超时提示
    mapped = httpx.ReadTimeout(str(httpcore.ReadTimeout()))
    local_timeout = RuntimeError(f"Streaming request failed to http://127.0.0.1:11434/v1/chat/completions: {mapped}")
    local_timeout.__cause__ = mapped
    payload_local = chat_error_payload(local_timeout, provider_id="custom")
    assert payload_local["error_code"] == "NETWORK_TIMEOUT"
    assert "本机模型" in payload_local["error"] or "响应太慢" in payload_local["error"] or "仍在加载" in payload_local["error"]
    assert "代理" not in payload_local["error"]
    assert "API Key" not in payload_local["error"]
    assert "模型名称" not in payload_local["error"]

    # 5. Remote timeout -> 保持现有海外代理提示
    remote_timeout = RuntimeError(f"Streaming request failed to https://api.openai.com/v1/chat/completions: {mapped}")
    remote_timeout.__cause__ = mapped
    payload_remote = chat_error_payload(remote_timeout, provider_id="openai")
    assert payload_remote["error_code"] == "NETWORK_TIMEOUT"
    assert "代理" in payload_remote["error"]


def test_format_provider_error_overload_and_endpoint_not_found():
    from galgame2voice.utils.error_diagnostics import format_provider_error

    diag_404 = format_provider_error("custom", 404, "<html>404 Not Found</html>")
    assert diag_404["error_code"] == "ENDPOINT_NOT_FOUND"
    assert "接口地址" in diag_404["error"] or "Base URL" in diag_404["diagnostic"]

    diag_503 = format_provider_error("openai", 503, "The model is currently overloaded. Please try again.")
    assert diag_503["error_code"] == "SERVER_OVERLOADED"
    assert "繁忙" in diag_503["error"] or "繁忙" in diag_503["diagnostic"]


@pytest.mark.parametrize(
    ("err", "expect_in_msg"),
    [
        (ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。"), "10054"),
        (ConnectionResetError("Connection reset by peer"), "Connection reset"),
        (ConnectionAbortedError(10053, "你的主机中的软件中止了一个已建立的连接。"), "10053"),
        (ConnectionAbortedError("Software caused connection abort"), "connection abort"),
        (httpx.RemoteProtocolError("peer closed connection without sending complete message body"), "peer closed connection"),
        (httpx.ReadError("connection reset by peer"), "ReadError"),
        (httpx.WriteError("write failed"), "WriteError"),
        (BrokenPipeError("Broken pipe"), "BrokenPipeError"),
        (http.client.RemoteDisconnected("Remote end closed connection without response"), "Remote end closed connection"),
        (http.client.IncompleteRead(b"partial_stream_data", 256), "IncompleteRead"),
        ("ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。", "10054"),
        ("httpx.RemoteProtocolError: peer closed connection without sending complete message body", "RemoteProtocolError"),
    ],
)
def test_connection_reset_and_transport_interruption_diagnosis(err, expect_in_msg):
    from galgame2voice.utils.error_diagnostics import chat_error_payload, format_provider_error

    result = diagnose_llm_error(err)
    assert result.error_code == "CONNECTION_RESET"
    assert result.status_code == 502
    assert "网络连接被中途断开或重置" in result.message
    assert "请检查 Base URL 地址、API Key 凭据以及模型名称配置是否正确" not in result.guidance
    assert "无需修改 API Key 或模型名称" in result.guidance
    assert "重新发送重试" in result.guidance
    assert expect_in_msg.lower() in (result.raw_error or "").lower()

    payload = chat_error_payload(err, provider_id="openai")
    assert payload["error_code"] == "CONNECTION_RESET"
    assert payload["status_code"] == 502
    assert "网络连接被中途断开或重置" in payload["error"]
    assert "请检查 Base URL 地址、API Key 凭据以及模型名称配置是否正确" not in payload["error"]
    assert "无需修改 API Key 或模型名称" in payload["error"]

    diag_dict = format_provider_error("openai", 502, err)
    assert diag_dict["error_code"] == "CONNECTION_RESET"
    assert diag_dict["status_code"] == 502
    assert "网络连接被中途断开或重置" in diag_dict["error"]


@pytest.mark.parametrize(
    "cause",
    [
        ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。"),
        httpx.RemoteProtocolError("peer closed connection without sending complete message body"),
        httpx.ReadError("read error during stream"),
        http.client.RemoteDisconnected("Remote end closed connection without response"),
    ],
)
def test_streaming_runtime_error_wrapping_connection_reset(cause):
    from galgame2voice.utils.error_diagnostics import chat_error_payload

    streaming_err = RuntimeError(f"Streaming request failed to https://api.openai.com/v1/chat/completions: {cause}")
    streaming_err.__cause__ = cause

    result = diagnose_llm_error(streaming_err)
    assert result.error_code == "CONNECTION_RESET"
    assert result.status_code == 502
    assert "请检查 Base URL 地址、API Key 凭据以及模型名称配置是否正确" not in result.guidance
    assert "无需修改 API Key 或模型名称" in result.guidance

    payload = chat_error_payload(streaming_err, provider_id="openai")
    assert payload["error_code"] == "CONNECTION_RESET"
    assert payload["status_code"] == 502
    assert "网络连接被中途断开或重置" in payload["error"]
    assert "请检查 Base URL 地址、API Key 凭据以及模型名称配置是否正确" not in payload["error"]


@pytest.mark.parametrize(
    ("err", "provider_id", "status_code"),
    [
        ("400 prompt is too long: 210054 tokens > 200000 maximum", None, None),
        (
            RuntimeError(
                'Anthropic API error (400): {"type":"error","error":{"type":"invalid_request_error",'
                '"message":"prompt is too long: 210054 tokens > 200000 maximum"}}'
            ),
            "anthropic",
            None,
        ),
        (
            RuntimeError(
                'API returned status 400: {"error":{"message":"prompt is too long: 10053 tokens > 8192 maximum"}}'
            ),
            "openai",
            None,
        ),
        (
            RuntimeError('API returned status 400: {"error":{"message":"Invalid field threaderror or on_readerror"}}'),
            "openai",
            None,
        ),
        (
            '{"error":{"message":"Upstream validation error 10054: ReadError policy invalid"}}',
            "openai",
            400,
        ),
    ],
)
def test_provider_400_with_10054_or_readerror_substring_not_connection_reset(err, provider_id, status_code):
    from galgame2voice.utils.error_diagnostics import chat_error_payload, format_provider_error

    result = diagnose_llm_error(err, provider_id=provider_id, status_code=status_code)
    assert result.error_code != "CONNECTION_RESET"
    expected_code = "CONTEXT_LENGTH_EXCEEDED" if "prompt is too long" in str(err) else "PROVIDER_ERROR"
    assert result.error_code == expected_code
    assert "无需修改 API Key 或模型名称" not in result.guidance

    if status_code is not None:
        diag_dict = format_provider_error(provider_id, status_code, err)
        assert diag_dict["error_code"] != "CONNECTION_RESET"
        assert diag_dict["error_code"] == expected_code
        assert "无需修改 API Key 或模型名称" not in diag_dict["diagnostic"]
    else:
        payload = chat_error_payload(err, provider_id=provider_id, api_key_configured=True)
        assert payload["error_code"] != "CONNECTION_RESET"
        assert payload["error_code"] == expected_code
        assert "无需修改 API Key 或模型名称" not in payload["error"]


def test_provider_401_and_404_with_10054_substrings_preserve_specific_diagnosis():
    from galgame2voice.utils.error_diagnostics import chat_error_payload, format_provider_error

    openai_401 = ValueError(
        'Authentication error (401): {"error":{"message":"Incorrect API key provided",'
        '"code":"invalid_api_key"},"request_id":"req_10054"}'
    )
    res_401 = diagnose_llm_error(openai_401, provider_id="openai")
    assert res_401.error_code == "OPENAI_AUTH_FAILED"
    assert res_401.status_code == 401
    assert "无需修改 API Key 或模型名称" not in res_401.guidance

    payload_401 = chat_error_payload(openai_401, provider_id="openai", api_key_configured=True)
    assert payload_401["error_code"] == "OPENAI_AUTH_FAILED"
    assert "无需修改 API Key 或模型名称" not in payload_401["error"]

    fmt_401 = format_provider_error(
        "openai",
        401,
        '{"error":{"message":"Incorrect API key provided"},"request_id":"10054"}',
    )
    assert fmt_401["error_code"] == "OPENAI_AUTH_FAILED"
    assert fmt_401["status_code"] == 401

    anthropic_401 = ValueError(
        'Anthropic auth failed (401): {"type":"error","error":{"type":"authentication_error",'
        '"message":"invalid x-api-key"},"request_id":"req_10053"}'
    )
    assert diagnose_llm_error(anthropic_401, provider_id="anthropic").error_code == "ANTHROPIC_AUTH_FAILED"

    model_404 = RuntimeError(
        'API returned status 404: {"error":{"message":"The model `gpt-4o-unknown` does not exist",'
        '"code":"model_not_found"},"request_id":"req_100549"}'
    )
    res_model_404 = diagnose_llm_error(model_404, provider_id="openai")
    assert res_model_404.error_code == "MODEL_NOT_FOUND"
    assert res_model_404.status_code == 404
    assert "无需修改 API Key 或模型名称" not in res_model_404.guidance

    endpoint_404 = RuntimeError('API returned status 404: {"error":"Not Found","request_id":"req_100549"}')
    res_endpoint_404 = diagnose_llm_error(endpoint_404, provider_id="custom")
    assert res_endpoint_404.error_code == "ENDPOINT_NOT_FOUND"
    assert res_endpoint_404.status_code == 404
    assert "无需修改 API Key 或模型名称" not in res_endpoint_404.guidance


@pytest.mark.parametrize(
    "err",
    [
        "[WinError 10054] 远程主机强迫关闭了一个现有的连接。",
        "[WinError 10053] 你的主机中的软件中止了一个已建立的连接。",
        "[Errno 10054] Connection reset by peer",
        "[Errno 10053] Software caused connection abort",
        "wsarecv: os error 10054",
        "ReadError: ",
        httpx.ReadError(""),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
    ],
)
def test_502_wrapped_and_contextual_errno_transport_resets_remain_connection_reset(err):
    from galgame2voice.utils.error_diagnostics import format_provider_error

    result = diagnose_llm_error(err, status_code=502)
    assert result.error_code == "CONNECTION_RESET"
    assert result.status_code == 502
    assert "无需修改 API Key 或模型名称" in result.guidance

    diag_dict = format_provider_error("openai", 502, err)
    assert diag_dict["error_code"] == "CONNECTION_RESET"
    assert diag_dict["status_code"] == 502


class TestAdapterRegistryAndFactory:
    """Tests for Provider Registry presets and dynamic adapter factory instantiation."""

    def test_get_provider_preset_valid_and_invalid(self):
        from galgame2voice.adapters.registry import get_provider_preset

        deepseek = get_provider_preset("deepseek")
        assert deepseek is not None
        assert deepseek["id"] == "deepseek"
        assert "deepseek" in deepseek["default_chat_model"]

        nonexistent = get_provider_preset("unknown_provider_xyz")
        assert nonexistent is None

    def test_get_llm_adapter_by_dict_and_model(self):
        from galgame2voice.adapters.llm import GLMLLMAdapter, MoonshotLLMAdapter
        from galgame2voice.adapters.registry import get_llm_adapter
        from galgame2voice.database.models import ProviderInDB

        config_dict = {
            "provider_type": "glm",
            "api_key": "glm-token-123",
            "api_base_url": "https://open.bigmodel.cn/api/paas/v4",
            "custom_headers": {"X-Test": "1"},
        }
        adapter = get_llm_adapter(config_dict)
        assert isinstance(adapter, GLMLLMAdapter)
        assert adapter.api_key == "glm-token-123"
        assert adapter.extra_config["custom_headers"]["X-Test"] == "1"

        provider_obj = ProviderInDB(
            id="moonshot",
            name="Moonshot",
            api_base_url="https://api.moonshot.cn/v1",
            api_key="sk-moonshot-key",
            chat_model="moonshot-v1-8k",
        )
        adapter2 = get_llm_adapter(provider_obj)
        assert isinstance(adapter2, MoonshotLLMAdapter)
        assert adapter2.api_key == "sk-moonshot-key"


class TestAdaptersExecutionWithMockServer:
    """Tests LLM adapter execution, streaming, and error handling with MockLLMServer."""

    @pytest.mark.asyncio
    async def test_llm_list_models_mock(self, mock_llm_server):
        from galgame2voice.adapters.llm import SiliconFlowLLMAdapter

        adapter = SiliconFlowLLMAdapter(api_key="sk-valid-key", client_override=mock_llm_server)
        models = await adapter.list_models()
        assert "deepseek-chat" in models
        assert "gpt-4o" in models


class TestErrorDiagnosticsEngine:
    """Verifies format_provider_error and diagnose_llm_error Chinese guidance."""

    def test_gemini_403_region_restriction(self):
        diag = format_provider_error("gemini", 403, "User location is not supported for the API use.")
        assert diag["error_code"] == "GEMINI_REGION_RESTRICTED"
        assert "地区访问受限" in diag["error"]
        assert "代理" in diag["diagnostic"]

    def test_gemini_400_invalid_key(self):
        diag = format_provider_error("gemini", 400, "API key not valid. Please pass a valid API key.")
        assert diag["error_code"] == "GEMINI_KEY_INVALID"
        assert "aistudio.google.com" in diag["diagnostic"]

    def test_xai_400_and_401_auth_failure(self):
        diag400 = format_provider_error("xai", 400, "Incorrect API key provided.")
        assert diag400["error_code"] == "XAI_AUTH_FAILED"
        assert "console.x.ai" in diag400["diagnostic"]

        diag401 = format_provider_error("xai", 401, "Unauthorized")
        assert diag401["error_code"] == "XAI_AUTH_FAILED"
        assert "console.x.ai" in diag401["diagnostic"]

    def test_xai_429_quota_exceeded(self):
        diag = format_provider_error("xai", 429, "Rate limit exceeded. Not enough credits.")
        assert diag["error_code"] == "XAI_QUOTA_EXCEEDED"
        assert "积分" in diag["diagnostic"] or "Credits" in diag["diagnostic"]

    def test_openai_401_and_429_quota(self):
        diag401 = format_provider_error("openai", 401, "Incorrect API key provided")
        assert diag401["error_code"] == "OPENAI_AUTH_FAILED"
        assert "platform.openai.com" in diag401["diagnostic"]

        diag429 = format_provider_error("openai", 429, "You exceeded your current quota, please check your plan and billing details.")
        assert diag429["error_code"] == "OPENAI_QUOTA_EXCEEDED"
        assert "额度已耗尽" in diag429["error"]
        assert "充值" in diag429["diagnostic"] or "billing" in diag429["diagnostic"]

    def test_network_timeout_and_connection_refused(self):
        diag_timeout = diagnose_llm_error(exc_or_msg=httpx.ConnectTimeout("Connection timed out"), status_code=408)
        assert diag_timeout.error_code == "NETWORK_TIMEOUT"
        assert "超时" in diag_timeout.message

        diag_refused = diagnose_llm_error(exc_or_msg=httpx.ConnectError("[WinError 10061] No connection could be made"), status_code=502)
        assert diag_refused.error_code == "CONNECTION_REFUSED"
        assert "被拒绝" in diag_refused.message
        assert any(term in diag_refused.guidance for term in ("端口", "Base URL", "反向代理"))

    def test_http_402_insufficient_balance(self):
        """DeepSeek / Moonshot / SiliconFlow 402 Insufficient Balance must be diagnosed clearly without blaming API Key / Base URL."""
        raw_error = 'RuntimeError: Streaming endpoint returned HTTP 402: {"error": {"message": "Insufficient Balance"}}'
        diag = diagnose_llm_error(raw_error, provider_id="deepseek")
        assert diag.error_code == "QUOTA_EXCEEDED"
        assert "余额" in diag.message or "余额" in diag.guidance
        assert "Base URL" not in diag.guidance
        assert "API Key" not in diag.guidance

        diag_moonshot = format_provider_error("moonshot", 402, '{"error": {"message": "Insufficient Balance"}}')
        assert diag_moonshot["error_code"] == "QUOTA_EXCEEDED"
        assert "余额" in diag_moonshot["error"] or "余额" in diag_moonshot["diagnostic"]
        assert "Base URL" not in diag_moonshot["diagnostic"]

        payload = chat_error_payload(RuntimeError('API returned status 402: {"message": "余额不足"}'), provider_id="siliconflow")
        assert payload["error_code"] == "QUOTA_EXCEEDED"
        assert "余额" in payload["error"]

        # Real httpx.HTTPStatusError with 402 response status code
        req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        resp = httpx.Response(402, request=req)
        exc = httpx.HTTPStatusError("Client error '402' for url 'https://api.deepseek.com/chat/completions'", request=req, response=resp)
        diag_exc = diagnose_llm_error(exc, provider_id="deepseek")
        assert diag_exc.error_code == "QUOTA_EXCEEDED"
        assert diag_exc.status_code == 402
        assert "余额" in diag_exc.message or "余额" in diag_exc.guidance

    def test_http_400_context_length_exceeded(self):
        """HTTP 400 Context Length Exceeded must diagnose as CONTEXT_LENGTH_EXCEEDED, not auth failed or provider error."""
        raw_error = "RuntimeError: API returned status 400: This model's maximum context length is 32768 tokens. However, your messages resulted in 35000 tokens."
        diag = diagnose_llm_error(raw_error, provider_id="openai")
        assert diag.error_code == "CONTEXT_LENGTH_EXCEEDED"
        assert "上下文" in diag.guidance

        diag_xai = format_provider_error("xai", 400, "This model's maximum context length is 32768 tokens")
        assert diag_xai["error_code"] == "CONTEXT_LENGTH_EXCEEDED"
        assert diag_xai["error_code"] != "XAI_AUTH_FAILED"
        assert "上下文" in diag_xai["diagnostic"]

        diag_gemini = format_provider_error("gemini", 400, "INVALID_ARGUMENT: This model's maximum context length is 32768 tokens")
        assert diag_gemini["error_code"] == "CONTEXT_LENGTH_EXCEEDED"
        assert diag_gemini["error_code"] != "GEMINI_KEY_INVALID"
        assert "上下文" in diag_gemini["diagnostic"]

    def test_xai_and_gemini_generic_400_not_misdiagnosed_as_key_invalid(self):
        """Generic 400 (e.g. parameter error) must not be reported as auth/key failure, while real key errors still are."""
        diag_xai_param = format_provider_error("xai", 400, '{"error": {"message": "temperature must be between 0 and 2"}}')
        assert diag_xai_param["error_code"] != "XAI_AUTH_FAILED"
        assert "身份验证失败" not in diag_xai_param["error"]

        diag_gemini_param = format_provider_error("gemini", 400, '{"error": {"message": "INVALID_ARGUMENT: temperature must be between 0 and 2"}}')
        assert diag_gemini_param["error_code"] != "GEMINI_KEY_INVALID"
        assert "密钥验证失败" not in diag_gemini_param["diagnostic"]

        diag_xai_auth = format_provider_error("xai", 400, '{"code":"invalid-argument","error":"Incorrect API key provided."}')
        assert diag_xai_auth["error_code"] == "XAI_AUTH_FAILED"

        diag_gemini_key = format_provider_error("gemini", 400, 'API key not valid. Please pass a valid API key.')
        assert diag_gemini_key["error_code"] == "GEMINI_KEY_INVALID"

    def test_cloudflare_524_and_nginx_502_html_sanitized_and_diagnosed(self):
        """Cloudflare 524 and Nginx 502 HTML error pages must not leak HTML tags and must not blame API Key."""
        cf_524_html = (
            "<!DOCTYPE html>\n<html>\n<head><title>524 A timeout occurred</title></head>\n"
            "<body>\n<h1>Error 524</h1>\n<p>A timeout occurred</p>\n</body>\n</html>"
        )
        diag_524 = diagnose_llm_error(cf_524_html, provider_id="deepseek")
        assert diag_524.error_code == "NETWORK_TIMEOUT"
        assert diag_524.status_code == 524
        assert "超时" in diag_524.message
        assert "<html>" not in diag_524.message and "<title>" not in diag_524.message
        assert "<!DOCTYPE" not in diag_524.raw_error and "<html>" not in diag_524.raw_error
        assert "API Key" not in diag_524.guidance

        nginx_502_html = (
            "<!DOCTYPE html>\n<html>\n<head><title>502 Bad Gateway</title></head>\n"
            "<body>\n<center><h1>502 Bad Gateway</h1></center>\n<hr><center>nginx/1.24.0</center>\n</body>\n</html>"
        )
        diag_502 = diagnose_llm_error(nginx_502_html, status_code=502, provider_id="openai")
        assert diag_502.error_code == "BAD_GATEWAY"
        assert diag_502.status_code == 502
        assert "网关" in diag_502.message
        assert "<html>" not in diag_502.message and "<title>" not in diag_502.message
        assert "<!DOCTYPE" not in diag_502.raw_error and "<html>" not in diag_502.raw_error
        assert "API Key" not in diag_502.guidance

        stream_err = RuntimeError(f"API returned status 502: {nginx_502_html}")
        payload = chat_error_payload(stream_err, provider_id="custom")
        assert payload["error_code"] == "BAD_GATEWAY"
        assert payload["status_code"] == 502
        assert "<html>" not in payload["error"] and "<title>" not in payload["error"]

    def test_http_402_and_429_with_api_key_substring_not_auth_failure(self):
        """OpenAI/xAI 402 or 429 mentioning 'API key' must not be misdiagnosed as auth failure."""
        diag_openai_402 = format_provider_error("openai", 402, "API key has insufficient balance")
        assert diag_openai_402["error_code"] == "QUOTA_EXCEEDED"
        assert diag_openai_402["error_code"] != "OPENAI_AUTH_FAILED"

        diag_xai_402 = format_provider_error("xai", 402, "API key has insufficient balance")
        assert diag_xai_402["error_code"] == "QUOTA_EXCEEDED"
        assert diag_xai_402["error_code"] != "XAI_AUTH_FAILED"

        diag_xai_429 = format_provider_error("xai", 429, "Rate limit exceeded for API key")
        assert diag_xai_429["error_code"] == "XAI_QUOTA_EXCEEDED"
        assert diag_xai_429["error_code"] != "XAI_AUTH_FAILED"

    def test_gemini_and_xai_field_key_or_author_not_misdiagnosed_as_auth_failure(self):
        """Bare words like 'key' in JSON/schema error or 'author' must not trigger auth failure."""
        diag_gemini = format_provider_error("gemini", 400, 'Invalid JSON: missing key "contents"')
        assert diag_gemini["error_code"] != "GEMINI_KEY_INVALID"
        assert "密钥验证失败" not in diag_gemini["diagnostic"]

        diag_xai_key = format_provider_error("xai", 400, 'Unknown key "temperature"')
        assert diag_xai_key["error_code"] != "XAI_AUTH_FAILED"
        assert "身份验证失败" not in diag_xai_key["error"]

        diag_xai_author = format_provider_error("xai", 400, 'Invalid author parameter')
        assert diag_xai_author["error_code"] != "XAI_AUTH_FAILED"

    def test_html_sanitizer_edge_cases(self):
        """_clean_html_text correctly handles empty title, scripts with fake title, and HTML entities."""
        from galgame2voice.utils.error_diagnostics import _clean_html_text

        empty_title_html = "<html><head><title></title></head><body><h1>502 Bad Gateway</h1></body></html>"
        cleaned_empty = _clean_html_text(empty_title_html)
        assert cleaned_empty == "502 Bad Gateway"

        script_title_html = '<html><head><script>var x = "<title>fake</title>";</script><title>Real 524 Timeout</title></head><body>body</body></html>'
        cleaned_script = _clean_html_text(script_title_html)
        assert cleaned_script == "Real 524 Timeout"

        entity_html = "<title>504 Gateway Time-out &mdash; &quot;Server Error&quot;</title>"
        cleaned_entity = _clean_html_text(entity_html)
        assert cleaned_entity == '504 Gateway Time-out — "Server Error"'

        # Non-HTML text with angle bracket placeholders must not be stripped
        non_html = "Invalid value for <model_name>: custom"
        assert _clean_html_text(non_html) == non_html
        traceback_str = "File <string>, line 1, in <module>"
        assert _clean_html_text(traceback_str) == traceback_str

    def test_extract_status_code_prioritizes_explicit_status_over_token_or_ms_count(self):
        """High-confidence status 502 must not be hijacked by '400 ms' in the error message."""
        from galgame2voice.utils.error_diagnostics import _extract_status_code

        code = _extract_status_code(None, "RuntimeError: API returned status 502: request took 400 ms")
        assert code == 502

        assert _extract_status_code(None, "httpx.HTTPStatusError: Client error '402' for url 'https://api.deepseek.com'") == 402
        assert _extract_status_code(None, "status_code=400 message: bad request") == 400
        assert _extract_status_code(None, "Server error: [524] Gateway timeout") == 524

    def test_context_window_and_chinese_variants(self):
        """Context window exceeded variants and Chinese phrases must diagnose as CONTEXT_LENGTH_EXCEEDED."""
        diag_window = format_provider_error("custom", 400, "The model's context window is 4096 tokens, but your request has 5000 tokens")
        assert diag_window["error_code"] == "CONTEXT_LENGTH_EXCEEDED"

        diag_zh = format_provider_error("deepseek", 400, "超出模型上下文限制")
        assert diag_zh["error_code"] == "CONTEXT_LENGTH_EXCEEDED"
