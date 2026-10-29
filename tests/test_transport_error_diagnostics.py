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
