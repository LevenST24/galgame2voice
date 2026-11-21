"""
Tests for FastAPI Core Application Routes, Health Telemetry, Static Fallback, and Chat History.
"""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice import __version__
from galgame2voice.main import create_app
from galgame2voice.routers.health import _get_process_memory_mb, _probe_gpt_sovits, _scan_dir_sync
from galgame2voice.utils.logger import MaskingFilter


class TestCoreMainAndHealthEndpoints:
    """Test production FastAPI application, health diagnostics, static fallback, and chat history."""

    @pytest.mark.asyncio
    async def test_main_app_health_endpoints(self):
        """Verifies GET /api/health and GET /api/system/status on the real galgame2voice app instance."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # Health check
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["app"] == "galgame2voice"
            assert data["version"] == __version__
            assert "uptime_seconds" in data

            # Full telemetry diagnostic
            diag_resp = await client.get("/api/system/status")
            assert diag_resp.status_code == 200
            d_data = diag_resp.json()
            assert d_data["status"] in ("healthy", "degraded")
            assert "app" in d_data
            assert "database" in d_data
            assert "gpt_sovits" in d_data
            assert "storage" in d_data
            assert "telegram" not in d_data

    def test_health_helpers_dir_scan_and_memory(self, tmp_path):
        """Verifies _scan_dir_sync missing/populated directory metrics and _get_process_memory_mb."""
        assert _scan_dir_sync(tmp_path / "missing") == (0, 0.0)

        scan_dir = tmp_path / "audio_scan"
        (scan_dir / "nested").mkdir(parents=True)
        (scan_dir / "sample1.wav").write_bytes(b"\x00" * (512 * 1024))
        (scan_dir / "nested" / "sample2.wav").write_bytes(b"\x00" * (1024 * 1024))
        assert _scan_dir_sync(scan_dir) == (2, 1.5)

        mem_mb = _get_process_memory_mb()
        if mem_mb is not None:
            assert isinstance(mem_mb, float)
            assert mem_mb >= 0.0

    def test_masking_filter_empty_and_none(self):
        assert MaskingFilter.sanitize(None) is None
        assert MaskingFilter.sanitize("") == ""


    @pytest.mark.asyncio
    async def test_chat_history_endpoints(self, temp_db_path):
        """Verifies GET /api/chat/history and DELETE /api/chat/history."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # 1. Fetch history for empty session
            resp = await client.get("/api/chat/history?session_id=test_hist_sess")
            assert resp.status_code == 200
            data = resp.json()
            assert data["session_id"] == "test_hist_sess"
            assert isinstance(data["messages"], list)

            # 2. Delete history
            del_resp = await client.delete("/api/chat/history?session_id=test_hist_sess")
            assert del_resp.status_code == 200
            assert del_resp.json()["status"] == "cleared"

    @pytest.mark.asyncio
    async def test_static_ui_files_served(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r_root = await c.get("/")
            assert r_root.status_code == 200
            assert "Gal2Voice" in r_root.text

            asset_names = re.findall(r'/static/assets/(index-[\w-]+\.(?:js|css))', r_root.text)
            assert asset_names, "index.html must reference built /static/assets files"
            for name in asset_names:
                r_asset = await c.get(f"/static/assets/{name}")
                assert r_asset.status_code == 200


    @pytest.mark.asyncio
    async def test_non_standard_http_methods_on_health(self):
        """Verify non-standard and unhandled HTTP methods return 405 Method Not Allowed cleanly."""
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            for method in ["POST", "PUT", "DELETE", "PATCH", "TRACE"]:
                res = await client.request(method, "/api/health")
                assert res.status_code == 405, f"Expected 405 for {method} /api/health, got {res.status_code}"
                assert "detail" in res.json()

            for method in ["POST", "PUT", "DELETE", "PATCH"]:
                res = await client.request(method, "/api/system/status")
                assert res.status_code == 405, f"Expected 405 for {method} /api/system/status, got {res.status_code}"

    @pytest.mark.asyncio
    async def test_gpt_sovits_probe_connection_anomalies(self):
        """Test _probe_gpt_sovits under connection timeout, 500, 404, and 200 responses."""
        # 1. Connection Timeout
        with patch("httpx.AsyncClient.get", side_effect=httpx.ConnectTimeout("Connect timeout")):
            telemetry = await _probe_gpt_sovits("http://127.0.0.1:9880")
            assert telemetry.status == "unreachable"
            assert "Timeout" in telemetry.error or "ConnectTimeout" in telemetry.error
            assert telemetry.latency_ms is not None

        # 2. Connection Refused / Network Error
        with patch("httpx.AsyncClient.get", side_effect=httpx.ConnectError("Connection refused")):
            telemetry = await _probe_gpt_sovits("http://127.0.0.1:9880")
            assert telemetry.status == "unreachable"
            assert "ConnectError" in telemetry.error

        # 3. HTTP 500 Internal Server Error
        class MockResponse500:
            status_code = 500
        with patch("httpx.AsyncClient.get", return_value=MockResponse500()):
            telemetry = await _probe_gpt_sovits("http://127.0.0.1:9880")
            assert telemetry.status == "unreachable"
            assert "500" in telemetry.error

        # 4. HTTP 404 Not Found
        class MockResponse404:
            status_code = 404
        with patch("httpx.AsyncClient.get", return_value=MockResponse404()):
            telemetry = await _probe_gpt_sovits("http://127.0.0.1:9880")
            assert telemetry.status == "unreachable"
            assert "404" in telemetry.error

        # 5. Success 200 OK
        class MockResponse200:
            status_code = 200
        with patch("httpx.AsyncClient.get", return_value=MockResponse200()):
            telemetry = await _probe_gpt_sovits("http://127.0.0.1:9880")
            assert telemetry.status == "reachable"
            assert telemetry.error is None
            assert telemetry.latency_ms >= 0.0

    @pytest.mark.asyncio
    async def test_refused_custom_address_is_not_retried_on_another_path(self):
        """Settings can probe an address other than the client's; a refused one is not tried again on "/"."""
        with patch("httpx.AsyncClient.get", side_effect=httpx.ConnectError("Connection refused")) as get:
            telemetry = await _probe_gpt_sovits("http://other-engine.test:9880")
        assert telemetry.status == "unreachable"
        assert "ConnectError" in telemetry.error
        assert [c.args[0] for c in get.call_args_list] == ["http://other-engine.test:9880/control"]

    @pytest.mark.asyncio
    async def test_large_query_and_headers_fuzzing(self):
        """Verify endpoint robustness against massive headers and oversized query strings."""
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            # 100 query parameters
            query_string = "&".join([f"key_{i}=val_{i*100}" for i in range(100)])
            res = await client.get(f"/api/health?{query_string}")
            assert res.status_code == 200
            assert res.json()["status"] == "ok"

            # 4KB custom ASCII header
            headers = {
                "X-Fuzz-Header": "A" * 4096,
                "X-Custom-Trace-Id": "trace-12345-xyz-98765",
            }
            res = await client.get("/api/system/status", headers=headers)
            assert res.status_code == 200

    @pytest.mark.asyncio
    async def test_high_concurrency_burst(self, monkeypatch):
        """Test /api/health with 200 simultaneous async requests.

        The production rate limiter is disabled for this raw-throughput test
        (a 200-request burst legitimately triggers 429s otherwise)."""
        monkeypatch.setenv("GALGAME2VOICE_RATE_LIMIT_DISABLED", "1")
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            tasks = [client.get("/api/health") for _ in range(200)]
            responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30.0)
            assert len(responses) == 200
            assert all(r.status_code == 200 for r in responses)
            for r in responses:
                assert r.json()["status"] == "ok"


