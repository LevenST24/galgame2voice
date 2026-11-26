"""Read the local background library, one pack per work, omitting bad packs and entries.

A character package may carry its work's pack in its own ``background`` folder; that copy is used
when the library has no pack for the work.
"""

from contextlib import suppress
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path, PureWindowsPath
import re

logger = logging.getLogger(__name__)

BACKGROUND_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
BACKGROUND_VARIANTS = frozenset({"day", "evening", "night"})
IMAGE_EXTENSIONS = frozenset({".webp", ".png", ".jpg", ".jpeg", ".avif"})
BUNDLED_PACK_FOLDER = "background"


@dataclass(frozen=True)
class Background:
    work: str
    id: str
    name: str
    variants: dict[str, Path]

    @property
    def key(self) -> str:
        return f"{self.work}/{self.id}"


@dataclass(frozen=True)
class BackgroundPack:
    work: str
    name: str
    folder: Path
    backgrounds: list[Background] = field(default_factory=list)
    default: str | None = None

    @property
    def default_key(self) -> str | None:
        """The background shown for this work's characters: the declared default, else the first."""
        ids = [background.id for background in self.backgrounds]
        chosen = self.default if self.default in ids else (ids[0] if ids else None)
        return f"{self.work}/{chosen}" if chosen else None


def resolve_background_file(root: Path, relative_path: str) -> Path | None:
    """Accept only existing image files that resolve within the given pack folder."""
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


def _load_pack(pack_dir: Path, work: str) -> BackgroundPack | None:
    """A single pack whose declared work matches its folder; bad packs return None."""
    try:
        data = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError, RecursionError):
        logger.warning("Cannot read background manifest in %s", pack_dir)
        return None
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data["version"] != 1
        or data.get("work") != work
        or not isinstance(data.get("backgrounds"), list)
    ):
        logger.warning("Invalid background pack in %s", pack_dir)
        return None

    name = data.get("name")
    name = name.strip() if isinstance(name, str) and name.strip() else work

    backgrounds = []
    seen = set()
    for index, entry in enumerate(data["backgrounds"]):
        if not isinstance(entry, dict):
            logger.warning("Skipping invalid background entry %s in %s", index, pack_dir)
            continue
        bg_id = entry.get("id")
        bg_name = entry.get("name")
        variants = entry.get("variants")
        if (
            not isinstance(bg_id, str)
            or not BACKGROUND_ID_RE.fullmatch(bg_id)
            or bg_id in seen
            or not isinstance(bg_name, str)
            or not bg_name.strip()
            or not isinstance(variants, dict)
            or not variants
            or not variants.keys() <= BACKGROUND_VARIANTS
            or any(
                not isinstance(path, str) or PureWindowsPath(path).suffix.lower() not in IMAGE_EXTENSIONS
                for path in variants.values()
            )
        ):
            logger.warning("Skipping invalid or duplicate background entry %s in %s", index, pack_dir)
            continue

        existing = {}
        for variant, path in variants.items():
            resolved = resolve_background_file(pack_dir, path)
            if resolved is None:
                logger.warning("Skipping missing or invalid background variant %s/%s", bg_id, variant)
                continue
            existing[variant] = resolved
        if existing:
            backgrounds.append(Background(work, bg_id, bg_name, existing))
            seen.add(bg_id)
    default = data.get("default")
    default = default if isinstance(default, str) and BACKGROUND_ID_RE.fullmatch(default) else None
    return BackgroundPack(work, name, pack_dir, backgrounds, default)


def _declared_work(pack_dir: Path) -> str | None:
    with suppress(OSError, ValueError, UnicodeError, RecursionError):
        data = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8-sig"))
        work = data.get("work") if isinstance(data, dict) else None
        if isinstance(work, str) and BACKGROUND_ID_RE.fullmatch(work):
            return work
    logger.warning("Skipping bundled background pack in %s", pack_dir)
    return None


def _bundled_packs(characters_root: Path | None, work: str | None = None):
    """Packs in ``<character>/background`` folders, in folder order; ``work`` keeps only that work's."""
    try:
        children = sorted(characters_root.iterdir(), key=lambda child: child.name) if characters_root else []
    except (OSError, ValueError):
        return
    for child in children:
        folder = child / BUNDLED_PACK_FOLDER
        if not (folder / "manifest.json").is_file():
            continue
        declared = _declared_work(folder)
        if declared is not None and work in (None, declared):
            pack = _load_pack(folder, declared)
            if pack is not None and pack.backgrounds:
                yield pack


def load_background_pack(root: Path, work: str, characters_root: Path | None = None) -> BackgroundPack | None:
    """One work's pack (the library's, else one a character bundles), without reading the others."""
    if not BACKGROUND_ID_RE.fullmatch(work):
        return None
    pack = _load_pack(root / work, work)
    if pack is not None and pack.backgrounds:
        return pack
    return next(_bundled_packs(characters_root, work), None)


def load_background_packs(root: Path, characters_root: Path | None = None) -> list[BackgroundPack]:
    """Load every ``<root>/<work_id>/manifest.json`` pack, skipping bad packs, then the packs that
    character folders bundle for works the library lacks.

    A missing library or unreadable directory is empty; a root-level
    ``manifest.json`` is no longer read.
    """
    try:
        children = sorted(root.iterdir(), key=lambda child: child.name)
    except FileNotFoundError:
        children = []
    except (OSError, ValueError):
        logger.warning("Cannot list backgrounds directory %s", root)
        children = []
    packs = []
    for child in children:
        if not child.is_dir():
            continue
        work = child.name
        if not BACKGROUND_ID_RE.fullmatch(work):
            logger.warning("Skipping background pack with invalid work id %s", work)
            continue
        pack = _load_pack(child, work)
        if pack is not None and pack.backgrounds:
            packs.append(pack)
    for pack in _bundled_packs(characters_root):
        if all(pack.work != have.work for have in packs):
            packs.append(pack)
    return sorted(packs, key=lambda pack: pack.work)
