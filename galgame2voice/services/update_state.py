"""
Persistent state management for pending frontend build tasks across restarts.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("galgame2voice.services.update_state")

PENDING_UPDATE_FILE = ".update_pending.json"


def get_pending_state_path(data_dir: Path) -> Path:
    """Returns the path to the pending update state file inside data_dir."""
    return data_dir / PENDING_UPDATE_FILE


def load_update_pending_state(data_dir: Path) -> dict | None:
    """
    Loads pending update state from data_dir/.update_pending.json.
    Returns None if the file does not exist or cannot be parsed.
    """
    path = get_pending_state_path(data_dir)
    if not path.is_file():
        return None

    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
        if isinstance(data, dict) and data.get("build_pending"):
            return data
        return None
    except Exception as exc:
        logger.warning("Failed to read pending update state from %s: %s", path, exc)
        return None


def save_update_pending_state(data_dir: Path, state: dict) -> bool:
    """
    Persists pending update state to data_dir/.update_pending.json atomically.
    Ensures data_dir exists before writing.
    """
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        path = get_pending_state_path(data_dir)
        payload = dict(state)
        payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())

        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)
        return True
    except Exception as exc:
        logger.error("Failed to save pending update state to %s: %s", data_dir, exc)
        return False


def clear_update_pending_state(data_dir: Path) -> bool:
    """Removes the pending update state file if present. Returns True if file is successfully cleared."""
    path = get_pending_state_path(data_dir)
    try:
        if path.is_file():
            path.unlink(missing_ok=True)
        return not path.is_file()
    except Exception as exc:
        logger.warning("Failed to remove pending update state file %s: %s", path, exc)
        return False
