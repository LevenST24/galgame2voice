"""
Structured Error Diagnostics & User Guidance for galgame2voice.
Intercepts provider HTTP errors, authentication failures, and network transport faults,
translating them into user-friendly Chinese diagnostic recommendations.
"""

from dataclasses import dataclass
import html
import re
from typing import Any
from galgame2voice.utils.logger import MaskingFilter


RETRYABLE_DIAGNOSTIC_CODES = frozenset({
    "NETWORK_TIMEOUT",
    "CONNECTION_RESET",
    "NETWORK_CONNECTION_FAILED",
    "BAD_GATEWAY",
    "SERVER_OVERLOADED",
    "RATE_LIMIT",
    "OPENAI_RATE_LIMIT",
    "ANTHROPIC_RATE_LIMIT",
    "STREAM_INCOMPLETE",
    "EMPTY_REPLY",
})

_DIAGNOSTIC_CODE_ALIASES: dict[str, str] = {
    "API_KEY_MISSING": "MISSING_API_KEY",
    "AUTH_FAILED": "AUTH_INVALID_KEY",
    "OPENAI_AUTH_FAILED": "AUTH_INVALID_KEY",
    "ANTHROPIC_AUTH_FAILED": "AUTH_INVALID_KEY",
    "XAI_AUTH_FAILED": "AUTH_INVALID_KEY",
    "GEMINI_KEY_INVALID": "AUTH_INVALID_KEY",
    "FORBIDDEN": "AUTH_FORBIDDEN",
    "XAI_FORBIDDEN": "AUTH_FORBIDDEN",
    "GEMINI_PERMISSION_DENIED": "AUTH_FORBIDDEN",
    "GEMINI_REGION_RESTRICTED": "AUTH_FORBIDDEN",
    "OPENAI_QUOTA_EXCEEDED": "QUOTA_EXCEEDED",
    "XAI_QUOTA_EXCEEDED": "QUOTA_EXCEEDED",
}


def normalize_diagnostic_code(error_code: str | None) -> str:
    """Normalizes provider or runtime error codes into stable UPPER_SNAKE_CASE diagnostic codes."""
    raw = re.sub(r"[^A-Za-z0-9]+", "_", (error_code or "").strip()).strip("_").upper()
    if not raw:
        return "PROVIDER_ERROR"
    return _DIAGNOSTIC_CODE_ALIASES.get(raw, raw)


def is_retryable_diagnostic_code(diagnostic_code: str | None) -> bool:
    """Returns True only for transient faults that can be retried directly without configuration edits."""
    if not diagnostic_code:
        return False
    return diagnostic_code.strip().upper() in RETRYABLE_DIAGNOSTIC_CODES


@dataclass
class DiagnosticResult:
    """Structured diagnostic representation of provider or runtime errors with user guidance."""

    error_code: str
    message: str
    guidance: str
    status_code: int | None = None
    raw_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        diagnostic_code = normalize_diagnostic_code(self.error_code)
        return {
            "error": self.message,
            "diagnostic": self.guidance,
            "error_code": self.error_code,
            "status_code": self.status_code,
            "diagnostic_code": diagnostic_code,
            "summary_zh": self.message,
            "action_zh": self.guidance,
            "retryable": is_retryable_diagnostic_code(diagnostic_code),
        }


CONTENT_FILTER_FINISH_REASONS = (
    "content_filter",
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "refusal",
)
EMPTY_REPLY_CONTENT_FILTERED = "模型输出触发了服务商内容安全过滤，未能生成回复。可以调整表达后重试或更换模型。"
STREAM_TRUNCATED_CONTENT_FILTERED = "模型输出触发了服务商内容安全过滤，回复未能完整生成。可以调整表达后重试或更换模型。"
STREAM_TRUNCATED_ON_LENGTH = "模型输出达到最大长度限制，回复未能完整生成。建议调大「最大生成长度」或降低「思考强度」。"


EMPTY_REPLY_STOPPED_ON_LENGTH = (
    "模型把生成长度用完了，却没有写出回复，通常是思考占满了额度。"
    "可以在模型设置里把“思考强度”调低，或在会话设置里调大“最大生成长度”。"
)
EMPTY_REPLY_DEFAULT = "模型这次没有返回任何内容，可以重新发送或换个模型试试。"


class EmptyReplyError(ValueError):
    """Raised when the LLM finishes generating without producing visible reply text."""

    def __init__(self, message: str | None = None, *, stopped_on_length: bool | None = None):
        if stopped_on_length is None:
            stopped_on_length = message == EMPTY_REPLY_STOPPED_ON_LENGTH
        if not message:
            message = EMPTY_REPLY_STOPPED_ON_LENGTH if stopped_on_length else EMPTY_REPLY_DEFAULT
        super().__init__(message)
        self.stopped_on_length = stopped_on_length


class StreamIncompleteError(RuntimeError):
    """Raised when an SSE stream ends abruptly without any terminal signal ([DONE] or finish_reason)."""

    is_truncation: bool = True

    def __init__(self, message: str = "上游模型连接意外中断，回复未能完整结束。可以重新发送尝试重试。"):
        super().__init__(message)


