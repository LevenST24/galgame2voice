"""
Character Package representation and asset resolution for Galgame2Voice.

Encapsulates character package layout, path traversal protection,
weight resolution (including pointer files), and audio probe caching.
"""

from __future__ import annotations

import logging
from pathlib import Path

from galgame2voice.config import get_settings
from galgame2voice.schemas.character_manifest import CharacterManifest
from galgame2voice.utils.path_guard import (
    contains_traversal_payload,
    get_authorized_roots,
    to_project_relative_path,
)

logger = logging.getLogger("galgame2voice.services.character_package")

# Lightweight in-memory audio probe cache: (canonical_path, file_size, mtime) -> (file_hash, duration)
_AUDIO_PROBE_CACHE: dict[tuple[str, int, float], tuple[str, float | None]] = {}


def _resolve_pointer_file_target(target_file: Path) -> str | None:
    """Inspects if target_file is a lightweight pointer file (<4KB) referencing model weights."""
    try:
        if target_file.stat().st_size >= 4096:
            return None
        content = target_file.read_text(encoding="utf-8").strip()
        if not content or not content.endswith((".ckpt", ".pth")):
            return None
        if contains_traversal_payload(content):
            return ""
        settings = get_settings()
        ptr_path = Path(content)
        if not ptr_path.is_absolute():
            ptr_target = settings.project_root / ptr_path
            if ptr_target.exists():
                return to_project_relative_path(ptr_target)
        return content
    except (OSError, UnicodeDecodeError):
        return None


class CharacterPackage:
    """Represents a fully self-contained character package on disk."""

    def __init__(
        self,
        folder_path: Path,
        manifest: CharacterManifest,
        system_prompt: str = "",
        validation_errors: list[str] | None = None,
    ) -> None:
        self.folder_path = folder_path.resolve()
        self.manifest = manifest
        self.system_prompt = system_prompt or manifest.system_prompt or ""
        self.validation_errors: list[str] = validation_errors or []
        self.is_valid: bool = not self.validation_errors

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def folder(self) -> Path:
        return self.folder_path

    @property
    def name(self) -> str:
        return self.manifest.name

    def resolve_audio_path(self, relative_or_absolute: str) -> Path | None:
        """
        Resolves an audio path relative to the character package directory.
        Strictly guarded against directory traversal and out-of-boundary references.
        """
        if not relative_or_absolute:
            return None

        clean_str = str(relative_or_absolute).strip().strip("\"'")
        if contains_traversal_payload(clean_str):
            logger.warning("Directory traversal rejected in resolve_audio_path: %s", clean_str)
            return None

        p = Path(clean_str)
        settings = get_settings()
        char_dir = getattr(settings, "characters_dir", settings.project_root / "characters")
        authorized = [self.folder_path, Path(char_dir)] + get_authorized_roots()

        if p.is_absolute():
            if p.is_file():
                res = p.resolve()
                if any(res == root or res.is_relative_to(root) for root in authorized):
                    return res
            return None

        # Check relative to package folder, project root, and audio_dir
        norm_rel = clean_str.lstrip("/\\")
        for base in (self.folder_path, settings.project_root, settings.audio_dir):
            cand = (base / norm_rel).resolve()
            if cand.is_file() and (cand == base or cand.is_relative_to(base)):
                return cand

        return None

    def resolve_weight_path(self, field_name: str) -> str:
        """
        Resolves gpt_weights or sovits_weights. Supports text pointer files.
        If the file exists and is small (<4KB) and contains a path, returns that path.
        Otherwise returns the resolved absolute or relative path string.
        """
        val = getattr(self.manifest, field_name, None)
        if not val:
            return ""

        if contains_traversal_payload(str(val)):
            logger.warning("Directory traversal rejected in resolve_weight_path: %s", val)
            return ""

        clean_str = str(val).strip().strip("\"'")
        p = Path(clean_str)
        target_file = (self.folder_path / clean_str.lstrip("/\\")) if not p.is_absolute() else p

        if target_file.is_file():
            ptr_target = _resolve_pointer_file_target(target_file)
            if ptr_target is not None:
                return ptr_target
            return to_project_relative_path(target_file)

        # If file does not exist directly in package folder, check project root
        settings = get_settings()
        root_target = settings.project_root / clean_str.lstrip("/\\")
        if root_target.exists():
            return to_project_relative_path(root_target)

        return str(val)


__all__ = [
    "CharacterPackage",
    "_AUDIO_PROBE_CACHE",
]
