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


def write_precision_cache(
    project_root: Path,
    sovits_dir: str,
    is_half: bool,
    device: str = "cuda",
) -> None:
    """Persists a verified precision calibration and device bound to the engine directory."""
    try:
        path = _cache_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "is_half": is_half,
                "device": device.lower(),
                "sovits_dir": str(sovits_dir),
                "verified_at": int(time.time()),
            }),
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


def read_sovits_yaml_device(sovits_dir: Optional[Union[str, Path]]) -> Optional[str]:
    """Reads the custom.device setting ('cuda' | 'cpu') directly from tts_infer.yaml."""
    yaml_path = find_sovits_yaml_path(sovits_dir)
    if not yaml_path:
        return None
    try:
        content = yaml_path.read_text(encoding="utf-8")
        import re
        m = re.search(r"custom:\s*\n(?:[ \t]+[^\n]*\n)*?[ \t]+device:\s*([a-zA-Z0-9_]+)", content)
        if m:
            return m.group(1).lower().strip()
        m2 = re.search(r"device:\s*([a-zA-Z0-9_]+)", content)
        if m2:
            return m2.group(1).lower().strip()
    except Exception as e:
        logger.debug("Could not read device from %s: %s", yaml_path, e)
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


def write_sovits_yaml_config(
    sovits_dir: Optional[Union[str, Path]],
    is_half: bool,
    device: Optional[str] = None,
) -> Optional[Path]:
    """
    Physically synchronizes custom.is_half and custom.device in tts_infer.yaml on disk.
    GPT-SoVITS api_v2.py ONLY determines precision and compute device from this YAML file;
    setting OS environment variables alone has zero effect.
    """
    yaml_path = find_sovits_yaml_path(sovits_dir)
    if not yaml_path:
        return None
    try:
        content = yaml_path.read_text(encoding="utf-8")
        target_half = "true" if is_half else "false"
        import re

        # 1. Synchronize is_half
        pattern_half = r"(custom:\s*\n(?:[ \t]+[^\n]*\n)*?[ \t]+is_half:\s*)(?:true|false|True|False)"
        content, count_half = re.subn(pattern_half, rf"\g<1>{target_half}", content, count=1)
        if count_half == 0:
            if "custom:" in content:
                content = content.replace("custom:\n", f"custom:\n  is_half: {target_half}\n", 1)
            else:
                content = f"custom:\n  is_half: {target_half}\n" + content

        # 2. Synchronize device if requested
        if device:
            dev_target = device.lower().strip()
            pattern_dev = r"(custom:\s*\n(?:[ \t]+[^\n]*\n)*?[ \t]+device:\s*)(?:cuda|cpu|mps|auto|[a-zA-Z0-9_]+)"
            content, count_dev = re.subn(pattern_dev, rf"\g<1>{dev_target}", content, count=1)
            if count_dev == 0:
                if "custom:" in content:
                    content = content.replace("custom:\n", f"custom:\n  device: {dev_target}\n", 1)
                else:
                    content = f"custom:\n  device: {dev_target}\n" + content

        yaml_path.write_text(content, encoding="utf-8")
        logger.info("Synchronized %s with is_half=%s, device=%s", yaml_path, is_half, device)
        return yaml_path
    except Exception as e:
        logger.warning("Failed to write config to %s: %s", yaml_path, e)
        return None


def write_sovits_yaml_is_half(sovits_dir: Optional[Union[str, Path]], is_half: bool) -> Optional[Path]:
    """Compatibility helper for synchronizing custom.is_half."""
    return write_sovits_yaml_config(sovits_dir, is_half=is_half)


def read_db_precision(project_root: Path) -> Optional[str]:
    """Reads inference_precision ('auto' | 'fp16' | 'fp32' | 'cpu') from SQLite settings table."""
    db_path = project_root / "data" / "galgame2voice.db"
    if not db_path.is_file():
        return None
    try:
        import sqlite3
        with sqlite3.connect(str(db_path), timeout=2.0) as conn:
            cur = conn.cursor()
            cur.execute("SELECT inference_precision FROM settings WHERE id = 1 LIMIT 1;")
            row = cur.fetchone()
            if row and row[0]:
                return str(row[0]).strip().lower()
    except Exception as exc:
        logger.debug("Could not read inference_precision from DB: %s", exc)
    return None


def resolve_initial_device_and_half(
    project_root: Path,
    sovits_dir: Path,
    environ: Optional[Dict[str, str]] = None,
) -> tuple[str, bool, str]:
    """
    Decides the initial device ('cuda' | 'cpu') and is_half setting for engine launch.
    Priority: CLI/ENV override > SQLite settings > verified cache > existing YAML setting > hardware default.
    Returns (device, is_half, source) where source is "env" | "db" | "cache" | "yaml" | "default".
    """
    env = environ if environ is not None else os.environ
    override = str(env.get(_PRECISION_ENV_VAR, "")).strip().lower()
    dev_override = str(env.get("GPT_SOVITS_DEVICE", "")).strip().lower()

    if dev_override == "cpu" or override == "cpu":
        return "cpu", False, "env"
    if override in ("fp16", "half", "true", "1"):
        return "cuda", True, "env"
    if override in ("fp32", "float32", "false", "0"):
        return "cuda", False, "env"

    # User configured setting in SQLite database
    db_prec = read_db_precision(project_root)
    if db_prec == "cpu":
        return "cpu", False, "db"
    if db_prec in ("fp32", "float32"):
        return "cuda", False, "db"
    if db_prec in ("fp16", "half"):
        return "cuda", True, "db"

    # Calibration cache in precision.json
    cache = read_precision_cache(project_root)
    if cache is not None:
        cached_dir = str(cache.get("sovits_dir", "")).strip()
        try:
            matched = (not cached_dir or Path(cached_dir).resolve() == Path(sovits_dir).resolve())
        except Exception:
            matched = (cached_dir == str(sovits_dir))
        if matched:
            cached_dev = str(cache.get("device", "cuda")).lower()
            if cached_dev == "cpu":
                return "cpu", False, "cache"
            return "cuda", bool(cache.get("is_half", True)), "cache"

    # Existing YAML on disk
    yaml_dev = read_sovits_yaml_device(sovits_dir)
    if yaml_dev == "cpu":
        return "cpu", False, "yaml"
    yaml_half = read_sovits_yaml_is_half(sovits_dir)
    if yaml_half is not None and yaml_half is False:
        return "cuda", False, "yaml"

    # Default fallback: check discrete GPU availability
    try:
        from galgame2voice.utils.hardware import detect_gpu_capability
        has_gpu, _, _ = detect_gpu_capability()
    except Exception:
        has_gpu = True

    if not has_gpu:
        return "cpu", False, "default"
    return "cuda", True, "default"


def resolve_initial_is_half(
    project_root: Path,
    sovits_dir: Path,
    environ: Optional[Dict[str, str]] = None,
) -> tuple[bool, str]:
    """Compatibility helper returning (is_half, source)."""
    _, is_half, source = resolve_initial_device_and_half(project_root, sovits_dir, environ)
    return is_half, source
