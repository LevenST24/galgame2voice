"""run.sh repairs a .venv that has no pip instead of failing on .venv/bin/pip."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix launcher")

# A stand-in venv python: dependency imports fail, pip is missing, and
# ensurepip cannot install it (as on Debian without python3-venv).
FAKE_PYTHON = """#!/bin/sh
case "$1" in
  -c) exit 1 ;;
  -m) exit 1 ;;
  scripts/run_server.py) echo SERVER_STARTED; exit 0 ;;
esac
exit 0
"""


def _checkout(tmp_path):
    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    (checkout / ".venv" / "bin").mkdir(parents=True)
    shutil.copy2(source / "run.sh", checkout / "run.sh")
    (checkout / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    fake = checkout / ".venv" / "bin" / "python"
    fake.write_text(FAKE_PYTHON, encoding="utf-8")
    fake.chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    os.symlink(sys.executable, tools / "python3")
    return checkout, tools


def _run(checkout, tools):
    env = {"PATH": f"{tools}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(checkout)}
    return subprocess.run(
        ["bash", str(checkout / "run.sh")], cwd=checkout, env=env,
        capture_output=True, text=True, timeout=60,
    )


def test_repairs_with_uv_when_venv_has_no_pip(tmp_path):
    checkout, tools = _checkout(tmp_path)
    log = tmp_path / "uv.log"
    uv = tools / "uv"
    uv.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n', encoding="utf-8")
    uv.chmod(0o755)

    result = _run(checkout, tools)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SERVER_STARTED" in result.stdout
    args = log.read_text(encoding="utf-8").split()
    assert args[:3] == ["pip", "install", "-r"]
    assert args[-2:] == ["--python", str(checkout / ".venv" / "bin" / "python")]


@pytest.mark.skipif(
    shutil.which("uv", path=f"/usr/bin{os.pathsep}/bin") is not None,
    reason="a system-wide uv would repair the venv",
)
def test_explains_how_to_recover_without_uv_or_pip(tmp_path):
    checkout, tools = _checkout(tmp_path)

    result = _run(checkout, tools)

    assert result.returncode == 1
    assert "请删除 .venv" in result.stdout
    assert "No such file" not in result.stderr
