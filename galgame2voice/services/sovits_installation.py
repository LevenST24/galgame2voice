"""Local engine directory hints shared by the launcher, settings and model browser."""

import os
from pathlib import Path
import sys
import tempfile


def read_sovits_directory(project_root: Path, hint_file: Path | None = None) -> Path | None:
    """Resolve quoted/BOM/relative hints against the install root, never the cwd."""
    file = hint_file if hint_file is not None else project_root / "data" / "sovits_dir.txt"
    if not file.is_file():
        return None
    value = file.read_text(encoding="utf-8-sig").strip().strip("\"'")
    if not value:
        return None
    directory = Path(value).expanduser()
    if not directory.is_absolute():
        directory = project_root / directory
    return directory.resolve()


def find_engine_python(directory: Path) -> Path | None:
    candidates = (
        (directory / "runtime" / "python.exe", directory / "runtime" / "python",
         directory / "runtime" / "python" / "bin" / "python3")
        if sys.platform == "win32"
        else (directory / "runtime" / "python" / "bin" / "python3", directory / "runtime" / "python")
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def inspect_sovits_directory(project_root: Path) -> dict:
    try:
        directory = read_sovits_directory(project_root)
        if directory is None:
            return {"directory": "", "valid": False, "runtime_available": False, "message": "尚未设置本机语音引擎目录；连接远端引擎时无需设置。"}
        valid = directory.is_dir() and (directory / "api_v2.py").is_file()
        runtime = find_engine_python(directory) is not None if valid else False
        message = "引擎目录有效，可在状态诊断中启动或重启引擎。" if valid else "目录已失效或缺少 api_v2.py，请重新选择解压后的引擎文件夹。"
        if valid and not runtime:
            message = "目录有效，但未找到引擎自带的 Python；便携版需使用包含 runtime 的完整集成包。"
        return {"directory": str(directory), "valid": valid, "runtime_available": runtime, "message": message}
    except (OSError, ValueError, UnicodeError):
        return {"directory": "", "valid": False, "runtime_available": False, "message": "无法读取引擎目录配置，请重新选择并保存。"}


def save_sovits_directory(project_root: Path, value: str) -> dict:
    value = value.strip().strip("\"'")
    if not value or "\x00" in value:
        raise ValueError("请选择有效的引擎文件夹。")
    directory = Path(value).expanduser()
    if not directory.is_absolute():
        directory = project_root / directory
    directory = directory.resolve()
    if not directory.is_dir() or not (directory / "api_v2.py").is_file():
        raise ValueError("所选文件夹需包含 api_v2.py。请先完整解压 GPT-SoVITS，再选择引擎目录。")
    if getattr(sys, "frozen", False) and find_engine_python(directory) is None:
        raise ValueError("所选引擎缺少 runtime/python.exe，请选择包含运行环境的完整集成包。")
    # Keep sibling installs portable when their containing folder is moved.
    stored = str(directory)
    if directory.is_relative_to(project_root.resolve().parent):
        stored = os.path.relpath(directory, project_root.resolve())
    data_dir = project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=data_dir,
                                         prefix=".sovits-dir-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(stored)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, data_dir / "sovits_dir.txt")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return inspect_sovits_directory(project_root)
