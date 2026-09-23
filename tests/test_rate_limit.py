"""Tests for the sliding-window rate limiter."""

import pytest

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
            scope = {"type": "http", "path": "/api/chat", "client": ("9.9.9.9", 1)}
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
