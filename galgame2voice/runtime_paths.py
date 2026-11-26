"""Writable installation root shared by source and Windows portable builds."""

import shutil
import sys
from pathlib import Path


def get_install_root() -> Path:
    """Keep portable user data beside the executable, outside bundled libraries."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


_LOW_DISK_BYTES = 200 * 1024 * 1024


def low_disk_hint(root: Path) -> str | None:
    """A startup failure with almost no free space left is the full disk, not a broken install."""
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        return None
    if free >= _LOW_DISK_BYTES:
        return None
    return (
        f"程序所在的磁盘只剩 {free // (1024 * 1024)} MB 可用空间，程序没法保存数据。"
        "请先清理这个磁盘（例如清空回收站、删除不用的大文件），再重新双击「启动.bat」。"
    )
