"""Failed replies explain the next step in Chinese and carry a stable error code."""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, patch

import httpx
from httpx import ASGITransport, AsyncClient
import pytest

from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, MissingApiKeyError, TestResult
from galgame2voice.adapters.registry import get_llm_adapter
from galgame2voice.database.session import init_db
from galgame2voice.main import create_app
from galgame2voice.routers.chat import set_chat_service
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.audio_identity import AudioIdentity, VoicePreparation
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services import gpt_sovits_client as gpt_sovits_client_module
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.voice_manager import VoiceManager
from galgame2voice.utils.error_diagnostics import chat_error_payload, format_provider_error


def test_missing_key_names_the_provider_and_the_settings_page():
    payload = chat_error_payload(MissingApiKeyError(), provider_id="gemini", api_key_configured=False)
    assert payload["error_code"] == "API_KEY_MISSING"
    assert "Google Gemini" in payload["error"] and "在上面的 API 密钥里填入后再测试" in payload["error"]
    # Still a ValueError mentioning authentication, as callers and tests expect.
    assert isinstance(MissingApiKeyError(), ValueError)
    assert "Authentication error" in str(MissingApiKeyError())


def test_private_endpoint_block_is_not_reported_as_a_dns_failure():
    exc = PermissionError("SSRF/DNS-Rebinding 阻止: 目标地址解析到环回/私网……请在设置中开启「允许私网 LLM 端点」。")
    payload = chat_error_payload(exc, provider_id="custom", api_key_configured=True)
    assert payload["error_code"] == "PRIVATE_ENDPOINT_BLOCKED"
    assert "允许私网 LLM 端点" in payload["error"]
    assert "SSRF" not in payload["error"]


@pytest.mark.asyncio
async def test_unloaded_voice_model_says_text_only(monkeypatch):
    client = GptSovitsClient(base_url="http://127.0.0.1:9")

    async def refuse(_target):
        return False

    monkeypatch.setattr(client, "_switch_voice_profile_locked", refuse)
    monkeypatch.setattr(gpt_sovits_client_module.VoiceResourceGuard, "check", lambda self, force=False: None)
    captured = {"gpt_weights_path": "gpt.ckpt", "sovits_weights_path": "sovits.pth",
                "ref_audio_path": "ref.wav", "_engine_url": client.base_url}
    with pytest.raises(ValueError, match="没有声音。可在「运行状态」查看引擎。"):
        await client._validate_audio_options({
            **captured, "voice_profile_id": 1,
            "_voice_preparation": VoicePreparation(name="拖拽角色"),
            "_audio_identity": AudioIdentity.capture(captured),
        })


def test_generic_rejected_key_omits_the_status_code():
    payload = format_provider_error("deepseek", 401, "401 Unauthorized")
    assert payload["summary_zh"] == "密钥不被接受。请到申请密钥的网站重新复制。"


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


@pytest.mark.asyncio
async def test_chat_service_upstream_llm_timeout_handling(tmp_path):
    """
    When upstream LLM provider hangs or raises httpx.ReadTimeout,
    the stream should yield an informative error event and terminate cleanly.
    """
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    await init_db(db_path)

    client = GptSovitsClient()
    tts_service = TtsService(client=client, audio_dir=tmp_path)
    chat_service = ChatService(tts_service=tts_service, db_path=db_path)

    class TimeoutLLM(BaseLLMAdapter):
        async def stream_chat(self, *args, **kwargs):
            raise httpx.ReadTimeout("Upstream LLM timed out after 30s")
            yield ""

        async def chat(self, *args, **kwargs):
            raise httpx.ReadTimeout("Upstream LLM timed out")

        async def test_connection(self, *args, **kwargs):
            return TestResult(success=False, message="Timeout")

        async def list_models(self):
            return []

    mock_llm = TimeoutLLM(api_key="sk-test", base_url="http://test")
    chat_service._get_active_llm_adapter = AsyncMock(return_value=(mock_llm, "test-llm"))

    events = []
    async for ev in chat_service.stream_chat(prompt="超时测试", session_id="timeout_sess"):
        events.append(ev)

    assert len(events) >= 1
    # Expect error event
    error_events = [e for e in events if e["event"] == "error"]
    assert len(error_events) == 1
    assert "超时" in error_events[0]["data"]["error"]

    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_sse_stream_error_handling_when_llm_fails(temp_db_path, mock_gpt_sovits, tmp_path):
    """
    When LLM provider raises an exception, the SSE stream should emit
    an `event: error` rather than crashing the application.
    """
    app = create_app()
    client = GptSovitsClient(server=mock_gpt_sovits)
    tts_service = TtsService(client=client, audio_dir=tmp_path)
    chat_service = ChatService(tts_service=tts_service, db_path=temp_db_path)

    class FailingLLM(BaseLLMAdapter):
        async def chat(self, *args, **kwargs):
            raise RuntimeError("LLM 429 Rate Limit")

        async def stream_chat(self, *args, **kwargs):
            raise RuntimeError("LLM 429 Rate Limit")
            yield ""

        async def test_connection(self, *args, **kwargs):
            return TestResult(success=False, message="Error")

        async def list_models(self):
            return []

    async def _mock_adapter(*args, **kwargs):
        return (FailingLLM(api_key="", base_url=""), "mock")

    chat_service._get_active_llm_adapter = _mock_adapter
    set_chat_service(chat_service)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as c:
            resp = await c.post(
                "/api/chat/stream",
                json={"session_id": "test_err_sess", "prompt": "fail test", "stream": True},
            )
            assert resp.status_code == 200
            body = resp.text
            assert "event: error" in body or "error" in body.lower()
    finally:
        set_chat_service(None)


