"""
Path Traversal and File Boundary Guard for galgame2voice.
Ensures all user-provided or API-received audio paths and voice profile references
are strictly bounded within authorized directories (audio_dir, data_dir, project_root),
rejecting path traversal attempts, symlink escapes, UNC paths, and Windows device names.
"""

import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from galgame2voice.config import get_settings


class PathTraversalError(ValueError):
    """Raised when a path attempts directory traversal or escapes authorized boundaries."""
    pass


# Windows DOS Device Names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
_DEVICE_NAME_REGEX = re.compile(
    r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$",
    re.IGNORECASE,
)

# Traversal signature patterns in raw strings
_TRAVERSAL_STRING_REGEX = re.compile(
    r"(?:^|[\\/])\.\.(?:[\\/]|$)|%2e%2e|%2f|%5c",
    re.IGNORECASE,
)


def is_windows_device_name(name: str) -> bool:
    """Checks if filename matches a reserved Windows device name (e.g., CON, NUL, AUX.wav)."""
    if not name:
        return False
    clean_name = os.path.basename(name).strip()
    return bool(_DEVICE_NAME_REGEX.match(clean_name))


def contains_traversal_payload(raw_path: str) -> bool:
    """
    Detects directory traversal sequences, URL encoding tricks, null bytes,
    and UNC paths in user-provided path strings.
    """
    if not raw_path:
        return False

    # 1. Null byte injection
    if "\x00" in raw_path:
        return True

    # 2. URL-decoded traversal
    try:
        decoded = urllib.parse.unquote(raw_path)
    except Exception:
        decoded = raw_path

    for test_str in (raw_path, decoded):
        if "\x00" in test_str:
            return True
        # UNC paths (\\server\share or //server/share)
        if test_str.startswith(r"\\") or test_str.startswith("//"):
            return True
        # .. segments
        if ".." in test_str:
            parts = re.split(r"[\\/]+", test_str)
            if ".." in parts:
                return True
        if _TRAVERSAL_STRING_REGEX.search(test_str):
            return True

    return False


def is_safe_filename(filename: str) -> bool:
    """
    Verifies that a string represents a pure, safe filename without
    directory traversal, path separators, or device names.
    """
    if not filename or not isinstance(filename, str):
        return False
    clean = filename.strip()
    if not clean or len(clean) > 255:
        return False
    if "/" in clean or "\\" in clean:
        return False
    if contains_traversal_payload(clean):
        return False
    if is_windows_device_name(clean):
        return False
    return True


def get_authorized_roots(
    custom_roots: Optional[Sequence[Union[str, Path]]] = None,
    include_sovits: bool = True,
) -> List[Path]:
    """
    Returns resolved absolute paths of all authorized system directories:
    audio_dir, data_dir, and project_root.
    """
    settings = get_settings()
    roots: List[Path] = [
        settings.audio_dir.resolve(),
        settings.data_dir.resolve(),
        settings.project_root.resolve(),
    ]

    if include_sovits:
        # Check environment or sovits_dir.txt if configured
        env_sovits = os.environ.get("GPT_SOVITS_DIR")
        if env_sovits and Path(env_sovits).is_dir():
            roots.append(Path(env_sovits).resolve())

        sovits_txt = settings.data_dir / "sovits_dir.txt"
        if sovits_txt.is_file():
            try:
                target_dir = sovits_txt.read_text(encoding="utf-8").strip()
                if target_dir and Path(target_dir).is_dir():
                    roots.append(Path(target_dir).resolve())
            except Exception:
                pass

        # Discovered standard GPT-SoVITS install locations
        for cand in (
            r"E:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604",
            r"D:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604",
            r"C:\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604",
        ):
            try:
                cp = Path(cand)
                if cp.is_dir():
                    res = cp.resolve()
                    if res not in roots:
                        roots.append(res)
            except Exception:
                pass

    if custom_roots:
        for r in custom_roots:
            try:
                p = Path(r).resolve()
                if p not in roots:
                    roots.append(p)
            except Exception:
                pass

    return roots


