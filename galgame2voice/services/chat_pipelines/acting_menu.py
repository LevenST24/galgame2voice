"""Acting menu for the P2b reply format and per-line act sanitization.

The menu advertises only choices a whole-image portrait pack can actually render,
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

    labels: list[str] = []
    face_ids: dict[str, list[str]] = {}
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


def sanitize_act(act: Any, menu: dict | None, *, line_index: int, character: str) -> dict:
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
    for field, value in act.items():
        if field not in ACT_FIELDS:
            _log_drop(character, line_index, str(field), value, "unsupported act key")
            continue

        if field == "face":
            if menu is None:
                _log_drop(character, line_index, field, value, "no acting menu")
            elif not isinstance(value, str):
                _log_drop(character, line_index, field, value, "face must be a string")
            elif value not in allowed_faces:
                _log_drop(character, line_index, field, value, "face not in menu")
            else:
                clean["face"] = value
        elif field == "pose":
            if menu is None:
                _log_drop(character, line_index, field, value, "no acting menu")
            elif value not in allowed_poses:
                _log_drop(character, line_index, field, value, "pose not in menu")
            else:
                clean["pose"] = value
        elif field == "motion":
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
        else:  # fx
            if value is None or value in FX:
                clean["fx"] = value
            else:
                _log_drop(character, line_index, field, value, "fx not allowed")

    return clean


__all__ = ["build_acting_menu", "sanitize_act"]
