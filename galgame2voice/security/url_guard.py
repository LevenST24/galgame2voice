"""
SSRF guard for user-supplied LLM provider base URLs.

Only applies to LLM provider endpoints — NOT to the local GPT-SoVITS
inference server, whose default address (http://127.0.0.1:9880) is a
legitimate private endpoint.

Policy:
- Scheme must be http/https; known official provider hosts must use https.
- When `allow_private` is disabled (default), the resolved IPs may not be
  loopback/private/link-local/reserved/multicast or otherwise non-global
  (blocks 127.0.0.0/8, 10/8, 172.16/12, 192.168/16, 169.254/16 including
  cloud metadata, 100.64/10 CGNAT such as Tailscale, ::1, fc00::/7, the
  NAT64 prefix 64:ff9b::/96 inside the reserved ::/8, etc.).
- When `allow_private` is enabled the address is trusted as-is (operator
  explicitly runs a local Ollama/vLLM or a LAN relay).
"""

import collections
import ipaddress
import socket
import threading
import time
from typing import Dict, Optional, Tuple
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
    if isinstance(addr, ipaddress.IPv6Address):
        for embedded in (addr.ipv4_mapped, addr.sixtofour):
            if embedded is not None and _is_blocked_ip(embedded):
                return True
        if addr.teredo is not None:
            for teredo_ip in addr.teredo:
                if _is_blocked_ip(teredo_ip):
                    return True
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or not addr.is_global
    )


_DNS_CACHE_LOCK = threading.Lock()
_DNS_CACHE: Dict[Tuple[str, int], Tuple[float, bool, str]] = collections.OrderedDict()
_DNS_CACHE_TTL_SECONDS = 60.0
_DNS_CACHE_MAXSIZE = 256


def clear_dns_cache() -> None:
    """Flushes the internal DNS resolution cache (useful for testing and network reconfiguration)."""
    with _DNS_CACHE_LOCK:
        _DNS_CACHE.clear()


def _resolve_host(host: str, port: int) -> Tuple[bool, str]:
    norm_host = host.lower().strip()
    key = (norm_host, port)
    now = time.monotonic()

    with _DNS_CACHE_LOCK:
        cached = _DNS_CACHE.get(key)
        if cached is not None:
            expire_at, ok, reason = cached
            if now < expire_at:
                _DNS_CACHE.move_to_end(key)
                return ok, reason
            else:
                _DNS_CACHE.pop(key, None)

    try:
        infos = socket.getaddrinfo(norm_host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError):
        # A host that doesn't resolve cannot be connected to at all, so it
        # poses no SSRF risk; let normal connection-time error handling deal
        # with it instead of blocking configuration.
        # Do not cache transient resolution failures.
        return True, ""
    if not infos:
        return True, ""

    for info in infos:
        raw_ip = str(info[4][0]).split("%")[0]
        try:
            addr = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if _is_blocked_ip(addr):
            res = (False, _PRIVATE_REASONS)
            with _DNS_CACHE_LOCK:
                if len(_DNS_CACHE) >= _DNS_CACHE_MAXSIZE:
                    _DNS_CACHE.popitem(last=False)
                _DNS_CACHE[key] = (now + _DNS_CACHE_TTL_SECONDS, res[0], res[1])
            return res

    res = (True, "")
    with _DNS_CACHE_LOCK:
        if len(_DNS_CACHE) >= _DNS_CACHE_MAXSIZE:
            _DNS_CACHE.popitem(last=False)
        _DNS_CACHE[key] = (now + _DNS_CACHE_TTL_SECONDS, res[0], res[1])
    return res


_resolve_host.cache_clear = clear_dns_cache  # type: ignore[attr-defined]


def validate_llm_base_url(url: Optional[str], allow_private: bool = False) -> Tuple[bool, str]:
    """
    Validates an LLM provider base URL. Returns (ok, reason).
    Blocking network calls (DNS) must run via asyncio.to_thread by the caller.
    """
    if not url or not str(url).strip():
        return False, "接口地址不能为空"
    candidate = str(url).strip()

    try:
        parsed = urlparse(candidate)
        scheme = parsed.scheme
        username = parsed.username
        password = parsed.password
        host = parsed.hostname
        raw_port = parsed.port
    except ValueError:
        return False, "接口地址格式或端口无效"

    if scheme not in ("http", "https"):
        return False, "接口地址仅支持 http/https 协议"
    if username or password:
        return False, "接口地址不应包含用户名/密码"
    if not host:
        return False, "接口地址缺少主机名"

    port = raw_port or (443 if scheme == "https" else 80)

    if host.lower() in OFFICIAL_LLM_HOSTS and scheme != "https":
        return False, "官方提供商接口必须使用 https"

    if allow_private:
        return True, ""

    # IP literal short-circuit avoids DNS for the common SSRF payloads
    clean_host = host.split("%")[0]
    try:
        addr = ipaddress.ip_address(clean_host)
    except ValueError:
        if "%" in host:
            return False, "接口地址主机名格式无效"
        return _resolve_host(host, port)
    if _is_blocked_ip(addr):
        return False, _PRIVATE_REASONS
    return True, ""


def validate_local_service_url(url: Optional[str]) -> Tuple[bool, str]:
    """Validates a local/LAN service URL (e.g. the GPT-SoVITS inference server).

    Allows private/loopback hosts (the service legitimately lives on the LAN),
    but rejects non-http(s) schemes (file://, gopher://, ...), embedded
    credentials, and URLs without a hostname. No DNS resolution is performed.
    Returns (ok, reason).
    """
    return validate_llm_base_url(url, allow_private=True)


def _invalidate_host_cache(url: Optional[str]) -> None:
    """Evicts cached DNS entry for the URL's (host, port) so pre-request checks re-resolve DNS."""
    if not url:
        return
    try:
        parsed = urlparse(str(url).strip())
        host = parsed.hostname
        if not host:
            return
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return
    key = (host.lower().strip(), port)
    with _DNS_CACHE_LOCK:
        _DNS_CACHE.pop(key, None)


async def assert_llm_url_safe(url: str, allow_private: bool = False) -> None:
    """
    Re-resolve and validate the target before an outbound LLM or STT request.

    This detects a change visible at validation time; it does not pin the
    address used by the transport or a proxy, so DNS rebinding remains possible.
    """
    import asyncio
    _invalidate_host_cache(url)
    ok, reason = await asyncio.to_thread(validate_llm_base_url, url, allow_private)
    if not ok:
        raise PermissionError(f"SSRF/DNS-Rebinding 阻止: {reason}")


__all__ = [
    "validate_llm_base_url",
    "validate_local_service_url",
    "assert_llm_url_safe",
    "OFFICIAL_LLM_HOSTS",
    "clear_dns_cache",
]
