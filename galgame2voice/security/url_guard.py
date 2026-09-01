"""
SSRF guard for user-supplied LLM provider base URLs.

Only applies to LLM provider endpoints — NOT to the local GPT-SoVITS
inference server, whose default address (http://127.0.0.1:9880) is a
legitimate private endpoint.

Policy:
- Scheme must be http/https; known official provider hosts must use https.
- When `allow_private` is disabled (default), the resolved IPs may not be
  loopback/private/link-local/reserved/multicast (blocks 127.0.0.0/8,
  10/8, 172.16/12, 192.168/16, 169.254/16 including cloud metadata, ::1,
  fc00::/7, etc.).
- When `allow_private` is enabled the address is trusted as-is (operator
  explicitly runs a local Ollama/vLLM or a LAN relay).
"""

import ipaddress
import socket
from typing import Optional, Tuple
from urllib.parse import urlparse

# Hosts operated by the seeded official provider presets. These are forced
# to https so a typo'd http URL can't silently downgrade transport security.
OFFICIAL_LLM_HOSTS = {
    "api.openai.com",
    "api.deepseek.com",
    "api.anthropic.com",
    "api.x.ai",
    "open.bigmodel.cn",
    "dashscope.aliyuncs.com",
    "generativelanguage.googleapis.com",
}

_PRIVATE_REASONS = (
    "目标地址解析到环回/私网/链路本地/保留网段（含云元数据地址 169.254.169.254）。"
    "如确需连接本地或局域网模型服务，请在设置中开启「允许私网 LLM 端点」。"
)


def _is_blocked_ip(addr: ipaddress._BaseAddress) -> bool:
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _resolve_host(host: str, port: int) -> Tuple[bool, str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return False, f"域名解析失败: {exc}"
    except OSError as exc:
        return False, f"域名解析异常: {exc}"
    if not infos:
        return False, "域名解析结果为空"
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if _is_blocked_ip(addr):
            return False, _PRIVATE_REASONS
    return True, ""


def validate_llm_base_url(url: Optional[str], allow_private: bool = False) -> Tuple[bool, str]:
    """
    Validates an LLM provider base URL. Returns (ok, reason).
    Blocking network calls (DNS) must run via asyncio.to_thread by the caller.
    """
    if not url or not str(url).strip():
        return False, "接口地址不能为空"
    candidate = str(url).strip()

    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        return False, "接口地址仅支持 http/https 协议"
    if parsed.username or parsed.password:
        return False, "接口地址不应包含用户名/密码"
    host = parsed.hostname
    if not host:
        return False, "接口地址缺少主机名"

    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    if host.lower() in OFFICIAL_LLM_HOSTS and parsed.scheme != "https":
        return False, "官方提供商接口必须使用 https"

    if allow_private:
        return True, ""

    # IP literal short-circuit avoids DNS for the common SSRF payloads
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return _resolve_host(host, port)
    if _is_blocked_ip(addr):
        return False, _PRIVATE_REASONS
    return True, ""


__all__ = ["validate_llm_base_url", "OFFICIAL_LLM_HOSTS"]
