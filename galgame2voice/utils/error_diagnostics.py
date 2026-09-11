"""
Structured Error Diagnostics & User Guidance for galgame2voice.
Intercepts provider HTTP errors, authentication failures, and network transport faults,
translating them into user-friendly Chinese diagnostic recommendations.
"""

from dataclasses import dataclass
from typing import Optional, Union, Dict, Any
from galgame2voice.utils.logger import MaskingFilter


@dataclass
class DiagnosticResult:
    error_code: str
    message: str
    guidance: str
    status_code: Optional[int] = None
    raw_error: Optional[str] = None

    def format_user_message(self) -> str:
        if self.guidance:
            return f"{self.message}：{self.guidance}"
        return self.message

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": self.message,
            "diagnostic": self.guidance,
            "error_code": self.error_code,
            "status_code": self.status_code,
        }


def format_provider_error(
    provider_id: Optional[str] = None,
    status_code: Optional[int] = None,
    raw_error: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Translates HTTP status code and raw error from a provider into structured
    Chinese guidance dictionary {"error": ..., "diagnostic": ..., "error_code": ..., "status_code": ...}.
    """
    diag = diagnose_llm_error(
        exc_or_msg=raw_error,
        provider_id=provider_id,
        status_code=status_code,
        raw_body=raw_error,
    )
    return diag.to_dict()


def diagnose_llm_error(
    exc_or_msg: Optional[Union[Exception, str]] = None,
    provider_id: Optional[str] = None,
    status_code: Optional[int] = None,
    raw_body: Optional[str] = None,
) -> DiagnosticResult:
    """
    Inspects exceptions, status codes, and provider context to produce
    a comprehensive DiagnosticResult with actionable Chinese guidance.
    """
    p_id = (provider_id or "").strip().lower()

    # Extract exception type name if provided
    exc_type = ""
    if isinstance(exc_or_msg, Exception):
        exc_type = type(exc_or_msg).__name__.lower()
        msg_str = str(exc_or_msg)
    else:
        msg_str = str(exc_or_msg or "")

    raw_combined = f"{msg_str} {raw_body or ''}".strip()
    raw_lower = raw_combined.lower()
    sanitized_raw = MaskingFilter.sanitize(raw_combined)[:300] if raw_combined else ""

    if status_code is None:
        for code in (401, 403, 429, 404, 400, 408, 502, 503, 504):
            if f"({code})" in raw_lower or f" {code} " in raw_lower or f"http {code}" in raw_lower or f"status {code}" in raw_lower:
                status_code = code
                break

    # 1. Transport & Network Faults
    if (
        "timeout" in exc_type
        or "timeouterror" in raw_lower
        or "timed out" in raw_lower
        or status_code in (408, 504)
    ):
        return DiagnosticResult(
            error_code="NETWORK_TIMEOUT",
            message="网络连接超时 (Request Timeout)",
            guidance="连接提供商服务器超时。海外模型服务（如 Google / OpenAI / xAI）在国内网络直连通常受阻，请确保本地代理客户端（如 Clash / v2rayN）正在运行，或配置支持该服务的中转反向代理 Base URL。",
            status_code=status_code or 408,
            raw_error=sanitized_raw,
        )

    if (
        "connecterror" in exc_type
        or "10061" in raw_lower
        or "connection refused" in raw_lower
        or "refused" in raw_lower
        or (status_code in (502, 503) and ("connect" in raw_lower or not raw_lower))
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
        or "nodename nor servname provided" in raw_lower
    ):
        return DiagnosticResult(
            error_code="DNS_ERROR",
            message="服务器域名解析失败 (DNS Error)",
            guidance="无法解析 Base URL 的服务器域名，请检查网址拼写、主机名或本地网络 DNS 设置。",
            status_code=status_code or 502,
            raw_error=sanitized_raw,
        )

    # 2. Model Not Found (Universal check: 404 or specific error phrases in 400/404)
    if status_code == 404 or any(
        kw in raw_lower
        for kw in (
            "model not found",
            "model_not_found",
            "does not exist",
            "the model",
            "unknown model",
            "no such model",
        )
    ):
        # Unless it's an explicit 404 from Telegram or auth endpoint
        if "bot token" not in raw_lower:
            return DiagnosticResult(
                error_code="MODEL_NOT_FOUND",
                message="指定的模型名称不存在 (404 Model Not Found)",
                guidance="提供商无法识别填写的模型名称。请从官方推荐预设列表下拉选择（如 xAI 选 grok-3 / grok-3-mini，Groq 选 llama-3.3-70b-versatile），或核对自定义模型拼写无误。",
                status_code=status_code or 404,
                raw_error=sanitized_raw,
            )

    # 3. Google Gemini Specifics
    if p_id == "gemini" or "generativelanguage.googleapis.com" in raw_lower or "googleapis.com" in raw_lower:
        # Region Restriction (403 with user location unsupported / FAILED_PRECONDITION)
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

        # Invalid API Key (Gemini returns 400 or 401 with API_KEY_INVALID or INVALID_ARGUMENT)
        if (
            status_code in (400, 401)
            or "api key not valid" in raw_lower
            or "please pass a valid api key" in raw_lower
            or "invalid_argument" in raw_lower
            or "api_key_invalid" in raw_lower
        ):
            return DiagnosticResult(
                error_code="GEMINI_KEY_INVALID",
                message="Google Gemini API Key 无效或格式错误 (400/401)",
                guidance="Gemini 密钥验证失败。建议前往 Google AI Studio (https://aistudio.google.com/) 重新申领有效的 API Key（通常以 AIzaSy 开头）。",
                status_code=status_code or 400,
                raw_error=sanitized_raw,
            )

        # Cloud API permission denied
        if status_code == 403 or "permission_denied" in raw_lower:
            return DiagnosticResult(
                error_code="GEMINI_PERMISSION_DENIED",
                message="Google Cloud API 权限未开通 (403 Permission Denied)",
                guidance="当前 Google 账户未启用 Generative Language API 权限，请登录 Google Cloud 控制台开通相应服务权限并绑定结算账号。",
                status_code=403,
                raw_error=sanitized_raw,
            )

    # 4. xAI (Grok) Specifics
    if p_id == "xai" or "api.x.ai" in raw_lower:
        # Auth failure (xAI returns HTTP 400 or 401 with "Incorrect API key provided" / "Unauthorized")
        if (
            status_code in (400, 401)
            or "incorrect api key" in raw_lower
            or "unauthorized" in raw_lower
            or "invalid-argument" in raw_lower
            or "api key" in raw_lower
        ):
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

    # 5. Groq Specifics
    if p_id == "groq" or "api.groq.com" in raw_lower:
        if status_code == 401 or "invalid api key" in raw_lower or "unauthorized" in raw_lower or "api key" in raw_lower:
            return DiagnosticResult(
                error_code="GROQ_AUTH_FAILED",
                message="Groq API Key 无效 (401 Unauthorized)",
                guidance="Groq 身份验证失败。请登录 Groq Console (https://console.groq.com/keys) 复制以 gsk_ 开头的有效 Key。",
                status_code=401,
                raw_error=sanitized_raw,
            )
        if status_code == 429:
            return DiagnosticResult(
                error_code="GROQ_RATE_LIMIT",
                message="Groq 速率超限 (429 Rate Limit)",
                guidance="Groq 免费层具有每分钟请求限制 (RPM/TPM)。请稍等 10-30 秒后重试，或在 Groq 控制台绑定支付方式升级配额。",
                status_code=429,
                raw_error=sanitized_raw,
            )

    # 6. OpenAI Specifics
    if p_id == "openai" or "api.openai.com" in raw_lower:
        if (
            status_code == 401
            or "invalid_api_key" in raw_lower
            or "incorrect api key" in raw_lower
            or "unauthorized" in raw_lower
            or "api key" in raw_lower
        ):
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

    # 7. Anthropic Claude Specifics
    if p_id == "anthropic" or "api.anthropic.com" in raw_lower:
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

    # 8. Generic HTTP Status Fallbacks
    if status_code == 401 or "unauthorized" in raw_lower or "invalid key" in raw_lower:
        return DiagnosticResult(
            error_code="AUTH_FAILED",
            message="API Key 身份验证失败 (401 Unauthorized)",
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

    # 9. Final Fallback
    clean_err = sanitized_raw or f"HTTP {status_code or 'Error'}"
    return DiagnosticResult(
        error_code="PROVIDER_ERROR",
        message=f"提供商调用异常: {clean_err[:120]}",
        guidance="请检查 Base URL 地址、API Key 凭据以及模型名称配置是否正确。",
        status_code=status_code,
        raw_error=sanitized_raw,
    )
