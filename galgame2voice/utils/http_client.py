"""
Shared httpx client construction for galgame2voice.

Why this module exists
----------------------
``httpx.AsyncClient()`` reads proxy configuration from the environment unless
``trust_env=False`` is passed, and that environment parsing is not defensive: a
single entry that cannot be turned into a URL pattern makes *every* client
construction raise before a single request is sent.

The concrete trigger observed in the field is ``NO_PROXY`` containing the
bracketed IPv6 loopback ``[::1]`` — a very common thing to write in a shell
profile or in a proxy tool's bypass list. httpx expands that entry into the
mount pattern ``all://*[::1]`` and then fails with::

    httpx.InvalidURL: Invalid port: ':1]'

Left alone, a cosmetic environment quirk takes down every LLM, STT,
and provider-test HTTP call at once. So client construction degrades gracefully
here: when the ambient proxy configuration cannot be parsed, the caller's
explicit arguments are kept and the client is retried with environment proxies
disabled, logging why so the misconfiguration stays visible.

Explicitly passed ``proxy=...`` arguments are unaffected, because httpx skips
environment parsing entirely in that case.

Clients built here also share one SSL context, so the CA bundle is loaded once
per process instead of once per client and proxy transport (see
``_shared_ssl_context``).
"""

import functools
import logging
import ssl
from typing import Any

import httpx

logger = logging.getLogger("galgame2voice.utils.http_client")

__all__ = ["create_async_client", "proxy_environment_is_usable", "PROXY_ENV_HINT"]

PROXY_ENV_HINT = (
    "Check NO_PROXY/HTTP_PROXY/HTTPS_PROXY for malformed entries such as the "
    "bracketed IPv6 literal '[::1]'."
)


def _build_with_fallback(factory: Any, kwargs: dict) -> Any:
    """Builds a client, retrying with environment proxies off if that is the blocker."""
    try:
        return factory(**kwargs)
    except httpx.InvalidURL as exc:
        if kwargs.get("trust_env") is False:
            # The caller already asked to ignore the environment, so there is
            # nothing to fall back to: surface the real failure.
            raise
        logger.warning(
            "Unusable HTTP proxy configuration in the environment (%s); "
            "continuing with proxy environment variables ignored. %s",
            exc,
            PROXY_ENV_HINT,
        )
        retry_kwargs = dict(kwargs)
        retry_kwargs["trust_env"] = False
        return factory(**retry_kwargs)


@functools.cache
def _shared_ssl_context(trust_env: bool) -> ssl.SSLContext:
    """
    The CA-loaded SSL context that every client built here shares.

    httpx loads the CA bundle again for every client, once per transport: the
    default one plus one for each proxy in the environment (HTTP_PROXY,
    HTTPS_PROXY, ALL_PROXY). With OpenSSL 3.0, which python.org's Windows builds
    and so the portable package ship, one load measured about 0.2 s of work that
    blocks the event loop, and a client is built for every chat request.
    httpcore sets ALPN on the context per connection; that is the same value
    for every client here, because none of them enables HTTP/2.
    """
    return httpx.create_ssl_context(trust_env=trust_env)


def _async_client_with_shared_ssl(**kwargs: Any) -> httpx.AsyncClient:
    kwargs.setdefault("verify", _shared_ssl_context(kwargs.get("trust_env", True)))
    return httpx.AsyncClient(**kwargs)


def create_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """
    Builds an ``httpx.AsyncClient`` that survives a broken proxy environment.

    Equivalent to ``httpx.AsyncClient(**kwargs)``, except that an unusable
    ambient proxy configuration (see module docstring) falls back to
    ``trust_env=False`` instead of raising ``httpx.InvalidURL``, and that the
    default certificate verification reuses one shared SSL context.
    """
    return _build_with_fallback(_async_client_with_shared_ssl, kwargs)


def proxy_environment_is_usable() -> bool:
    """
    Returns True when a client can be built from the ambient proxy environment.

    Used by diagnostics/doctor output; it never raises.
    """
    try:
        probe = httpx.Client()
    except Exception:
        return False
    try:
        probe.close()
    except Exception:
        pass
    return True
