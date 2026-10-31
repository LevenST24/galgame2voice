"""Transport failures must not invent a refused port or hide a DNS failure."""

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

