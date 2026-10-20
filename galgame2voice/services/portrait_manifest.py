"""Read numbered or legacy portraits without rewriting installed character packs."""

import json
import logging
import math
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CANVAS_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
LAYER_FILE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+\.(webp|png)")

EMOTION_FACES = {
    "gentle": ("gentle", "normal"), "happy": ("happy", "smile", "wink"),
    "shy": ("shy", "blush"), "tsundere": ("tsundere", "pout"),
    "cool": ("cool", "normal"), "sad": ("sad",), "angry": ("angry", "pout"),
    "surprised": ("surprised",), "thinking": ("thinking",),
}


def _is_finite_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_positive_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _is_nonnegative_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


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


VALID_EMOTIONS = frozenset({
    "gentle", "normal", "happy", "shy", "blush", "surprised",
    "tsundere", "cool", "sleepy", "sad", "thinking", "angry",
})


def _validate_v3(root: Path, data: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "layers.json must be a JSON object"

    version = data.get("version")
    if version != 3 or isinstance(version, bool):
        return False, f"version must be 3, got {version!r}"

    for container_key in ("canvases", "outfits", "poses", "faces", "looks", "layers"):
        val = data.get(container_key)
        if not isinstance(val, dict) or not val:
            return False, f"'{container_key}' must be a non-empty object"

    default_outfit = data.get("defaultOutfit")
    default_pose = data.get("defaultPose")
    if not isinstance(default_outfit, str) or not default_outfit:
        return False, "'defaultOutfit' must be a non-empty string"
    if not isinstance(default_pose, str) or not default_pose:
        return False, "'defaultPose' must be a non-empty string"

    if default_outfit not in data["outfits"]:
        return False, f"defaultOutfit '{default_outfit}' not found in outfits"
    if default_pose not in data["poses"]:
        return False, f"defaultPose '{default_pose}' not found in poses"

    # 1. Canvases
    for cid, c in data["canvases"].items():
        if not isinstance(cid, str) or not CANVAS_ID_RE.fullmatch(cid):
            return False, f"canvas '{cid}' must match ^[A-Za-z0-9_-]{{1,32}}$"
        if not isinstance(c, dict):
            return False, f"canvas '{cid}' must be an object"
        w, h = c.get("width"), c.get("height")
        if not _is_positive_int(w) or not _is_positive_int(h):
            return False, f"canvas '{cid}' width and height must be positive integers"

    # 2. Outfits
    for oid, o in data["outfits"].items():
        if not isinstance(o, dict):
            return False, f"outfit '{oid}' must be an object"
        name = o.get("name")
        if not isinstance(name, str) or not name:
            return False, f"outfit '{oid}' must have a non-empty string 'name'"
        if "icon" in o and not isinstance(o["icon"], str):
            return False, f"outfit '{oid}' icon must be a string"

    # 3. Poses
    for pid, p in data["poses"].items():
        if not isinstance(p, dict):
            return False, f"pose '{pid}' must be an object"
        name = p.get("name")
        if not isinstance(name, str) or not name:
            return False, f"pose '{pid}' must have a non-empty string 'name'"
        p_canvas = p.get("canvas")
        if p_canvas not in data["canvases"]:
            return False, f"pose '{pid}' references unknown canvas '{p_canvas}'"

    # 4. Faces
    for fid, f in data["faces"].items():
        if not isinstance(f, dict):
            return False, f"face '{fid}' must be an object"
        label = f.get("label")
        if not isinstance(label, str) or not label:
            return False, f"face '{fid}' must have a non-empty string 'label'"
        emotion = f.get("emotion")
        if emotion not in VALID_EMOTIONS:
            return False, f"face '{fid}' has invalid emotion '{emotion}'"
        if "weights" in f:
            weights = f["weights"]
            if not isinstance(weights, dict):
                return False, f"face '{fid}' weights must be an object"
            for w_emo, w_val in weights.items():
                if w_emo not in VALID_EMOTIONS or not isinstance(w_val, (int, float)) or isinstance(w_val, bool):
                    return False, f"face '{fid}' weights entry '{w_emo}' is invalid"

    # 5. Layers
    for lid, layer in data["layers"].items():
        if not isinstance(layer, dict):
            return False, f"layer '{lid}' must be an object"
        l_canvas = layer.get("canvas")
        if l_canvas not in data["canvases"]:
            return False, f"layer '{lid}' references unknown canvas '{l_canvas}'"
        cw = data["canvases"][l_canvas]["width"]
        ch = data["canvases"][l_canvas]["height"]

        rect = layer.get("rect")
        if not isinstance(rect, dict):
            return False, f"layer '{lid}' rect must be an object"
        rx, ry, rw, rh = rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height")
        if not (_is_nonnegative_int(rx) and _is_nonnegative_int(ry) and _is_positive_int(rw) and _is_positive_int(rh)):
            return False, f"layer '{lid}' rect coordinates and dimensions must be positive integers"
        if rx + rw > cw or ry + rh > ch:
            return False, f"layer '{lid}' rect lies outside canvas '{l_canvas}'"

        z = layer.get("z")
        if not isinstance(z, int) or isinstance(z, bool):
            return False, f"layer '{lid}' z must be an integer"
        op = layer.get("opacity")
        if not isinstance(op, (int, float)) or isinstance(op, bool) or not (0 <= op <= 1) or not math.isfinite(op):
            return False, f"layer '{lid}' opacity must be a number between 0 and 1"

        file_path = layer.get("file")
        if not isinstance(file_path, str) or not file_path:
            return False, f"layer '{lid}' file must be a non-empty string"
        if "\\" in file_path or file_path.startswith("/") or ":" in file_path or ".." in file_path.split("/"):
            return False, f"layer '{lid}' file '{file_path}' is not a safe relative path"
        if not (file_path.lower().endswith(".webp") or file_path.lower().endswith(".png")):
            return False, f"layer '{lid}' file '{file_path}' must end in .webp or .png"
        name = file_path.rsplit("/", 1)[-1]
        if (file_path != f"layers/{l_canvas}/{name}"
                or not LAYER_FILE_NAME_RE.fullmatch(name) or ".." in name):
            return False, f"layer '{lid}' file must match layers/{l_canvas}/<name>.webp or .png"
        try:
            resolved_file = (root / file_path).resolve()
            if not resolved_file.is_relative_to(root.resolve() / "layers" / l_canvas):
                return False, f"layer '{lid}' file '{file_path}' escapes layer canvas folder"
        except (ValueError, OSError):
            return False, f"layer '{lid}' file '{file_path}' path resolution failed"

    # 6. Looks
    used_outfits = set()
    used_poses = set()

    def _check_layer(lid: Any, context_name: str, look_key: str, l_canvas: str) -> str | None:
        if lid not in data["layers"]:
            return f"look '{look_key}' {context_name} references unknown layer '{lid}'"
        if data["layers"][lid]["canvas"] != l_canvas:
            return (
                f"look '{look_key}' {context_name} layer '{lid}' canvas "
                f"'{data['layers'][lid]['canvas']}' does not match pose canvas '{l_canvas}'"
            )
        return None

    for look_key, look in data["looks"].items():
        if not isinstance(look, dict):
            return False, f"look '{look_key}' must be an object"
        l_pose = look.get("pose")
        l_outfit = look.get("outfit")
        if l_pose not in data["poses"]:
            return False, f"look '{look_key}' references unknown pose '{l_pose}'"
        if l_outfit not in data["outfits"]:
            return False, f"look '{look_key}' references unknown outfit '{l_outfit}'"
        if look_key != f"{l_pose}/{l_outfit}":
            return False, f"look key '{look_key}' does not match pose/outfit '{l_pose}/{l_outfit}'"

        used_outfits.add(l_outfit)
        used_poses.add(l_pose)

        l_canvas = data["poses"][l_pose]["canvas"]
        cw = data["canvases"][l_canvas]["width"]
        ch = data["canvases"][l_canvas]["height"]

        crop = look.get("crop")
        if not isinstance(crop, dict):
            return False, f"look '{look_key}' crop must be an object"
        cl, ct, cw_crop, ch_crop = crop.get("left"), crop.get("top"), crop.get("width"), crop.get("height")
        if not (_is_nonnegative_int(cl) and _is_nonnegative_int(ct) and _is_positive_int(cw_crop) and _is_positive_int(ch_crop)):
            return False, f"look '{look_key}' crop coordinates and dimensions must be positive integers"
        if cl + cw_crop > cw or ct + ch_crop > ch:
            return False, f"look '{look_key}' crop lies outside canvas '{l_canvas}'"

        face_rect = look.get("faceRect")
        if not isinstance(face_rect, dict):
            return False, f"look '{look_key}' faceRect must be an object"
        fx, fy, fw, fh = face_rect.get("x"), face_rect.get("y"), face_rect.get("width"), face_rect.get("height")
        if not (_is_nonnegative_int(fx) and _is_nonnegative_int(fy) and _is_positive_int(fw) and _is_positive_int(fh)):
            return False, f"look '{look_key}' faceRect coordinates and dimensions must be positive integers"
        if fx + fw > cw_crop or fy + fh > ch_crop:
            return False, f"look '{look_key}' faceRect lies outside crop"

        head_anchor = look.get("headAnchor")
        if not isinstance(head_anchor, dict):
            return False, f"look '{look_key}' headAnchor must be an object"
        hx, hy = head_anchor.get("x"), head_anchor.get("y")
        if not (_is_nonnegative_int(hx) and _is_nonnegative_int(hy)):
            return False, f"look '{look_key}' headAnchor coordinates must be non-negative integers"
        if hx > cw_crop or hy > ch_crop:
            return False, f"look '{look_key}' headAnchor lies outside crop"

        if "stage" in look:
            stage = look["stage"]
            if not isinstance(stage, dict):
                return False, f"look '{look_key}' stage must be an object"
            scale = stage.get("scale")
            if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0 or not math.isfinite(scale):
                return False, f"look '{look_key}' stage scale must be a positive number"
            offset = stage.get("offset")
            if not isinstance(offset, dict) or not isinstance(offset.get("x"), (int, float)) or not isinstance(offset.get("y"), (int, float)):
                return False, f"look '{look_key}' stage offset must have finite x and y"

        body = look.get("body")
        if not isinstance(body, list) or not body:
            return False, f"look '{look_key}' body must be a non-empty list of layer ids"
        for lid in body:
            err = _check_layer(lid, "body", look_key, l_canvas)
            if err:
                return False, err

        if "blush" in look:
            blush = look["blush"]
            if not isinstance(blush, list):
                return False, f"look '{look_key}' blush must be a list"
            for lid in blush:
                err = _check_layer(lid, "blush", look_key, l_canvas)
                if err:
                    return False, err

        if "fx" in look:
            fx_dict = look["fx"]
            if not isinstance(fx_dict, dict):
                return False, f"look '{look_key}' fx must be an object"
            for fx_name, fx_layers in fx_dict.items():
                if not isinstance(fx_layers, list):
                    return False, f"look '{look_key}' fx '{fx_name}' must be a list"
                for lid in fx_layers:
                    err = _check_layer(lid, f"fx '{fx_name}'", look_key, l_canvas)
                    if err:
                        return False, err

        look_faces = look.get("faces")
        if not isinstance(look_faces, dict) or not look_faces:
            return False, f"look '{look_key}' faces must be a non-empty object"

        default_face = look.get("defaultFace")
        if default_face not in look_faces:
            return False, f"look '{look_key}' defaultFace '{default_face}' not in look faces"

        for l_fid, l_fentry in look_faces.items():
            if not isinstance(l_fentry, dict):
                return False, f"look '{look_key}' face '{l_fid}' must be an object"
            if l_fid not in data["faces"]:
                return False, f"look '{look_key}' face '{l_fid}' not found in top-level faces"
            f_layers = l_fentry.get("layers")
            if not isinstance(f_layers, list) or not f_layers:
                return False, f"look '{look_key}' face '{l_fid}' layers must be a non-empty list"
            for lid in f_layers:
                err = _check_layer(lid, f"face '{l_fid}'", look_key, l_canvas)
                if err:
                    return False, err
            eyes_closed = l_fentry.get("eyesClosed")
            if not isinstance(eyes_closed, bool):
                return False, f"look '{look_key}' face '{l_fid}' eyesClosed must be a boolean"
            if "blush" in l_fentry:
                f_blush = l_fentry["blush"]
                if not isinstance(f_blush, list):
                    return False, f"look '{look_key}' face '{l_fid}' blush must be a list"
                for lid in f_blush:
                    err = _check_layer(lid, f"face '{l_fid}' blush", look_key, l_canvas)
                    if err:
                        return False, err

            if "blink" in l_fentry:
                blink = l_fentry["blink"]
                if not isinstance(blink, dict):
                    return False, f"look '{look_key}' face '{l_fid}' blink must be an object"
                if eyes_closed is not False:
                    return False, f"look '{look_key}' face '{l_fid}' has blink but eyesClosed is true"
                if "closed" not in blink:
                    return False, f"look '{look_key}' face '{l_fid}' blink missing 'closed'"
                closed_id = blink["closed"]
                if closed_id not in look_faces:
                    return False, f"look '{look_key}' face '{l_fid}' blink closed partner '{closed_id}' not in look faces"
                if look_faces[closed_id].get("eyesClosed") is not True:
                    return False, f"look '{look_key}' face '{l_fid}' blink closed partner '{closed_id}' must have eyesClosed: true"
                if "half" in blink:
                    half_id = blink["half"]
                    if half_id not in look_faces:
                        return False, f"look '{look_key}' face '{l_fid}' blink half partner '{half_id}' not in look faces"

            if "mouth" in l_fentry:
                mouth = l_fentry["mouth"]
                if not isinstance(mouth, dict):
                    return False, f"look '{look_key}' face '{l_fid}' mouth must be an object"
                if "closed" not in mouth or "open" not in mouth:
                    return False, f"look '{look_key}' face '{l_fid}' mouth must have 'closed' and 'open'"
                m_closed = mouth["closed"]
                m_open = mouth["open"]
                if m_closed not in look_faces:
                    return False, f"look '{look_key}' face '{l_fid}' mouth closed partner '{m_closed}' not in look faces"
                if m_open not in look_faces:
                    return False, f"look '{look_key}' face '{l_fid}' mouth open partner '{m_open}' not in look faces"
                if "half" in mouth:
                    m_half = mouth["half"]
                    if m_half not in look_faces:
                        return False, f"look '{look_key}' face '{l_fid}' mouth half partner '{m_half}' not in look faces"

            if "combinedFrames" in l_fentry:
                combined = l_fentry["combinedFrames"]
                if not isinstance(combined, dict):
                    return False, f"look '{look_key}' face '{l_fid}' combinedFrames must be an object"
                for ck, cv in combined.items():
                    parts = ck.split(":")
                    if len(parts) != 2:
                        return False, f"look '{look_key}' face '{l_fid}' combinedFrames key '{ck}' malformed"
                    eye_part, mouth_part = parts
                    if eye_part not in ("half", "closed") or mouth_part not in ("half", "open"):
                        return False, f"look '{look_key}' face '{l_fid}' combinedFrames key '{ck}' has invalid phases"
                    if cv not in look_faces:
                        return False, f"look '{look_key}' face '{l_fid}' combinedFrames target '{cv}' not in look faces"

    # 7. Cross-checks
    default_look_key = f"{default_pose}/{default_outfit}"
    if default_look_key not in data["looks"]:
        return False, f"default look '{default_look_key}' does not exist in looks"

    for oid in data["outfits"]:
        if oid not in used_outfits:
            return False, f"outfit '{oid}' is not used by any look"

    for pid in data["poses"]:
        if pid not in used_poses:
            return False, f"pose '{pid}' is not used by any look"

    return True, ""


def _normalize_v3(root: Path, data: dict[str, Any]) -> dict[str, Any]:
    default_outfit = data["defaultOutfit"]
    default_pose = data["defaultPose"]

    metadata = []
    for oid, outfit in data["outfits"].items():
        look_key = f"{default_pose}/{oid}"
        if look_key not in data["looks"]:
            look_key = next((k for k, lk in data["looks"].items() if lk.get("outfit") == oid), None)
        default_look = data["looks"][look_key] if look_key else None

        costume_meta: dict[str, Any] = {
            "id": oid,
            "name": outfit.get("name") or oid,
            "icon": outfit.get("icon") or "🎒",
        }
        if default_look:
            costume_meta["crop"] = default_look["crop"]
            costume_meta["faceRect"] = default_look["faceRect"]
            costume_meta["headAnchor"] = default_look["headAnchor"]
            if "stage" in default_look:
                costume_meta["stage"] = default_look["stage"]
        metadata.append(costume_meta)

    faces = {}
    for fid, f in data["faces"].items():
        emotion = f.get("emotion", "gentle")
        weights = f.get("weights")
        if not isinstance(weights, dict):
            weights = {emotion: 3}
        faces[fid] = {
            "label": f.get("label", fid),
            "primary": emotion,
            "emotion": emotion,
            "weights": weights,
            "jp": f.get("jp", ""),
            "parts": [],
            "blush": None,
        }

    pools = {}
    default_look = data["looks"].get(f"{default_pose}/{default_outfit}")
    default_face_id = default_look.get("defaultFace") if default_look else next(iter(faces))

    for emotion, aliases in EMOTION_FACES.items():
        candidates = []
        for fid, f_meta in faces.items():
            f_weights = f_meta.get("weights", {})
            if emotion in f_weights:
                candidates.append((fid, f_weights[emotion]))
            elif f_meta.get("primary") == emotion or f_meta.get("primary") in aliases:
                candidates.append((fid, 3 if f_meta.get("primary") == emotion else 1))
        candidates.sort(key=lambda item: item[1], reverse=True)
        if not candidates:
            candidates = [(default_face_id, 1)]
        pools[emotion] = [{"id": cid, "strength": strength} for cid, strength in candidates]

    return {
        "version": 3,
        "enabled": True,
        "default_costume": default_outfit,
        "defaultOutfit": default_outfit,
        "default_pose": default_pose,
        "defaultPose": default_pose,
        "costumes": metadata,
        "outfits": data["outfits"],
        "canvases": data["canvases"],
        "poses": data["poses"],
        "faces": faces,
        "expression_sets": pools,
        "looks": data["looks"],
        "layers": data["layers"],
        "sprites": {},
    }


def load_portrait_manifest(root: Path) -> dict[str, Any] | None:
    """Prefer layered portrait packs (v3), then numbered differentials (v2), then legacy packs (v1)."""
    raw_v3 = _read_object(root / "layers.json")
    if raw_v3 is not None:
        valid, reason = _validate_v3(root, raw_v3)
        if valid:
            payload = _normalize_v3(root, raw_v3)
            if payload is not None:
                legacy = _load_whole_image_manifest(root)
                if legacy is not None:
                    payload["legacy"] = legacy
                return payload
        else:
            logger.warning("Invalid layers.json in %s: %s", root, reason)

    return _load_whole_image_manifest(root)


def _load_whole_image_manifest(root: Path) -> dict[str, Any] | None:
    """Load the whole-image payload for fallback and the legacy chat UI."""
    for filename, legacy in (("expressions.json", False), ("manifest.json", True)):
        raw = _read_object(root / filename)
        if raw is not None:
            payload = _normalize(root, raw, legacy=legacy)
            if payload is not None:
                return payload
    return None
