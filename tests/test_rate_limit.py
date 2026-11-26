"""Tests for the sliding-window rate limiter."""

import pytest

from galgame2voice.security import rate_limit
from galgame2voice.security.rate_limit import RateLimitMiddleware, SlidingWindowCounter


class TestSlidingWindowCounter:
    def test_allows_under_limit(self):
        counter = SlidingWindowCounter(limit=5)
        now = 1000.0
        assert all(counter.allow(now + i * 0.1) for i in range(5))

    def test_blocks_over_limit(self):
        counter = SlidingWindowCounter(limit=5)
        now = 1000.0
        for i in range(5):
            assert counter.allow(now + i * 0.1)
        assert counter.allow(now + 0.6) is False

    def test_window_expiry_restores_budget(self):
        counter = SlidingWindowCounter(limit=2, window_seconds=10.0)
        assert counter.allow(0.0)
        assert counter.allow(1.0)
        assert counter.allow(2.0) is False
        assert counter.allow(11.5) is True


class TestMiddleware:
    @pytest.mark.asyncio
    async def test_disabled_env_passes_through(self, monkeypatch):
        monkeypatch.setenv("GALGAME2VOICE_RATE_LIMIT_DISABLED", "1")
        called = []

        async def app(scope, receive, send):
            called.append(scope)

        mw = RateLimitMiddleware(app)
        scope = {"type": "http", "path": "/api/config", "client": ("1.2.3.4", 1234)}
        sent = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent.append(message)

        await mw(scope, receive, send)
        assert called

    @pytest.mark.asyncio
    async def test_rejects_after_burst(self, monkeypatch):
        monkeypatch.delenv("GALGAME2VOICE_RATE_LIMIT_DISABLED", raising=False)
        handled = []

        async def app(scope, receive, send):
            handled.append(scope)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw = RateLimitMiddleware(app)
        statuses = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        for i in range(35):
            scope = {"type": "http", "path": "/api/chat/stream", "client": ("9.9.9.9", 1)}
            await mw(scope, receive, send)

        assert len(handled) == 30  # protected budget exhausted
        assert 429 in statuses

    @pytest.mark.asyncio
    async def test_static_paths_exempt(self, monkeypatch):
        monkeypatch.delenv("GALGAME2VOICE_RATE_LIMIT_DISABLED", raising=False)
        handled = []

        async def app(scope, receive, send):
            handled.append(scope)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = RateLimitMiddleware(app)

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            pass

        for i in range(200):
            await mw({"type": "http", "path": "/static/js/app.js", "client": ("8.8.8.8", 1)}, receive, send)
        assert len(handled) == 200


class TestRejectedRequestsSpareOtherBudgets:
    @staticmethod
    async def _call(mw, ip, path="/api/characters"):
        statuses = []

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw.app = app

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        await mw({"type": "http", "path": path, "method": "GET", "client": (ip, 1)}, receive, send)
        return statuses[0]

    @pytest.mark.asyncio
    async def test_runaway_client_does_not_lock_out_others(self, monkeypatch):
        monkeypatch.delenv("GALGAME2VOICE_RATE_LIMIT_DISABLED", raising=False)
        mw = RateLimitMiddleware(None)
        # One LAN client loops far past its own per-IP budget.
        results = [await self._call(mw, "192.168.1.50") for _ in range(400)]
        assert results.count(200) == 120
        # The PC's own browser must still get through.
        assert await self._call(mw, "127.0.0.1") == 200

    @pytest.mark.asyncio
    async def test_global_rejection_does_not_spend_client_budget(self, monkeypatch):
        monkeypatch.delenv("GALGAME2VOICE_RATE_LIMIT_DISABLED", raising=False)
        monkeypatch.setattr(rate_limit, "_MAX_TRACKED_IPS", 3)
        mw = RateLimitMiddleware(None)
        # Fill the 240-request global window across 3 clients (80 each < 120 per-IP).
        for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
            for _ in range(80):
                assert await self._call(mw, ip) == 200
        # Global rejection must not insert a new IP, evict tracked IPs, or spend client budget.
        assert await self._call(mw, "10.0.0.4") == 429
        assert set(mw.ip_counters) == {"10.0.0.1", "10.0.0.2", "10.0.0.3"}
        assert await self._call(mw, "10.0.0.5", path="/api/chat/stream") == 429
        assert "10.0.0.5" not in mw.protected_counters
        assert await self._call(mw, "10.0.0.1") == 429
        assert len(mw.ip_counters["10.0.0.1"].events) == 80