class StreamTruncatedError(RuntimeError):
    """Raised when an SSE stream is truncated by length/max_tokens limit."""

    is_truncation: bool = True

    def __init__(
        self,
        message: str = "模型输出达到最大长度限制，回复未能完整生成。建议调大「最大生成长度」或降低「思考强度」。",
        *,
        stopped_on_length: bool = True,
    ):
        super().__init__(message)
        self.stopped_on_length = stopped_on_length


class ConfigurationSelectionError(ValueError):
    """A requested identity is missing or ambiguous, before any inference."""

    def __init__(self, message: str, *, error_code: str, status_code: int = 404):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


_CONFIGURATION_SELECTION_GUIDANCE: dict[str, tuple[str, str]] = {
    "PROVIDER_NOT_FOUND": (
        "指定的模型服务配置不存在",
        "请打开「设置中心 → 对话模型」重新选择并启用可用的模型服务。",
    ),
    "VOICE_PROFILE_NOT_FOUND": (
        "所选角色音色已不存在",
        "请打开「设置中心 → 角色与会话」重新选择可用的角色或自定义音色。",
    ),
    "VOICE_PROFILE_AMBIGUOUS": (
        "存在重名的角色音色配置",
        "请打开「设置中心 → 角色与会话」明确选择要使用的具体音色。",
    ),
}


def format_provider_error(
    provider_id: str | None = None,
    status_code: int | None = None,
    raw_error: Exception | str | None = None,
    api_key_configured: bool | None = None,
) -> dict[str, Any]:
    """
    Translates HTTP status code and raw error from a provider into structured
    Chinese guidance dictionary {"error": ..., "diagnostic": ..., "error_code": ..., "status_code": ...}.
    """
    diag = diagnose_llm_error(
        exc_or_msg=raw_error,
        provider_id=provider_id,
        status_code=status_code,
        raw_body=str(raw_error) if not isinstance(raw_error, BaseException) else None,
        api_key_configured=api_key_configured,
    )
    return diag.to_dict()


def chat_error_payload(
    exc: Exception | str | None,
    provider_id: str | None = None,
    api_key_configured: bool | None = None,
) -> dict[str, Any]:
    """
    SSE `error` payload for a reply the model could not produce: one readable Chinese
    sentence with the next step, plus a stable `error_code` the console uses to offer
    the matching settings page.
    """
    if isinstance(exc, ConfigurationSelectionError):
        diagnostic_code = normalize_diagnostic_code(exc.error_code)
        msg = str(exc).strip()
        default_summary = msg if re.search(r"[\u4e00-\u9fff]", msg) else "当前模型或角色配置不可用"
        summary_zh, action_zh = _CONFIGURATION_SELECTION_GUIDANCE.get(
            diagnostic_code,
            (default_summary, "请打开「设置中心」检查对话模型与角色音色配置后重试。"),
        )
        return {
            "error": str(exc),
            "error_code": exc.error_code,
            "status_code": exc.status_code,
            "diagnostic_code": diagnostic_code,
            "summary_zh": summary_zh,
            "action_zh": action_zh,
            "retryable": is_retryable_diagnostic_code(diagnostic_code),
        }
    if isinstance(exc, EmptyReplyError):
        if exc.stopped_on_length:
            diagnostic_code = "EMPTY_REPLY_LENGTH_LIMIT"
            summary_zh = "模型生成长度耗尽，未能输出回复"
            action_zh = "请在模型设置中调低「思考强度」，或在会话设置中调大「最大生成长度」后重试。"
        elif str(exc) == EMPTY_REPLY_CONTENT_FILTERED:
            diagnostic_code = "EMPTY_REPLY_CONTENT_FILTERED"
            summary_zh = "模型输出触发了服务商内容安全过滤"
            action_zh = "请调整对话表达后重试，或切换其他模型。"
        else:
            diagnostic_code = "EMPTY_REPLY"
            summary_zh = "模型本次未返回任何可见回复"
            action_zh = "可以直接重新发送重试，或切换其他模型试试。"
        return {
            "error": str(exc),
            "error_code": "empty_reply",
            "kind": "empty_reply",
            "status_code": 200,
            "diagnostic_code": diagnostic_code,
            "summary_zh": summary_zh,
            "action_zh": action_zh,
            "retryable": is_retryable_diagnostic_code(diagnostic_code),
        }
    if isinstance(exc, StreamIncompleteError):
        diagnostic_code = "STREAM_INCOMPLETE"
        return {
            "error": str(exc),
            "diagnostic": str(exc),
            "error_code": "stream_incomplete",
            "status_code": 200,
            "diagnostic_code": diagnostic_code,
            "summary_zh": "上游模型连接意外中断，回复未完整结束",
            "action_zh": "无需修改配置，可直接重新发送尝试重试。",
            "retryable": is_retryable_diagnostic_code(diagnostic_code),
        }
    if isinstance(exc, StreamTruncatedError):
        if exc.stopped_on_length:
            diagnostic_code = "STREAM_TRUNCATED_ON_LENGTH"
            error_code = "stream_truncated_on_length"
            summary_zh = "模型输出达到最大长度限制，回复未完整生成"
            action_zh = "建议在会话设置中调大「最大生成长度」，或在模型设置中调低「思考强度」。"
        else:
            diagnostic_code = "STREAM_TRUNCATED_CONTENT_FILTERED"
            error_code = "stream_truncated_content_filtered"
            summary_zh = "模型输出触发内容安全过滤，回复未完整生成"
            action_zh = "建议调整对话表达后重试，或切换其他模型。"
        return {
            "error": str(exc),
            "diagnostic": str(exc),
            "error_code": error_code,
            "status_code": 200,
            "diagnostic_code": diagnostic_code,
            "summary_zh": summary_zh,
            "action_zh": action_zh,
            "retryable": is_retryable_diagnostic_code(diagnostic_code),
        }
    diag = diagnose_llm_error(exc, provider_id=provider_id, api_key_configured=api_key_configured)
    diag_dict = diag.to_dict()
    return {
        "error": f"{diag.message}：{diag.guidance}",
        "error_code": diag.error_code,
        "status_code": diag.status_code,
        "diagnostic_code": diag_dict["diagnostic_code"],
        "summary_zh": diag_dict["summary_zh"],
        "action_zh": diag_dict["action_zh"],
        "retryable": diag_dict["retryable"],
    }


