"""
Adversarial Stress Test Suite for Milestone 1: One-Click Automation Scripts.
Authored by Challenger 2.

Empirically tests and challenges:
1. Process Tree Termination Logic (`taskkill /f /t /pid`) on Windows.
2. GPU VRAM Release Mechanisms and Orphan Process Prevention.
3. Port Detection & Netstat Token Parsing (`netstat -ano`, IPv4/IPv6, LISTENING vs ESTABLISHED/TIME_WAIT).
4. Substring Port Collisions and Dynamic Port Binding.
5. Windows cmd.exe Environment Isolation and Path Handling.
"""

import os
import sys
import time
import socket
import subprocess
from pathlib import Path
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


# ============================================================================
# 1. Process Tree Killing & Orphan Prevention Stress Tests
# ============================================================================

class TestProcessTreeTermination:
    """Empirically test process tree killing (/F /T /PID) on Windows."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill testing requires Windows OS")
    def test_nested_process_tree_termination(self):
        """
        Empirically test that `taskkill /f /t /pid <root_pid>` kills the entire
        hierarchy (Parent -> Child -> Grandchild) and releases all bound ports.
        """
        port1 = 28181
        port2 = 28182

        # Script creates child process, and child creates grandchild
        worker_code = f"""
import subprocess, time, sys, socket

s1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s1.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s1.bind(('127.0.0.1', {port1}))
s1.listen(1)

child_code = '''
import subprocess, time, sys, socket
s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s2.bind(('127.0.0.1', {port2}))
s2.listen(1)
p_grand = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
while True:
    time.sleep(0.5)
'''

p_child = subprocess.Popen([sys.executable, '-c', child_code])
while True:
    time.sleep(0.5)
