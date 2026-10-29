"""Failed replies explain the next step in Chinese and carry a stable error code."""

import httpx
import pytest

from galgame2voice.adapters.base import MissingApiKeyError
from galgame2voice.adapters.registry import get_llm_adapter
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_service import TtsService
from galgame2voice.utils.error_diagnostics import chat_error_payload, format_provider_error


def test_missing_key_names_the_provider_and_the_settings_page():
    payload = chat_error_payload(MissingApiKeyError(), provider_id="gemini", api_key_configured=False)
    assert payload["error_code"] == "API_KEY_MISSING"
    assert "Google Gemini" in payload["error"] and "设置中心 → 对话模型" in payload["error"]
    # Still a ValueError mentioning authentication, as callers and tests expect.
    assert isinstance(MissingApiKeyError(), ValueError)
    assert "Authentication error" in str(MissingApiKeyError())


def test_private_endpoint_block_is_not_reported_as_a_dns_failure():
    exc = PermissionError("SSRF/DNS-Rebinding 阻止: 目标地址解析到环回/私网……请在设置中开启「允许私网 LLM 端点」。")
    payload = chat_error_payload(exc, provider_id="custom", api_key_configured=True)
    assert payload["error_code"] == "PRIVATE_ENDPOINT_BLOCKED"
    assert "允许私网 LLM 端点" in payload["error"]
    assert "SSRF" not in payload["error"]


def test_auth_failures_without_a_key_point_to_the_missing_key():
    raw = 'Authentication error (401): {"type": "authentication_error"}'
    assert chat_error_payload(ValueError(raw), "anthropic", api_key_configured=False)["error_code"] == "API_KEY_MISSING"
    assert chat_error_payload(ValueError(raw), "anthropic", api_key_configured=True)["error_code"] == "ANTHROPIC_AUTH_FAILED"
    # Unknown key state keeps the previous provider-specific diagnosis.
    assert format_provider_error("anthropic", 401, raw)["error_code"] == "ANTHROPIC_AUTH_FAILED"


def test_timeouts_and_unknown_failures_stay_readable():
    timeout = chat_error_payload(httpx.ReadTimeout("Upstream LLM timed out"), "openai", api_key_configured=True)
    assert timeout["error_code"] == "NETWORK_TIMEOUT" and "超时" in timeout["error"]
    unknown = chat_error_payload(RuntimeError("Simulated adapter failure"), "custom", api_key_configured=True)
    assert unknown["error_code"] == "PROVIDER_ERROR" and "Simulated adapter failure" in unknown["error"]


@pytest.mark.asyncio
async def test_stream_reports_a_missing_cloud_key_before_contacting_the_provider(temp_db_path, mock_gpt_sovits, tmp_path):
    chat_service = ChatService(
        tts_service=TtsService(client=GptSovitsClient(server=mock_gpt_sovits), audio_dir=tmp_path),
        db_path=temp_db_path,
    )
    adapter = get_llm_adapter("gemini", api_key="")

    async def _adapter(*args, **kwargs):
        return adapter, "gemini-test", "gemini"

    chat_service._get_active_llm_adapter = _adapter
    events = [event async for event in chat_service.stream_chat(prompt="你好", session_id="no-key")]
    errors = [event["data"] for event in events if event["event"] == "error"]
    assert len(errors) == 1
    assert errors[0]["error_code"] == "API_KEY_MISSING"
    assert "Google Gemini" in errors[0]["error"]
    assert not [event for event in events if event["event"] == "done"]
