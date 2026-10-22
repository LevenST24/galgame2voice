"""Authenticated background listings and expiring signed image links."""

import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from galgame2voice.config import get_settings
from galgame2voice.security.media_urls import sign_media_url, verify_media_url_signature
from galgame2voice.services.background_manifest import (
    BACKGROUND_ID_RE,
    BACKGROUND_VARIANTS,
    load_background_pack,
    load_background_packs,
)

router = APIRouter(prefix="/api/backgrounds", tags=["backgrounds"])
media_router = APIRouter(prefix="/api/media/background", tags=["media"])
# Explicit types: Windows hosts may not map these suffixes, and browsers need image/*.
MEDIA_TYPES = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".avif": "image/avif"}


@router.get("")
def list_backgrounds() -> dict:
    works = []
    backgrounds = []
    for pack in load_background_packs(get_settings().backgrounds_dir):
        works.append({"id": pack.work, "name": pack.name, "default": pack.default_key})
        for background in pack.backgrounds:
            variants = {}
            for variant, path in background.variants.items():
                try:
                    info = path.stat()
                except OSError:
                    continue
                version = f"{info.st_mtime_ns:x}-{info.st_size:x}"
                variants[variant] = sign_media_url(
                    f"/api/media/background/{background.work}/{background.id}/{variant}", v=version,
                )
            if variants:
                backgrounds.append({
                    "key": background.key, "work": background.work,
                    "id": background.id, "name": background.name, "variants": variants,
                })
    return {"works": works, "backgrounds": backgrounds}


@media_router.get("/{work}/{bg_id}/{variant}")
def get_media_background(
    work: str,
    bg_id: str,
    variant: str,
    v: str = Query(default=""),
    exp: str | None = Query(default=None),
    sig: str | None = Query(default=None),
) -> FileResponse:
    if (
        not BACKGROUND_ID_RE.fullmatch(work)
        or not BACKGROUND_ID_RE.fullmatch(bg_id)
        or variant not in BACKGROUND_VARIANTS
    ):
        raise HTTPException(404, "Background not found")
    decoded_path = f"/api/media/background/{work}/{bg_id}/{variant}"
    if not sig or not sig.isascii() or exp is None or not verify_media_url_signature(
        decoded_path, exp, v, sig,
    ):
        raise HTTPException(403, "Forbidden")

    root = get_settings().backgrounds_dir
    pack = load_background_pack(root, work)
    background = next((bg for bg in pack.backgrounds if bg.id == bg_id), None) if pack else None
    path = background.variants.get(variant) if background else None
    if path is None:
        raise HTTPException(404, "Background not found")
    try:
        pack_root = (root / work).resolve()
        path = path.resolve()
        if not path.is_relative_to(pack_root) or not path.is_file():
            raise HTTPException(404, "Background not found")
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(404, "Background not found") from None

    max_age = min(86400, max(0, int(int(exp) - time.time())))
    return FileResponse(path, media_type=MEDIA_TYPES.get(path.suffix.lower()),
                        headers={"Cache-Control": f"private, max-age={max_age}"})
