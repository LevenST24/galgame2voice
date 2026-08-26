"""
REST API Router for Dynamic Character Affection & Easter Eggs (/api/affection).
Supports querying affection status, manual adjustments, resets, and dialogue unlocks gallery.
"""

import logging
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from galgame2voice.database import crud
from galgame2voice.database.models import (
    CharacterAffectionResponse, CharacterAffectionUpdate
)
from galgame2voice.database.session import get_db
from galgame2voice.services.affection_service import AffectionService

logger = logging.getLogger("galgame2voice.routers.affection")

router = APIRouter(prefix="/api/affection", tags=["affection"])




class AffectionUpdateRequest(BaseModel):
    user_id: str = "default_user"
    character_id: int = 1
    affection_score: Optional[int] = Field(default=None, ge=0, le=100)
    affection_level: Optional[int] = Field(default=None, ge=1, le=5)
    current_emotion: Optional[str] = None
    custom_nickname: Optional[str] = None
    unlocked_dialogues: Optional[List[str]] = None


class AffectionResetRequest(BaseModel):
    user_id: str = "default_user"
    character_id: int = 1


@router.get("", response_model=CharacterAffectionResponse, summary="Get character affection state")
async def get_character_affection_endpoint(
    user_id: str = Query(default="default_user", description="User identifier"),
    character_id: int = Query(default=1, description="Character Voice Profile ID"),
):
    """
    Retrieves current affection score, level, emotion, and unlocked dialogue count.
    """
    async with get_db() as conn:
        affection = await crud.get_or_create_character_affection(
            conn, user_id=user_id, character_id=character_id
        )
        return affection


@router.post("/update", response_model=CharacterAffectionResponse, summary="Update character affection state")
async def update_character_affection_endpoint(req: AffectionUpdateRequest):
    """
    Manually modifies character affection score, level, emotion, or custom nickname.
    """
    async with get_db() as conn:
        updates = CharacterAffectionUpdate(
            affection_score=req.affection_score,
            affection_level=req.affection_level,
            current_emotion=req.current_emotion,
            custom_nickname=req.custom_nickname,
            unlocked_dialogues=req.unlocked_dialogues,
        )
        updated = await crud.update_character_affection(
            conn,
            user_id=req.user_id,
            character_id=req.character_id,
            updates=updates,
        )
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Failed to update character affection",
            )
        return updated


@router.post("/reset", response_model=CharacterAffectionResponse, summary="Reset character affection state")
async def reset_character_affection_endpoint(req: Optional[AffectionResetRequest] = None):
    """
    Resets affection score to 0, level to 1, and emotion to 'normal'.
    """
    user_id = req.user_id if req else "default_user"
    character_id = req.character_id if req else 1

    async with get_db() as conn:
        reset_result = await crud.reset_character_affection(
            conn, user_id=user_id, character_id=character_id
        )
        return reset_result


@router.get("/dialogues", summary="Get milestone & easter egg dialogue gallery")
async def get_dialogue_gallery_endpoint(
    user_id: str = Query(default="default_user", description="User ID"),
    character_id: int = Query(default=1, description="Character ID"),
):
    """
    Returns full list of milestone lines and easter egg voicelines with unlock status.
    """
    service = AffectionService()
    gallery = await service.get_dialogue_gallery(
        user_id=user_id, character_id=character_id
    )
    return {
        "user_id": user_id,
        "character_id": character_id,
        "total_count": len(gallery),
        "unlocked_count": sum(1 for d in gallery if d["is_unlocked"]),
        "dialogues": gallery,
    }
