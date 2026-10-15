"""Read numbered or legacy portraits without rewriting installed character packs."""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EMOTION_FACES = {
    "gentle": ("gentle", "normal"), "happy": ("happy", "smile", "wink"),
    "shy": ("shy", "blush"), "tsundere": ("tsundere", "pout"),
    "cool": ("cool", "normal"), "sad": ("sad",), "angry": ("angry", "pout"),
    "surprised": ("surprised",), "thinking": ("thinking",),
}


def _read_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        logger.warning("Cannot read portrait index: %s", path)
        return None


def _available_sprites(root: Path, costume: str, entries: Any) -> dict[str, Any]:
    if not costume or any(char in costume for char in ("/", "\\")) or costume in (".", ".."):
        return {}
    if not isinstance(entries, dict):
        return {}
    available = {}
    for face_id, sprite in entries.items():
        if not isinstance(sprite, dict) or not isinstance(sprite.get("file"), str):
            continue
        # Legacy indexes used /static/characters/... URLs; files live in the pack.
        filename = sprite["file"].replace("\\", "/").rsplit("/", 1)[-1]
        path = root / costume / filename
        if path.suffix.lower() not in {".png", ".webp", ".jpg", ".jpeg"}:
            continue
        try:
            valid_file = path.resolve().is_relative_to(root.resolve()) and path.is_file()
        except (OSError, ValueError):
            valid_file = False
        if not valid_file:
            continue
        available[str(face_id)] = {**sprite, "file": filename}
    return available


def _normalize(root: Path, data: dict[str, Any], *, legacy: bool) -> dict[str, Any] | None:
    raw_costumes = data.get("costumes")
    if legacy:
        if not isinstance(raw_costumes, list) or not isinstance(data.get("sprites"), dict):
            return None
        costumes = [c for c in raw_costumes if isinstance(c, dict) and isinstance(c.get("id"), str)]
    else:
        if not isinstance(raw_costumes, dict):
            return None
        costumes = [{**c, "id": cid} for cid, c in raw_costumes.items() if isinstance(c, dict)]

    sprites = {}
    metadata = []
    for costume in costumes:
        cid = costume["id"]
        if not isinstance(cid, str):
            continue
        entries = data["sprites"].get(cid) if legacy else costume.get("sprites")
        available = _available_sprites(root, cid, entries)
        if not available:
            continue
        sprites[cid] = available
        metadata.append({"id": cid, "name": costume.get("name") or cid, "icon": costume.get("icon") or "🎎"})
    if not sprites:
        return None

    if legacy:
        faces = {}
        for entries in sprites.values():
            for face_id, sprite in entries.items():
                faces.setdefault(face_id, {"label": sprite.get("description") or face_id,
                    "primary": face_id, "parts": [], "blush": None})
        base = next((face_id for face_id in ("gentle", "normal") if face_id in faces), next(iter(faces)))
        pools = {}
        for emotion, names in EMOTION_FACES.items():
            candidates = [name for name in names if name in faces] or [base]
            pools[emotion] = [{"id": name, "strength": len(candidates) - index}
                              for index, name in enumerate(candidates)]
    else:
        raw_faces = data.get("faces")
        raw_pools = data.get("expression_sets")
        if not isinstance(raw_faces, dict) or not isinstance(raw_pools, dict):
            return None
        available_ids = {face_id for entries in sprites.values() for face_id in entries}
        faces = {face_id: meta for face_id, meta in raw_faces.items()
                 if face_id in available_ids and isinstance(meta, dict)}
        pools = {emotion: [entry for entry in entries if isinstance(entry, dict)
                           and isinstance(entry.get("id"), str) and entry["id"] in faces]
                 for emotion, entries in raw_pools.items() if isinstance(entries, list)}
        if not faces or not any(pools.values()):
            return None

    default = data.get("default_costume")
    return {"enabled": True, "pose": data.get("pose", ""),
            "default_costume": default if isinstance(default, str) and default in sprites else metadata[0]["id"],
            "costumes": metadata, "sprites": sprites, "faces": faces, "expression_sets": pools}


def load_portrait_manifest(root: Path) -> dict[str, Any] | None:
    """Prefer numbered differentials, then support installed emotion-PNG packs."""
    for filename, legacy in (("expressions.json", False), ("manifest.json", True)):
        raw = _read_object(root / filename)
        if raw is not None:
            payload = _normalize(root, raw, legacy=legacy)
            if payload is not None:
                return payload
    return None
