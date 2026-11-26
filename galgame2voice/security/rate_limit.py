"""
In-memory sliding-window rate limiter as ASGI middleware.

Single-user local deployment: a global bucket is the main defense against a
runaway frontend loop or scripted abuse; a per-client-IP bucket adds a second
layer for exposed deployments. Disabled for the test suite via
GALGAME2VOICE_RATE_LIMIT_DISABLED=1.
"""

import os
import time
from collections import deque
from typing import Deque, Dict

from galgame2voice.config import get_settings

# Long-lived SSE requests only count once at request start, so generous
# window sizes keep interactive use unaffected.
_DEFAULT_GLOBAL_LIMIT = 240      # requests per window, all clients combined
_DEFAULT_IP_LIMIT = 120          # requests per window, per client IP
_WINDOW_SECONDS = 60.0
_PROTECTED_PREFIXES = (
    "/api/chat",
)
# High-risk admin/config routes. Their expensive/dangerous operations are the
# MUTATING ones (settings writes, provider activation, network tests), so only
# those take the strict budget; plain GETs of /api/config and /api/providers
# stay on the normal per-IP budget so the console cannot throttle itself while
# polling.
_ADMIN_STRICT_PREFIXES = ("/api/config", "/api/providers")
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PROTECTED_LIMIT = 30            # stricter budget for LLM/TTS-spending and high-risk admin routes
_MAX_TRACKED_IPS = 10000


def _evict_oldest(counters: Dict[str, "SlidingWindowCounter"]) -> None:
    """Evicts the stalest per-IP counter instead of clearing the whole table.

    Clearing everything lets an attacker with spoofed source IPs periodically
    reset EVERY client's rate-limit budget; evicting only the entry whose
    newest event is oldest keeps honest clients' limits intact.
    """
    oldest_key = min(
        counters,
        key=lambda k: counters[k].events[-1] if counters[k].events else 0.0,
    )
    counters.pop(oldest_key, None)


class SlidingWindowCounter:
    def __init__(self, limit: int, window_seconds: float = _WINDOW_SECONDS):
        self.limit = limit
        self.window = window_seconds
        self.events: Deque[float] = deque()

    def has_room(self, now: float) -> bool:
        """Reports whether one more request fits, without recording it."""
        while self.events and now - self.events[0] >= self.window:
            self.events.popleft()
        return len(self.events) < self.limit

    def allow(self, now: float) -> bool:
        if not self.has_room(now):
            return False
        self.events.append(now)
        return True


class RateLimitMiddleware:
    def __init__(self, app):
        self.app = app
        self.global_counter = SlidingWindowCounter(_DEFAULT_GLOBAL_LIMIT)
        self.ip_counters: Dict[str, SlidingWindowCounter] = {}
        self.protected_counters: Dict[str, SlidingWindowCounter] = {}

    def _client_ip(self, scope) -> str:
        client = scope.get("client")
        return client[0] if client else "unknown"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self._is_disabled():
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.startswith(("/static/", "/audio/")):
            await self.app(scope, receive, send)
            return

        now = time.monotonic()
        ip = self._client_ip(scope)

        is_protected = path.startswith(_PROTECTED_PREFIXES) or (
            path.startswith(_ADMIN_STRICT_PREFIXES)
            and str(scope.get("method", "GET")).upper() in _MUTATING_METHODS
        )
        counters = self.protected_counters if is_protected else self.ip_counters
        limit = _PROTECTED_LIMIT if is_protected else _DEFAULT_IP_LIMIT
        counter = counters.get(ip)

        # Check both budgets before mutating either or tracking a new IP:
        # a rejected request must not spend the other budget or churn the IP table.
        if not ((counter is None or counter.has_room(now)) and self.global_counter.has_room(now)):
            await self._reject(send)
            return

        if counter is None:
            if len(counters) >= _MAX_TRACKED_IPS:
                _evict_oldest(counters)
            counter = counters[ip] = SlidingWindowCounter(limit)
        counter.events.append(now)
        self.global_counter.events.append(now)

        await self.app(scope, receive, send)

    async def _reject(self, send) -> None:
        retry_after = str(int(_WINDOW_SECONDS))
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", retry_after.encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": b'{"detail": "Too Many Requests"}',
        })

    @staticmethod
    def _is_disabled() -> bool:
        if os.getenv("GALGAME2VOICE_RATE_LIMIT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
            return True
        try:
            return bool(get_settings().rate_limit_disabled)
        except Exception:
            return False


__all__ = ["RateLimitMiddleware", "SlidingWindowCounter"]
