"""Failed replies explain the next step in Chinese and carry a stable error code."""

import asyncio
import logging
import re
from unittest.mock import patch

import httpx
from httpx import ASGITransport, AsyncClient
import pytest

from galgame2voice.adapters.base import BaseLLMAdapter, ChatMessage, LLMResponse, MissingApiKeyError, TestResult
from galgame2voice.adapters.llm.openai_adapter import OpenAICompatibleLLMAdapter
from galgame2voice.adapters.registry import get_llm_adapter
from galgame2voice.main import create_app
from galgame2voice.routers.chat import set_chat_service
from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.audio_identity import AudioIdentity, VoicePreparation
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services import gpt_sovits_client as gpt_sovits_client_module
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.voice_manager import VoiceManager
from galgame2voice.utils.error_diagnostics import chat_error_payload, diagnose_llm_error, format_provider_error


def test_missing_key_names_the_provider_and_the_settings_page():
    payload = chat_error_payload(MissingApiKeyError(), provider_id="gemini", api_key_configured=False)
    assert payload["error_code"] == "API_KEY_MISSING"
    assert all(k in payload["error"] for k in ("Google Gemini", "设置中心 → 对话模型", "保存并启用此模型", "自定义 / 本地模型 (Ollama / vLLM)")) and "上面的 API 密钥" not in payload["error"]
    # Still a ValueError mentioning authentication, as callers and tests expect.
    assert isinstance(MissingApiKeyError(), ValueError)
    assert "Authentication error" in str(MissingApiKeyError())


def test_private_endpoint_block_is_not_reported_as_a_dns_failure():
    exc = PermissionError(
        "SSRF/DNS-Rebinding 阻止: 目标地址解析到环回/私网……请在「设置中心 → 对话模型」勾选「允许连接本机或局域网的模型服务」。"
    )
    payload = chat_error_payload(exc, provider_id="custom", api_key_configured=True)
    assert payload["error_code"] == "PRIVATE_ENDPOINT_BLOCKED"
    assert "允许连接本机或局域网的模型服务" in payload["error"]
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
    assert unknown["error_code"] == "PROVIDER_ERROR"
    assert "模型服务返回异常" in unknown["error"] and "Simulated adapter failure" not in unknown["error"]


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
        assert "模型服务返回异常" in events[1]["data"]["error"]
        assert "Simulated adapter failure" not in events[1]["data"]["error"]

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
    assert anthropic["summary_zh"] == "账户可用余额不足"
    assert "充值或检查账单" in anthropic["action_zh"]
    assert "（402）" not in anthropic["action_zh"] and "（400）" not in anthropic["summary_zh"]
    assert "credit balance" not in anthropic["error"].lower()

    qwen_raw = '{"code":"Arrearage","message":"Access denied, please make sure your account is in good standing."}'
    qwen = format_provider_error("qwen", 400, qwen_raw)
    assert qwen["error_code"] == "QUOTA_EXCEEDED"
    assert qwen["summary_zh"] == "账户可用余额不足"
    assert "充值或检查账单" in qwen["action_zh"]


_STATUS_PAREN_RE = re.compile(r"[（(]\d{3}(?:/\d{3})?[）)]")