@pytest.mark.asyncio
async def test_cross_module_provider_failover_and_error_recovery(
    isolate_test_database, configured_voice, mock_gpt_sovits,
):
    """Error in LLM streaming emits structured error event and leaves session recoverable for next turn."""
    class _FailoverMockAdapter(BaseLLMAdapter):
        def __init__(self, custom_tokens=None, fail_after_tokens=-1):
            super().__init__(api_key="sk-mock-key", base_url="https://api.openai.com/v1")
            self.custom_tokens = custom_tokens or ['{"chinese": "ok", "japanese": "ok"}']
            self.fail_after_tokens = fail_after_tokens

        async def chat(self, messages: list[ChatMessage], model: str, temperature: float = 1.0, **kwargs) -> LLMResponse:
            return LLMResponse(content="".join(self.custom_tokens))

        async def stream_chat(self, messages: list[ChatMessage], model: str, temperature: float = 1.0, **kwargs):
            for idx, token in enumerate(self.custom_tokens):
                if self.fail_after_tokens >= 0 and idx >= self.fail_after_tokens:
                    raise RuntimeError(f"Simulated adapter failure at token index {idx}")
                await asyncio.sleep(0.001)
                yield token

        async def test_connection(self, model=None) -> TestResult:
            return TestResult(success=True, message="OK", latency_ms=1.0)

        async def list_models(self):
            return ["custom-model-v1"]

    voice_manager = VoiceManager(gpt_sovits_client_or_server=mock_gpt_sovits, db_path=isolate_test_database)
    chat_service = ChatService(tts_service=voice_manager.tts_service, db_path=isolate_test_database)

    failing_adapter = _FailoverMockAdapter(fail_after_tokens=0)
    with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=failing_adapter):
        events = []
        async for event in chat_service.stream_chat("Fail prompt", session_id="recovery_session"):
            events.append(event)

        assert len(events) == 2
        assert events[0]["event"] == "turn_started"
        assert events[0]["data"]["session_id"] == "recovery_session"
        assert isinstance(events[0]["data"]["user_message_id"], int) and events[0]["data"]["user_message_id"] > 0
        assert events[1]["event"] == "error"
        assert "Simulated adapter failure" in events[1]["data"]["error"]

    working_adapter = _FailoverMockAdapter(custom_tokens=[
        '{"chinese": "已成功恢复！", "japanese": "無事に回復しました！"}'
    ])
    with patch("galgame2voice.services.chat_service.get_llm_adapter", return_value=working_adapter):
        events = []
        async for event in chat_service.stream_chat("Recover prompt", session_id="recovery_session"):
            events.append(event)

        done_event = next(e for e in events if e["event"] == "done")
        assert done_event["data"]["chinese"] == "已成功恢复！"


def test_credit_balance_and_arrearage_400_report_chinese_quota_guidance():
    anthropic_raw = (
        'API returned status 400: {"type":"error","error":{"type":"invalid_request_error",'
        '"message":"Your credit balance is too low to access the Anthropic API."}}'
    )
    anthropic = format_provider_error("anthropic", 400, anthropic_raw)
    assert anthropic["error_code"] == "QUOTA_EXCEEDED"
    assert "余额不足" in anthropic["summary_zh"]
    assert "充值或检查账单" in anthropic["action_zh"]
    assert "（402）" not in anthropic["action_zh"]
    assert "credit balance" not in anthropic["error"].lower()

    qwen_raw = '{"code":"Arrearage","message":"Access denied, please make sure your account is in good standing."}'
    qwen = format_provider_error("qwen", 400, qwen_raw)
    assert qwen["error_code"] == "QUOTA_EXCEEDED"
    assert "余额不足" in qwen["summary_zh"]
    assert "充值或检查账单" in qwen["action_zh"]
