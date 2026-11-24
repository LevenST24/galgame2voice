"""The source launcher explains an unwritable program folder in Chinese instead of a traceback."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_SERVER = ROOT / "scripts" / "run_server.py"


def test_unwritable_program_folder_prints_chinese_hint(tmp_path):
    shim = tmp_path / "shim.py"
    shim.write_text(
        "import pathlib, runpy, sys\n"
        "orig = pathlib.Path.mkdir\n"
        f"root = pathlib.Path({str(ROOT)!r})\n"
        "def mkdir(self, *a, **k):\n"
        "    if self.parent == root and self.name in ('logs', 'data', 'audio'):\n"
        "        raise PermissionError(13, '拒绝访问。', str(self))\n"
        "    return orig(self, *a, **k)\n"
        "pathlib.Path.mkdir = mkdir\n"
        f"sys.argv = [{str(RUN_SERVER)!r}, '--check-only']\n"
        f"runpy.run_path({str(RUN_SERVER)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(shim)], cwd=str(tmp_path), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    text = result.stdout + result.stderr
    assert result.returncode == 1
    assert "Traceback" not in text, text[-1500:]
    assert "无法在程序文件夹里创建" in text
    assert "可以写入的位置" in text
