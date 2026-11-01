"""
Character Management REST API Router for galgame2voice (/api/characters).
Provides unified character profile query, active character switching, and affection integration.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote
import aiosqlite
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from galgame2voice.database import crud
from galgame2voice.database.session import get_db, immediate_transaction
from galgame2voice.routers.common import persist_active_voice_profile_or_raise, switch_voice_profile_or_raise, validate_user_id
from galgame2voice.security.media_urls import (
    get_media_signing_key,
    sign_media_url,
    verify_media_url_signature,
)
from galgame2voice.services.character_manager import get_character_manager, get_character_work
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

# 同一进程里所有保存人设请求共用这一把锁。后来的保存要等前一次
# 写完 manifest、重新扫描并写完数据库之后才开始，避免磁盘和数据库各留一份。
_persona_save_lock = asyncio.Lock()


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
        async with lock:
            char_mgr = get_character_manager()
            discovered = await asyncio.to_thread(char_mgr.discover_characters)
            async with get_db() as conn:
                synced = await char_mgr.sync_with_db(conn)
                profiles = await crud.list_voice_profiles(conn)
                active = await crud.get_active_voice_profile(conn)
            from galgame2voice.routers.voice import _scan_cache
            generation = _scan_cache.get("generation", 0) + 1
            _scan_cache.clear()
            _scan_cache["generation"] = generation
        return {
            "status": "ok",
            "discovered_count": len(discovered),
            "synced_count": synced,
            "total_profiles": len(profiles),
            "active_profile_id": active.id if active else (profiles[0].id if profiles else None),
            "characters": [p.name for p in discovered],
            # Rejected folders and why, the same notice the voice list shows.
            "catalog_warning": char_mgr.catalog_warning,
        }
    except Exception as exc:
        safe_err = sanitize_error_detail(exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"扫描角色包失败: {safe_err}",
        ) from exc


@router.get(
    "/{character_id}",
    summary="Get Character Details",
    description="Returns details of a specific character profile including affection state.",
)
async def get_character_detail(
    character_id: int,
    user_id: str = Query(default="default_user", min_length=1, max_length=128, description="User ID"),
) -> dict[str, Any]:
    if character_id < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="character_id must be a positive integer >= 1",
        )

    clean_user = validate_user_id(user_id)

    async with get_db() as conn:
        try:
            prof = await _get_voice_profile_or_404(conn, character_id)

            active_profile = await crud.get_active_voice_profile(conn)
            active_id = active_profile.id if active_profile else 1
            affection = await crud.get_or_create_character_affection(
                conn, user_id=clean_user, character_id=prof.id
            )

            return {
                "id": prof.id,
                "name": prof.name,
                "description": prof.description,
                "work": get_character_work(prof.name),
                "is_active": (prof.id == active_id),
                "is_default": prof.is_default,
                "gpt_weights_path": prof.gpt_weights_path,
                "sovits_weights_path": prof.sovits_weights_path,
                "ref_audio_path": prof.ref_audio_path,
                "prompt_text": prof.prompt_text,
                "prompt_lang": prof.prompt_lang,
                "text_lang": prof.text_lang,
                "system_prompt": prof.system_prompt,
                "affection": affection.model_dump() if affection else None,
                "portrait": _resolve_character_portrait(prof.name, prof.id),
            }
        except HTTPException:
            raise
        except Exception as exc:
            safe_err = sanitize_error_detail(exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get character: {safe_err}",
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

    decoded_path = f"/api/media/portrait/{character_id}/{unquoted_costume}/{unquoted_file}"
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
    file_path = (portraits_root / unquoted_costume / unquoted_file).resolve()
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


def _restore_manifest(manifest_path: Path, original: bytes) -> None:
    """Put manifest.json back to the bytes read before this save."""
    tmp_path = manifest_path.with_name("manifest.json.tmp")
    try:
        tmp_path.write_bytes(original)
        os.replace(tmp_path, manifest_path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def _write_manifest_system_prompt(manifest_path: Path, text: str) -> None:
    """Safely updates system_prompt in manifest.json using atomic temp file replacement."""
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        safe_err = sanitize_error_detail(exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"无法解析角色 manifest.json: {safe_err}",
        ) from exc

    data["system_prompt"] = text
    tmp_path = manifest_path.with_name("manifest.json.tmp")
    try:
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp_path, manifest_path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        safe_err = sanitize_error_detail(exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"写入角色 manifest.json 失败: {safe_err}",
        ) from exc


@router.put(
    "/{character_id}/system-prompt",
    summary="Update Character System Prompt",
    description="Saves the persona prompt on this voice profile. When the profile belongs "
                "to a character package, also writes that package's manifest.json.",
)
async def update_character_system_prompt(character_id: int, req: SystemPromptUpdate) -> dict[str, Any]:
    text = req.system_prompt.strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="人设提示词不能为空",
        )

    from galgame2voice.services.character_identity import (
        DuplicateCharacterId,
        owned_package_for_profile,
    )

    async with get_db() as conn:
        prof = await _get_voice_profile_or_404(conn, character_id)
        profiles = await crud.list_voice_profiles(conn)

    mgr = get_character_manager()
    pkg = owned_package_for_profile(prof, profiles, mgr)
    manifest_path: Path | None = None
    async with _persona_save_lock:
        if pkg is not None:
            manifest_path = pkg.folder / "manifest.json"
            if not manifest_path.is_file():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"角色包缺少 manifest.json: {manifest_path}",
                )
            original_manifest = manifest_path.read_bytes()
            _write_manifest_system_prompt(manifest_path, text)
            try:
                # 内存里的角色包缓存也要刷新，否则本进程后续仍读到旧人设
                mgr.discover_characters()
            except DuplicateCharacterId as exc:
                _restore_manifest(manifest_path, original_manifest)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=str(exc),
                ) from exc
            except Exception:
                _restore_manifest(manifest_path, original_manifest)
                raise

        async with get_db() as conn:
            async with immediate_transaction(conn):
                from galgame2voice.database.models import VoiceProfileUpdate

                await crud.update_voice_profile(conn, character_id, VoiceProfileUpdate(system_prompt=text))

    logger.info("System prompt updated for character %s (%d chars)", prof.name, len(text))
    return {
        "character_id": character_id,
        "name": prof.name,
        "system_prompt": text,
        "length": len(text),
        "manifest_path": str(manifest_path) if manifest_path is not None else None,
    }


async def _resolve_profile_by_name(conn: aiosqlite.Connection, char_name: str) -> Any | None:
    """Use the same identity rules for switching as for package reconciliation."""
    from galgame2voice.services.character_identity import (
        AmbiguousProfileName,
        resolve_profile_for_query,
    )

    cm = get_character_manager()

    async def find_profile() -> tuple[Any | None, Any | None]:
        profiles = await crud.list_voice_profiles(conn)
        try:
            matched, pkg, conflicting = resolve_profile_for_query(profiles, char_name, manager=cm)
        except AmbiguousProfileName as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        if conflicting:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"角色“{char_name}”匹配到多个角色包；请使用唯一的角色 ID。",
            )
        return matched, pkg

    profile, pkg = await find_profile()
    if profile is None and pkg is not None:
        await cm.sync_with_db(conn)
        profile, _ = await find_profile()
    return profile


async def _lookup_character_profile(
    conn: aiosqlite.Connection,
    char_id: int | None,
    char_name: str | None,
) -> Any | None:
    """Resolves character profile by ID or name fallback."""
    if char_id is not None:
        return await crud.get_voice_profile(conn, char_id)
    if char_name:
        return await _resolve_profile_by_name(conn, char_name)
    return None


@router.post(
    "/switch",
    summary="Switch Active Character",
    description="Atomically switches the active character voice profile with memory safety and auto-rollback.",
)
async def switch_character(req: CharacterSwitchRequest) -> dict[str, Any]:
    char_id = req.character_id
    raw_name = req.character_name
    char_name = raw_name.strip() if raw_name else None

    if char_id is None and not char_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing character_id or character_name in switch request",
        )

    if char_id is not None and char_id < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="character_id must be a positive integer >= 1",
        )

    # 1. 404 Precedence: Resolve character entity first from database
    ident = char_id if char_id is not None else char_name
    async with get_db() as conn:
        profile = await _lookup_character_profile(conn, char_id, char_name)
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Character '{ident}' not found",
            )

    manager = get_voice_manager()

    # 2. Concurrency & State Guard
    async with manager.switch_lock:
        async with get_db() as conn:
            verified_profile = await crud.get_voice_profile(conn, profile.id)
            if not verified_profile:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Character '{ident}' not found",
                )
            profile = verified_profile

        is_already_active = await asyncio.to_thread(manager.is_active_profile, profile, force=req.force)

        if is_already_active:
            await persist_active_voice_profile_or_raise(profile.id)
        else:
            await switch_voice_profile_or_raise(manager, profile, force=req.force)

        return {
            "status": "switched",
            "character": profile.name,
            "character_id": profile.id,
        }