def _provider_display_name(provider_id: str | None) -> str:
    """Preset display name for a provider id, or an empty string when unknown."""
    if not provider_id:
        return ""
    try:
        from galgame2voice.adapters.registry import get_provider_preset

        preset = get_provider_preset(str(provider_id))
    except Exception:
        return ""
    return str((preset or {}).get("name") or "")


def _diagnose_local_configuration(
    raw_lower: str,
    sanitized_raw: str,
    provider_id: str | None,
    api_key_configured: bool | None,
) -> DiagnosticResult | None:
    """Failures caused by this console's own settings rather than by the provider."""
    if "ssrf/dns-rebinding" in raw_lower or "允许私网 llm 端点" in raw_lower:
        return DiagnosticResult(
            error_code="PRIVATE_ENDPOINT_BLOCKED",
            message="模型地址指向本机或局域网，当前未允许连接",
            guidance="如果这是你信任的本地模型（如 Ollama、LM Studio），请在「设置中心 → 对话模型」开启「允许私网 LLM 端点」后重试；否则请改用服务商提供的公网地址。",
            raw_error=sanitized_raw,
        )
    is_timeout_text = any(kw in raw_lower for kw in ("timed out", "timeouterror", "readtimeout", "connecttimeout"))
    auth_failure = not is_timeout_text and any(
        kw in raw_lower for kw in ("authentication", "unauthorized", "api key", "401")
    )
    if "api key is not configured" in raw_lower or (api_key_configured is False and auth_failure):
        name = _provider_display_name(provider_id)
        return DiagnosticResult(
            error_code="API_KEY_MISSING",
            message=f"尚未填写{f'「{name}」的' if name else ''} API Key",
            guidance="在上面的 API 密钥里填入后再测试；使用 Ollama、LM Studio 等本地模型时，请选择「自定义 / 本地模型」。",
            raw_error=sanitized_raw,
        )
    return None


def _unwrap_exception_chain(exc: BaseException) -> list[BaseException]:
    """Traverses __cause__ and __context__ to collect all chained exceptions."""
    chain: list[BaseException] = []
    curr: BaseException | None = exc
    visited: set[int] = set()
    while curr is not None and id(curr) not in visited:
        visited.add(id(curr))
        chain.append(curr)
        curr = curr.__cause__ or curr.__context__
    return chain


def _clean_html_text(text: str) -> str:
    """Strips HTML markup and extracts title if present to avoid leaking raw HTML tags."""
    if not text:
        return ""
    lower = text.lower()
    has_html = any(tag in lower for tag in (
        "<!doctype", "<html", "<head", "<body", "<title", "<center",
        "<h1", "<h2", "<h3", "</h", "</p>", "</div>", "</span>",
        "</table", "<script", "<style"
    ))
    if not has_html:
        return text

    # Strip comments, scripts, and styles first so they don't corrupt title extraction or body text
    cleaned = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)

    title_match = re.search(r"<title[^>]*>(.*?)</title>", cleaned, re.IGNORECASE | re.DOTALL)
    if title_match:
        title_content = re.sub(r"<[^>]+>", " ", title_match.group(1)).strip()
        title_content = html.unescape(re.sub(r"\s+", " ", title_content)).strip()
        if title_content:
            html_start = re.search(r"(<!doctype|<html|<head|<body|<title)", text, re.IGNORECASE)
            if html_start and html_start.start() > 0:
                prefix = re.sub(r"\s+", " ", text[:html_start.start()]).strip()
                if prefix:
                    return f"{prefix} {title_content}".strip()
            return title_content

    # If no title or title was empty, strip all tags and decode entities
    stripped = re.sub(r"<[^>]+>", " ", cleaned)
    stripped = html.unescape(stripped)
    return re.sub(r"\s+", " ", stripped).strip()


_VALID_STATUS_CODES = (401, 402, 403, 429, 404, 400, 408, 502, 503, 504, 524, 529)

_EXPLICIT_STATUS_RE = re.compile(
    r"(?:"
    r"\bstatus(?:[\s_]+code)?[:\s=]+['\"]?(\d{3})\b|"
    r"\bhttp(?:/[0-9.]+)?\s+['\"]?(\d{3})\b|"
    r"\bcode[:\s=]+['\"]?(\d{3})\b|"
    r"\berror(?:[\s_]+code)?[:\s=]+['\"]?(\d{3})\b|"
    r"\((\d{3})\)|"
    r"\[(\d{3})\]|"
    r"<(?:title|h1)[^>]*>(?:error\s+)?(\d{3})\b|"
    r"^(\d{3})(?:[\s:]|$)"
    r")",
    re.IGNORECASE,
)