@pytest.mark.asyncio
class TestHealthRouterHardening:
    async def test_probe_invalid_url_scheme(self):
        from galgame2voice.routers.health import _probe_gpt_sovits
        telemetry = await _probe_gpt_sovits("ftp://localhost:9880")
        assert telemetry.status == "unreachable"
        assert "scheme must be http or https" in telemetry.error

        telemetry2 = await _probe_gpt_sovits("file:///etc/passwd")
        assert telemetry2.status == "unreachable"
        assert "scheme must be http or https" in telemetry2.error

    async def test_probe_sanitizes_exception_details(self):
        from galgame2voice.routers.health import _probe_gpt_sovits
        with patch("galgame2voice.services.gpt_sovits_client.get_gpt_sovits_client") as mock_get:
            mock_client = MagicMock()
            mock_client.base_url = "http://localhost:9880"
            mock_client.check_health = AsyncMock(side_effect=Exception("Failed with api_key=sk-1234567890abcdef"))
            mock_get.return_value = mock_client

            telemetry = await _probe_gpt_sovits("http://localhost:9880")
            assert telemetry.status == "unreachable"
            assert "sk-1234567890abcdef" not in telemetry.error
            assert "REDACTED" in telemetry.error or "***" in telemetry.error or "api_key" in telemetry.error


class TestCorsSecurityAndHeaders:
    """Validates CORS preflight handling, allowed headers, origin reflection, and credential semantics."""

    @pytest.mark.asyncio
    async def test_cors_preflight_standard_origin(self):
        """Verify standard localhost preflight returns appropriate CORS headers."""
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            preflight_headers = {
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type, Authorization, X-Requested-With",
            }
            res = await client.options("/api/health", headers=preflight_headers)
            assert res.status_code == 200
            assert res.headers.get("access-control-allow-origin") == "http://localhost:8080"
            assert "access-control-allow-methods" in res.headers
            assert "access-control-allow-headers" in res.headers

    @pytest.mark.asyncio
    async def test_cors_preflight_arbitrary_origin(self):
        """Verify arbitrary external origins are rejected (CORS tightened to localhost only)."""
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
            preflight_headers = {
                "Origin": "https://external-dashboard.example.com",
                "Access-Control-Request-Method": "GET",
            }
            res = await client.options("/api/system/status", headers=preflight_headers)
            # Tightened CORS must NOT grant access to arbitrary external origins
            assert res.headers.get("access-control-allow-origin") != "https://external-dashboard.example.com"
            assert res.headers.get("access-control-allow-origin") != "*"
