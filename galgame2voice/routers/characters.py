"""
Character Management REST API Router for galgame2voice (/api/characters).
Provides unified character profile query, active character switching, and affection integration.
"""

import logging
import os
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.services.voice_manager import get_voice_manager, InsufficientMemoryError
from galgame2voice.utils.logger import sanitize_error_detail

import json

logger = logging.getLogger("galgame2voice.routers.characters")

router = APIRouter(prefix="/api/characters", tags=["characters"])


def _rewrite_sprite_urls(data: Dict[str, Any], character_id: int) -> None:
    """把立绘清单里 sprites 的 file 路径改写为后端文件接口 URL（自包含读取）。"""
    sprites = data.get("sprites") or {}
    if not isinstance(sprites, dict):
        return
    for costume_id, faces in sprites.items():
        if not isinstance(faces, dict):
            continue
        for _face_key, sprite in faces.items():
            if isinstance(sprite, dict) and "file" in sprite:
                file_name = str(sprite["file"]).rsplit("/", 1)[-1]
                sprite["file"] = f"/api/characters/{character_id}/portrait/file/{costume_id}/{file_name}"


def _resolve_character_portrait(char_name: str, character_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """立绘完全由角色包内的 portrait/expressions.json 驱动（表情编号差分体系）。"""
    if not char_name:
        return None
    from galgame2voice.services.character_manager import get_character_manager
    mgr = get_character_manager()
    pkg = mgr.get_character(char_name)
    if pkg is None:
        return None

    path = pkg.folder / "portrait" / "expressions.json"
    if not path.is_file():
        return None
    try:
        expr = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Malformed portrait index: %s", path)
        return None

    costumes = expr.get("costumes") or {}
    if not costumes:
        return None

    data: Dict[str, Any] = {
        "enabled": True,
        "character_id": pkg.manifest.id,
        "character_name": pkg.manifest.name,
        "pose": expr.get("pose", ""),
        "default_costume": expr.get("default_costume") or next(iter(costumes)),
        "costumes": [
            {"id": cid, "name": c.get("name", cid), "icon": c.get("icon", "🎎")}
            for cid, c in costumes.items()
        ],
        "faces": expr.get("faces") or {},
        "expression_sets": expr.get("expression_sets") or {},
        "sprites": {cid: (c.get("sprites") or {}) for cid, c in costumes.items()},
    }
    if character_id is not None:
        _rewrite_sprite_urls(data, character_id)
    return data


class CharacterSwitchRequest(BaseModel):
    character_id: Optional[int] = Field(default=None, ge=1)
    character_name: Optional[str] = Field(default=None, max_length=100)
    force: bool = False


@router.get(
    "",
    summary="List Characters",
    description="Returns all available character profiles with active flag and user affection state.",
)
async def list_characters(
    user_id: str = Query(default="default_user", min_length=1, max_length=128, description="User ID"),
):
    clean_user = user_id.strip()
    if not clean_user:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="user_id cannot be empty",
        )

    async with get_db() as conn:
        try:
            profiles = await crud.list_voice_profiles(conn)
            active_profile = await crud.get_active_voice_profile(conn)
            active_id = active_profile.id if active_profile else (profiles[0].id if profiles else None)

            results = []
            for prof in profiles:
                affection = await crud.get_or_create_character_affection(
                    conn, user_id=clean_user, character_id=prof.id
                )
                results.append({
                    "id": prof.id,
                    "name": prof.name,
                    "description": prof.description,
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
            }
        except Exception as exc:
            safe_err = sanitize_error_detail(exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list characters: {safe_err}",
            ) from exc


@router.get(
    "/{character_id}",
    summary="Get Character Details",
    description="Returns details of a specific character profile including affection state.",
)
async def get_character_detail(
    character_id: int,
    user_id: str = Query(default="default_user", min_length=1, max_length=128, description="User ID"),
):
    if character_id < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="character_id must be a positive integer >= 1",
        )

    clean_user = user_id.strip()
    if not clean_user:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="user_id cannot be empty",
        )

    async with get_db() as conn:
        try:
            prof = await crud.get_voice_profile(conn, character_id)
            if not prof:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Character with ID {character_id} not found",
                )

            active_profile = await crud.get_active_voice_profile(conn)
            active_id = active_profile.id if active_profile else 1
            affection = await crud.get_or_create_character_affection(
                conn, user_id=clean_user, character_id=prof.id
            )

            return {
                "id": prof.id,
                "name": prof.name,
                "description": prof.description,
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
async def get_character_portrait(character_id: int):
    async with get_db() as conn:
        prof = await crud.get_voice_profile(conn, character_id)
        if not prof:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Character with ID {character_id} not found",
            )
        portrait = _resolve_character_portrait(prof.name, character_id)
        if not portrait:
            return {"enabled": False, "message": "No standing CG portrait available for this character"}
        return {"enabled": True, **portrait}