@pytest.mark.asyncio
async def test_error_copy_uses_real_ui_labels_without_raw_english_or_status_codes(monkeypatch, caplog):
    # 1. Context length exceeded points to real UI controls and omits stale setting names.
    ctx = chat_error_payload(
        RuntimeError("API returned status 400: This model's maximum context length is 32768 tokens."),
        provider_id="openai",
        api_key_configured=True,
    )
    assert ctx["error_code"] == "CONTEXT_LENGTH_EXCEEDED"
    assert "角色与会话 → 对话高级参数" in ctx["action_zh"]
    assert "携带上下文条数" in ctx["action_zh"] and "点舞台左上角「会话」后点击「新建会话」" in ctx["action_zh"]
    assert "携带上下文条数" in ctx["error"] and "新建会话" in ctx["error"]
    for stale in ("会话设置", "最大上下文轮数", "清理历史记录"):
        for field in ("summary_zh", "action_zh", "error"):
            assert stale not in ctx[field]

    # 2. HTTP 400 unsupported parameter and unclassified RuntimeError omit raw English, Base URL, and status parens.
    unsupported = chat_error_payload(
        RuntimeError("API returned status 400: Invalid parameter 'reasoning_effort' is not supported by this model"),
        provider_id="openai",
        api_key_configured=True,
    )
    assert unsupported["error_code"] == "PARAMETER_NOT_SUPPORTED"
    assert unsupported["summary_zh"] == "模型参数不受支持"
    assert "设置中心 → 对话模型" in unsupported["action_zh"]
    assert "默认（由模型决定）" in unsupported["action_zh"] and "关闭" in unsupported["action_zh"]
    for field in ("summary_zh", "action_zh", "error"):
        assert "原始信息" not in unsupported[field]
        assert "Invalid parameter" not in unsupported[field]
        assert "not supported" not in unsupported[field]
        assert "Base URL" not in unsupported[field]
        assert not _STATUS_PAREN_RE.search(unsupported[field])

    unclassified = chat_error_payload(
        RuntimeError("Unexpected upstream WireProtocol explosion"),
        provider_id="custom",
        api_key_configured=True,
    )
    assert unclassified["error_code"] == "PROVIDER_ERROR"
    assert unclassified["summary_zh"] == "模型服务返回异常"
    for label in ("设置中心 → 对话模型", "接口地址", "API 密钥", "模型名称"):
        assert label in unclassified["action_zh"]
    for field in ("summary_zh", "action_zh", "error"):
        assert "原始信息" not in unclassified[field]
        assert "Unexpected upstream" not in unclassified[field]
        assert "WireProtocol" not in unclassified[field]
        assert "Base URL" not in unclassified[field]
        assert not _STATUS_PAREN_RE.search(unclassified[field])

    # 3. Route-level POST /api/providers/test for 429, 402, ftp:// preflight, and warning logs.
    app = create_app()
    orig_get = httpx.AsyncClient.get
    orig_post = httpx.AsyncClient.post
    upstream_state = {"status": 429, "body": '{"error":{"message":"Rate limit reached on tokens per min (TPM)"}}'}

    def _is_internal(url_str: str) -> bool:
        return (
            not url_str.startswith("http://") and not url_str.startswith("https://")
        ) or url_str.startswith("http://127.0.0.1:8080")

    async def mocked_get(self_client, url, *args, **kwargs):
        url_str = str(url)
        if _is_internal(url_str):
            return await orig_get(self_client, url, *args, **kwargs)
        return httpx.Response(
            upstream_state["status"],
            text=upstream_state["body"],
            request=httpx.Request("GET", url_str),
        )

    async def mocked_post(self_client, url, *args, **kwargs):
        url_str = str(url)
        if _is_internal(url_str):
            return await orig_post(self_client, url, *args, **kwargs)
        return httpx.Response(
            upstream_state["status"],
            text=upstream_state["body"],
            request=httpx.Request("POST", url_str),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", mocked_get)
    monkeypatch.setattr(httpx.AsyncClient, "post", mocked_post)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        # 429 rate limit
        resp_429 = await client.post(
            "/api/providers/test",
            json={"id": "openai", "api_key": "sk-openai-429-key-12345", "base_url": "https://api.openai.com/v1"},
        )
        assert resp_429.status_code == 200
        d429 = resp_429.json()
        assert d429["status_code"] == 429 and d429["error_code"] == "OPENAI_RATE_LIMIT"
        assert "角色与会话 → 对话高级参数" in d429["action_zh"] and "最大生成长度" in d429["action_zh"]

        # 402 insufficient balance
        upstream_state.update(status=402, body='{"error":{"message":"Insufficient Balance"}}')
        resp_402 = await client.post(
            "/api/providers/test",
            json={"id": "deepseek", "api_key": "sk-deepseek-402-key-12345", "base_url": "https://api.deepseek.com"},
        )
        assert resp_402.status_code == 200
        d402 = resp_402.json()
        assert d402["status_code"] == 402 and d402["error_code"] == "QUOTA_EXCEEDED"
        assert d402["summary_zh"] == "账户可用余额不足"

        # ftp:// invalid scheme preflight
        resp_ftp = await client.post(
            "/api/providers/test",
            json={"id": "custom", "api_key": "sk-ftp-test-key-12345", "base_url": "ftp://api.example.com/v1"},
        )
        assert resp_ftp.status_code == 400
        dftp = resp_ftp.json()
        assert dftp["diagnostic_code"] == "INVALID_BASE_URL"
        assert "设置中心 → 对话模型" in dftp["action_zh"] and "接口地址" in dftp["action_zh"]

        for payload in (d429, d402, dftp):
            for field in ("summary_zh", "action_zh", "error", "error_title", "diagnostic"):
                for forbidden in ("RPM/TPM", "会话设置", "Base URL"):
                    assert forbidden not in payload[field]
                assert not _STATUS_PAREN_RE.search(payload[field])

        # Unclassified 500 + Anthropic 400 keep sanitized raw error & status code in warning logs, not user copy
        with caplog.at_level(logging.WARNING, logger="galgame2voice.adapters.llm"):
            upstream_state.update(status=500, body="Upstream 500 failure with secret sk-secretleak1234567890abcd")
            resp_500 = await client.post(
                "/api/providers/test",
                json={"id": "openai", "api_key": "sk-openai-500-key-12345", "base_url": "https://api.openai.com/v1"},
            )
            d500 = resp_500.json()
            assert d500["error_code"] == "SERVER_OVERLOADED"
            assert d500["summary_zh"] == "模型服务临时故障"
            for field in ("summary_zh", "action_zh", "error", "error_title", "diagnostic"):
                assert "Upstream 500 failure" not in d500[field]
                assert "原始信息" not in d500[field]
                assert "Base URL" not in d500[field]
                assert not _STATUS_PAREN_RE.search(d500[field])

            upstream_state.update(
                status=400,
                body='{"type":"error","error":{"type":"invalid_request_error","message":"Your credit balance is too low to access the Anthropic API."}}',
            )
            resp_ant = await client.post(
                "/api/providers/test",
                json={"id": "anthropic", "api_key": "sk-ant-test-key-1234567890", "base_url": "https://api.anthropic.com/v1"},
            )
            dant = resp_ant.json()
            assert dant["error_code"] == "QUOTA_EXCEEDED" and dant["status_code"] == 400
            assert dant["summary_zh"] == "账户可用余额不足"

        assert "Upstream 500 failure" in caplog.text and "status=500" in caplog.text
        assert "sk-secretleak1234567890abcd" not in caplog.text
        assert "credit balance is too low" in caplog.text and "status=400" in caplog.text

        # Router-level exception fallback uses diag_info["error"] without raw English
        with patch.object(
            OpenAICompatibleLLMAdapter,
            "test_connection",
            side_effect=RuntimeError("Unexpected adapter crash"),
        ):
            resp_crash = await client.post(
                "/api/providers/test",
                json={"id": "openai", "api_key": "sk-crash-test-12345", "base_url": "https://api.openai.com/v1"},
            )
        dcrash = resp_crash.json()
        assert dcrash["error_code"] == "PROVIDER_ERROR"
        assert dcrash["message"] == "模型服务返回异常"
        for field in ("message", "summary_zh", "action_zh", "error", "error_title", "diagnostic"):
            assert "Unexpected adapter crash" not in dcrash[field]
            assert "连接测试异常" not in dcrash[field]
            assert "Base URL" not in dcrash[field]
            assert not _STATUS_PAREN_RE.search(dcrash[field])


@pytest.mark.parametrize("raw", [
    "max_tokens is too large: 32768. This model supports at most 4096 completion tokens, whereas you provided 32768.",
    "Invalid max_tokens value, the valid range of max_tokens is [1, 8192]",
    "Range of max_tokens should be [1, 8192]",
    "Invalid 'max_completion_tokens': integer above maximum value. Expected a value <= 16384, but got 32768 instead.",
    "max_tokens: 100000 > 64000, which is the maximum allowed number of output tokens for claude-opus-4-1",
])
def test_max_tokens_too_large_points_at_output_length(raw):
    """「最大生成长度」超过模型上限时，提示去调小它，而不是调小上下文或只说「模型服务返回异常」。"""
    result = diagnose_llm_error(raw, status_code=400)
    assert result.error_code == "MAX_TOKENS_TOO_LARGE"
    assert "「角色与会话 → 对话高级参数」" in result.guidance
    assert "携带上下文条数" not in result.guidance


@pytest.mark.asyncio
async def test_deepseek_model_not_exist_400_is_diagnosed_as_model_not_found():
    """DeepSeek returns HTTP 400 with "Model Not Exist" for bad/retired model names."""
    body = b'{"error":{"message":"Model Not Exist","type":"invalid_request_error","param":null,"code":"invalid_request_error"}}'

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    adapter = OpenAICompatibleLLMAdapter(
        api_key="sk-test", base_url=f"http://127.0.0.1:{port}/v1", allow_private=True, max_retries=0,
    )
    try:
        with pytest.raises(RuntimeError) as exc_info:
            async for _ in adapter.stream_chat([ChatMessage(role="user", content="你好")], model="deepseek-removed"):
                pass
    finally:
        server.close()
        await server.wait_closed()

    payload = chat_error_payload(exc_info.value, provider_id="deepseek", api_key_configured=True)
    assert payload["error_code"] == "MODEL_NOT_FOUND"
    assert payload["retryable"] is False
    assert "模型名称" in payload["summary_zh"]

@pytest.mark.asyncio
async def test_gemini_context_length_400_is_diagnosed_as_context_exceeded():
    """Gemini reports input token overflow with "exceeds the maximum number of tokens allowed"."""
    from galgame2voice.adapters.llm.gemini_adapter import GeminiLLMAdapter

    body = (
        b'[{"error":{"code":400,'
        b'"message":"The input token count (1200000) exceeds the maximum number of tokens allowed (1048576).",'
        b'"status":"INVALID_ARGUMENT"}}]'
    )

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    adapter = GeminiLLMAdapter(
        api_key="AIza-test", base_url=f"http://127.0.0.1:{port}/v1beta/openai", allow_private=True, max_retries=0,
    )
    try:
        with pytest.raises(RuntimeError) as exc_info:
            async for _ in adapter.stream_chat([ChatMessage(role="user", content="你好")], model="gemini-2.5-flash"):
                pass
    finally:
        server.close()
        await server.wait_closed()

    payload = chat_error_payload(exc_info.value, provider_id="gemini", api_key_configured=True)
    assert payload["error_code"] == "CONTEXT_LENGTH_EXCEEDED"
    assert "携带上下文条数" in payload["action_zh"] and "新建会话" in payload["action_zh"]

@pytest.mark.asyncio
async def test_upstream_500_is_diagnosed_as_temporary_server_fault():
    """HTTP 500 from upstream LLM providers is a temporary server fault, not a config error."""
    body = b'{"error":{"message":"The server had an error while processing your request.","type":"server_error"}}'

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    adapter = OpenAICompatibleLLMAdapter(
        api_key="sk-test", base_url=f"http://127.0.0.1:{port}/v1", allow_private=True, max_retries=0,
    )
    try:
        with pytest.raises(RuntimeError) as exc_info:
            async for _ in adapter.stream_chat([ChatMessage(role="user", content="你好")], model="gpt-4o"):
                pass
    finally:
        server.close()
        await server.wait_closed()

    payload = chat_error_payload(exc_info.value, provider_id="openai", api_key_configured=True)
    assert payload["error_code"] == "SERVER_OVERLOADED"
    assert payload["retryable"] is True
    assert "临时故障" in payload["summary_zh"] and "无需修改配置" in payload["action_zh"]


def test_body_reason_wins_over_a_bare_500():
    # Local servers and relays also answer a full context with HTTP 500; the user must still be told to trim it.
    result = diagnose_llm_error("This model's maximum context length is 8192 tokens.", status_code=500)
    assert result.error_code == "CONTEXT_LENGTH_EXCEEDED"
