"""Read numbered or legacy portraits without rewriting installed character packs."""

import json
import logging
import math
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EMOTION_FACES = {
    "gentle": ("gentle", "normal"), "happy": ("happy", "smile", "wink"),
    "shy": ("shy", "blush"), "tsundere": ("tsundere", "pout"),
    "cool": ("cool", "normal"), "sad": ("sad",), "angry": ("angry", "pout"),
    "surprised": ("surprised",), "thinking": ("thinking",),
}


def _is_finite_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _read_image_size(path: Path) -> tuple[int, int] | None:
    """Reads width and height from PNG, WebP, or JPEG image headers without external dependencies."""
    if not path.is_file():
        return None
    try:
        with path.open("rb") as f:
            head = f.read(32)
            if len(head) < 16:
                return None
            if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
                w = int.from_bytes(head[16:20], "big")
                h = int.from_bytes(head[20:24], "big")
                return (w, h) if w > 0 and h > 0 else None
            if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
                chunk = head[12:16]
                if chunk == b"VP8 ":
                    f.seek(26)
                    buf = f.read(4)
                    if len(buf) == 4:
                        w = (int.from_bytes(buf[0:2], "little")) & 0x3FFF
                        h = (int.from_bytes(buf[2:4], "little")) & 0x3FFF
                        return (w, h) if w > 0 and h > 0 else None
                elif chunk == b"VP8L":
                    f.seek(21)
                    buf = f.read(4)
                    if len(buf) == 4:
                        w = 1 + (((buf[1] & 0x3F) << 8) | buf[0])
                        h = 1 + (((buf[3] & 0x0F) << 10) | (buf[2] << 2) | ((buf[1] & 0xC0) >> 6))
                        return (w, h) if w > 0 and h > 0 else None
                elif chunk == b"VP8X":
                    f.seek(24)
                    buf = f.read(6)
                    if len(buf) == 6:
                        w = 1 + int.from_bytes(buf[0:3], "little")
                        h = 1 + int.from_bytes(buf[3:6], "little")
                        return (w, h) if w > 0 and h > 0 else None
            if head.startswith(b"\xff\xd8"):
                f.seek(2)
                while True:
                    m = f.read(2)
                    if len(m) < 2 or m[0] != 0xFF:
                        break
                    marker = m[1]
                    if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        f.read(3)
                        h = int.from_bytes(f.read(2), "big")
                        w = int.from_bytes(f.read(2), "big")
                        return (w, h) if w > 0 and h > 0 else None
                    elif marker in (0xD9, 0xDA):
                        break
                    else:
                        lb = f.read(2)
                        if len(lb) < 2:
                            break
                        length = int.from_bytes(lb, "big")
                        if length < 2:
                            break
                        f.seek(length - 2, 1)
    except Exception as exc:
        logger.debug("Failed reading image dimensions from %s: %s", path, exc)
    return None


def _get_costume_sprite_size(root: Path, costume_id: str, available: dict[str, Any]) -> tuple[int, int] | None:
    for sp in available.values():
        if isinstance(sp, dict):
            w = sp.get("width")
            h = sp.get("height")
            if _is_finite_number(w) and _is_finite_number(h) and w > 0 and h > 0:
                return int(w), int(h)
    for sp in available.values():
        if isinstance(sp, dict) and "file" in sp:
            path = root / costume_id / str(sp["file"])
            size = _read_image_size(path)
            if size is not None:
                return size
    return None


def _validate_face_rect(rect: Any, sprite_size: tuple[int, int] | None) -> dict[str, Any] | None:
    if not isinstance(rect, dict):
        return None
    x, y, w, h = rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height")
    if not (_is_finite_number(x) and _is_finite_number(y) and _is_finite_number(w) and _is_finite_number(h)):
        return None
    if w <= 0 or h <= 0 or x < 0 or y < 0:
        return None
    if sprite_size is not None:
        sw, sh = sprite_size
        if x + w > sw or y + h > sh:
            return None
    return {"x": x, "y": y, "width": w, "height": h}


def _validate_head_anchor(anchor: Any) -> dict[str, Any] | None:
    if not isinstance(anchor, dict):
        return None
    x, y = anchor.get("x"), anchor.get("y")
    if not (_is_finite_number(x) and _is_finite_number(y)):
        return None
    return {"x": x, "y": y}


def _validate_crop(crop: Any) -> dict[str, Any] | None:
    if not isinstance(crop, dict):
        return None
    left, top, w, h = crop.get("left"), crop.get("top"), crop.get("width"), crop.get("height")
    if not (_is_finite_number(left) and _is_finite_number(top) and _is_finite_number(w) and _is_finite_number(h)):
        return None
    return {"left": left, "top": top, "width": w, "height": h}


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

        costume_meta: dict[str, Any] = {
            "id": cid,
            "name": costume.get("name") or cid,
            "icon": costume.get("icon") or "🎎",
        }
        if not legacy:
            sprite_size = _get_costume_sprite_size(root, cid, available)
            raw_face_rect = costume.get("faceRect") if "faceRect" in costume else data.get("faceRect")
            if raw_face_rect is not None:
                valid_face_rect = _validate_face_rect(raw_face_rect, sprite_size)
                if valid_face_rect is not None:
                    costume_meta["faceRect"] = valid_face_rect
                else:
                    logger.debug("Dropping invalid faceRect for costume %s: %r", cid, raw_face_rect)

            raw_head_anchor = costume.get("headAnchor") if "headAnchor" in costume else data.get("headAnchor")
            if raw_head_anchor is not None:
                valid_head_anchor = _validate_head_anchor(raw_head_anchor)
                if valid_head_anchor is not None:
                    costume_meta["headAnchor"] = valid_head_anchor
                else:
                    logger.debug("Dropping invalid headAnchor for costume %s: %r", cid, raw_head_anchor)

            raw_crop = costume.get("crop") if "crop" in costume else data.get("crop")
            if raw_crop is not None:
                valid_crop = _validate_crop(raw_crop)
                if valid_crop is not None:
                    costume_meta["crop"] = valid_crop
                else:
                    logger.debug("Dropping invalid crop for costume %s: %r", cid, raw_crop)

        metadata.append(costume_meta)
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
