"""Authenticated background listings and expiring signed image links."""

import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from galgame2voice.config import get_settings
from galgame2voice.security.media_urls import sign_media_url, verify_media_url_signature
from galgame2voice.services.background_manifest import (
    BACKGROUND_ID_RE,
    BACKGROUND_VARIANTS,
    load_background_manifest,
)

router = APIRouter(prefix="/api/backgrounds", tags=["backgrounds"])
media_router = APIRouter(prefix="/api/media/background", tags=["media"])


@router.get("")
def list_backgrounds() -> dict:
    backgrounds = []
    for background in load_background_manifest(get_settings().backgrounds_dir):
        variants = {}
        for variant, path in background.variants.items():
            try:
                info = path.stat()
            except OSError:
                continue
            version = f"{info.st_mtime_ns:x}-{info.st_size:x}"
            variants[variant] = sign_media_url(
                f"/api/media/background/{background.id}/{variant}", v=version,
            )
        if variants:
            backgrounds.append({
                "id": background.id, "name": background.name,
                "source": background.source, "variants": variants,
            })
    return {"backgrounds": backgrounds}


@media_router.get("/{bg_id}/{variant}")
def get_media_background(
    bg_id: str,
    variant: str,
    v: str = Query(default=""),
    exp: str | None = Query(default=None),
    sig: str | None = Query(default=None),
) -> FileResponse:
    if not BACKGROUND_ID_RE.fullmatch(bg_id) or variant not in BACKGROUND_VARIANTS:
        raise HTTPException(404, "Background not found")
    if not sig or not sig.isascii() or exp is None or not verify_media_url_signature(
        f"/api/media/background/{bg_id}/{variant}", exp, v, sig,
    ):
        raise HTTPException(403, "Forbidden")

    root = get_settings().backgrounds_dir
    background = next((bg for bg in load_background_manifest(root) if bg.id == bg_id), None)
    path = background.variants.get(variant) if background else None
    if path is None:
        raise HTTPException(404, "Background not found")
    try:
        path = path.resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise HTTPException(404, "Background not found")
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(404, "Background not found") from None

    max_age = min(86400, max(0, int(int(exp) - time.time())))
    return FileResponse(path, headers={"Cache-Control": f"private, max-age={max_age}"})
