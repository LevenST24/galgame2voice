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
from typing import Any, Dict, Optional, Union

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


def find_sovits_yaml_path(sovits_dir: Optional[Union[str, Path]]) -> Optional[Path]:
    """Locates the tts_infer.yaml configuration inside the GPT-SoVITS directory."""
    if not sovits_dir:
        return None
    s_dir = Path(sovits_dir)
    candidates = [
        s_dir / "GPT_SoVITS" / "configs" / "tts_infer.yaml",
        s_dir / "configs" / "tts_infer.yaml",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def read_sovits_yaml_is_half(sovits_dir: Optional[Union[str, Path]]) -> Optional[bool]:
    """Reads the custom.is_half setting directly from tts_infer.yaml."""
    yaml_path = find_sovits_yaml_path(sovits_dir)
    if not yaml_path:
        return None
    try:
        content = yaml_path.read_text(encoding="utf-8")
        import re
        m = re.search(r"custom:\s*\n(?:[ \t]+[^\n]*\n)*?[ \t]+is_half:\s*(true|false|True|False)", content)
        if m:
            return m.group(1).lower() == "true"
        m2 = re.search(r"is_half:\s*(true|false|True|False)", content)
        if m2:
            return m2.group(1).lower() == "true"
    except Exception as e:
        logger.debug("Could not read is_half from %s: %s", yaml_path, e)
    return None


def write_sovits_yaml_is_half(sovits_dir: Optional[Union[str, Path]], is_half: bool) -> Optional[Path]:
    """
    Physically synchronizes custom.is_half in tts_infer.yaml on disk.
    GPT-SoVITS api_v2.py ONLY determines precision from this YAML file;
    setting OS environment variables alone has zero effect.
    """
    yaml_path = find_sovits_yaml_path(sovits_dir)
    if not yaml_path:
        return None
    try:
        content = yaml_path.read_text(encoding="utf-8")
        target_val = "true" if is_half else "false"
        import re
        pattern = r"(custom:\s*\n(?:[ \t]+[^\n]*\n)*?[ \t]+is_half:\s*)(?:true|false|True|False)"
        new_content, count = re.subn(pattern, rf"\g<1>{target_val}", content, count=1)
        if count == 0:
            if "custom:" in content:
                new_content = content.replace("custom:\n", f"custom:\n  is_half: {target_val}\n", 1)
            else:
                new_content = f"custom:\n  is_half: {target_val}\n" + content
        yaml_path.write_text(new_content, encoding="utf-8")
        logger.info("Synchronized %s with is_half=%s", yaml_path, is_half)
        return yaml_path
    except Exception as e:
        logger.warning("Failed to write is_half=%s to %s: %s", is_half, yaml_path, e)
        return None


def resolve_initial_is_half(project_root: Path, sovits_dir: Path, environ: Optional[Dict[str, str]] = None) -> tuple[bool, str]:
    """
    Decides the initial is_half setting for a fresh engine launch.
    Priority: GPT_SOVITS_PRECISION env override > verified cache for this engine dir > existing YAML setting > FP16 default.
    Returns (is_half, source) where source is "env" | "cache" | "yaml" | "default".
    """
    env = environ if environ is not None else os.environ
    override = str(env.get(_PRECISION_ENV_VAR, "")).strip().lower()
    if override in ("fp16", "half", "true", "1"):
        return True, "env"
    if override in ("fp32", "float32", "false", "0"):
        return False, "env"

    cache = read_precision_cache(project_root)
    if cache is not None:
        cached_dir = str(cache.get("sovits_dir", "")).strip()
        try:
            if not cached_dir or Path(cached_dir).resolve() == Path(sovits_dir).resolve():
                return bool(cache["is_half"]), "cache"
        except Exception:
            if cached_dir == str(sovits_dir):
                return bool(cache["is_half"]), "cache"

    yaml_half = read_sovits_yaml_is_half(sovits_dir)
    if yaml_half is not None and yaml_half is False:
        return False, "yaml"

    return True, "default"
