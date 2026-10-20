"""Acting menu for the P2b reply format and per-line act sanitization.

The menu advertises only choices a whole-image or layered portrait pack can actually render,
so the model is never asked for a face, pose or blush overlay that does not exist.
"""

from __future__ import annotations

import logging
from typing import Any

from galgame2voice.services.chat_pipelines.line_scanner import FX, MOTIONS

logger = logging.getLogger(__name__)

FACE_LIMIT = 40
ACT_FIELDS = ("face", "pose", "motion", "blush", "fx")


def _costume_sprites(portrait: dict[str, Any], outfit: str | None) -> tuple[str | None, dict[str, Any]]:
    sprites = portrait.get("sprites")
    if not isinstance(sprites, dict):
        sprites = {}
    if isinstance(outfit, str) and outfit in sprites:
        return outfit, sprites[outfit]
    default_outfit = portrait.get("default_costume")
    if isinstance(default_outfit, str) and default_outfit in sprites:
        return default_outfit, sprites[default_outfit]
    costumes = portrait.get("costumes")
    if isinstance(costumes, list):
        for costume in costumes:
            costume_id = costume.get("id") if isinstance(costume, dict) else None
            if isinstance(costume_id, str) and costume_id in sprites:
                return costume_id, sprites[costume_id]
    if sprites:
        first = next(iter(sprites))
        return first, sprites[first]
    return (outfit if isinstance(outfit, str) else None), {}


def build_acting_menu(portrait: dict | None, outfit: str | None = None) -> dict | None:
    """Builds the acting menu from a normalized portrait payload, or None without one."""
    if portrait is None:
        return None

    if portrait.get("version") == 3:
        outfits = portrait.get("outfits") or {}
        default_costume = portrait.get("default_costume") or portrait.get("defaultOutfit")
        if isinstance(outfit, str) and outfit in outfits:
            resolved_outfit = outfit
        elif isinstance(default_costume, str) and default_costume in outfits:
            resolved_outfit = default_costume
        elif outfits:
            resolved_outfit = next(iter(outfits))
        else:
            resolved_outfit = (outfit if isinstance(outfit, str) else None)

        poses_meta = portrait.get("poses") or {}
        looks_meta = portrait.get("looks") or {}
        poses = [pid for pid in poses_meta if f"{pid}/{resolved_outfit}" in looks_meta]

        default_pose = portrait.get("default_pose") or portrait.get("defaultPose")
        if default_pose in poses:
            default_look_pose = default_pose
        elif poses:
            default_look_pose = poses[0]
        else:
            default_look_pose = None

        if default_look_pose is not None:
            default_look = looks_meta.get(f"{default_look_pose}/{resolved_outfit}") or {}
        else:
            default_look = {}

        faces_meta = portrait.get("faces") or {}
        look_faces_dict = default_look.get("faces") or {}
        labels: list[str] = []
        face_ids: dict[str, list[str]] = {}
        for face_id in look_faces_dict:
            meta = faces_meta.get(face_id)
            label = meta.get("label") if isinstance(meta, dict) else face_id
            if not isinstance(label, str) or not label:
                label = str(face_id)
            if label not in face_ids:
                if len(labels) >= FACE_LIMIT:
                    continue
                labels.append(label)
                face_ids[label] = []
            face_ids[label].append(face_id)

        look_blush = default_look.get("blush") or []
        blush = False
        for f_entry in look_faces_dict.values():
            if not isinstance(f_entry, dict):
                continue
            f_blush = f_entry.get("blush")
            if f_blush is not None:
                if len(f_blush) > 0:
                    blush = True
                    break
            elif len(look_blush) > 0:
                blush = True
                break

        look_faces: dict[str, list[str]] = {}
        for pid in poses:
            l_look = looks_meta.get(f"{pid}/{resolved_outfit}") or {}
            l_labels: list[str] = []
            for fid in l_look.get("faces", {}):
                meta = faces_meta.get(fid)
                lbl = meta.get("label") if isinstance(meta, dict) else fid
                if not isinstance(lbl, str) or not lbl:
                    lbl = str(fid)
                if lbl not in l_labels:
                    l_labels.append(lbl)
            look_faces[pid] = l_labels

        labels = [label for label in labels
                  if all(label in pose_labels for pose_labels in look_faces.values())]

        return {
            "outfit": resolved_outfit,
            "faces": labels,
            "face_ids": face_ids,
            "poses": poses,
            "pose_names": {pid: poses_meta[pid]["name"] for pid in poses},
            "blush": blush,
            "motions": list(MOTIONS),
            "fx": list(FX),
            "default_pose": default_look_pose,
            "look_faces": look_faces,
        }

    resolved_outfit, costume_sprites = _costume_sprites(portrait, outfit)
    if not isinstance(costume_sprites, dict):
        costume_sprites = {}

    faces_meta = portrait.get("faces")
    if not isinstance(faces_meta, dict):
        faces_meta = {}
    blush_targets = {
        meta.get("blush")
        for meta in faces_meta.values()
        if isinstance(meta, dict) and isinstance(meta.get("blush"), str) and meta["blush"]
    }

    labels = []
    face_ids = {}
    for face_id in costume_sprites:
        if face_id in blush_targets:
            continue
        meta = faces_meta.get(face_id)
        label = meta.get("label") if isinstance(meta, dict) else None
        if not isinstance(label, str) or not label:
            continue
        if label not in face_ids:
            if len(labels) >= FACE_LIMIT:
                continue
            labels.append(label)
            face_ids[label] = []
        face_ids[label].append(face_id)

    blush = False
    for label in labels:
        for face_id in face_ids[label]:
            meta = faces_meta.get(face_id)
            target = meta.get("blush") if isinstance(meta, dict) else None
            if isinstance(target, str) and target in costume_sprites:
                blush = True
                break
        if blush:
            break

    return {
        "outfit": resolved_outfit,
        "faces": labels,
        "face_ids": face_ids,
        "poses": [],
        "blush": blush,
        "motions": list(MOTIONS),
        "fx": list(FX),
    }