def _extract_status_code(status_code: int | None, raw_lower: str) -> int | None:
    if status_code is not None:
        return status_code

    # Pass 1: high-confidence explicit status/http/code patterns
    for m in _EXPLICIT_STATUS_RE.finditer(raw_lower):
        matched = next((int(g) for g in m.groups() if g is not None), None)
        if matched in _VALID_STATUS_CODES:
            return matched

    # Pass 2: secondary/gateway phrases
    for code in (524, 502, 504, 503, 529, 402, 429, 401, 403, 404, 400, 408):
        if (
            f"{code} a timeout occurred" in raw_lower
            or f"{code} bad gateway" in raw_lower
            or f" {code} " in raw_lower
            or f" {code}:" in raw_lower
            or f"'{code}'" in raw_lower
            or f'"{code}"' in raw_lower
        ):
            return code
    return None


def _diagnose_unsupported_parameter(
    raw_lower: str,
    status_code: int | None,
    sanitized_raw: str,
) -> DiagnosticResult | None:
    """Parameter or feature incompatibilities such as reasoning_effort or thinkingConfig."""
    if status_code != 400:
        return None
    is_reasoning_parameter = any(kw in raw_lower for kw in (
        "reasoning_effort", "thinking level", "thinkingconfig", "thinking_config",
    ))
    is_rejected = any(kw in raw_lower for kw in (
        "not supported", "unsupported", "invalid", "unknown", "unrecognized", "not allowed", "not permitted",
    ))
    if is_reasoning_parameter and is_rejected:
        return DiagnosticResult(
            error_code="PARAMETER_NOT_SUPPORTED",
            message="模型参数不受支持 (400 Bad Request)",
            guidance=f"提供商或所选模型不支持当前参数（如思考强度 reasoning_effort）。请尝试将「思考强度」设为默认或调整模型配置。原始信息: {sanitized_raw[:160]}",
            status_code=400,
            raw_error=sanitized_raw,
        )
    return None


_RESET_TYPES = (
    "connectionreseterror",
    "connectionabortederror",
    "remoteprotocolerror",
    "readerror",
    "writeerror",
    "brokenpipeerror",
    "remotedisconnected",
    "incompleteread",
)

_RESET_PHRASES = (
    "connection reset",
    "connection abort",
    "peer closed connection",
    "server disconnected",
    "remote end closed connection",
    "incomplete chunked read",
)

_RESET_TEXT_RE = re.compile(
    r"\b(?:"
    + "|".join(_RESET_TYPES)
    + r"|(?:winerror|errno|error)\s+1005[34]"
    + r")\b",
    re.ASCII,
)


