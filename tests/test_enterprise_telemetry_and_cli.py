"""
Unit tests for enterprise telemetry, hardware utilities, CLI flags, and static caching:
1. galgame2voice.utils.hardware (detect_gpu_capability, is_turing_tu116_tu117_gpu, get_system_memory_status).
2. Backward-compatible re-exports in scripts/run_server.py.
3. SystemStatusResponse hardware telemetry in /api/system/status.
4. Server launcher CLI argument parsing and --check-only pre-flight execution.
5. HTTP static asset Cache-Control immutable caching and 404/method guards.
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

    def test_linux_proc_meminfo_parsing(self, monkeypatch, tmp_path):
        import galgame2voice.utils.hardware as hw
        meminfo_file = tmp_path / "meminfo"
        meminfo_file.write_text(
            "MemTotal:       16384000 kB\n"
            "MemFree:         4096000 kB\n"
            "MemAvailable:    8192000 kB\n"
            "Buffers:          500000 kB\n"
            "Cached:          3000000 kB\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(hw.sys, "platform", "linux")
        monkeypatch.setattr(hw.os.path, "exists", lambda p: p == "/proc/meminfo" or str(p) == "/proc/meminfo")
        real_open = open

        def fake_open(p, *args, **kwargs):
            if str(p) == "/proc/meminfo":
                return real_open(meminfo_file, *args, **kwargs)
            return real_open(p, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fake_open)

        total, avail = hw.get_system_memory_status()
        assert total == 15.62
        assert avail == 7.81

    def test_linux_proc_meminfo_fallback_buffers_cached(self, monkeypatch, tmp_path):
        import galgame2voice.utils.hardware as hw
        meminfo_file = tmp_path / "meminfo_old"
        # Kernel without MemAvailable
        meminfo_file.write_text(
            "MemTotal:       16384000 kB\n"
            "MemFree:         2000000 kB\n"
            "Buffers:         1000000 kB\n"
            "Cached:          3000000 kB\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(hw.sys, "platform", "linux")
        monkeypatch.setattr(hw.os.path, "exists", lambda p: p == "/proc/meminfo" or str(p) == "/proc/meminfo")
        real_open = open

        def fake_open(p, *args, **kwargs):
            if str(p) == "/proc/meminfo":
                return real_open(meminfo_file, *args, **kwargs)
            return real_open(p, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fake_open)

        total, avail = hw.get_system_memory_status()
        assert total == 15.62
        # (2000000 + 1000000 + 3000000) / (1024 * 1024) = 5.72
        assert avail == 5.72

    def test_macos_sysctl_memory_parsing(self, monkeypatch):
        import galgame2voice.utils.hardware as hw
        monkeypatch.setattr(hw.sys, "platform", "darwin")
        if hasattr(hw.os, "sysconf"):
            monkeypatch.setattr(hw.os, "sysconf", lambda name: 0)
        monkeypatch.setattr(hw.subprocess, "check_output", lambda *args, **kwargs: "17179869184\n")

        total, avail = hw.get_system_memory_status()
        assert total == 16.0
        assert avail is None


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

    def test_run_server_detect_gpu_capability_respects_subprocess_monkeypatch(self, monkeypatch):
        import subprocess as real_sub
        mock_torch_no_cuda = type("Torch", (), {
            "cuda": type("Cuda", (), {
                "is_available": lambda: False,
            })
        })
        monkeypatch.setitem(sys.modules, "torch", mock_torch_no_cuda)
        monkeypatch.setattr(rs, "subprocess", type("M", (), {
            "check_output": lambda *args, **kwargs: "GeForce RTX 3070\n",
            "DEVNULL": real_sub.DEVNULL,
        }))
        avail, name, count = rs.detect_gpu_capability()
        assert avail is True
        assert name == "GeForce RTX 3070"
        assert count == 1


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

    def test_health_router_gpu_telemetry_caching(self, monkeypatch):
        import galgame2voice.routers.health as health_mod
        health_mod._gpu_telemetry_cache = None

        call_count = [0]
        def mock_detect():
            call_count[0] += 1
            return True, "Mock RTX 5000", 1

        monkeypatch.setattr(health_mod, "detect_gpu_capability", mock_detect)

        val1 = health_mod._get_gpu_telemetry_cached()
        assert val1 == (True, "Mock RTX 5000", False)
        assert call_count[0] == 1

        val2 = health_mod._get_gpu_telemetry_cached()
        assert val2 == val1
        # Subsequent call should hit TTL cache, not calling detect_gpu_capability again
        assert call_count[0] == 1


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

    def test_parse_args_env_overrides(self, monkeypatch):
        monkeypatch.setenv("GALGAME_PORT", "9999")
        monkeypatch.setenv("GALGAME_HOST", "0.0.0.0")
        monkeypatch.setenv("GALGAME_NO_BROWSER", "1")
        args = rs.parse_args([])
        assert args.port == 9999
        assert args.host == "0.0.0.0"
        assert args.no_browser is True

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

    @pytest.mark.asyncio
    async def test_static_assets_error_status_not_cached_immutably(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/static/assets/non_existent_asset_xyz.js")
            assert resp.status_code == 404
            assert "immutable" not in resp.headers.get("cache-control", "")
            assert "no-cache" in resp.headers.get("cache-control", "")

    @pytest.mark.asyncio
    async def test_static_assets_post_method_not_cached(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/static/assets/index-C5oKplHJ.js")
            # POST should not receive immutable static cache header
            assert "immutable" not in resp.headers.get("cache-control", "")
