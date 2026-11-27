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

import asyncio
import functools
import ipaddress
import logging
import ssl
import urllib.request
from urllib.parse import urlsplit
import weakref
from typing import Any

import httpx

logger = logging.getLogger("galgame2voice.utils.http_client")

SHARED_CLIENT_KEEPALIVE_EXPIRY: float = 300.0

__all__ = [
    "create_async_client",
    "get_shared_async_client",
    "close_shared_async_clients",
    "is_local_or_lan_url",
    "proxy_environment_is_usable",
    "PROXY_ENV_HINT",
    "SHARED_CLIENT_KEEPALIVE_EXPIRY",
]

_shared_clients_by_loop: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[Any, tuple[tuple[tuple[str, str], ...], httpx.AsyncClient]],
] = weakref.WeakKeyDictionary()
_retired_clients_by_loop: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    list[httpx.AsyncClient],
] = weakref.WeakKeyDictionary()
_no_loop_shared_clients: dict[Any, tuple[tuple[tuple[str, str], ...], httpx.AsyncClient]] = {}
_no_loop_retired_clients: list[httpx.AsyncClient] = []
_retire_tasks_by_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, set[asyncio.Task[Any]]] = weakref.WeakKeyDictionary()
_RETIRED_CLIENT_POLL_SECONDS: float = 0.25

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

_PROXY_BYPASS_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "::1/128", "fe80::/10", "fd00::/8")
)


def is_local_or_lan_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr.version == net.version and addr in net for net in _PROXY_BYPASS_NETWORKS)


def _current_proxy_key() -> tuple[tuple[str, str], ...]:
    """Snapshots the active ambient/system proxy settings so proxy toggles get a fresh pool."""
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return ()
    return tuple(sorted((str(k).lower(), str(v)) for k, v in proxies.items()))


def _loop_client_stores() -> tuple[
    dict[Any, tuple[tuple[tuple[str, str], ...], httpx.AsyncClient]],
    list[httpx.AsyncClient],
]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _no_loop_shared_clients, _no_loop_retired_clients
    bucket = _shared_clients_by_loop.get(loop)
    if bucket is None:
        bucket = {}
        _shared_clients_by_loop[loop] = bucket
    retired = _retired_clients_by_loop.get(loop)
    if retired is None:
        retired = []
        _retired_clients_by_loop[loop] = retired
    return bucket, retired


def _client_is_busy(client: httpx.AsyncClient) -> bool:
    for transport in (getattr(client, "_transport", None), *getattr(client, "_mounts", {}).values()):
        pool = getattr(transport, "_pool", None)
        if pool is not None and (getattr(pool, "_requests", None) or any(not c.is_idle() and not c.is_closed() for c in getattr(pool, "connections", ()))):
            return True
    return False

def _schedule_retired_close(retired: list[httpx.AsyncClient], client: httpx.AsyncClient) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    async def _drain_and_close() -> None:
        try:
            await asyncio.sleep(0)
            while not getattr(client, "is_closed", False) and _client_is_busy(client):  # noqa: ASYNC110
                await asyncio.sleep(_RETIRED_CLIENT_POLL_SECONDS)
            if not getattr(client, "is_closed", False):
                await client.aclose()
        except Exception as exc:
            logger.debug("Failed closing retired shared HTTP client: %s", exc)
        finally:
            if client in retired:
                retired.remove(client)
    tasks = _retire_tasks_by_loop.setdefault(loop, set())
    task = loop.create_task(_drain_and_close())
    tasks.add(task)
    task.add_done_callback(tasks.discard)

def get_shared_async_client(*, factory: Any = None, trust_env: bool = True) -> httpx.AsyncClient:
    """
    Returns a long-lived ``httpx.AsyncClient`` keyed by running event loop and
    current proxy settings (``urllib.request.getproxies()``).

    Connections stay alive for ``SHARED_CLIENT_KEEPALIVE_EXPIRY`` (300 s) so
    consecutive chat turns reuse TLS/TCP connections. No client-level timeout is
    configured; callers must pass ``timeout=...`` on each request.
    """
    builder = create_async_client if factory is None else factory
    cache_key = (builder, trust_env)
    proxy_key = _current_proxy_key() if trust_env else ()
    bucket, retired = _loop_client_stores()
    entry = bucket.get(cache_key)
    if entry is not None:
        cached_proxy_key, cached_client = entry
        if cached_proxy_key == proxy_key and not getattr(cached_client, "is_closed", False):
            return cached_client
        if not getattr(cached_client, "is_closed", False):
            retired.append(cached_client)
            _schedule_retired_close(retired, cached_client)

    limits = httpx.Limits(
        max_keepalive_connections=20,
        max_connections=50,
        keepalive_expiry=SHARED_CLIENT_KEEPALIVE_EXPIRY,
    )
    client = builder(limits=limits) if trust_env else builder(limits=limits, trust_env=False)
    bucket[cache_key] = (proxy_key, client)
    return client


async def close_shared_async_clients() -> None:
    """Closes all cached and retired shared async clients for the current loop."""
    bucket, retired = _loop_client_stores()
    to_close: list[httpx.AsyncClient] = [client for _, client in bucket.values()]
    to_close.extend(retired)
    bucket.clear()
    retired.clear()
    pending = list(_retire_tasks_by_loop.pop(asyncio.get_running_loop(), ()))
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    if bucket is not _no_loop_shared_clients:
        to_close.extend(client for _, client in _no_loop_shared_clients.values())
        to_close.extend(_no_loop_retired_clients)
        _no_loop_shared_clients.clear()
        _no_loop_retired_clients.clear()

    seen: set[int] = set()
    for client in to_close:
        if id(client) in seen or getattr(client, "is_closed", False):
            continue
        seen.add(id(client))
        try:
            await client.aclose()
        except Exception as exc:
            logger.debug("Failed closing shared HTTP client: %s", exc)



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
