"""
Adversarial Empirical Verification Suite for Milestone 1 (M1 Challenger 2).
Covers:
1. Abnormal port states and conflict detection in scripts & server.
2. Stale PID recovery, PID corruption, and process lookup safety.
3. Non-standard HTTP methods & fuzzing on /api/health and /status.
4. CORS header validation under various origins and credentials modes.
5. Process termination simulation & graceful lifespan shutdown.
"""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Any
from unittest.mock import patch, AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from galgame2voice.config import Settings, get_settings
from galgame2voice.main import create_app
from galgame2voice.routers.health import (
    _probe_gpt_sovits,
    _get_dir_metrics,
    _get_process_memory_mb,
    GptSovitsTelemetry,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
PID_FILE = PROJECT_ROOT / "galgame2voice.pid"


# ============================================================================
# 1. FastAPI Endpoints, HTTP Methods & Fuzzing
# ============================================================================

class TestFastApiAdversarialEndpoints:
    """Stress-tests FastAPI endpoints with non-standard methods, invalid inputs, and simulated backend failures."""

    @pytest.mark.asyncio
    async def test_non_standard_http_methods_on_health(self):
        """Verify non-standard and unhandled HTTP methods return 405 Method Not Allowed cleanly."""
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for method in ["POST", "PUT", "DELETE", "PATCH", "TRACE"]:
                res = await client.request(method, "/api/health")
                assert res.status_code == 405, f"Expected 405 for {method} /api/health, got {res.status_code}"
                assert "detail" in res.json()

            for method in ["POST", "PUT", "DELETE", "PATCH"]:
                res = await client.request(method, "/status")
                assert res.status_code == 405, f"Expected 405 for {method} /status, got {res.status_code}"

            for method in ["POST", "PUT", "DELETE", "PATCH"]:
                res = await client.request(method, "/api/system/status")
                assert res.status_code == 405, f"Expected 405 for {method} /api/system/status, got {res.status_code}"

    @pytest.mark.asyncio
    async def test_gpt_sovits_probe_connection_anomalies(self):
        """Empirically test _probe_gpt_sovits under connection timeout, 500, 502, 404, and invalid URLs."""
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
    async def test_system_status_degraded_when_backend_offline(self):
        """Verify /api/system/status accurately reports overall status 'degraded' when GPT-SoVITS is offline."""
        app = create_app()
        transport = httpx.ASGITransport(app=app)

        mock_offline = GptSovitsTelemetry(
            status="unreachable",
            base_url="http://127.0.0.1:9880",
            latency_ms=2000.0,
            error="ConnectTimeout",
        )
        with patch("galgame2voice.routers.health._probe_gpt_sovits", new=AsyncMock(return_value=mock_offline)):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                res = await client.get("/api/system/status")
                assert res.status_code == 200
                data = res.json()
                assert data["status"] == "degraded"
                assert data["gpt_sovits"]["status"] == "unreachable"
                assert data["gpt_sovits"]["error"] == "ConnectTimeout"

    @pytest.mark.asyncio
    async def test_large_query_and_headers_fuzzing(self):
        """Verify endpoint robustness against massive headers and oversized query strings."""
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
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
    async def test_high_concurrency_burst(self):
        """Stress-test /api/health with 200 simultaneous async requests."""
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            tasks = [client.get("/api/health") for _ in range(200)]
            responses = await asyncio.gather(*tasks)
            assert len(responses) == 200
            assert all(r.status_code == 200 for r in responses)
            for r in responses:
                assert r.json()["status"] == "ok"


# ============================================================================
# 2. CORS Header Validation
# ============================================================================

class TestCorsSecurityAndHeaders:
    """Validates CORS preflight handling, allowed headers, origin reflection, and credential semantics."""

    @pytest.mark.asyncio
    async def test_cors_preflight_standard_origin(self):
        """Verify standard localhost preflight returns appropriate CORS headers."""
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            preflight_headers = {
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type, Authorization, X-Requested-With",
            }
            res = await client.options("/api/health", headers=preflight_headers)
            assert res.status_code == 200
            assert res.headers.get("access-control-allow-origin") in ("*", "http://localhost:3000")
            assert "access-control-allow-methods" in res.headers
            assert "access-control-allow-headers" in res.headers

    @pytest.mark.asyncio
    async def test_cors_preflight_arbitrary_origin(self):
        """Verify preflight with arbitrary external origin under wildcard config."""
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            preflight_headers = {
                "Origin": "https://external-dashboard.example.com",
                "Access-Control-Request-Method": "GET",
            }
            res = await client.options("/api/system/status", headers=preflight_headers)
            assert res.status_code == 200
            assert res.headers.get("access-control-allow-origin") in ("*", "https://external-dashboard.example.com")


# ============================================================================
# 3. Batch Lifecycle Scripts, Stale PID Recovery & Port Conflict
# ============================================================================

class TestLifecycleAdversarialAndPid:
    """Adversarial stress-testing of Windows batch script logic, PID recovery, and port conflicts."""

    def test_stale_pid_recovery_in_stop_script(self):
        """Verify stop-galgame2voice.bat gracefully handles non-existent stale PID without hanging."""
        if sys.platform != "win32":
            pytest.skip("Windows batch execution requires Windows")

        # Write stale dead PID
        PID_FILE.write_text("99999999\n", encoding="utf-8")
        assert PID_FILE.exists()

        res = subprocess.run(
            ["cmd.exe", "/c", str(SCRIPTS_DIR / "stop-galgame2voice.bat")],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert res.returncode == 0
        # Should have cleaned up the stale PID file
        assert not PID_FILE.exists()

    def test_corrupt_pid_file_handling(self):
        """Verify stop-galgame2voice.bat handles corrupted PID files (alphanumeric garbage, empty)."""
        if sys.platform != "win32":
            pytest.skip("Windows batch execution requires Windows")

        # Case 1: Text garbage
        PID_FILE.write_text("INVALID_PID_GARBAGE\n", encoding="utf-8")
        res = subprocess.run(
            ["cmd.exe", "/c", str(SCRIPTS_DIR / "stop-galgame2voice.bat")],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert res.returncode == 0
        if PID_FILE.exists():
            PID_FILE.unlink()

        # Case 2: Empty PID file
        PID_FILE.write_text("", encoding="utf-8")
        res = subprocess.run(
            ["cmd.exe", "/c", str(SCRIPTS_DIR / "stop-galgame2voice.bat")],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert res.returncode == 0
        if PID_FILE.exists():
            PID_FILE.unlink()

    def test_port_collision_detection_logic(self):
        """Verify that when a port is actively occupied, port scanning identifies the collision."""
        # Find an open port and bind a temporary server socket to occupy it
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        occupied_port = sock.getsockname()[1]

        try:
            # Check netstat command output detection for occupied_port
            netstat_proc = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            matching_lines = [
                line for line in netstat_proc.stdout.splitlines()
                if f":{occupied_port}" in line and "LISTENING" in line
            ]
            assert len(matching_lines) > 0, f"Expected occupied port {occupied_port} in netstat output"
            # Extract PID
            parts = matching_lines[0].strip().split()
            detected_pid = int(parts[-1])
            assert detected_pid == os.getpid()
        finally:
            sock.close()


# ============================================================================
# 4. Process Termination & Graceful Shutdown Simulation
# ============================================================================

class TestProcessTerminationAndLifespan:
    """Verifies FastAPI application startup lifespan and graceful shutdown handling."""

    @pytest.mark.asyncio
    async def test_app_lifespan_lifecycle(self):
        """Verifies lifespan startup creates required directories and finishes cleanly upon exit."""
        settings = get_settings()
        app = create_app()

        # Execute lifespan startup and shutdown
        async with app.router.lifespan_context(app):
            assert hasattr(app.state, "start_time")
            assert hasattr(app.state, "start_time_iso")
            assert settings.data_dir.exists()
            assert settings.audio_dir.exists()
            assert settings.logs_dir.exists()

    def test_subprocess_server_spawn_and_terminate(self):
        """Simulate spawning real uvicorn worker process and sending termination signal."""
        test_port = 18095
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "galgame2voice.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(test_port),
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            # Wait for server to become responsive
            online = False
            for _ in range(15):
                time.sleep(0.5)
                try:
                    r = httpx.get(f"http://127.0.0.1:{test_port}/api/health", timeout=1.0)
                    if r.status_code == 200:
                        online = True
                        break
                except Exception:
                    pass

            assert online, "Subprocess server failed to start within 7.5 seconds"

            # Query system status
            r_sys = httpx.get(f"http://127.0.0.1:{test_port}/api/system/status", timeout=2.0)
            assert r_sys.status_code == 200
            data = r_sys.json()
            assert data["app"]["name"] == "galgame2voice"
            assert isinstance(data["app"]["pid"], int) and data["app"]["pid"] > 0

        finally:
            # Send termination signal
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

            assert proc.poll() is not None, "Process did not terminate cleanly"
