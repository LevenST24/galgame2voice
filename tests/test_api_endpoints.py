"""
Tests for FastAPI Core Application Routes, Health Telemetry, Static Fallback, and Chat History.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice import __version__
from galgame2voice.main import create_app
from galgame2voice.routers.health import _get_process_memory_mb, _scan_dir_sync
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
    async def test_root_fallback_and_static_mount(self):
        """Verifies root fallback endpoint returns app metadata."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/")
            assert resp.status_code == 200

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
