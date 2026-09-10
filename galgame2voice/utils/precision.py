"""
Engine Precision Calibration Store for galgame2voice.
Replaces GPU-model-name whitelists: the launcher probes the engine with a test
synthesis and records the verified is_half setting in data/precision.json,
keyed to the engine directory so swapping engine packages recalibrates.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("galgame2voice.utils.precision")

_PRECISION_ENV_VAR = "GPT_SOVITS_PRECISION"


def _cache_path(project_root: Path) -> Path:
    return Path(project_root) / "data" / "precision.json"


def read_precision_cache(project_root: Path) -> Optional[Dict[str, Any]]:
    """Returns the stored calibration dict, or None if missing/corrupt."""
    try:
        path = _cache_path(project_root)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("is_half"), bool):
            return None
        return data
    except (OSError, ValueError):
        return None


def write_precision_cache(project_root: Path, sovits_dir: str, is_half: bool) -> None:
    """Persists a verified precision calibration bound to the engine directory."""
    try:
        path = _cache_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"is_half": is_half, "sovits_dir": str(sovits_dir), "verified_at": int(time.time())}),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("Could not write precision cache: %s", exc)


def resolve_initial_is_half(project_root: Path, sovits_dir: Path, environ: Optional[Dict[str, str]] = None) -> tuple[bool, str]:
    """
    Decides the initial is_half setting for a fresh engine launch.
    Priority: GPT_SOVITS_PRECISION env override > verified cache for this engine dir > FP16 default.
    Returns (is_half, source) where source is "env" | "cache" | "default".
    """
    env = environ if environ is not None else os.environ
    override = str(env.get(_PRECISION_ENV_VAR, "")).strip().lower()
    if override in ("fp16", "half", "true", "1"):
        return True, "env"
    if override in ("fp32", "float32", "false", "0"):
        return False, "env"

    cache = read_precision_cache(project_root)
    if cache is not None and cache.get("sovits_dir") == str(sovits_dir):
        return bool(cache["is_half"]), "cache"

    return True, "default"
