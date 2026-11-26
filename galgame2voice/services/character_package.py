"""
Character Package representation and asset resolution for Galgame2Voice.

Encapsulates character package layout, path traversal protection,
weight resolution (including pointer files), and audio probe caching.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re

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
        content = re.sub(r"^[\s\"']+|[\s\"']+$", "", target_file.read_text(encoding="utf-8-sig"))
        if not content or not content.lower().endswith((".ckpt", ".pth")):
            return None
        if contains_traversal_payload(content):
            return ""
        ptr_path = Path(content)
        if not ptr_path.is_absolute() and (ptr_path.anchor or ":" in content):
            return ""
        settings = get_settings()
        has_dirs = getattr(settings, "audio_dir", None) is not None and getattr(settings, "data_dir", None) is not None
        roots = get_authorized_roots() if has_dirs else [Path(settings.project_root).resolve()]
        if ptr_path.is_absolute():
            norm_p = Path(os.path.normpath(content))
            lex_roots = (*roots, Path(os.path.normpath(str(settings.project_root))))
            if not any(norm_p == r or norm_p.is_relative_to(r) for r in lex_roots):
                return ""
            ptr_target = ptr_path.resolve()
        else:
            ptr_target = Path(settings.project_root) / ptr_path
            if not ptr_target.exists():
                return content
            ptr_target = ptr_target.resolve()
        if not any(ptr_target == r or ptr_target.is_relative_to(r) for r in roots):
            return ""
        return to_project_relative_path(ptr_target)
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
        *,
        has_manifest: bool = True,
    ) -> None:
        self.folder_path = folder_path.resolve()
        self.manifest = manifest
        self.system_prompt = system_prompt or manifest.system_prompt or ""
        self.validation_errors: list[str] = validation_errors or []
        self.is_valid: bool = not self.validation_errors
        self.has_manifest: bool = has_manifest

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

        if p.is_absolute():
            if p.is_file():
                res = p.resolve()
                char_dir = getattr(settings, "characters_dir", settings.project_root / "characters")
                authorized = [self.folder_path, Path(char_dir)] + get_authorized_roots()
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

        clean_str = re.sub(r"^[\s\"']+|[\s\"']+$", "", str(val))
        if not clean_str or contains_traversal_payload(clean_str):
            logger.warning("Directory traversal rejected in resolve_weight_path: %s", val)
            return ""

        p = Path(clean_str)
        if not p.is_absolute() and (p.anchor or ":" in clean_str):
            return ""

        settings = get_settings()
        has_dirs = getattr(settings, "audio_dir", None) is not None and getattr(settings, "data_dir", None) is not None
        local = (self.folder_path.resolve(), Path(settings.project_root).resolve())

        def inside(path: Path, roots) -> bool:
            return any(path == r or path.is_relative_to(r) for r in roots)

        def allowed(path: Path) -> bool:
            # Scanning the authorized roots touches every drive root, so only do it for paths outside the package and project.
            if inside(path, local):
                return True
            return inside(path, [self.folder_path, *(get_authorized_roots() if has_dirs else local[1:])])

        if p.is_absolute():
            norm_p = Path(os.path.normpath(clean_str))
            if not (inside(norm_p, (Path(os.path.normpath(str(settings.project_root))),)) or allowed(norm_p)):
                return ""
            target_file = p.resolve()
            if not allowed(target_file):
                return ""
        else:
            target_file = self.folder_path / p

        if target_file.is_file():
            resolved = target_file.resolve()
            if not allowed(resolved):
                return ""
            ptr_target = _resolve_pointer_file_target(resolved)
            return ptr_target if ptr_target is not None else to_project_relative_path(resolved)

        if p.is_absolute():
            return to_project_relative_path(target_file)

        root_target = Path(settings.project_root) / p
        if root_target.exists():
            resolved = root_target.resolve()
            if not allowed(resolved):
                return ""
            return to_project_relative_path(resolved)

        return clean_str


__all__ = [
    "CharacterPackage",
    "_AUDIO_PROBE_CACHE",
]
