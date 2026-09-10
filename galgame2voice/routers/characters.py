"""
Character Management REST API Router for galgame2voice (/api/characters).
Provides unified character profile query, active character switching, and affection integration.
"""

import logging
import os
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.services.voice_manager import get_voice_manager, InsufficientMemoryError
from galgame2voice.utils.logger import sanitize_error_detail

logger = logging.getLogger("galgame2voice.routers.characters")

router = APIRouter(prefix="/api/characters", tags=["characters"])


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
            active_id = active_profile.id if active_profile else (profiles[0].id if profiles else 1)

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
            )


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
            }
        except HTTPException:
            raise
        except Exception as exc:
            safe_err = sanitize_error_detail(exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get character: {safe_err}",
            )


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
            profile = await crud.get_voice_profile_by_name(conn, char_name)
            if not profile:
                profiles = await crud.list_voice_profiles(conn)
                for p in profiles:
                    if p.name == char_name:
                        profile = p
                        break

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

        active_prof = manager.active_profile
        is_already_active = False
        if active_prof and not req.force:
            active_id = getattr(active_prof, "id", None) or (active_prof.get("id") if isinstance(active_prof, dict) else None)
            active_gpt = getattr(active_prof, "gpt_weights_path", None) or (active_prof.get("gpt_weights_path") if isinstance(active_prof, dict) else None)
            active_sovits = getattr(active_prof, "sovits_weights_path", None) or (active_prof.get("sovits_weights_path") if isinstance(active_prof, dict) else None)
            active_ref = getattr(active_prof, "ref_audio_path", None) or getattr(active_prof, "refer_audio_path", None) or (active_prof.get("ref_audio_path") if isinstance(active_prof, dict) else None)
            active_prompt = getattr(active_prof, "prompt_text", None) or getattr(active_prof, "refer_text", None) or (active_prof.get("prompt_text") if isinstance(active_prof, dict) else None)

            prof_ref = getattr(profile, "ref_audio_path", None) or getattr(profile, "refer_audio_path", None)
            prof_prompt = getattr(profile, "prompt_text", None) or getattr(profile, "refer_text", None)

            if (
                active_id == profile.id
                and active_gpt == profile.gpt_weights_path
                and active_sovits == profile.sovits_weights_path
                and active_ref == prof_ref
                and active_prompt == prof_prompt
            ):
                is_already_active = True

        if is_already_active:
            try:
                async with get_db() as conn:
                    await crud.set_active_voice_profile(conn, profile.id)
            except Exception as exc:
                logger.debug("Failed syncing active character to settings: %s", exc)
        else:
            try:
                success = await manager.switch_profile(profile, persist=True, _already_locked=True)
            except InsufficientMemoryError as mem_err:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=str(mem_err),
                )
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
