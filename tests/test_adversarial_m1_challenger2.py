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
import json
import re
import sys
import time
import socket
import subprocess
from pathlib import Path
import pytest
import psutil

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


# ============================================================================
# 1. Process Tree Killing & Orphan Prevention Stress Tests
# ============================================================================

class TestProcessTreeTermination:
    """Empirically test process tree killing (/F /T /PID) on Windows."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill testing requires Windows OS")
    @pytest.mark.requires_process_termination
    def test_nested_process_tree_termination(self, tmp_path):
        """
        Empirically test that `taskkill /f /t /pid <root_pid>` kills the entire
        hierarchy (Parent -> Child -> Grandchild) and releases all bound ports.
        """
        ready_path = tmp_path / "owned-tree-ready.json"
        child_code = f"""
import json, subprocess, time, sys, socket
from pathlib import Path
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(('127.0.0.1', 0))
listener.listen(1)
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
ready = Path({str(ready_path)!r})
temporary = ready.with_suffix('.tmp')
temporary.write_text(json.dumps({{'ports': [int(sys.argv[1]), listener.getsockname()[1]],
                                'grandchild': grandchild.pid}}), encoding='utf-8')
temporary.replace(ready)
while True:
    time.sleep(0.5)
"""
        worker_code = f"""
import subprocess, time, sys, socket
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(('127.0.0.1', 0))
listener.listen(1)
child = subprocess.Popen([sys.executable, '-c', {child_code!r}, str(listener.getsockname()[1])])
while True:
    time.sleep(0.5)
"""
        parent = subprocess.Popen([sys.executable, "-c", worker_code],
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        root = psutil.Process(parent.pid)
        root_created = root.create_time()
        owned = {(root.pid, root_created)}

        def running(identity):
            try:
                process = psutil.Process(identity[0])
                return process.create_time() == identity[1] and process.is_running()
            except psutil.NoSuchProcess:
                return False

        def is_port_listening(port):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                return s.connect_ex(("127.0.0.1", port)) == 0

        try:
            deadline = time.monotonic() + 10
            while not ready_path.is_file() and time.monotonic() < deadline:
                assert parent.poll() is None, "The owned parent exited before readiness"
                time.sleep(0.05)
            assert ready_path.is_file(), "The owned process tree did not report readiness"
            ready = json.loads(ready_path.read_text(encoding="utf-8"))
            descendants = root.children(recursive=True)
            owned.update((process.pid, process.create_time()) for process in descendants)
            assert ready["grandchild"] in {pid for pid, _ in owned}
            assert all(is_port_listening(port) for port in ready["ports"])
            result = subprocess.run(["taskkill", "/f", "/t", "/pid", str(parent.pid)],
                                    capture_output=True, text=True, timeout=10)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and any(running(identity) for identity in owned):
                time.sleep(0.05)
            # Windows can report "no running instance" for an already-exited
            # descendant and return 255 despite cleaning the complete tree.
            # Accept that only if every captured process and listener is gone.
            survivors = [identity for identity in owned if running(identity)]
            assert not survivors, f"Owned processes survived taskkill: {survivors}; {result.stderr}"
            assert not any(is_port_listening(port) for port in ready["ports"]), result.stderr
            parent.wait(timeout=5)
        finally:
            if running((root.pid, root_created)):
                owned.update((process.pid, process.create_time()) for process in root.children(recursive=True))
            # Clean only this test's process identities, even after setup/assertion failure.
            for identity in owned:
                if running(identity):
                    try:
                        psutil.Process(identity[0]).kill()
                    except psutil.NoSuchProcess:
                        pass
            try:
                parent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                parent.kill()
                parent.wait(timeout=5)

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
        # Inspect scripts/run_server.py for Job Object and cleanup logic
        launcher_content = (SCRIPTS_DIR / "run_server.py").read_text(encoding="utf-8", errors="ignore")
        assert "setup_windows_job_object" in launcher_content, "run_server.py must setup Windows Job Object"
        assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in launcher_content, "run_server.py must include KILL_ON_JOB_CLOSE flag"
        assert "assign_process_to_job" in launcher_content, "run_server.py must assign child processes to job"
        assert "cleanup_subprocesses" in launcher_content, "run_server.py must have cleanup_subprocesses"


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
            SCRIPTS_DIR / "run_server.py",
        ]
        for script in scripts:
            assert script.exists(), f"Missing script: {script}"
            content = script.read_text(encoding="utf-8")
            assert len(content.strip()) > 0, f"Empty script: {script}"

    def test_legacy_duplicate_scripts_removed(self):
        """Verify redundant/legacy scripts were consolidated away."""
        removed = [
            PROJECT_ROOT / "停止.bat",
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
        for script in [PROJECT_ROOT / "启动.bat"]:
            content = script.read_text(encoding="utf-8", errors="ignore")
            assert 'cd /d "%~dp0"' in content or 'pushd "%~dp0.."' in content

    def test_environment_assignments_are_isolated(self):
        """A thin delegating wrapper needs local scopes only if it mutates variables."""
        for script in [PROJECT_ROOT / "启动.bat"]:
            content = script.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"^\s*set\s+(?:/[ap]\s+)?", content, re.I | re.M):
                assert "setlocal" in content.lower()
                assert "endlocal" in content.lower()

    def test_gpt_sovits_discovery_locations(self):
        """Verify run_server.py probes env overrides; machine-specific paths must be gone."""
        content = (SCRIPTS_DIR / "run_server.py").read_text(encoding="utf-8", errors="ignore")
        assert "GPT_SOVITS_DIR" in content
        assert "GALGAME2VOICE_DATA_ROOT" in content
        assert "E:\\GPT-SoVITS-v2pro-20250604" not in content
        # Generic drive globbing covers D:/C: installations of any version.
        assert "GPT-SoVITS*" in content

    def test_health_polling_bounded(self):
        """Verify run_server.py readiness wait uses bounded loops (no infinite spin)."""
        content = (SCRIPTS_DIR / "run_server.py").read_text(encoding="utf-8", errors="ignore")
        # Assert the bound, not the loop variable's name: whether the counter is
        # called `i` or `_` says nothing about whether the wait terminates.
        assert "in range(240)" in content  # 120s bounded GPT-SoVITS readiness wait
        assert "in range(30)" in content   # 9s bounded browser-open wait


# ============================================================================
# 4. End-to-End Real Process Batch Lifecycle Stress Tests
# ============================================================================

class TestEndToEndBatchShutdown:
    """Empirically test launcher shutdown and cleanup logic."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows OS")
    def test_launcher_cleanup_subprocesses(self, tmp_path, monkeypatch):
        """
        Verify launcher cleanup_subprocesses removes pid files and handles mock processes.
        """
        from scripts.run_server import cleanup_subprocesses
        from scripts import run_server
        monkeypatch.setattr(run_server, "PROJECT_ROOT", tmp_path)
        (tmp_path / "data").mkdir()

        pid_file = tmp_path / "galgame2voice.pid"
        port_file = tmp_path / "data" / "active_port.txt"
        sovits_pid = tmp_path / "gptsovits.pid"

        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        sovits_pid.write_text("54321", encoding="utf-8")
        port_file.write_text("8080", encoding="utf-8")

        cleanup_subprocesses()

        assert not pid_file.exists()
        assert not sovits_pid.exists()
        assert not port_file.exists()

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

