"""
REST API Router for Dynamic Character Affection & Easter Eggs (/api/affection).
Supports querying affection status, manual adjustments, resets, and dialogue unlocks gallery.
"""

import logging
from typing import Any
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from galgame2voice.database import crud
from galgame2voice.database.models import (
    CharacterAffectionResponse, CharacterAffectionUpdate
)
from galgame2voice.database.session import get_db
from galgame2voice.services.affection_service import AffectionService
from galgame2voice.services.memory_service import MemoryService
from galgame2voice.utils.logger import sanitize_error_detail

logger = logging.getLogger("galgame2voice.routers.affection")

router = APIRouter(prefix="/api/affection", tags=["affection"])


class AffectionUpdateRequest(BaseModel):
    """Payload for manually updating character affection status and metrics."""
    user_id: str = Field(default="default_user", min_length=1, max_length=128)
    character_id: int = Field(default=1, ge=1)
    affection_score: int | None = Field(default=None, ge=0, le=100)
    affection_level: int | None = Field(default=None, ge=1, le=5)
    current_emotion: str | None = Field(default=None, max_length=32)
    custom_nickname: str | None = Field(default=None, max_length=50)
    unlocked_dialogues: list[str] | None = None


class AffectionResetRequest(BaseModel):
    """Payload for resetting character affection scores to default levels."""
    user_id: str = Field(default="default_user", min_length=1, max_length=128)
    character_id: int = Field(default=1, ge=1)


@router.get("", response_model=CharacterAffectionResponse, summary="Get character affection state")
async def get_character_affection_endpoint(
    user_id: str = Query(default="default_user", min_length=1, max_length=128, description="User identifier"),
    character_id: int = Query(default=1, ge=1, description="Character Voice Profile ID"),
) -> CharacterAffectionResponse:
    """
    Retrieves the current affection score, level, emotion, and unlocked dialogue
    IDs, creating the default record on first access.
    """
    clean_user = user_id.strip() or "default_user"
    async with get_db() as conn:
        return await crud.get_or_create_character_affection(
            conn, user_id=clean_user, character_id=character_id
        )


@router.post("/update", response_model=CharacterAffectionResponse, summary="Update character affection state")
async def update_character_affection_endpoint(req: AffectionUpdateRequest) -> CharacterAffectionResponse:
    """
    Manually modifies character affection score, level, emotion, or custom nickname.
    """
    clean_user = req.user_id.strip() or "default_user"
    # Defensively sanitize emotion and nickname to prevent prompt injection and control character leakage
    clean_emotion = MemoryService.sanitize_fact_value(req.current_emotion, max_len=20) if req.current_emotion else None
    clean_nickname = MemoryService.sanitize_fact_value(req.custom_nickname, max_len=20) if req.custom_nickname else None

    async with get_db() as conn:
        updates = CharacterAffectionUpdate(
            affection_score=req.affection_score,
            affection_level=req.affection_level,
            current_emotion=clean_emotion,
            custom_nickname=clean_nickname,
            unlocked_dialogues=req.unlocked_dialogues,
        )
        try:
            updated = await crud.update_character_affection(
                conn,
                user_id=clean_user,
                character_id=req.character_id,
                updates=updates,
            )
            if not updated:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Character affection record not found",
                )
            return updated
        except HTTPException:
            raise
        except Exception as exc:
            safe_err = sanitize_error_detail(exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update character affection: {safe_err}",
            ) from exc


@router.post("/reset", response_model=CharacterAffectionResponse, summary="Reset character affection state")
async def reset_character_affection_endpoint(
    req: AffectionResetRequest | None = None,
) -> CharacterAffectionResponse:
    """
    Resets affection score to 0, level to 1, and emotion to 'normal'.
    """
    user_id = (req.user_id.strip() if req else "default_user") or "default_user"
    character_id = req.character_id if req else 1

    async with get_db() as conn:
        return await crud.reset_character_affection(
            conn, user_id=user_id, character_id=character_id
        )


@router.get("/dialogues", summary="Get milestone & easter egg dialogue gallery")
async def get_dialogue_gallery_endpoint(
    user_id: str = Query(default="default_user", min_length=1, max_length=128, description="User ID"),
    character_id: int = Query(default=1, ge=1, description="Character ID"),
) -> dict[str, Any]:
    """
    Returns a summary envelope (total and unlocked counts) wrapping the full list
    of milestone lines and easter egg voicelines with their unlock status.
    """
    clean_user = user_id.strip() or "default_user"
    service = AffectionService()
    gallery = await service.get_dialogue_gallery(
        user_id=clean_user, character_id=character_id
    )
    return {
        "user_id": clean_user,
        "character_id": character_id,
        "total_count": len(gallery),
        "unlocked_count": sum(1 for d in gallery if d["is_unlocked"]),
        "dialogues": gallery,
    }
