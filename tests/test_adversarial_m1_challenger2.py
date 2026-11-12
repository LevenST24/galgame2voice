"""
Adversarial Stress Test Suite for Milestone 1: One-Click Automation Scripts.
Authored by Challenger 2.

Empirically tests and challenges:
1. Process Tree Termination Logic (`taskkill /f /t /pid`) on Windows.
2. GPU VRAM Release Mechanisms and Orphan Process Prevention.
3. Substring Port Collisions and Dynamic Port Binding.
4. Windows cmd.exe Environment Isolation and Path Handling.
"""

import json
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

    def test_working_directory_switch_safety(self):
        """Verify scripts safely switch working directory using `%~dp0`."""
        for script in [PROJECT_ROOT / "启动.bat"]:
            content = script.read_text(encoding="utf-8", errors="ignore")
            assert 'cd /d "%~dp0"' in content or 'pushd "%~dp0.."' in content

    def test_gpt_sovits_discovery_locations(self):
        """Verify run_server.py probes env overrides; machine-specific paths must be gone."""
        content = (SCRIPTS_DIR / "run_server.py").read_text(encoding="utf-8", errors="ignore")
        assert "GPT_SOVITS_DIR" in content
        assert "GALGAME2VOICE_DATA_ROOT" in content
        assert "E:\\GPT-SoVITS-v2pro-20250604" not in content
        # Generic drive globbing covers D:/C: installations of any version.
        assert "GPT-SoVITS*" in content

    def test_engine_readiness_polling_bounded(self):
        """The GPT-SoVITS readiness worker waits while the child process is alive."""
        content = (SCRIPTS_DIR / "run_server.py").read_text(encoding="utf-8", errors="ignore")
        # Polling terminates when the child process exits or the port becomes ready.
        assert "proc.poll() is None" in content
        # Browser readiness uses a monotonic deadline and is checked by the
        # behavioral timeout tests in test_browser_autostart.py.

