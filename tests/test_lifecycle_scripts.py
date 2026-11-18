"""
Tests for the Windows Lifecycle Scripts (启动.bat / 停止.bat) and the
intelligent launcher scripts/run_server.py.
Covers script structure, command presence, path escaping, graceful kill,
PID file lifecycle, and the launcher's GPT-SoVITS discovery contract.
"""

import os
import json
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
        """Verifies the sole canonical lifecycle script and the launcher exist."""
        required_scripts = [
            PROJECT_ROOT / "启动.bat",
            SCRIPTS_DIR / "run_server.py",
        ]
        for script in required_scripts:
            assert script.exists(), f"Required script missing: {script}"
            assert script.is_file(), f"Script is not a file: {script}"

    def test_no_legacy_duplicate_scripts(self):
        """Verifies redundant/legacy scripts have been removed for pure single-file simplicity."""
        removed = [
            PROJECT_ROOT / "停止.bat",
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
            assert not path.exists(), f"Redundant/legacy script should be removed: {path}"

    def test_scripts_utf8_encoding(self):
        """Verify all batch scripts can be read as valid UTF-8 without decoding errors."""
        for bat_file in list(SCRIPTS_DIR.glob("*.bat")) + list(PROJECT_ROOT.glob("*.bat")):
            content = bat_file.read_text(encoding="utf-8")
            assert len(content) > 0
        start_bat = (PROJECT_ROOT / "启动.bat").read_text(encoding="utf-8")
        assert "chcp 65001" in start_bat

    @pytest.mark.skipif(sys.platform != "win32", reason="executes the Windows batch entry point")
    @pytest.mark.requires_piped_subprocess
    def test_start_script_delegates_python_and_preserves_arguments(self, tmp_path):
        """Runs the real wrapper against a stub launcher, never the actual server."""
        root = tmp_path / "launch ! folder"
        (root / "scripts").mkdir(parents=True)
        start_script = root / "启动.bat"
        start_script.write_bytes((PROJECT_ROOT / "启动.bat").read_bytes())
        (root / "scripts" / "run_server.py").write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path('launch.json').write_text(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}), encoding='utf-8')\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        args = ["--fp32", "--gpt-sovits-dir", "voice! with spaces"]
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "call", str(start_script), *args],
            cwd=tmp_path, env=env, capture_output=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == 0, result.stderr
        recorded = json.loads((root / "launch.json").read_text(encoding="utf-8"))
        assert recorded["argv"] == args
        assert Path(recorded["cwd"]) == root
