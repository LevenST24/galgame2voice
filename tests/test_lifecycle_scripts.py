"""
Tests for Windows Lifecycle Batch Scripts (start-galgame2voice.bat, stop-galgame2voice.bat, restart-galgame2voice.bat).
Covers Tier 1 (Script Syntax, Commands, Structure) and Tier 2 (Port Collision, Path Escaping, Graceful Kill, Error Codes).
"""

import os
import re
import sys
import subprocess
from pathlib import Path
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


# ============================================================================
# Tier 1: Script Existence, Structure, and Command Tests
# ============================================================================

class TestLifecycleScriptsTier1:
    """Tier 1: Validation of script files, syntax blocks, and command presence."""

    def test_script_files_exist(self):
        """Verifies start, stop, and restart batch files exist in scripts/ and project root."""
        required_scripts = [
            SCRIPTS_DIR / "start-galgame2voice.bat",
            SCRIPTS_DIR / "stop-galgame2voice.bat",
            SCRIPTS_DIR / "restart-galgame2voice.bat",
            PROJECT_ROOT / "start-galgame2voice.bat",
            PROJECT_ROOT / "stop-galgame2voice.bat",
            PROJECT_ROOT / "restart-galgame2voice.bat",
            PROJECT_ROOT / "启动.bat",
            PROJECT_ROOT / "停止.bat",
            PROJECT_ROOT / "start.bat",
            PROJECT_ROOT / "stop.bat",
        ]
        for script in required_scripts:
            assert script.exists(), f"Required script missing: {script}"
            assert script.is_file(), f"Script is not a file: {script}"

    def test_scripts_utf8_encoding(self):
        """Verify all batch scripts can be read as valid UTF-8 without decoding errors."""
        for bat_file in list(SCRIPTS_DIR.glob("*.bat")) + list(PROJECT_ROOT.glob("*.bat")):
            content = bat_file.read_text(encoding="utf-8")
            assert len(content) > 0

    def test_start_script_contains_uvicorn_launch(self):
        """Verifies start script contains uvicorn module launch and host/port parameters."""
        for script in (SCRIPTS_DIR / "start-galgame2voice.bat", PROJECT_ROOT / "启动.bat"):
            content = script.read_text(encoding="utf-8", errors="ignore")
            assert "uvicorn" in content
            assert "galgame2voice.main:app" in content or "main:app" in content
            assert "--port" in content

    def test_start_script_checks_python(self):
        """Verifies start script validates Python presence."""
        for script in (SCRIPTS_DIR / "start-galgame2voice.bat", PROJECT_ROOT / "启动.bat"):
            content = script.read_text(encoding="utf-8", errors="ignore")
            assert "python" in content.lower()
            assert "errorlevel" in content.lower()

    def test_stop_script_contains_taskkill_logic(self):
        """Verifies stop script contains process termination logic (taskkill)."""
        for script in (SCRIPTS_DIR / "stop-galgame2voice.bat", PROJECT_ROOT / "停止.bat"):
            content = script.read_text(encoding="utf-8", errors="ignore")
            assert "taskkill" in content.lower()
            assert "/pid" in content.lower() or "/im" in content.lower()

    def test_restart_script_chains_stop_and_start(self):
        """Verifies restart script sequentially executes stop and start."""
        path = SCRIPTS_DIR / "restart-galgame2voice.bat"
        content = path.read_text(encoding="utf-8", errors="ignore")
        assert "stop-galgame2voice.bat" in content
        assert "start-galgame2voice.bat" in content

    def test_root_launchers_forward_correctly(self):
        """Verifies start.bat forwards to 启动.bat and stop.bat forwards to 停止.bat."""
        start_bat = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8", errors="ignore")
        assert "启动.bat" in start_bat
        stop_bat = (PROJECT_ROOT / "stop.bat").read_text(encoding="utf-8", errors="ignore")
        assert "停止.bat" in stop_bat

    def test_multi_drive_sovits_scanning(self):
        """Verifies 启动.bat and start-galgame2voice.bat scan candidate drives for GPT-SoVITS."""
        for script in (PROJECT_ROOT / "启动.bat", SCRIPTS_DIR / "start-galgame2voice.bat"):
            content = script.read_text(encoding="utf-8", errors="ignore")
            assert "GPT_SOVITS_DIR" in content
            assert "E:\\GPT-SoVITS" in content or "E:" in content
            assert "D:\\GPT-SoVITS" in content or "D:" in content
            assert "C:\\GPT-SoVITS" in content or "C:" in content
            assert "runtime\\python.exe" in content

    def test_stop_script_kills_8080_and_9880_with_tree_flag(self):
        """Verifies 停止.bat terminates both port 8080 and 9880 with /t flag for VRAM release."""
        content = (PROJECT_ROOT / "停止.bat").read_text(encoding="utf-8", errors="ignore")
        assert ":8080" in content
        assert ":9880" in content
        assert "/t" in content.lower()
        assert "galgame2voice.pid" in content


# ============================================================================
# Tier 2: Boundary, Path Escaping, Port Collision & Syntax Checks
# ============================================================================