def _diagnose_transport_fault(
    exc_type: str,
    raw_lower: str,
    status_code: int | None,
    sanitized_raw: str,
    exc_chain: list[BaseException] | None = None,
) -> DiagnosticResult | None:
    exc_chain = exc_chain or []
    chain_types = [type(e).__name__.lower() for e in exc_chain]

    is_timeout = (
        "timeout" in exc_type
        or any("timeout" in t for t in chain_types)
        or "timeouterror" in raw_lower
        or "timed out" in raw_lower
        or "readtimeout" in raw_lower
        or "connecttimeout" in raw_lower
        or "a timeout occurred" in raw_lower
        or "524 timeout" in raw_lower
        or status_code in (408, 504, 524)
    )

    if is_timeout:
        is_read_timeout = (
            any("readtimeout" in t for t in chain_types)
            or "readtimeout" in raw_lower
            or "read timeout" in raw_lower
            or "reading stream" in raw_lower
            or "read operation timed out" in raw_lower
        )
        is_local = any(h in raw_lower for h in ("127.0.0.1", "localhost", "::1"))

        if is_local and is_read_timeout:
            return DiagnosticResult(
                error_code="NETWORK_TIMEOUT",
                message="本机模型响应超时 (Read Timeout)",
                guidance="本机模型响应太慢或仍在加载。本地服务（如 Ollama / LM Studio）在首次加载模型权重到显存或生成长文本时耗时较长，请检查本地推理服务运行状态，或调大超时时间后重试。",
                status_code=status_code or 408,
                raw_error=sanitized_raw,
            )

        if (
            status_code == 524
            or "524 timeout" in raw_lower
            or "a timeout occurred" in raw_lower
            or " 524 " in raw_lower
            or "(524)" in raw_lower
            or "'524'" in raw_lower
            or '"524"' in raw_lower
        ):
            return DiagnosticResult(
                error_code="NETWORK_TIMEOUT",
                message="网关连接超时 (524 Timeout)",
                guidance="Cloudflare 或反向代理网关等待上游源站响应超时（524 A Timeout Occurred）。若正在使用推理模型（如 DeepSeek-R1 / QwQ），长思考可能超出了网关超时时限；建议降低思考强度、简化提示词或在反代中调大超时后重试。",
                status_code=524,
                raw_error=sanitized_raw,
            )

        return DiagnosticResult(
            error_code="NETWORK_TIMEOUT",
            message="网络连接超时 (Request Timeout)",
            guidance="连接提供商服务器超时。海外模型服务（如 Google / OpenAI / xAI）在国内网络直连通常受阻，请确保本地代理客户端（如 Clash / v2rayN）正在运行，或配置支持该服务的中转反向代理 Base URL。",
            status_code=status_code or 408,
            raw_error=sanitized_raw,
        )

    if (
        "10061" in raw_lower
        or "connection refused" in raw_lower
        or "refused" in raw_lower
    ):
        return DiagnosticResult(
            error_code="CONNECTION_REFUSED",
            message="网络连接被拒绝 (Connection Refused)",
            guidance="目标地址或本地反向代理端口未开放。请检查 Base URL 拼写及端口配置，若使用本地反向代理请确认代理软件已启动且监听相应端口。",
            status_code=status_code or 502,
            raw_error=sanitized_raw,
        )

    if (
        "getaddrinfo failed" in raw_lower
        or "dns" in raw_lower
        or "resolutionerror" in exc_type
        or any("resolutionerror" in t for t in chain_types)
        or "nodename nor servname provided" in raw_lower
    ):
        return DiagnosticResult(
            error_code="DNS_ERROR",
            message="服务器域名解析失败 (DNS Error)",
            guidance="无法解析 Base URL 的服务器域名，请检查网址拼写、主机名或本地网络 DNS 设置。",
            status_code=status_code or 502,
            raw_error=sanitized_raw,
        )

    is_reset_exc = (
        any(t in exc_type for t in _RESET_TYPES)
        or any(t in ct for ct in chain_types for t in _RESET_TYPES)
    )
    is_upstream_4xx = status_code is not None and 400 <= status_code < 500
    is_reset_text = not is_upstream_4xx and (
        any(ph in raw_lower for ph in _RESET_PHRASES)
        or bool(_RESET_TEXT_RE.search(raw_lower))
    )
    if is_reset_exc or is_reset_text:
        return DiagnosticResult(
            error_code="CONNECTION_RESET",
            message="网络连接被中途断开或重置 (Connection Reset)",
            guidance="与模型服务的连接在传输中途中断（常见于网络波动、本地代理切换节点/重启或睡眠唤醒）。请检查网络与代理稳定性后直接重新发送重试，无需修改 API Key 或模型名称。",
            status_code=status_code or 502,
            raw_error=sanitized_raw,
        )

    # ConnectError also covers TLS failures and other connection setup errors.
    # Its type alone cannot establish that a particular port refused the request.
    if (
        "connecterror" in exc_type
        or any("connecterror" in t for t in chain_types)
        or "connecterror" in raw_lower
    ):
        return DiagnosticResult(
            error_code="NETWORK_CONNECTION_FAILED",
            message="网络连接失败 (Connection Failed)",
            guidance="无法与模型服务建立连接。请检查网络、代理和服务地址；若使用本机代理，请确认程序启动时已配置相应代理。",
            status_code=status_code or 502,
            raw_error=sanitized_raw,
        )

    if status_code == 502 or "bad gateway" in raw_lower:
        return DiagnosticResult(
            error_code="BAD_GATEWAY",
            message="网关连接异常 (502 Bad Gateway)",
            guidance="上游网关或反向代理暂时无法连接源站服务器（502 Bad Gateway）。服务可能暂时不可用或正在维护，建议稍后重试或检查反向代理服务状态。",
            status_code=502,
            raw_error=sanitized_raw,
        )

    return None


def _diagnose_overload(
    raw_lower: str,
    status_code: int | None,
    sanitized_raw: str,
) -> DiagnosticResult | None:
    is_overload = (
        status_code in (503, 529)
        or any(kw in raw_lower for kw in (
            "overloaded",
            "overload",
            "过载",
            "繁忙",
            "temporarily unavailable",
            "service unavailable",
            "capacity",
        ))
    )
    if not is_overload:
        return None

    code = status_code or 503
    name = "Overloaded" if code == 529 else "Service Unavailable"
    return DiagnosticResult(
        error_code="SERVER_OVERLOADED",
        message=f"服务暂时繁忙 ({code} {name})",
        guidance="模型服务当前过载或暂时繁忙，请稍后重试。通常无需修改配置。",
        status_code=code,
        raw_error=sanitized_raw,
    )


def _diagnose_model_not_found(
    raw_lower: str,
    status_code: int | None,
    sanitized_raw: str,
) -> DiagnosticResult | None:
    if "bot token" in raw_lower:
        return None

    has_model_keyword = "model" in raw_lower or "模型" in raw_lower
    has_not_found_keyword = any(
        kw in raw_lower
        for kw in (
            "not found",
            "not_found",
            "does not exist",
            "unknown model",
            "no such model",
            "不存在",
            "找不到",
            "未找到",
            "无此模型",
        )
    )

    is_model_not_found = (
        "model_not_found" in raw_lower
        or "model not found" in raw_lower
        or "unknown model" in raw_lower
        or "no such model" in raw_lower
        or (has_model_keyword and has_not_found_keyword)
    )

    if not is_model_not_found:
        return None

    return DiagnosticResult(
        error_code="MODEL_NOT_FOUND",
        message=f"指定的模型名称不存在 ({status_code or 404} Model Not Found)",
        guidance="提供商无法识别填写的模型名称。请从官方推荐预设列表下拉选择，或核对自定义模型拼写无误。",
        status_code=status_code or 404,
        raw_error=sanitized_raw,
    )