def _log_drop(character: str, line_index: int, field: str, value: Any, reason: str) -> None:
    logger.info(
        "Dropped act field %s for %s at line %d: %s (%s)",
        field,
        character,
        line_index,
        repr(value)[:40],
        reason,
    )


def sanitize_act(
    act: Any,
    menu: dict | None,
    *,
    line_index: int,
    character: str,
    current_pose: str | None = None,
) -> dict:
    """Drops invalid act fields individually, keeping every valid member of the line."""
    if not isinstance(act, dict):
        logger.info(
            "Dropped act for %s at line %d: %s (act is not a dict)",
            character,
            line_index,
            repr(act)[:40],
        )
        return {}

    faces = menu.get("faces") if isinstance(menu, dict) else None
    poses = menu.get("poses") if isinstance(menu, dict) else None
    allowed_faces = faces if isinstance(faces, list) else []
    allowed_poses = poses if isinstance(poses, list) else []
    blush_available = bool(menu.get("blush")) if isinstance(menu, dict) else False

    clean: dict[str, Any] = {}

    # 1. Validate pose first
    effective_pose = None
    if "pose" in act:
        val = act["pose"]
        if menu is None:
            _log_drop(character, line_index, "pose", val, "no acting menu")
        elif not isinstance(val, str) or val not in allowed_poses:
            _log_drop(character, line_index, "pose", val, "pose not in menu")
        else:
            clean["pose"] = val
            effective_pose = val

    if effective_pose is None:
        if current_pose and current_pose in allowed_poses:
            effective_pose = current_pose
        elif isinstance(menu, dict) and menu.get("default_pose") in allowed_poses:
            effective_pose = menu["default_pose"]
        elif allowed_poses:
            effective_pose = allowed_poses[0]

    # 2. Determine allowed faces for the effective look
    if isinstance(menu, dict) and "look_faces" in menu and effective_pose in menu["look_faces"]:
        look_allowed_faces = menu["look_faces"][effective_pose]
    else:
        look_allowed_faces = allowed_faces

    # 3. Validate face against effective look
    if "face" in act:
        val = act["face"]
        if menu is None:
            _log_drop(character, line_index, "face", val, "no acting menu")
        elif not isinstance(val, str):
            _log_drop(character, line_index, "face", val, "face must be a string")
        elif val not in look_allowed_faces:
            _log_drop(character, line_index, "face", val, "face not in menu")
        else:
            clean["face"] = val

    # 4. Validate motion, blush, fx
    for field in ("motion", "blush", "fx"):
        if field not in act:
            continue
        value = act[field]
        if field == "motion":
            if value is None or value in MOTIONS:
                clean["motion"] = value
            else:
                _log_drop(character, line_index, field, value, "motion not allowed")
        elif field == "blush":
            if not isinstance(value, bool):
                _log_drop(character, line_index, field, value, "blush must be a boolean")
            elif value and not blush_available:
                _log_drop(character, line_index, field, value, "blush not available")
            else:
                clean["blush"] = value
        elif field == "fx":
            if value is None or value in FX:
                clean["fx"] = value
            else:
                _log_drop(character, line_index, field, value, "fx not allowed")

    # 5. Log unsupported act keys
    for field, value in act.items():
        if field not in ACT_FIELDS:
            _log_drop(character, line_index, str(field), value, "unsupported act key")

    return clean


__all__ = ["build_acting_menu", "sanitize_act"]
