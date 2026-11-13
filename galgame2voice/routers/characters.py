"""
Character Management REST API Router for galgame2voice (/api/characters).
Provides unified character profile query, active character switching, and affection integration.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote
import aiosqlite
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.routers.common import validate_user_id
from galgame2voice.security.media_urls import (
    get_media_signing_key,
    sign_media_url,
    verify_media_url_signature,
)
from galgame2voice.services.character_manager import get_character_manager, get_character_work
from galgame2voice.services.character_package_import import import_character_archive
from galgame2voice.services.character_voice_catalog import get_character_voice_catalog
from galgame2voice.schemas.character_manifest import clean_character_work
from galgame2voice.services.portrait_manifest import (
    CANVAS_ID_RE,
    LAYER_FILE_NAME_RE,
    load_portrait_manifest,
)
from galgame2voice.services.voice_manager import get_voice_manager
from galgame2voice.utils.logger import sanitize_error_detail

logger = logging.getLogger("galgame2voice.routers.characters")

router = APIRouter(prefix="/api/characters", tags=["characters"])
media_router = APIRouter(prefix="/api/media", tags=["media"])


async def _get_voice_profile_or_404(conn: aiosqlite.Connection, character_id: int) -> Any:
    """Fetches voice profile by ID or raises 404 HTTPException if not found."""
    prof = await crud.get_voice_profile(conn, character_id)
    if not prof:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Character with ID {character_id} not found",
        )
    return prof


def _rewrite_sprite_urls(data: dict[str, Any], character_id: int, root: Path) -> None:
    """把立绘清单里 sprites 和 v3 layers 的 file 路径改写为签名媒体 URL（密钥不可用时回退为旧版文件接口 URL）。"""
    sprites = data.get("sprites") or {}
    if not isinstance(sprites, dict):
        return
    for costume_id, faces in sprites.items():
        if not isinstance(faces, dict):
            continue
        for _face_key, sprite in faces.items():
            if isinstance(sprite, dict) and "file" in sprite:
                file_name = str(sprite["file"]).rsplit("/", 1)[-1]
                v = None
                try:
                    info = (root / costume_id / file_name).stat()
                    v = f"{info.st_mtime_ns:x}-{info.st_size:x}"
                except OSError:
                    pass

                signing_key = get_media_signing_key()
                if signing_key is None:
                    url = (
                        f"/api/characters/{character_id}/portrait/file/"
                        f"{quote(costume_id, safe='')}/{quote(file_name, safe='')}"
                    )
                    if v:
                        url += f"?v={v}"
                    sprite["file"] = url
                else:
                    media_path = (
                        f"/api/media/portrait/{character_id}/"
                        f"{quote(costume_id, safe='')}/{quote(file_name, safe='')}"
                    )
                    sprite["file"] = sign_media_url(media_path, v=v)

    legacy = data.get("legacy")
    if isinstance(legacy, dict):
        _rewrite_sprite_urls(legacy, character_id, root)

    layers = data.get("layers") or {}
    if isinstance(layers, dict):
        signing_key = get_media_signing_key()
        for layer in layers.values():
            if not isinstance(layer, dict) or "file" not in layer:
                continue
            raw_file = layer["file"]
            canvas = layer["canvas"]
            file_name = raw_file.rsplit("/", 1)[-1]
            v = None
            try:
                info = (root / raw_file).stat()
                v = f"{info.st_mtime_ns:x}-{info.st_size:x}"
            except OSError:
                pass
            if signing_key is None:
                url = (
                    f"/api/characters/{character_id}/portrait/layers/"
                    f"{quote(canvas, safe='')}/{quote(file_name, safe='')}"
                )
                if v:
                    url += f"?v={v}"
                layer["file"] = url
            else:
                media_path = (
                    f"/api/media/portrait/{character_id}/layers/"
                    f"{quote(canvas, safe='')}/{quote(file_name, safe='')}"
                )
                layer["file"] = sign_media_url(media_path, v=v)


def _resolve_character_portrait(char_name: str, character_id: int | None = None) -> dict[str, Any] | None:
    """Read numbered differentials and compatible legacy emotion portraits."""
    if not char_name:
        return None
    mgr = get_character_manager()
    pkg = mgr.get_character(char_name)
    if pkg is None:
        return None

    portrait = load_portrait_manifest(pkg.folder / "portrait")
    if portrait is None:
        return None

    data: dict[str, Any] = {
        **portrait,
        "character_id": pkg.manifest.id,
        "character_name": pkg.manifest.name,
        "work": clean_character_work(getattr(pkg.manifest, "work", None)),
    }
    if character_id is not None:
        _rewrite_sprite_urls(data, character_id, pkg.folder / "portrait")
    return data


class CharacterSwitchRequest(BaseModel):
    character_id: int | None = Field(default=None, ge=1)
    character_name: str | None = Field(default=None, max_length=100)
    force: bool = False


@router.get(
    "",
    summary="List Characters",
    description="Returns all available character profiles with active flag and user affection state.",
)
async def list_characters(
    user_id: str = Query(default="default_user", min_length=1, max_length=128, description="User ID"),
) -> dict[str, Any]:
    clean_user = validate_user_id(user_id)

    async with get_db() as conn:
        try:
            profiles = await crud.list_voice_profiles(conn)
            active_profile = await crud.get_active_voice_profile(conn)
            active_id = active_profile.id if active_profile else (profiles[0].id if profiles else None)

            profile_ids = [prof.id for prof in profiles]
            affections_by_id = await crud.get_user_affections_for_profiles(
                conn, user_id=clean_user, profile_ids=profile_ids
            )

            results = []
            for prof in profiles:
                affection = affections_by_id.get(prof.id)
                results.append({
                    "id": prof.id,
                    "name": prof.name,
                    "description": prof.description,
                    "work": get_character_work(prof.name),
                    "is_active": (prof.id == active_id),
                    "is_default": prof.is_default,
                    "prompt_lang": prof.prompt_lang,
                    "text_lang": prof.text_lang,
                    "affection": affection.model_dump() if affection else None,
                    "portrait": _resolve_character_portrait(prof.name, prof.id),
                })

            return {
                "characters": results,
                "active_character_id": active_id,
                "total": len(results),
                "catalog_warning": get_character_manager().catalog_warning,
            }
        except Exception as exc:
            safe_err = sanitize_error_detail(exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list characters: {safe_err}",
            ) from exc


@router.post(
    "/rescan",
    summary="Rescan Character Packages",
    description="Re-discovers character packages in characters/ and synchronizes them with SQLite voice_profiles without restarting the server.",
)
async def rescan_character_packages(request: Request) -> dict[str, Any]:
    try:
        # Keep scan, DB synchronization and cache invalidation in request order.
        # Store the lock on the app so tests/apps with separate loops do not share it.
        lock = getattr(request.app.state, "character_rescan_lock", None)
        if lock is None:
            lock = request.app.state.character_rescan_lock = asyncio.Lock()
        return await get_character_voice_catalog().rescan_packages(
            lock=lock,
            character_manager=get_character_manager(),
            db_factory=get_db,
        )
    except Exception as exc:
        safe_err = sanitize_error_detail(exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"扫描角色包失败: {safe_err}",
        ) from exc


@router.post(
    "/import",
    response_model=None,
    summary="Import Character Package Archive",
    description="Streams a .zip or .tar character package archive, validates it, installs into characters/, and syncs voice_profiles.",
)
async def import_character_packages(
    request: Request,
    filename: str = Query(default="", max_length=512, description="Original archive or folder name"),
    overwrite: bool = Query(default=False, description="Backup and overwrite existing folder(s) with the same name"),
) -> Any:
    try:
        return await import_character_archive(
            request,
            filename=filename,
            overwrite=overwrite,
        )
    except HTTPException:
        raise
    except Exception as exc:
        safe_err = sanitize_error_detail(exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"导入角色包失败: {safe_err}",
        ) from exc


@router.get(
    "/{character_id}/portrait",
    summary="Get Character Standing CG Portrait Manifest",
    description="Returns available standing CG sprites, outfits, emotions and coordinates.",
)
async def get_character_portrait(character_id: int) -> dict[str, Any]:
    async with get_db() as conn:
        prof = await _get_voice_profile_or_404(conn, character_id)
        portrait = _resolve_character_portrait(prof.name, character_id)
        if not portrait:
            return {"enabled": False, "message": "No standing CG portrait available for this character"}
        return {"enabled": True, **portrait}


@router.get(
    "/{character_id}/portrait/file/{costume}/{file_name}",
    summary="Get Character Portrait Sprite Image",
    description="Serves a standing CG portrait sprite image from the character's self-contained package.",
)
async def get_character_portrait_file(character_id: int, costume: str, file_name: str) -> FileResponse:
    # 安全：拒绝路径遍历
    if ".." in costume or ".." in file_name or "/" in file_name or "\\" in file_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sprite path")

    async with get_db() as conn:
        prof = await _get_voice_profile_or_404(conn, character_id)

    mgr = get_character_manager()
    pkg = mgr.get_character(prof.name)
    if pkg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Character package not found")

    portraits_root = (pkg.folder / "portrait").resolve()
    file_path = (portraits_root / costume / file_name).resolve()
    if not file_path.is_relative_to(portraits_root) or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sprite image not found")

    return FileResponse(str(file_path))


@media_router.get(
    "/portrait/{character_id}/{costume}/{file_name}",
    summary="Get Signed Character Portrait Sprite Image",
    description="Serves an HMAC-SHA256 signed, expiring standing CG portrait sprite image without requiring a console token header.",
)
async def get_media_portrait(
    request: Request,
    character_id: int,
    costume: str,
    file_name: str,
    v: str = Query(default=""),
    exp: str | None = Query(default=None),
    sig: str | None = Query(default=None),
) -> FileResponse:
    # 1. Reject path traversal
    raw_path = request.scope.get("raw_path", b"").decode("latin-1", errors="ignore")
    raw_lower = raw_path.lower()
    if "%2e%2e" in raw_lower or "%2f" in raw_lower or "%5c" in raw_lower:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sprite path")

    if (
        not costume
        or not file_name
        or ".." in costume
        or ".." in file_name
        or "/" in costume
        or "/" in file_name
        or "\\" in costume
        or "\\" in file_name
        or ":" in costume
        or ":" in file_name
        or "\0" in costume
        or "\0" in file_name
        or costume in (".", "..")
        or file_name in (".", "..")
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sprite path")

    unquoted_costume = unquote(costume)
    unquoted_file = unquote(file_name)
    if (
        ".." in unquoted_costume
        or ".." in unquoted_file
        or "/" in unquoted_costume
        or "/" in unquoted_file
        or "\\" in unquoted_costume
        or "\\" in unquoted_file
        or ":" in unquoted_costume
        or ":" in unquoted_file
        or "\0" in unquoted_costume
        or "\0" in unquoted_file
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sprite path")

    if Path(unquoted_costume).is_absolute() or Path(unquoted_file).is_absolute():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sprite path")

    # 2. Verify signature and expiry (missing, invalid, or expired -> 403 Forbidden)
    if not sig or exp is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    try:
        exp_int = int(exp)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden") from None

    decoded_path = f"/api/media/portrait/{character_id}/{costume}/{file_name}"
    if not verify_media_url_signature(decoded_path, exp_int, v, sig):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    # 3. Locate character and file
    if character_id < 1:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Character not found")

    async with get_db() as conn:
        prof = await _get_voice_profile_or_404(conn, character_id)

    mgr = get_character_manager()
    pkg = mgr.get_character(prof.name)
    if pkg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Character package not found")

    portraits_root = (pkg.folder / "portrait").resolve()
    file_path = (portraits_root / costume / file_name).resolve()
    if not file_path.is_relative_to(portraits_root) or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sprite image not found")

    if file_path.suffix.lower() not in {".png", ".webp", ".jpg", ".jpeg"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sprite file type")

    # 4. Cache-Control header: private, max-age capped at one day (86400s)
    now = time.time()
    seconds_until_exp = max(0, int(exp_int - now))
    max_age = min(seconds_until_exp, 86400)
    return FileResponse(str(file_path), headers={"Cache-Control": f"private, max-age={max_age}"})


def _validate_layer_segments(request: Request, canvas: str, file_name: str) -> tuple[str, str]:
    """Reject encoded separators and traversal before interpreting either segment."""
    raw_path = request.scope.get("raw_path", b"").decode("latin-1", errors="ignore").lower()
    if any(encoded in raw_path for encoded in ("%2e%2e", "%2f", "%5c")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sprite path")

    for segments in ((canvas, file_name), (unquote(canvas), unquote(file_name))):
        for segment in segments:
            if (not segment or ".." in segment
                    or any(char in segment for char in ("/", "\\", ":", "\0"))
                    or segment in (".", "..") or Path(segment).is_absolute()):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sprite path")
        if not CANVAS_ID_RE.fullmatch(segments[0]) or not LAYER_FILE_NAME_RE.fullmatch(segments[1]):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid layer path")
    return unquote(canvas), unquote(file_name)


async def _resolve_layer_file(character_id: int, canvas: str, file_name: str) -> Path:
    if character_id < 1:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Character not found")
    async with get_db() as conn:
        prof = await _get_voice_profile_or_404(conn, character_id)
    pkg = get_character_manager().get_character(prof.name)
    if pkg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Character package not found")

    # Keep the expected canvas directory literal so symlinks cannot switch canvases.
    canvas_root = (pkg.folder / "portrait").resolve() / "layers" / canvas
    file_path = (canvas_root / file_name).resolve()
    if not file_path.is_relative_to(canvas_root) or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sprite image not found")
    return file_path


@router.get("/{character_id}/portrait/layers/{canvas}/{file_name}")
async def get_character_portrait_layer(
    request: Request, character_id: int, canvas: str, file_name: str,
) -> FileResponse:
    canvas, file_name = _validate_layer_segments(request, canvas, file_name)
    return FileResponse(str(await _resolve_layer_file(character_id, canvas, file_name)))


@media_router.get("/portrait/{character_id}/layers/{canvas}/{file_name}")
async def get_media_portrait_layer(
    request: Request,
    character_id: int,
    canvas: str,
    file_name: str,
    v: str = Query(default=""),
    exp: str | None = Query(default=None),
    sig: str | None = Query(default=None),
) -> FileResponse:
    canvas, file_name = _validate_layer_segments(request, canvas, file_name)
    if not sig or exp is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    try:
        exp_int = int(exp)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden") from None

    decoded_path = f"/api/media/portrait/{character_id}/layers/{canvas}/{file_name}"
    if not verify_media_url_signature(decoded_path, exp_int, v, sig):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    file_path = await _resolve_layer_file(character_id, canvas, file_name)
    seconds_until_exp = max(0, int(exp_int - time.time()))
    max_age = min(seconds_until_exp, 86400)
    return FileResponse(str(file_path), headers={"Cache-Control": f"private, max-age={max_age}"})


class SystemPromptUpdate(BaseModel):
    system_prompt: str = Field(
        ..., min_length=1, max_length=20000,
        description="角色人设提示词（写回角色包，全局生效）",
    )


@router.put(
    "/{character_id}/system-prompt",
    summary="Update Character System Prompt",
    description="Saves the persona prompt on this voice profile. When the profile belongs "
                "to a character package, also writes that package's manifest.json.",
)
async def update_character_system_prompt(character_id: int, req: SystemPromptUpdate) -> dict[str, Any]:
    return await get_character_voice_catalog().update_persona(
        character_id,
        req.system_prompt,
        update_profile_fn=crud.update_voice_profile,
        character_manager=get_character_manager(),
        db_factory=get_db,
    )


async def _resolve_profile_by_name(conn: aiosqlite.Connection, char_name: str) -> Any | None:
    """Use the same identity rules for switching as for package reconciliation."""
    return await get_character_voice_catalog().resolve_profile_by_name(
        conn, char_name, character_manager=get_character_manager()
    )


async def _lookup_character_profile(
    conn: aiosqlite.Connection,
    char_id: int | None,
    char_name: str | None,
) -> Any | None:
    """Resolves character profile by ID or name fallback."""
    return await get_character_voice_catalog().lookup_profile(
        conn, char_id, char_name, character_manager=get_character_manager()
    )


@router.post(
    "/switch",
    summary="Switch Active Character",
    description="Atomically switches the active character voice profile with memory safety and auto-rollback.",
)
async def switch_character(req: CharacterSwitchRequest) -> dict[str, Any]:
    profile = await get_character_voice_catalog().switch_active_profile(
        profile_id=req.character_id,
        profile_name=req.character_name,
        force=req.force,
        entity_label="Character",
        id_field_name="character_id",
        missing_detail="Missing character_id or character_name in switch request",
        character_manager=get_character_manager(),
        voice_manager=get_voice_manager(),
        db_factory=get_db,
    )
    return {
        "status": "switched",
        "character": profile.name,
        "character_id": profile.id,
    }