def _diagnose_endpoint_not_found(
    raw_lower: str,
    status_code: int | None,
    sanitized_raw: str,
) -> DiagnosticResult | None:
    if status_code != 404 and " 404 " not in raw_lower and "status 404" not in raw_lower and "(404)" not in raw_lower:
        return None

    return DiagnosticResult(
        error_code="ENDPOINT_NOT_FOUND",
        message="接口地址未找到 (404 Not Found)",
        guidance="提供商未找到请求的接口路径。请检查 Base URL 地址配置（特别是路径部分，例如是否缺少或多写了 /v1 等后缀）。",
        status_code=404,
        raw_error=sanitized_raw,
    )


def _diagnose_context_length_exceeded(
    raw_lower: str,
    status_code: int | None,
    sanitized_raw: str,
) -> DiagnosticResult | None:
    context_keywords = (
        "context_length_exceeded",
        "maximum context length",
        "context length",
        "context window",
        "prompt is too long",
        "too many tokens",
        "exceeds maximum context",
        "exceeds the maximum token",
        "exceeds maximum token",
        "token count exceeds",
        "max_tokens is too large",
        "token limit exceeded",
    )
    is_context_exceeded = (
        any(kw in raw_lower for kw in context_keywords)
        or ("上下文" in raw_lower and any(kw in raw_lower for kw in ("超出", "超限", "过长", "上限", "限制")))
    )
    if is_context_exceeded:
        return DiagnosticResult(
            error_code="CONTEXT_LENGTH_EXCEEDED",
            message="对话上下文超出模型最大限制 (Context Length Exceeded)",
            guidance="对话上下文超出模型最大长度限制。建议在会话设置中调小「最大上下文轮数」，或清理历史记录、开启新会话。",
            status_code=status_code or 400,
            raw_error=sanitized_raw,
        )
    return None


def _diagnose_insufficient_balance(
    raw_lower: str,
    status_code: int | None,
    sanitized_raw: str,
) -> DiagnosticResult | None:
    is_insufficient_balance = (
        status_code == 402
        or (status_code != 429 and any(kw in raw_lower for kw in (
            "insufficient balance",
            "insufficient_balance",
            "insufficient_quota",
            "余额不足",
            "payment required",
        )))
    )
    if is_insufficient_balance:
        return DiagnosticResult(
            error_code="QUOTA_EXCEEDED",
            message=f"账户可用余额不足 ({status_code or 402} Payment Required)",
            guidance="当前提供商账户可用余额或额度已耗尽（402 Payment Required）。请前往对应模型服务商控制台充值或检查账单。",
            status_code=status_code or 402,
            raw_error=sanitized_raw,
        )
    return None


def _diagnose_gemini(
    raw_lower: str,
    status_code: int | None,
    sanitized_raw: str,
) -> DiagnosticResult | None:
    if (
        status_code == 403
        or "location is not supported" in raw_lower
        or "failed_precondition" in raw_lower
        or "region" in raw_lower
    ):
        return DiagnosticResult(
            error_code="GEMINI_REGION_RESTRICTED",
            message="Google Gemini 地区访问受限 (403 Region Restricted)",
            guidance="当前网络环境所在地区暂不受 Google Gemini 官方支持。请开启支持地区的海外网络代理（如美区、日区、新加坡节点），或配置第三方中转代理 Base URL。",
            status_code=403,
            raw_error=sanitized_raw,
        )

    has_gemini_key_phrase = any(
        kw in raw_lower
        for kw in (
            "api key not valid",
            "please pass a valid api key",
            "api_key_invalid",
            "invalid api key",
        )
    )
    is_gemini_key_error = (
        status_code == 401
        or has_gemini_key_phrase
        or (status_code in (400, None) and any(kw in raw_lower for kw in ("api key", "api_key")))
    )
    if is_gemini_key_error:
        return DiagnosticResult(
            error_code="GEMINI_KEY_INVALID",
            message="Google Gemini API Key 无效或格式错误 (400/401)",
            guidance="Gemini 密钥验证失败。建议前往 Google AI Studio (https://aistudio.google.com/) 重新申领有效的 API Key（通常以 AIzaSy 开头）。",
            status_code=status_code or 400,
            raw_error=sanitized_raw,
        )

    if status_code == 403 or "permission_denied" in raw_lower:
        return DiagnosticResult(
            error_code="GEMINI_PERMISSION_DENIED",
            message="Google Cloud API 权限未开通 (403 Permission Denied)",
            guidance="当前 Google 账户未启用 Generative Language API 权限，请登录 Google Cloud 控制台开通相应服务权限并绑定结算账号。",
            status_code=403,
            raw_error=sanitized_raw,
        )
    return None