def validate_path_containment(
    path: Union[str, Path],
    allowed_roots: Optional[Sequence[Union[str, Path]]] = None,
    base_dir: Optional[Union[str, Path]] = None,
) -> Path:
    """
    Validates that a path is strictly contained within authorized root directories.
    Dereferences symlinks (via Path.resolve) to prevent symlink escape attacks.
    Raises PathTraversalError if traversal or out-of-bounds access is detected.
    Returns canonical resolved Path.
    """
    str_path = str(path).strip()
    if not str_path:
        raise PathTraversalError("Path cannot be empty")

    if contains_traversal_payload(str_path):
        raise PathTraversalError(f"Directory traversal sequence detected in '{str_path}'")

    if is_windows_device_name(str_path):
        raise PathTraversalError(f"Reserved Windows device name not allowed: '{str_path}'")

    roots = [Path(r).resolve() for r in (allowed_roots or get_authorized_roots())]
    raw_p = Path(str_path)

    # If relative, anchor to base_dir (or audio_dir / project_root)
    if not raw_p.is_absolute():
        if base_dir is not None:
            candidate = (Path(base_dir) / raw_p).resolve()
        else:
            # Check if relative path exists under audio_dir first, then project_root
            settings = get_settings()
            cand_audio = (settings.audio_dir / raw_p).resolve()
            cand_root = (settings.project_root / raw_p).resolve()
            if cand_audio.exists():
                candidate = cand_audio
            elif cand_root.exists():
                candidate = cand_root
            else:
                candidate = cand_audio
    else:
        candidate = raw_p.resolve()

    # Check device names on resolved filename
    if is_windows_device_name(candidate.name):
        raise PathTraversalError(f"Resolved path targets reserved Windows device name: '{candidate.name}'")

    # Verify canonical path is inside at least one authorized root
    is_contained = any(
        candidate == root or candidate.is_relative_to(root)
        for root in roots
    )

    if not is_contained:
        raise PathTraversalError(
            f"Access denied: path '{str_path}' resolves to '{candidate}', "
            f"which is outside authorized boundaries: {[str(r) for r in roots]}"
        )

    return candidate


def is_path_safe(
    path: Union[str, Path],
    allowed_roots: Optional[Sequence[Union[str, Path]]] = None,
    base_dir: Optional[Union[str, Path]] = None,
) -> bool:
    """Returns True if path is valid and strictly bounded, False otherwise."""
    try:
        validate_path_containment(path, allowed_roots=allowed_roots, base_dir=base_dir)
        return True
    except (PathTraversalError, ValueError, OSError):
        return False


def safe_resolve_audio_path(
    path_or_filename: Union[str, Path],
    allowed_roots: Optional[Sequence[Union[str, Path]]] = None,
) -> Path:
    """
    Specifically validates and resolves audio file paths (reference audio, cache audio).
    Must resolve inside settings.audio_dir or settings.project_root.
    """
    settings = get_settings()
    roots = allowed_roots or [settings.audio_dir.resolve(), settings.project_root.resolve()]
    return validate_path_containment(path_or_filename, allowed_roots=roots, base_dir=settings.audio_dir)


def validate_voice_profile_paths(
    gpt_weights_path: Optional[str] = None,
    sovits_weights_path: Optional[str] = None,
    ref_audio_path: Optional[str] = None,
    allowed_roots: Optional[Sequence[Union[str, Path]]] = None,
) -> None:
    """
    Validates all file path references for a voice profile (weights and reference audio).
    Raises PathTraversalError if any reference attempts traversal or boundary escape.
    """
    roots = allowed_roots or get_authorized_roots(include_sovits=True)

    # Weights paths validation
    for name, p_val in [("gpt_weights_path", gpt_weights_path), ("sovits_weights_path", sovits_weights_path)]:
        if p_val:
            validate_path_containment(p_val, allowed_roots=roots)

    # Reference audio path validation
    if ref_audio_path:
        validate_path_containment(ref_audio_path, allowed_roots=roots)


__all__ = [
    "PathTraversalError",
    "is_windows_device_name",
    "contains_traversal_payload",
    "is_safe_filename",
    "get_authorized_roots",
    "validate_path_containment",
    "is_path_safe",
    "safe_resolve_audio_path",
    "validate_voice_profile_paths",
]
