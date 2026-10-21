"""Read the local background library, omitting malformed entries and unsafe files."""

from dataclasses import dataclass
import json
import logging
from pathlib import Path, PureWindowsPath
import re

logger = logging.getLogger(__name__)

BACKGROUND_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
BACKGROUND_VARIANTS = frozenset({"day", "evening", "night"})
IMAGE_EXTENSIONS = frozenset({".webp", ".png", ".jpg", ".jpeg", ".avif"})


@dataclass(frozen=True)
class Background:
    id: str
    name: str
    source: str
    variants: dict[str, Path]


def resolve_background_file(root: Path, relative_path: str) -> Path | None:
    """Accept only existing image files that resolve within the library root."""
    try:
        path = Path(relative_path)
        # Also reject drive-relative/UNC Windows paths when running on POSIX.
        if path.is_absolute() or PureWindowsPath(relative_path).anchor or "\0" in relative_path:
            return None
        resolved_root = root.resolve()
        resolved = (resolved_root / path).resolve()
        if (
            resolved.is_relative_to(resolved_root)
            and resolved.suffix.lower() in IMAGE_EXTENSIONS
            and resolved.is_file()
        ):
            return resolved
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def load_background_manifest(root: Path) -> list[Background]:
    """A missing/unreadable manifest is empty; invalid entries never hide neighbors."""
    try:
        data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError, UnicodeError, RecursionError):
        logger.warning("Cannot read background manifest in %s", root)
        return []
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data["version"] != 1
        or not isinstance(data.get("backgrounds"), list)
    ):
        logger.warning("Invalid background manifest in %s", root)
        return []

    backgrounds = []
    seen = set()
    for index, entry in enumerate(data["backgrounds"]):
        if not isinstance(entry, dict):
            logger.warning("Skipping invalid background entry %s", index)
            continue
        bg_id = entry.get("id")
        name = entry.get("name")
        source = entry.get("source", "")
        variants = entry.get("variants")
        if (
            not isinstance(bg_id, str)
            or not BACKGROUND_ID_RE.fullmatch(bg_id)
            or bg_id in seen
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(source, str)
            or not isinstance(variants, dict)
            or not variants
            or not variants.keys() <= BACKGROUND_VARIANTS
            or any(
                not isinstance(path, str) or PureWindowsPath(path).suffix.lower() not in IMAGE_EXTENSIONS
                for path in variants.values()
            )
        ):
            logger.warning("Skipping invalid or duplicate background entry %s", index)
            continue

        existing = {}
        for variant, path in variants.items():
            resolved = resolve_background_file(root, path)
            if resolved is None:
                logger.warning("Skipping missing or invalid background variant %s/%s", bg_id, variant)
                continue
            existing[variant] = resolved
        if existing:
            backgrounds.append(Background(bg_id, name, source, existing))
            seen.add(bg_id)
    return backgrounds
