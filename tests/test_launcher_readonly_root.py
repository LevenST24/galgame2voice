"""The source launcher explains an unwritable program folder in Chinese instead of a traceback."""

import os
import subprocess
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
RUN_SERVER = ROOT / "scripts" / "run_server.py"


def _run_shim(tmp_path: Path, patch: str) -> str:
    shim = tmp_path / "shim.py"
    shim.write_text(f"{patch}sys.argv = [{str(RUN_SERVER)!r}, '--check-only']\nrunpy.run_path({str(RUN_SERVER)!r}, run_name='__main__')\n", encoding="utf-8")
    env = os.environ.copy()
    if sys.platform == "win32": env.update({"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"})
    result = subprocess.run([sys.executable, str(shim)], cwd=str(tmp_path), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    text = result.stdout + result.stderr
    assert result.returncode == 1 and "Traceback" not in text, text[-1500:]
    return text


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("mode", ["missing", "existing"])
def test_unwritable_program_folder_prints_chinese_hint(tmp_path, mode):
    patch = (
        f"import pathlib, runpy, sys\norig, root = pathlib.Path.mkdir, pathlib.Path({str(ROOT)!r})\n"
        "def mkdir(self, *a, **k):\n"
        "    if self.parent == root and self.name in ('logs', 'data', 'audio'): raise PermissionError(13, '拒绝访问。', str(self))\n"
        "    return orig(self, *a, **k)\npathlib.Path.mkdir = mkdir\n"
    ) if mode == "missing" else (
        f"import os, pathlib, runpy, sys\norig, dirs = os.open, {{pathlib.Path({str(ROOT)!r}) / d for d in ('logs', 'data', 'audio')}}\n"
        "def deny_open(path, *a, **k):\n"
        "    if pathlib.Path(os.fsdecode(path)).parent in dirs: raise PermissionError(13, '拒绝访问。', str(path))\n"
        "    return orig(path, *a, **k)\nos.open = deny_open\n"
    )
    text = _run_shim(tmp_path, patch)
    assert "无法在程序文件夹里创建或写入" in text and "可以写入的位置" in text


@pytest.mark.requires_piped_subprocess
def test_missing_dependency_hint_uses_utf8_on_windows(tmp_path):
    patch = (
        "import builtins, runpy, sys\norig = builtins.__import__\n"
        "def blocked(name, *a, **k):\n"
        "    if name == 'pydantic' or name.startswith('pydantic.'): raise ModuleNotFoundError(\"No module named 'pydantic'\", name='pydantic')\n"
        "    return orig(name, *a, **k)\nbuiltins.__import__ = blocked\n"
    )
    text = _run_shim(tmp_path, patch)
    assert "运行依赖缺少" in text and "pydantic" in text