def _diagnose_xai(
    raw_lower: str,
    status_code: int | None,
    sanitized_raw: str,
) -> DiagnosticResult | None:
    has_xai_auth_phrase = any(
        kw in raw_lower
        for kw in (
            "incorrect api key",
            "invalid api key",
            "invalid_api_key",
        )
    )
    is_xai_auth_failure = (
        status_code == 401
        or has_xai_auth_phrase
        or (status_code in (400, None) and any(kw in raw_lower for kw in ("api key", "api_key", "unauthorized")))
    )
    if is_xai_auth_failure:
        return DiagnosticResult(
            error_code="XAI_AUTH_FAILED",
            message="xAI (Grok) 身份验证失败 (400/401 Unauthorized)",
            guidance="xAI API Key 鉴权失败，请检查密钥或在 console.x.ai 确认账户状态与额度，并确认已开通 Grok 模型使用权限。",
            status_code=401,
            raw_error=sanitized_raw,
        )

    if status_code == 403 or "forbidden" in raw_lower:
        return DiagnosticResult(
            error_code="XAI_FORBIDDEN",
            message="xAI (Grok) 访问受限 (403 Forbidden)",
            guidance="当前 IP 节点或账户无权调用 xAI API。请检查代理节点是否位于受支持地区，并确认账户已完成实名验证并绑定有效信用卡或积分。",
            status_code=403,
            raw_error=sanitized_raw,
        )

    if status_code == 429:
        return DiagnosticResult(
            error_code="XAI_QUOTA_EXCEEDED",
            message="xAI 积分不足或频次超限 (429 Rate Limit)",
            guidance="xAI 账户积分 (Credits) 已耗尽或请求并发超限。请前往 xAI 开发者控制台 (https://console.x.ai/) 充值积分或等待频次窗口重置后重试。",
            status_code=429,
            raw_error=sanitized_raw,
        )
    return None


def _diagnose_openai(
    raw_lower: str,
    status_code: int | None,
    sanitized_raw: str,
) -> DiagnosticResult | None:
    has_openai_auth_phrase = any(
        kw in raw_lower
        for kw in (
            "invalid_api_key",
            "incorrect api key",
        )
    )
    is_openai_auth_failure = (
        status_code == 401
        or has_openai_auth_phrase
        or (status_code in (400, None) and any(kw in raw_lower for kw in ("api key", "api_key", "unauthorized")))
    )
    if is_openai_auth_failure:
        return DiagnosticResult(
            error_code="OPENAI_AUTH_FAILED",
            message="OpenAI API Key 身份验证失败 (401 Unauthorized)",
            guidance="身份验证失败。请检查输入的 sk- 密钥是否完整无误，或在 OpenAI 控制台 (https://platform.openai.com/api-keys) 重新创建 Secret Key。",
            status_code=401,
            raw_error=sanitized_raw,
        )
    if status_code == 429:
        if "insufficient_quota" in raw_lower or "exceeded your current quota" in raw_lower or "quota" in raw_lower:
            return DiagnosticResult(
                error_code="OPENAI_QUOTA_EXCEEDED",
                message="OpenAI 账户额度已耗尽 (429 Insufficient Quota)",
                guidance="当前 OpenAI 账户可用额度为 0 或已过期。请登录 OpenAI 开发者平台 (https://platform.openai.com/account/billing) 充值或绑定支付方式。",
                status_code=429,
                raw_error=sanitized_raw,
            )
        return DiagnosticResult(
            error_code="OPENAI_RATE_LIMIT",
            message="OpenAI 请求频率超限 (429 Rate Limit)",
            guidance="请求速率超出了当前账户层级的 RPM/TPM 限制。请稍候重试，或在会话设置中减小单次生成的 max_tokens。",
            status_code=429,
            raw_error=sanitized_raw,
        )
    return None


def _diagnose_anthropic(
    raw_lower: str,
    status_code: int | None,
    sanitized_raw: str,
) -> DiagnosticResult | None:
    if status_code == 401 or "authentication_error" in raw_lower:
        return DiagnosticResult(
            error_code="ANTHROPIC_AUTH_FAILED",
            message="Anthropic Claude 身份验证失败 (401 Unauthorized)",
            guidance="Anthropic API Key 错误或未生效。请登录 Anthropic Console (https://console.anthropic.com/) 确认密钥并检查可用额度。",
            status_code=401,
            raw_error=sanitized_raw,
        )
    if status_code == 429 or "rate_limit_error" in raw_lower:
        return DiagnosticResult(
            error_code="ANTHROPIC_RATE_LIMIT",
            message="Anthropic Claude 速率超限或额度不足 (429)",
            guidance="已触发 Anthropic 账户的速率限制或账户余额耗尽，请检查控制台账单与请求限额。",
            status_code=429,
            raw_error=sanitized_raw,
        )
    return None


def _diagnose_provider_specific(
    p_id: str,
    raw_lower: str,
    status_code: int | None,
    sanitized_raw: str,
) -> DiagnosticResult | None:
    if p_id == "gemini" or "generativelanguage.googleapis.com" in raw_lower or "googleapis.com" in raw_lower:
        diag = _diagnose_gemini(raw_lower, status_code, sanitized_raw)
        if diag:
            return diag

    if p_id == "xai" or "api.x.ai" in raw_lower:
        diag = _diagnose_xai(raw_lower, status_code, sanitized_raw)
        if diag:
            return diag

    if p_id == "openai" or "api.openai.com" in raw_lower:
        diag = _diagnose_openai(raw_lower, status_code, sanitized_raw)
        if diag:
            return diag

    if p_id == "anthropic" or "api.anthropic.com" in raw_lower:
        diag = _diagnose_anthropic(raw_lower, status_code, sanitized_raw)
        if diag:
            return diag

    return None