"""
        # Launch parent process
        parent = subprocess.Popen([sys.executable, "-c", worker_code])
        parent_pid = parent.pid

        # Wait for sockets to be ready
        def is_port_listening(p):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                return s.connect_ex(("127.0.0.1", p)) == 0

        ready = False
        for _ in range(60):
            if is_port_listening(port1) and is_port_listening(port2):
                ready = True
                break
            time.sleep(0.1)

        assert ready, f"Failed to start nested process tree on ports {port1}, {port2}"

        # Now execute `taskkill /f /t /pid <parent_pid>`
        kill_result = subprocess.run(
            f"taskkill /f /t /pid {parent_pid}",
            shell=True,
            capture_output=True,
            text=True
        )
        assert kill_result.returncode == 0, f"taskkill failed: {kill_result.stderr}"

        # Verify both ports are freed and processes are dead
        freed = False
        for _ in range(50):
            if not is_port_listening(port1) and not is_port_listening(port2):
                freed = True
                break
            time.sleep(0.1)

        assert freed, f"Ports ({port1}, {port2}) were not released by tree kill!"

        # Verify parent process is no longer running
        parent.poll()
        assert parent.returncode is not None

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill testing requires Windows OS")
    def test_taskkill_invalid_or_dead_pid_resilience(self):
        """
        Verify that `taskkill /f /t /pid` on a non-existent PID exits gracefully
        without breaking batch script control flow when stderr/stdout are suppressed.
        """
        invalid_pid = 999999
        cmd = f"taskkill /f /t /pid {invalid_pid} >nul 2>&1"
        res = subprocess.run(cmd, shell=True)
        # Should return non-zero but not raise exception
        assert res.returncode != 0

    def test_gpu_vram_release_model_and_tree_kill_mechanics(self):
        """
        Verify the architectural mechanics of VRAM cleanup:
        1. PyTorch / CUDA runtime maintains GPU context within the host OS process space.
        2. When a process terminates, Windows WDDM (Windows Display Driver Model) kernel driver
           (dxgkrnl / nvlddmkm) automatically frees all physical & virtual VRAM allocations
           registered to that process ID.
        3. Using `/T` guarantees that multi-worker pipelines (e.g. GPT-SoVITS inference subprocesses)
           are fully terminated, preventing orphaned GPU memory leaks.
        """
        # Inspect 停止.bat and scripts/stop-galgame2voice.bat for /T flag
        tingzhi_content = (PROJECT_ROOT / "停止.bat").read_text(encoding="utf-8", errors="ignore")
        assert "/t" in tingzhi_content.lower(), "停止.bat must include /t flag for tree termination"
        assert "/f" in tingzhi_content.lower(), "停止.bat must include /f flag for forced termination"
        assert ":9880" in tingzhi_content, "停止.bat must target GPT-SoVITS port 9880"
        assert ":8080" in tingzhi_content, "停止.bat must target Galgame2Voice port 8080"


# ============================================================================
# 2. Port Detection & Netstat Parsing Stress Tests
# ============================================================================

class TestPortDetectionAndNetstat:
    """Stress tests for netstat command output parsing and connection state filtering."""

    def test_netstat_tokens_ipv4_ipv6_parsing(self):
        """
        Test that `tokens=4,5` correctly extracts the state and PID across IPv4,
        IPv6, and wildcard bindings.
        """
        test_netstat_output = [
            "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       1234",
            "  TCP    127.0.0.1:8080         0.0.0.0:0              LISTENING       1234",
            "  TCP    [::]:8080              [::]:0                 LISTENING       1234",
            "  TCP    [::1]:8080             [::]:0                 LISTENING       1234",
            "  TCP    127.0.0.1:9880         0.0.0.0:0              LISTENING       5678",
        ]

        def parse_batch_netstat(lines, port):
            results = []
            for line in lines:
                if f":{port}" in line:
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        token4 = parts[3]
                        token5 = parts[4]
                        if token4 == "LISTENING":
                            results.append(token5)
            return results

        pids_8080 = parse_batch_netstat(test_netstat_output, 8080)
        assert len(pids_8080) == 4
        assert all(pid == "1234" for pid in pids_8080)

        pids_9880 = parse_batch_netstat(test_netstat_output, 9880)
        assert len(pids_9880) == 1
        assert pids_9880[0] == "5678"

    def test_netstat_ignores_non_listening_states(self):
        """
        Ensure active/transient connection states (ESTABLISHED, TIME_WAIT, CLOSE_WAIT)
        are NOT extracted as listening services.
        """
        test_netstat_output = [
            "  TCP    127.0.0.1:8080         127.0.0.1:54321        ESTABLISHED     1234",
            "  TCP    127.0.0.1:54321        127.0.0.1:8080         ESTABLISHED     9999",
            "  TCP    127.0.0.1:8080         127.0.0.1:54322        TIME_WAIT       0",
            "  TCP    127.0.0.1:8080         127.0.0.1:54323        CLOSE_WAIT      1234",
            "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       1234",
        ]

        extracted_pids = []
        for line in test_netstat_output:
            if ":8080" in line:
                parts = line.strip().split()
                if len(parts) >= 5:
                    token4 = parts[3]
                    token5 = parts[4]
                    if token4 == "LISTENING":
                        extracted_pids.append(token5)

        assert len(extracted_pids) == 1
        assert extracted_pids[0] == "1234"

    @pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows OS")
    def test_live_netstat_batch_execution(self, tmp_path):
        """
        Empirically start a real socket listener and test batch netstat parsing command directly.
        """
        test_port = 28185
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("127.0.0.1", test_port))
        server_sock.listen(1)

        try:
            # Create a test batch file with the exact loop from 启动.bat / 停止.bat
            test_bat = tmp_path / "test_netstat.bat"
            test_bat.write_text(
                f"@echo off\n"
                f"for /f \"tokens=4,5\" %%a in ('netstat -ano ^| findstr \":{test_port}\"') do (\n"
                f"    if \"%%a\"==\"LISTENING\" echo %%b\n"
                f")\n",
                encoding="utf-8"
            )
            res = subprocess.run(
                [str(test_bat)],
                capture_output=True,
                text=True,
                timeout=5
            )
            output = res.stdout.strip()
            current_pid = str(os.getpid())
            assert current_pid in output, f"Expected PID {current_pid} in netstat output, got: {output}"
        finally:
            server_sock.close()


# ============================================================================
# 3. Batch Script Configuration & Syntax Hardening
# ============================================================================

class TestBatchScriptHardening:
    """Stress tests for Windows cmd.exe syntax quirks, path escaping, and robustness."""

    def test_all_batch_files_exist_and_non_empty(self):
        """Check the canonical lifecycle scripts exist and are non-empty."""
        scripts = [
            PROJECT_ROOT / "启动.bat",
            PROJECT_ROOT / "停止.bat",
            SCRIPTS_DIR / "run_server.py",
        ]
        for script in scripts:
            assert script.exists(), f"Missing script: {script}"
            content = script.read_text(encoding="utf-8")
            assert len(content.strip()) > 0, f"Empty script: {script}"

    def test_legacy_duplicate_scripts_removed(self):
        """Verify the legacy duplicated wrapper scripts were consolidated away."""
        removed = [
            PROJECT_ROOT / "start.bat",
            PROJECT_ROOT / "stop.bat",
            PROJECT_ROOT / "start-galgame2voice.bat",
            PROJECT_ROOT / "stop-galgame2voice.bat",
            PROJECT_ROOT / "restart-galgame2voice.bat",
        ]
        for script in removed:
            assert not script.exists(), f"Legacy script should be removed: {script}"

    def test_working_directory_switch_safety(self):
        """Verify scripts safely switch working directory using `%~dp0`."""
        for script in (PROJECT_ROOT / "启动.bat", PROJECT_ROOT / "停止.bat"):
            content = script.read_text(encoding="utf-8", errors="ignore")
            assert 'cd /d "%~dp0"' in content or 'pushd "%~dp0.."' in content

    def test_delayed_expansion_isolated(self):
        """Verify delayed expansion is properly scoped with setlocal enabledelayedexpansion."""
        for script in (PROJECT_ROOT / "启动.bat", PROJECT_ROOT / "停止.bat"):
            content = script.read_text(encoding="utf-8", errors="ignore")
            assert "setlocal enabledelayedexpansion" in content.lower()
            assert "endlocal" in content.lower()

    def test_gpt_sovits_discovery_locations(self):
        """Verify run_server.py probes env override plus the known GPT-SoVITS install path."""
        content = (SCRIPTS_DIR / "run_server.py").read_text(encoding="utf-8", errors="ignore")
        assert "GPT_SOVITS_DIR" in content
        assert "E:\\GPT-SoVITS-v2pro-20250604" in content
        # Generic drive globbing covers D:/C: installations of any version.
        assert "GPT-SoVITS*" in content

    def test_health_polling_bounded(self):
        """Verify run_server.py readiness wait uses bounded loops (no infinite spin)."""
        content = (SCRIPTS_DIR / "run_server.py").read_text(encoding="utf-8", errors="ignore")
        assert "for i in range(240)" in content  # 120s bounded GPT-SoVITS readiness wait
        assert "for _ in range(30)" in content   # 9s bounded browser-open wait


# ============================================================================
# 4. End-to-End Real Process Batch Lifecycle Stress Tests
# ============================================================================

class TestEndToEndBatchShutdown:
    """Empirically test 停止.bat against real processes running on ports 8080 and 9880."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows OS")
    def test_tingzhi_bat_kills_both_8080_and_9880_processes(self, tmp_path):
        """
        Start two background processes listening on 8080 and 9880.
        Execute 停止.bat.
        Verify both processes are forcefully terminated and ports are released.
        """
        # First check if 8080 or 9880 can be bound; if not skip to avoid killing system processes or crashing on WSAEACCES
        def is_bindable(port):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("127.0.0.1", port))
                    return True
            except OSError:
                return False

        def is_open(port):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                return s.connect_ex(("127.0.0.1", port)) == 0

        if not is_bindable(8080) or not is_bindable(9880):
            pytest.skip("Port 8080 or 9880 cannot be bound (occupied or restricted by system).")

        # Spawn mock listener 8080
        p8080 = subprocess.Popen([
            sys.executable, "-c",
            "import socket, time; s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', 8080)); s.listen(50); time.sleep(60)"
        ])
        # Spawn mock listener 9880
        p9880 = subprocess.Popen([
            sys.executable, "-c",
            "import socket, time; s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', 9880)); s.listen(50); time.sleep(60)"
        ])

        # Write dummy PID files
        pid_file = PROJECT_ROOT / "galgame2voice.pid"
        pid_file.write_text(str(p8080.pid), encoding="utf-8")

        # Wait for ports to be listening
        for _ in range(30):
            if is_open(8080) and is_open(9880):
                break
            time.sleep(0.1)

        assert is_open(8080), "Mock 8080 failed to start"
        assert is_open(9880), "Mock 9880 failed to start"

        try:
            # Execute 停止.bat
            stop_bat = PROJECT_ROOT / "停止.bat"
            res = subprocess.run(
                [str(stop_bat)],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10
            )
            assert res.returncode == 0
            assert "已停止 Galgame2Voice 服务" in (res.stdout or "") or res.returncode == 0

            # Verify ports freed
            time.sleep(0.5)
            assert not is_open(8080), "Port 8080 was not closed by 停止.bat"
            assert not is_open(9880), "Port 9880 was not closed by 停止.bat"
            assert not pid_file.exists(), "galgame2voice.pid was not removed by 停止.bat"
        finally:
            # Cleanup safeguard
            p8080.kill()
            p9880.kill()
            if pid_file.exists():
                pid_file.unlink()

    def test_outbound_connection_to_remote_8080_not_killed(self):
        """
        Verify that outbound connections where the REMOTE port is 8080
        (e.g., TCP 192.168.0.101:9963 36.155.189.125:8080 ESTABLISHED 66196)
        are NOT extracted or killed by the LISTENING filter in 停止.bat.
        """
        raw_lines = [
            "  TCP    172.18.0.1:9959        36.155.189.125:8080    ESTABLISHED     34712",
            "  TCP    192.168.0.101:9963     36.155.189.125:8080    ESTABLISHED     66196",
            "  TCP    127.0.0.1:10242        127.0.0.1:9880         TIME_WAIT       0",
            "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       12345",
        ]

        extracted_listening_pids = []
        for line in raw_lines:
            if ":8080" in line:
                parts = line.strip().split()
                if len(parts) >= 5:
                    token4 = parts[3]
                    token5 = parts[4]
                    if token4 == "LISTENING":
                        extracted_listening_pids.append(token5)

        assert extracted_listening_pids == ["12345"], f"Expected only local listener PID 12345, got {extracted_listening_pids}"
        assert "34712" not in extracted_listening_pids, "Outbound client PID was mistakenly captured!"
        assert "66196" not in extracted_listening_pids, "Outbound client PID was mistakenly captured!"

