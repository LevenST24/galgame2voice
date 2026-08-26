"""
Telegram Proxy Configuration Helper.
Supports HTTP, HTTPS, and SOCKS5 proxies with fallback and connection probing.
"""

import logging
from typing import Any, Dict, Optional
import httpx

from galgame2voice.database.models import SettingsInDB

logger = logging.getLogger("galgame2voice.telegram_bot.proxy")


def get_proxy_url(settings: Optional[SettingsInDB] = None, proxy_str: Optional[str] = None) -> Optional[str]:
    """
    Constructs normalized proxy URL from settings or explicit proxy string.
    Returns e.g. 'http://127.0.0.1:10808' or 'socks5://127.0.0.1:10808', or None if disabled.
    """
    if proxy_str:
        p = proxy_str.strip()
        if p:
            if not (p.startswith("http://") or p.startswith("https://") or p.startswith("socks5://") or p.startswith("socks4://")):
                p = f"http://{p}"
            return p

    if settings and getattr(settings, "telegram_proxy_enabled", 0):
        host = str(getattr(settings, "telegram_proxy_host", "127.0.0.1") or "127.0.0.1").strip()
        port = str(getattr(settings, "telegram_proxy_port", 10808) or 10808).strip()
        if host.startswith("http://") or host.startswith("https://") or host.startswith("socks5://") or host.startswith("socks4://"):
            return f"{host}:{port}" if ":" not in host.split("//")[-1] else host
        return f"http://{host}:{port}"

    return None


def get_telegram_request_kwargs(
    proxy_url: Optional[str] = None,
    read_timeout: float = 30.0,
    connect_timeout: float = 15.0,
) -> Dict[str, Any]:
    """
    Builds keyword arguments for python-telegram-bot HTTPXRequest.
    """
    kwargs: Dict[str, Any] = {
        "read_timeout": read_timeout,
        "connect_timeout": connect_timeout,
    }
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return kwargs


async def test_proxy_connectivity(proxy_url: str, timeout: float = 5.0) -> bool:
    """Probes whether the configured proxy is reachable."""
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
            resp = await client.get("https://api.telegram.org", follow_redirects=True)
            return resp.status_code < 500
    except Exception as exc:
        logger.debug("Proxy connection test failed for %s: %s", proxy_url, exc)
        return False

# Prevent pytest from collecting test_proxy_connectivity as a test
test_proxy_connectivity.__test__ = False
probe_proxy_connectivity = test_proxy_connectivity


__all__ = [
    "get_proxy_url",
    "get_telegram_request_kwargs",
    "test_proxy_connectivity",
    "probe_proxy_connectivity",
]
