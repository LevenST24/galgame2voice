"""Writable installation root shared by source and Windows portable builds."""

import sys
from pathlib import Path


def get_install_root() -> Path:
    """Keep portable user data beside the executable, outside bundled libraries."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]