def _diagnose_generic_http(
    raw_lower: str,
    status_code: int | None,
    sanitized_raw: str,
) -> DiagnosticResult | None:
    if status_code == 401 or "unauthorized" in raw_lower or "invalid key" in raw_lower:
        return DiagnosticResult(
            error_code="AUTH_FAILED",
            message="密钥不被接受。请到申请密钥的网站重新复制。",
            guidance="身份验证失败。请检查填写的 API Key 是否完整无误，或在提供商控制台重新生成有效密钥。",
            status_code=401,
            raw_error=sanitized_raw,
        )

    if status_code == 403 or "forbidden" in raw_lower:
        return DiagnosticResult(
            error_code="FORBIDDEN",
            message="访问权限受限 (403 Forbidden)",
            guidance="当前账户无权访问该资源或受到网络地区策略限制。请检查网络代理环境及提供商控制台权限设置。",
            status_code=403,
            raw_error=sanitized_raw,
        )

    if status_code == 429:
        if "quota" in raw_lower or "balance" in raw_lower:
            return DiagnosticResult(
                error_code="QUOTA_EXCEEDED",
                message="账户额度已耗尽 (429 Insufficient Quota)",
                guidance="当前账户可用额度已用尽或免费额度已过期。请登录提供商开发者控制台检查账单与充值余额。",
                status_code=429,
                raw_error=sanitized_raw,
            )
        return DiagnosticResult(
            error_code="RATE_LIMIT",
            message="请求频率超限 (429 Rate Limit)",
            guidance="请求速率超出了当前账户的频率限制。请稍候片刻重试，或调小每次对话的 max_tokens。",
            status_code=429,
            raw_error=sanitized_raw,
        )

    return None


def _diagnose_final_fallback(
    status_code: int | None,
    sanitized_raw: str,
) -> DiagnosticResult:
    clean_err = sanitized_raw or f"HTTP {status_code or 'Error'}"
    return DiagnosticResult(
        error_code="PROVIDER_ERROR",
        message=f"提供商调用异常: {clean_err[:120]}",
        guidance="请检查 Base URL 地址、API Key 凭据以及模型名称配置是否正确。",
        status_code=status_code,
        raw_error=sanitized_raw,
    )


def diagnose_llm_error(
    exc_or_msg: Exception | str | None = None,
    provider_id: str | None = None,
    status_code: int | None = None,
    raw_body: str | None = None,
    api_key_configured: bool | None = None,
) -> DiagnosticResult:
    """
    Inspects exceptions, status codes, and provider context to produce
    a comprehensive DiagnosticResult with actionable Chinese guidance.
    `api_key_configured=False` turns authentication failures into a missing-key hint.
    """
    p_id = (provider_id or "").strip().lower()

    exc_chain = _unwrap_exception_chain(exc_or_msg) if isinstance(exc_or_msg, BaseException) else []
    exc_type = type(exc_or_msg).__name__.lower() if isinstance(exc_or_msg, BaseException) else ""

    chain_parts: list[str] = []
    for e in exc_chain:
        t_name = type(e).__name__
        s_val = str(e).strip()
        if s_val:
            chain_parts.append(f"{t_name}: {s_val}")
        else:
            chain_parts.append(t_name)
        try:
            req = getattr(e, "request", None)
            if req is not None and getattr(req, "url", None) is not None:
                chain_parts.append(str(req.url))
        except Exception:
            pass

    if not exc_chain and exc_or_msg:
        chain_parts.append(str(exc_or_msg))

    chain_str = " | ".join(chain_parts) if chain_parts else ""
    raw_combined = f"{chain_str} {raw_body or ''}".strip()
    raw_lower = raw_combined.lower()
    sanitized_raw = MaskingFilter.sanitize(_clean_html_text(raw_combined))[:300] if raw_combined else ""

    if status_code is None and exc_chain:
        for e in exc_chain:
            code = getattr(e, "status_code", None)
            if code is None:
                resp = getattr(e, "response", None)
                if resp is not None:
                    code = getattr(resp, "status_code", None)
            if isinstance(code, int) and 100 <= code <= 599:
                status_code = code
                break

    status_code = _extract_status_code(status_code, raw_lower)

    return (
        _diagnose_local_configuration(raw_lower, sanitized_raw, p_id, api_key_configured)
        or _diagnose_unsupported_parameter(raw_lower, status_code, sanitized_raw)
        or _diagnose_transport_fault(exc_type, raw_lower, status_code, sanitized_raw, exc_chain=exc_chain)
        or _diagnose_overload(raw_lower, status_code, sanitized_raw)
        or _diagnose_model_not_found(raw_lower, status_code, sanitized_raw)
        or _diagnose_endpoint_not_found(raw_lower, status_code, sanitized_raw)
        or _diagnose_context_length_exceeded(raw_lower, status_code, sanitized_raw)
        or _diagnose_insufficient_balance(raw_lower, status_code, sanitized_raw)
        or _diagnose_provider_specific(p_id, raw_lower, status_code, sanitized_raw)
        or _diagnose_generic_http(raw_lower, status_code, sanitized_raw)
        or _diagnose_final_fallback(status_code, sanitized_raw)
    )
