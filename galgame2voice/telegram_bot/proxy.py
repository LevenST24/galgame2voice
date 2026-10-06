"""
Telegram Proxy Configuration Helper.
Supports HTTP, HTTPS, and SOCKS5 proxies with fallback and connection probing.
"""

import logging
from typing import Any
import httpx

from galgame2voice.database.models import SettingsInDB
from galgame2voice.utils.http_client import proxy_environment_is_usable

logger = logging.getLogger("galgame2voice.telegram_bot.proxy")


def get_proxy_url(settings: SettingsInDB | None = None, proxy_str: str | None = None) -> str | None:
    """
    Constructs normalized proxy URL from settings or explicit proxy string.
    Returns e.g. 'http://127.0.0.1:10808' or 'socks5://127.0.0.1:10808', or None if disabled.
    """
    if proxy_str:
        p = proxy_str.strip()
        if p:
            if not p.startswith(("http://", "https://", "socks5://", "socks4://")):
                p = f"http://{p}"
            return p

    if settings and getattr(settings, "telegram_proxy_enabled", 0):
        host = str(getattr(settings, "telegram_proxy_host", "127.0.0.1") or "127.0.0.1").strip()
        port = str(getattr(settings, "telegram_proxy_port", 10808) or 10808).strip()
        if host.startswith(("http://", "https://", "socks5://", "socks4://")):
            return f"{host}:{port}" if ":" not in host.split("//")[-1] else host
        return f"http://{host}:{port}"

    return None


def get_telegram_request_kwargs(
    proxy_url: str | None = None,
    read_timeout: float = 30.0,
    connect_timeout: float = 15.0,
) -> dict[str, Any]:
    """
    Builds keyword arguments for python-telegram-bot HTTPXRequest.
    """
    kwargs: dict[str, Any] = {
        "read_timeout": read_timeout,
        "connect_timeout": connect_timeout,
    }
    if proxy_url:
        kwargs["proxy"] = proxy_url
    elif not proxy_environment_is_usable():
        # HTTPXRequest builds its own httpx.AsyncClient and leaves trust_env at
        # its default, so an unparseable proxy environment entry (e.g. NO_PROXY
        # containing "[::1]") would make the whole bot fail to construct.
        # An explicit proxy URL skips environment parsing entirely, so this only
        # applies to the no-proxy-configured case.
        kwargs["httpx_kwargs"] = {"trust_env": False}
        logger.warning(
            "Proxy environment is unusable; Telegram HTTP client will ignore "
            "HTTP_PROXY/HTTPS_PROXY/NO_PROXY. Check those variables for "
            "malformed entries such as the bracketed IPv6 literal '[::1]'."
        )
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
