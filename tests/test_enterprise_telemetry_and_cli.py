"""
Unit tests for enterprise telemetry, hardware utilities, CLI flags, and static caching:
1. galgame2voice.utils.hardware (detect_gpu_capability, is_turing_tu116_tu117_gpu, get_system_memory_status).
2. Backward-compatible re-exports in scripts/run_server.py.
3. SystemStatusResponse hardware telemetry in /api/system/status.
4. Server launcher CLI argument parsing and --check-only pre-flight execution.
5. HTTP static asset Cache-Control immutable caching.
"""

import sys
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
import httpx
from httpx import AsyncClient, ASGITransport

from galgame2voice.main import create_app
from galgame2voice.utils.hardware import (
    detect_gpu_capability,
    is_turing_tu116_tu117_gpu,
    get_system_memory_status,
)
import scripts.run_server as rs


# ============================================================================
# 1. Hardware Utility Tests
# ============================================================================

class TestHardwareUtilities:
    def test_get_system_memory_status_returns_floats_or_none(self):
        total, avail = get_system_memory_status()
        if total is not None:
            assert isinstance(total, float)
            assert total > 0.0
        if avail is not None:
            assert isinstance(avail, float)
            assert avail >= 0.0

    def test_detect_gpu_capability_structure(self):
        gpu_avail, gpu_name, count = detect_gpu_capability()
        assert isinstance(gpu_avail, bool)
        assert isinstance(gpu_name, str)
        assert count is None or isinstance(count, int)

    def test_is_turing_gpu_detection_overrides(self):
        # Explicit Turing models
        assert is_turing_tu116_tu117_gpu("NVIDIA GeForce MX450") is True
        assert is_turing_tu116_tu117_gpu("GeForce GTX 1650") is True
        assert is_turing_tu116_tu117_gpu("GeForce GTX 1660 SUPER") is True
        assert is_turing_tu116_tu117_gpu("NVIDIA TU117") is True
        assert is_turing_tu116_tu117_gpu("GeForce MX550") is True

        # Non-Turing models
        assert is_turing_tu116_tu117_gpu("NVIDIA GeForce RTX 3080") is False
        assert is_turing_tu116_tu117_gpu("NVIDIA GeForce RTX 4090") is False
        assert is_turing_tu116_tu117_gpu("Intel Iris Xe Graphics") is False
        assert is_turing_tu116_tu117_gpu("AMD Radeon RX 6800") is False

    def test_detect_gpu_capability_with_mocked_torch(self, monkeypatch):
        mock_torch = type("Torch", (), {
            "cuda": type("Cuda", (), {
                "is_available": lambda: True,
                "device_count": lambda: 2,
                "get_device_name": lambda idx: "NVIDIA RTX 4080",
            })
        })
        monkeypatch.setitem(sys.modules, "torch", mock_torch)
        avail, name, count = detect_gpu_capability()
        assert avail is True
        assert name == "NVIDIA RTX 4080"
        assert count == 2

    def test_detect_gpu_capability_with_mocked_nvidia_smi(self, monkeypatch):
        mock_torch_no_cuda = type("Torch", (), {
            "cuda": type("Cuda", (), {
                "is_available": lambda: False,
            })
        })
        monkeypatch.setitem(sys.modules, "torch", mock_torch_no_cuda)

        import galgame2voice.utils.hardware as hw
        monkeypatch.setattr(hw.subprocess, "check_output", lambda *args, **kwargs: "GeForce GTX 1080\n")
        avail, name, count = hw.detect_gpu_capability()
        assert avail is True
        assert name == "GeForce GTX 1080"
        assert count == 1


# ============================================================================
# 2. Re-Export Compatibility in scripts/run_server.py
# ============================================================================

class TestRunServerReExports:
    def test_run_server_reexports_hardware_functions(self):
        assert hasattr(rs, "detect_gpu_capability")
        assert hasattr(rs, "is_turing_tu116_tu117_gpu")
        assert hasattr(rs, "get_system_memory_status")
        assert hasattr(rs, "get_system_ram_gb")

        total, avail = rs.get_system_ram_gb()
        assert isinstance(total, float)
        assert isinstance(avail, float)

    def test_run_hardware_diagnostics_structure(self):
        diag = rs.run_hardware_diagnostics()
        assert isinstance(diag, dict)
        assert "is_turing" in diag
        assert "cuda_available" in diag
        assert "gpu_names" in diag
        assert "total_ram_gb" in diag
        assert "avail_ram_gb" in diag
        assert "has_nvidia" in diag


# ============================================================================
# 3. System Status Endpoint Hardware Telemetry
# ============================================================================

class TestSystemStatusHardwareTelemetry:
    @pytest.mark.asyncio
    async def test_system_status_contains_hardware_telemetry(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/system/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "hardware" in data
            hw = data["hardware"]
            assert isinstance(hw["gpu_available"], bool)
            assert isinstance(hw["gpu_name"], str)
            assert isinstance(hw["turing_fp32_active"], bool)
            assert hw["system_memory_gb"] is None or isinstance(hw["system_memory_gb"], (int, float))
            assert hw["system_memory_avail_gb"] is None or isinstance(hw["system_memory_avail_gb"], (int, float))


# ============================================================================
# 4. CLI Argument Parsing and --check-only Execution
# ============================================================================

class TestServerLauncherCLI:
    def test_parse_args_defaults(self):
        args = rs.parse_args([])
        assert args.host == "127.0.0.1"
        assert args.port == 8080
        assert args.no_browser is False
        assert args.check_only is False

    def test_parse_args_custom_values(self):
        args = rs.parse_args(["--host", "0.0.0.0", "--port", "9090", "--no-browser", "--check-only"])
        assert args.host == "0.0.0.0"
        assert args.port == 9090
        assert args.no_browser is True
        assert args.check_only is True

    def test_main_check_only_success(self, monkeypatch):
        monkeypatch.setattr(rs, "check_python_environment", lambda: True)
        monkeypatch.setattr(rs, "run_hardware_diagnostics", lambda: {"is_turing": False})

        with pytest.raises(SystemExit) as exc_info:
            rs.main(["--check-only"])
        assert exc_info.value.code == 0

    def test_main_check_only_env_failure(self, monkeypatch):
        monkeypatch.setattr(rs, "check_python_environment", lambda: False)

        with pytest.raises(SystemExit) as exc_info:
            rs.main(["--check-only"])
        assert exc_info.value.code == 1


# ============================================================================
# 5. Static Asset Cache-Control Header Tests
# ============================================================================

class TestStaticAssetCaching:
    @pytest.mark.asyncio
    async def test_static_assets_immutable_cache_header(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Request under /static/assets/ (real existing asset)
            resp = await client.get("/static/assets/index-C5oKplHJ.js")
            assert resp.status_code == 200
            assert "cache-control" in resp.headers
            assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"

            # Request under /static/ (non-assets)
            resp_css = await client.get("/static/css/console.css")
            assert resp_css.status_code == 200
            assert "cache-control" in resp_css.headers
            assert resp_css.headers["cache-control"] == "public, max-age=3600"

            # Entry point index.html or settings.html
            resp_index = await client.get("/")
            assert resp_index.status_code == 200
            assert resp_index.headers.get("cache-control") == "no-cache"

            resp_settings = await client.get("/settings.html")
            assert resp_settings.status_code == 200
            assert resp_settings.headers.get("cache-control") == "no-cache"