@router.get(
    "/{character_id}/portrait/file/{costume}/{file_name}",
    summary="Get Character Portrait Sprite Image",
    description="Serves a standing CG portrait sprite image from the character's self-contained package.",
)
async def get_character_portrait_file(character_id: int, costume: str, file_name: str):
    # 安全：拒绝路径遍历
    if ".." in costume or ".." in file_name or "/" in file_name or "\\" in file_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sprite path")

    async with get_db() as conn:
        prof = await crud.get_voice_profile(conn, character_id)
        if not prof:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Character with ID {character_id} not found",
            )

    from galgame2voice.services.character_manager import get_character_manager
    mgr = get_character_manager()
    pkg = mgr.get_character(prof.name)
    if pkg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Character package not found")

    portraits_root = (pkg.folder / "portrait").resolve()
    file_path = (portraits_root / costume / file_name).resolve()
    if not file_path.is_relative_to(portraits_root) or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sprite image not found")

    return FileResponse(str(file_path))


class SystemPromptUpdate(BaseModel):
    system_prompt: str = Field(
        ..., min_length=1, max_length=20000,
        description="角色人设提示词（写回角色包，全局生效）",
    )


@router.put(
    "/{character_id}/system-prompt",
    summary="Update Character System Prompt",
    description="Writes the persona prompt back into the character package manifest.json "
                "(the single source of truth) and refreshes the database mirror.",
)
async def update_character_system_prompt(character_id: int, req: SystemPromptUpdate):
    text = req.system_prompt.strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="人设提示词不能为空",
        )

    async with get_db() as conn:
        prof = await crud.get_voice_profile(conn, character_id)
        if not prof:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Character with ID {character_id} not found",
            )

    from galgame2voice.services.character_manager import get_character_manager
    mgr = get_character_manager()
    pkg = mgr.get_character(prof.name)
    if pkg is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="角色包目录不存在，无法写回人设提示词；请先确认 characters/ 下的角色包完整",
        )

    manifest_path = pkg.folder / "manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"角色包缺少 manifest.json: {manifest_path}",
        )

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
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

    # 内存里的角色包缓存也要刷新，否则本进程后续仍读到旧人设
    mgr.discover_characters()

    async with get_db() as conn:
        await conn.execute(
            "UPDATE voice_profiles SET system_prompt = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
            (text, character_id),
        )
        # get_db() 不自动提交，漏掉 commit 会让这次 UPDATE 随连接关闭被回滚
        await conn.commit()

    logger.info("System prompt updated for character %s (%d chars)", prof.name, len(text))
    return {
        "character_id": character_id,
        "name": prof.name,
        "system_prompt": text,
        "length": len(text),
        "manifest_path": str(manifest_path),
    }


async def _resolve_profile_by_name(conn, char_name: str) -> Optional[Any]:
    """Resolves a voice profile by character name using exact, flexible, or package-synced match."""
    from galgame2voice.services.character_manager import get_character_manager
    cm = get_character_manager()
    pkg = cm.get_character(char_name)

    candidate_names: List[str] = []
    if pkg:
        candidate_names.extend([pkg.name, pkg.id])
    candidate_names.append(char_name)

    # 1. Exact name match in DB
    for c_name in candidate_names:
        profile = await crud.get_voice_profile_by_name(conn, c_name)
        if profile:
            return profile

    # 2. Flexible matching against existing DB voice profiles
    profiles = await crud.list_voice_profiles(conn)
    for p in profiles:
        for c_name in candidate_names:
            if p.name.lower() == c_name.lower():
                return p

    if pkg:
        for p in profiles:
            if p.name.startswith(pkg.name) or pkg.name.startswith(p.name):
                return p
            if pkg.id.lower() in p.name.lower() or pkg.name in p.name:
                return p

    for p in profiles:
        if p.name.startswith(char_name) or char_name in p.name:
            return p

    # 3. If still not found but package exists on disk, sync with DB and retry
    if pkg:
        await cm.sync_with_db(conn)
        profile = await crud.get_voice_profile_by_name(conn, pkg.name)
        if profile:
            return profile
        profiles = await crud.list_voice_profiles(conn)
        for p in profiles:
            if p.name.startswith(pkg.name) or pkg.name in p.name:
                return p

    return None


@router.post(
    "/switch",
    summary="Switch Active Character",
    description="Atomically switches the active character voice profile with memory safety and auto-rollback.",
)
async def switch_character(req: CharacterSwitchRequest):
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
    async with get_db() as conn:
        profile = None
        if char_id is not None:
            profile = await crud.get_voice_profile(conn, char_id)
        elif char_name:
            profile = await _resolve_profile_by_name(conn, char_name)

        if not profile:
            ident = char_id if char_id is not None else char_name
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
                ident = char_id if char_id is not None else char_name
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Character '{ident}' not found",
                )
            profile = verified_profile

        is_already_active = manager.is_active_profile(profile, force=req.force)

        if is_already_active:
            try:
                async with get_db() as conn:
                    await crud.set_active_voice_profile(conn, profile.id)
            except Exception as exc:
                logger.debug("Failed syncing active character to settings: %s", exc)
        else:
            try:
                success = await manager.switch_profile(profile, persist=True, _already_locked=True, force=req.force)
            except InsufficientMemoryError as mem_err:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=str(mem_err),
                ) from mem_err
            if not success:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Failed to load GPT/SoVITS model weights onto backend service",
                )

        return {
            "status": "switched",
            "character": profile.name,
            "character_id": profile.id,
        }
