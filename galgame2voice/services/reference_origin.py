"""Reference audio path origin and ownership classification for galgame2voice.

Enforces the unified path ownership contract:
1. PACKAGE_OWNED:
   - Physical normalized path containment inside pkg.folder; OR
   - Exact match with a safe relative audio path declared in pkg.manifest.emotions; OR
   - The path physically exists inside pkg.folder (e.g. pkg.folder / clean).
2. CROSS_PACKAGE:
   - Resolved path is inside another known valid character package directory.
   (NOTE: Unknown bare paths or subdirectories outside discovered valid packages
   are NEVER guessed to be cross-package.)
3. EXTERNAL_OR_CUSTOM:
   - Non-empty, non-cross-package, non-package-owned path that passes path guard.
4. EMPTY:
   - Empty or whitespace-only path.
"""

from collections.abc import Iterable
from enum import Enum
from pathlib import Path
from typing import Any

from galgame2voice.config import get_settings
from galgame2voice.utils.path_guard import contains_traversal_payload


class ReferenceOrigin(str, Enum):
    EMPTY = "empty"
    CROSS_PACKAGE = "cross_package"
    PACKAGE_OWNED = "package_owned"
    EXTERNAL_OR_CUSTOM = "external_or_custom"


def classify_reference_origin(
    ref_audio: str | None,
    pkg: Any | None,
    known_other_packages: Iterable[Any | Path | str] | None = None,
) -> ReferenceOrigin:
    """Classifies the origin of a reference audio path for a given character package."""
    clean = str(ref_audio or "").strip().strip("\"'").replace("\\", "/")
    if not clean:
        return ReferenceOrigin.EMPTY

    if pkg is None or not getattr(pkg, "is_valid", False):
        return ReferenceOrigin.EXTERNAL_OR_CUSTOM

    folder = getattr(pkg, "folder", None)
    if folder is None:
        return ReferenceOrigin.EXTERNAL_OR_CUSTOM

    pkg_folder = Path(folder).resolve()

    # Collect known other package folders if supplied by caller
    other_folders: set[Path] = set()
    if known_other_packages is not None:
        for item in known_other_packages:
            cand_folder = getattr(item, "folder", item)
            if cand_folder is not None:
                try:
                    p = Path(cand_folder).resolve()
                    if p != pkg_folder:
                        other_folders.add(p)
                except (OSError, ValueError):
                    pass

    # 1. Check if the path is explicitly inside pkg.folder or another known character package
    try:
        settings = get_settings()
        path = Path(clean)
        if not path.is_absolute():
            path = settings.project_root / path
        resolved = path.resolve()

        if resolved == pkg_folder or resolved.is_relative_to(pkg_folder):
            return ReferenceOrigin.PACKAGE_OWNED

        for other_pkg_folder in other_folders:
            if resolved == other_pkg_folder or resolved.is_relative_to(other_pkg_folder):
                return ReferenceOrigin.CROSS_PACKAGE
    except (OSError, ValueError):
        pass

    # 2. Check exact match with a safe relative audio path declared in pkg.manifest.emotions
    # Must be a safe relative path within the package boundary; cannot be an absolute path or traversal.
    if not Path(clean).is_absolute() and not contains_traversal_payload(clean):
        try:
            cand_path = (pkg_folder / clean).resolve()
            if cand_path == pkg_folder or cand_path.is_relative_to(pkg_folder):
                manifest = getattr(pkg, "manifest", None)
                emotions = getattr(manifest, "emotions", None) or {}
                for emo in emotions.values():
                    emo_audio = str(getattr(emo, "audio", "")).strip().strip("\"'").replace("\\", "/")
                    if not emo_audio or Path(emo_audio).is_absolute() or contains_traversal_payload(emo_audio):
                        continue
                    if clean == emo_audio:
                        return ReferenceOrigin.PACKAGE_OWNED
        except (OSError, ValueError):
            pass

    # 3. Check if the file physically exists inside pkg.folder
    try:
        if not Path(clean).is_absolute() and not contains_traversal_payload(clean):
            cand = (pkg_folder / clean).resolve()
            if cand.is_file() and (cand == pkg_folder or cand.is_relative_to(pkg_folder)):
                return ReferenceOrigin.PACKAGE_OWNED
    except (OSError, ValueError):
        pass

    # 4. Unknown path is not package-owned and not cross-package -> external/custom
    return ReferenceOrigin.EXTERNAL_OR_CUSTOM
