"""Transport failures must not invent a refused port or hide a DNS failure."""

import http.client
import httpx
import pytest

from galgame2voice.utils.error_diagnostics import diagnose_llm_error


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
    assert result.error_code == "PROVIDER_ERROR"
    assert "无需修改 API Key 或模型名称" not in result.guidance

    if status_code is not None:
        diag_dict = format_provider_error(provider_id, status_code, err)
        assert diag_dict["error_code"] != "CONNECTION_RESET"
        assert diag_dict["error_code"] == "PROVIDER_ERROR"
        assert "无需修改 API Key 或模型名称" not in diag_dict["diagnostic"]
    else:
        payload = chat_error_payload(err, provider_id=provider_id, api_key_configured=True)
        assert payload["error_code"] != "CONNECTION_RESET"
        assert payload["error_code"] == "PROVIDER_ERROR"
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