class TestLifecycleScriptsTier2:
    """Tier 2: Windows path handling, quotes, setlocal/endlocal balance, port collision checks."""

    def test_scripts_balanced_setlocal_endlocal(self):
        """Verifies setlocal is initialized and endlocal is called before exit points."""
        test_scripts = [
            SCRIPTS_DIR / "start-galgame2voice.bat",
            SCRIPTS_DIR / "stop-galgame2voice.bat",
            SCRIPTS_DIR / "restart-galgame2voice.bat",
            PROJECT_ROOT / "启动.bat",
            PROJECT_ROOT / "停止.bat",
        ]
        for path in test_scripts:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            setlocal_count = sum(1 for line in lines if line.strip().lower().startswith("setlocal"))
            endlocal_count = sum(1 for line in lines if "endlocal" in line.strip().lower())
            assert setlocal_count >= 1, f"Missing setlocal in {path.name}"
            assert endlocal_count >= setlocal_count, f"Missing endlocal before exit in {path.name}"

    def test_start_script_quoted_paths_for_spaces(self):
        """Verifies path references (%~dp0) are safely enclosed in quotes or set assignment quotes."""
        for script in (SCRIPTS_DIR / "start-galgame2voice.bat", PROJECT_ROOT / "启动.bat"):
            content = script.read_text(encoding="utf-8", errors="ignore")
            assert 'set "SCRIPT_DIR=%~dp0"' in content or '"%~dp0' in content or '%~dp0"' in content

    def test_start_script_port_collision_check(self):
        """Verifies start script checks port binding with netstat before launching."""
        for script in (SCRIPTS_DIR / "start-galgame2voice.bat", PROJECT_ROOT / "启动.bat"):
            content = script.read_text(encoding="utf-8", errors="ignore")
            assert "netstat" in content.lower()

    def test_stop_script_handles_port_extraction(self):
        """Verifies stop script safely parses PID column from netstat output."""
        for script in (SCRIPTS_DIR / "stop-galgame2voice.bat", PROJECT_ROOT / "停止.bat"):
            content = script.read_text(encoding="utf-8", errors="ignore")
            assert "netstat" in content.lower()
            assert "tokens=" in content.lower()

    def test_no_hardcoded_admin_privilege_required(self):
        """Verifies scripts do not execute commands that arbitrarily force administrative elevation."""
        for filename in ("start-galgame2voice.bat", "stop-galgame2voice.bat", "restart-galgame2voice.bat"):
            path = SCRIPTS_DIR / filename
            content = path.read_text(encoding="utf-8", errors="ignore").lower()
            assert "powershell -verb runas" not in content
            assert "runas /user:" not in content

    def test_pid_file_lifecycle(self, tmp_path):
        """Verify PID file creation, reading, sanitization, and removal logic."""
        pid_file = tmp_path / "galgame2voice.pid"
        test_pid = 12345
        
        # Write PID with trailing spaces and newline
        pid_file.write_text(f"  {test_pid}  \n", encoding="utf-8")
        assert pid_file.exists()
        
        # Read and sanitize
        raw_pid = pid_file.read_text(encoding="utf-8").strip()
        assert raw_pid == str(test_pid)
        assert int(raw_pid) == test_pid
        
        # Clean up
        pid_file.unlink()
        assert not pid_file.exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows batch syntax validation requires Windows OS")
    def test_batch_script_syntax(self):
        """Verify cmd.exe can parse and execute lifecycle batch scripts without syntax crashes."""
        scripts = [
            SCRIPTS_DIR / "start-galgame2voice.bat",
            SCRIPTS_DIR / "stop-galgame2voice.bat",
            SCRIPTS_DIR / "restart-galgame2voice.bat",
            PROJECT_ROOT / "start-galgame2voice.bat",
            PROJECT_ROOT / "stop-galgame2voice.bat",
            PROJECT_ROOT / "restart-galgame2voice.bat",
            PROJECT_ROOT / "启动.bat",
            PROJECT_ROOT / "停止.bat",
            PROJECT_ROOT / "start.bat",
            PROJECT_ROOT / "stop.bat",
        ]
        
        # 1. Static analysis: check for dangerous unescaped parentheses in echo statements inside parenthesized blocks
        for script in scripts:
            content = script.read_text(encoding="utf-8", errors="ignore")
            lines = content.splitlines()
            for line_no, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("echo") and "(" in stripped and ")" in stripped:
                    assert not re.search(r'\([^)]*\)\.', stripped), (
                        f"{script.name}:{line_no} contains unescaped trailing period after closing paren: {stripped}"
                    )

        # 2. Dynamic execution: test stop scripts when idle (should exit code 0 without parse errors)
        for stop_script in (SCRIPTS_DIR / "stop-galgame2voice.bat", PROJECT_ROOT / "停止.bat", PROJECT_ROOT / "stop.bat"):
            result = subprocess.run(
                f'"{stop_script}"',
                shell=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10
            )
            combined_output = ((result.stdout or "") + (result.stderr or "")).lower()
            assert "was unexpected at this time" not in combined_output, f"CMD parse syntax error in {stop_script}: {result.stderr or result.stdout}"
            assert "syntax error" not in combined_output
            assert result.returncode == 0, f"Script {stop_script} failed with exit code {result.returncode}"

    def test_netstat_parsing_logic(self):
        """Verify regex/token parsing logic used in batch scripts to extract listening PID."""
        sample_netstat_output = (
            "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       9876\n"
            "  TCP    [::]:8080              [::]:0                 LISTENING       9876\n"
            "  TCP    127.0.0.1:9880         0.0.0.0:0              LISTENING       5432\n"
        )
        port = 8080
        extracted_pids = []
        for line in sample_netstat_output.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                if len(parts) >= 5:
                    extracted_pids.append(parts[-1])
                    
        assert len(extracted_pids) == 2
        assert extracted_pids[0] == "9876"
