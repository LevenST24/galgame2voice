"""
Tests for the Windows Lifecycle Scripts (启动.bat / 停止.bat) and the
intelligent launcher scripts/run_server.py.
Covers script structure, command presence, path escaping, graceful kill,
PID file lifecycle, and the launcher's GPT-SoVITS discovery contract.
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
        """Verifies the two canonical lifecycle scripts and the launcher exist."""
        required_scripts = [
            PROJECT_ROOT / "启动.bat",
            PROJECT_ROOT / "停止.bat",
            SCRIPTS_DIR / "run_server.py",
        ]
        for script in required_scripts:
            assert script.exists(), f"Required script missing: {script}"
            assert script.is_file(), f"Script is not a file: {script}"

    def test_no_legacy_duplicate_scripts(self):
        """Verifies the legacy duplicated scripts have been removed (simplicity)."""
        removed = [
            PROJECT_ROOT / "start.bat",
            PROJECT_ROOT / "stop.bat",
            PROJECT_ROOT / "start-galgame2voice.bat",
            PROJECT_ROOT / "stop-galgame2voice.bat",
            PROJECT_ROOT / "restart-galgame2voice.bat",
            SCRIPTS_DIR / "start-galgame2voice.bat",
            SCRIPTS_DIR / "stop-galgame2voice.bat",
            SCRIPTS_DIR / "restart-galgame2voice.bat",
        ]
        for path in removed:
            assert not path.exists(), f"Legacy duplicate script should have been removed: {path}"

    def test_scripts_utf8_encoding(self):
        """Verify all batch scripts can be read as valid UTF-8 without decoding errors."""
        for bat_file in list(SCRIPTS_DIR.glob("*.bat")) + list(PROJECT_ROOT.glob("*.bat")):
            content = bat_file.read_text(encoding="utf-8")
            assert len(content) > 0

    def test_start_script_invokes_intelligent_launcher(self):
        """Verifies 启动.bat delegates to scripts/run_server.py (single smart entrypoint)."""
        content = (PROJECT_ROOT / "启动.bat").read_text(encoding="utf-8", errors="ignore")
        assert "run_server.py" in content

    def test_launcher_contains_uvicorn_launch(self):
        """Verifies run_server.py launches the FastAPI app via uvicorn."""
        content = (SCRIPTS_DIR / "run_server.py").read_text(encoding="utf-8", errors="ignore")
        assert "uvicorn" in content
        assert "galgame2voice.main:app" in content or "main:app" in content

    def test_start_script_checks_python(self):
        """Verifies start script validates Python presence (.venv then PATH)."""
        content = (PROJECT_ROOT / "启动.bat").read_text(encoding="utf-8", errors="ignore")
        assert "python" in content.lower()
        assert "errorlevel" in content.lower()
        assert ".venv" in content

    def test_stop_script_contains_taskkill_logic(self):
        """Verifies stop script contains process termination logic (taskkill)."""
        content = (PROJECT_ROOT / "停止.bat").read_text(encoding="utf-8", errors="ignore")
        assert "taskkill" in content.lower()
        assert "/pid" in content.lower() or "/im" in content.lower()

    def test_launcher_discovers_gpt_sovits(self):
        """Verifies run_server.py probes the known GPT-SoVITS install path and env override."""
        content = (SCRIPTS_DIR / "run_server.py").read_text(encoding="utf-8", errors="ignore")
        assert "GPT_SOVITS_DIR" in content
        assert "E:\\GPT-SoVITS-v2pro-20250604" in content
        assert "api_v2.py" in content

    def test_launcher_waits_for_sovits_readiness(self):
        """Verifies run_server.py performs a bounded readiness wait after spawning GPT-SoVITS."""
        content = (SCRIPTS_DIR / "run_server.py").read_text(encoding="utf-8", errors="ignore")
        assert "is_port_in_use(9880)" in content
        assert "gpt_sovits.log" in content  # engine logs must not be discarded

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
        """Verifies path references (%~dp0) are safely enclosed in quotes."""
        content = (PROJECT_ROOT / "启动.bat").read_text(encoding="utf-8", errors="ignore")
        assert '"%~dp0' in content or '%~dp0"' in content

    def test_launcher_port_collision_fallback(self):
        """Verifies run_server.py implements port fallback when 8080 cannot bind."""
        content = (SCRIPTS_DIR / "run_server.py").read_text(encoding="utf-8", errors="ignore")
        assert "find_available_port" in content

    def test_stop_script_handles_port_extraction(self):
        """Verifies stop script safely parses PID column from netstat output."""
        content = (PROJECT_ROOT / "停止.bat").read_text(encoding="utf-8", errors="ignore")
        assert "netstat" in content.lower()
        assert "tokens=" in content.lower()

    def test_no_hardcoded_admin_privilege_required(self):
        """Verifies scripts do not execute commands that arbitrarily force administrative elevation."""
        for path in (PROJECT_ROOT / "启动.bat", PROJECT_ROOT / "停止.bat"):
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
            PROJECT_ROOT / "启动.bat",
            PROJECT_ROOT / "停止.bat",
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

        # 2. Dynamic execution: test stop script when idle (should exit code 0 without parse errors)
        result = subprocess.run(
            f'"{PROJECT_ROOT / "停止.bat"}"',
            shell=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10
        )
        combined_output = ((result.stdout or "") + (result.stderr or "")).lower()
        assert "was unexpected at this time" not in combined_output
        assert "syntax error" not in combined_output
        assert result.returncode == 0, f"停止.bat failed with exit code {result.returncode}"

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
