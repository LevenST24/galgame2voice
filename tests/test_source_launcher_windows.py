"""Exercise the source checkout launcher without touching a real installation."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest


@pytest.mark.requires_piped_subprocess
@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch launcher")
@pytest.mark.parametrize("has_venv", [True, False])
def test_source_launcher_selects_project_python_and_preserves_arguments(tmp_path, has_venv):
    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "模拟 源码 ! checkout"
    checkout.mkdir()
    shutil.copy2(source / "启动.bat", checkout / "launch.cmd")
    (checkout / "scripts").mkdir()
    (checkout / "scripts" / "run_server.py").write_text(
        "import json, sys\nprint('LOCAL_PYTHON', json.dumps(sys.argv[1:]))\n", encoding="utf-8"
    )
    if has_venv:
        venv.EnvBuilder(with_pip=False).create(checkout / ".venv")
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    (fallback / "python.cmd").write_text("@echo FALLBACK_PYTHON %*\r\n", encoding="ascii")
    env = os.environ.copy()
    system32 = Path(env.get("SystemRoot") or env["SYSTEMROOT"]) / "System32"
    env["PATH"] = str(fallback) + os.pathsep + str(system32)
    env["NoDefaultCurrentDirectoryInExePath"] = "1"
    result = subprocess.run(
        [str(system32 / "cmd.exe"), "/d", "/c", "call", r".\launch.cmd", "--probe", "argument with spaces"],
        cwd=checkout, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    if has_venv:
        assert "FALLBACK_PYTHON" not in result.stdout
        assert 'LOCAL_PYTHON ' + json.dumps(["--probe", "argument with spaces"]) in result.stdout
    else:
        assert 'FALLBACK_PYTHON scripts\\run_server.py --probe "argument with spaces"' in result.stdout


@pytest.mark.requires_piped_subprocess
@pytest.mark.skipif(sys.platform != "win32", reason="Windows source launcher")
@pytest.mark.parametrize("missing", ["pydantic", "pydantic_settings", "pydantic_old", "pydantic_settings_old",
                                     "local_module"])
def test_incomplete_source_environment_prints_dependency_recovery_before_imports(tmp_path, missing):
    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "空白环境 ! checkout"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy2(source / "scripts" / "run_server.py", checkout / "scripts" / "run_server.py")
    shutil.copytree(source / "galgame2voice", checkout / "galgame2voice",
                    ignore=shutil.ignore_patterns("static", "__pycache__", "*.pyc"))
    if missing == "local_module":
        (checkout / "galgame2voice" / "config.py").unlink()
    venv.EnvBuilder(with_pip=False).create(checkout / ".venv")
    if missing in {"pydantic_settings", "pydantic_old", "pydantic_settings_old"}:
        package = checkout / ".venv" / "Lib" / "site-packages" / "pydantic"
        package.mkdir()
        contents = "Field = object\n" if missing == "pydantic_old" else "AliasChoices = Field = object\n"
        (package / "__init__.py").write_text(contents, encoding="utf-8")
        if missing == "pydantic_settings_old":
            settings = checkout / ".venv" / "Lib" / "site-packages" / "pydantic_settings"
            settings.mkdir()
            (settings / "__init__.py").write_text("# incompatible version\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [str(checkout / ".venv" / "Scripts" / "python.exe"), "scripts/run_server.py", "--check-only"],
        cwd=checkout, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    if missing == "local_module":
        assert "Traceback" in output and "galgame2voice.config" in output
        assert "uv sync --frozen" not in output
        return
    expected_module = missing.removesuffix("_old")
    assert "依赖" in output and expected_module in output and "uv sync --frozen" in output
    assert "Traceback" not in output
    if missing == "pydantic_old":
        imported = subprocess.run(
            [str(checkout / ".venv" / "Scripts" / "python.exe"), "-c", "import scripts.run_server"],
            cwd=checkout, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, check=False,
        )
        assert imported.returncode != 0 and "ImportError" in imported.stderr
