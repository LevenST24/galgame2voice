"""
Character Package representation and asset resolution for Galgame2Voice.

Encapsulates character package layout, path traversal protection,
weight resolution (including pointer files), and audio probe caching.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from galgame2voice.config import get_settings
from galgame2voice.schemas.character_manifest import CharacterManifest
from galgame2voice.utils.path_guard import (
    contains_traversal_payload,
    get_authorized_roots,
    to_project_relative_path,
)

logger = logging.getLogger("galgame2voice.services.character_package")

# Lightweight in-memory audio probe cache: (canonical_path, file_size, mtime) -> (file_hash, duration)
_AUDIO_PROBE_CACHE: Dict[Tuple[str, int, float], Tuple[str, Optional[float]]] = {}


class CharacterPackage:
    """Represents a fully self-contained character package on disk."""

    def __init__(
        self,
        folder_path: Path,
        manifest: CharacterManifest,
        system_prompt: str = "",
        validation_errors: Optional[List[str]] = None,
    ):
        self.folder_path = folder_path.resolve()
        self.manifest = manifest
        self.system_prompt = system_prompt or manifest.system_prompt or ""
        self.validation_errors: List[str] = validation_errors or []
        self.is_valid: bool = len(self.validation_errors) == 0

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def folder(self) -> Path:
        return self.folder_path

    @property
    def name(self) -> str:
        return self.manifest.name

    def resolve_audio_path(self, relative_or_absolute: str) -> Optional[Path]:
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

        # 1. Check relative to character package folder
        norm_rel = clean_str.lstrip("/\\")
        cand = (self.folder_path / norm_rel).resolve()
        if cand.is_file() and (cand == self.folder_path or cand.is_relative_to(self.folder_path)):
            return cand

        # 2. Check relative to project root
        root_cand = (settings.project_root / norm_rel).resolve()
        if root_cand.is_file() and (root_cand == settings.project_root or root_cand.is_relative_to(settings.project_root)):
            return root_cand

        # 3. Check relative to audio_dir
        audio_cand = (settings.audio_dir / norm_rel).resolve()
        if audio_cand.is_file() and (audio_cand == settings.audio_dir or audio_cand.is_relative_to(settings.audio_dir)):
            return audio_cand

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
            # Check if it's a pointer file (e.g. text containing path to .ckpt or .pth)
            try:
                if target_file.stat().st_size < 4096:
                    content = target_file.read_text(encoding="utf-8").strip()
                    if content and content.endswith((".ckpt", ".pth")):
                        if contains_traversal_payload(content):
                            return ""
                        # Pointer points to target
                        settings = get_settings()
                        ptr_path = Path(content)
                        if not ptr_path.is_absolute():
                            ptr_target = settings.project_root / ptr_path
                            if ptr_target.exists():
                                return to_project_relative_path(ptr_target)
                        return content
            except Exception:
                pass
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
