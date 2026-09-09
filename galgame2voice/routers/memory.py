"""
REST API Router for Long-Term User Fact Memories (/api/memory).
Supports listing, creating, updating, deleting, and clearing memories.
"""

import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from galgame2voice.database import crud
from galgame2voice.database.models import (
    UserMemoryCreate, UserMemoryUpdate, UserMemoryResponse
)
from galgame2voice.database.session import get_db
from galgame2voice.services.memory_service import MemoryService
from galgame2voice.utils.logger import sanitize_error_detail

logger = logging.getLogger("galgame2voice.routers.memory")

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.get("", response_model=List[UserMemoryResponse], summary="List user memories")
async def list_user_memories(
    user_id: str = Query(default="default_user", min_length=1, max_length=128, description="User ID"),
    character_id: Optional[int] = Query(default=None, ge=1, description="Character Voice Profile ID filter"),
    category: Optional[str] = Query(default=None, max_length=64, description="Memory category filter (nickname, preference, promise, etc.)"),
    limit: int = Query(default=100, ge=1, le=500, description="Max items to return"),
):
    """
    Returns list of stored user long-term facts and memories.
    """
    clean_user = user_id.strip()
    if not clean_user:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="user_id cannot be empty",
        )
    async with get_db() as conn:
        try:
            memories = await crud.list_memories(
                conn,
                user_id=clean_user,
                character_id=character_id,
                category=category.strip() if category else None,
                limit=limit,
            )
            return memories
        except Exception as exc:
            safe_err = sanitize_error_detail(exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list memories: {safe_err}",
            )


@router.post("", response_model=UserMemoryResponse, status_code=status.HTTP_201_CREATED, summary="Create or upsert memory")
async def create_user_memory(mem: UserMemoryCreate):
    """
    Manually creates or upserts a fact memory for a user and character.
    """
    clean_user = mem.user_id.strip() if mem.user_id else "default_user"
    if not clean_user:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="user_id cannot be empty",
        )
    if mem.character_id is not None and mem.character_id < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="character_id must be a positive integer >= 1",
        )
    if not mem.fact_key or not mem.fact_key.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="fact_key cannot be empty",
        )
    if not mem.fact_value or not mem.fact_value.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="fact_value cannot be empty",
        )

    # Sanitize and truncate extreme inputs to prevent injection & storage bloat
    clean_key = mem.fact_key.strip()[:100]
    clean_val = MemoryService.sanitize_fact_value(mem.fact_value.strip(), max_len=500)
    clean_cat = mem.category.strip()[:64] if mem.category else "preference"

    sanitized_mem = mem.model_copy(update={
        "user_id": clean_user,
        "fact_key": clean_key,
        "fact_value": clean_val,
        "category": clean_cat,
    })

    async with get_db() as conn:
        try:
            created = await crud.upsert_memory(conn, sanitized_mem)
            return created
        except Exception as exc:
            safe_err = sanitize_error_detail(exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create or upsert memory: {safe_err}",
            )


@router.put("/{memory_id}", response_model=UserMemoryResponse, summary="Update user memory")
async def update_user_memory(memory_id: int, updates: UserMemoryUpdate):
    """
    Updates an existing memory record by its ID.
    """
    if memory_id < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="memory_id must be a positive integer >= 1",
        )
    if updates.fact_key is not None and not updates.fact_key.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="fact_key cannot be empty",
        )
    if updates.fact_value is not None and not updates.fact_value.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="fact_value cannot be empty",
        )

    clean_updates_dict = {}
    if updates.fact_key is not None:
        clean_updates_dict["fact_key"] = updates.fact_key.strip()[:100]
    if updates.fact_value is not None:
        clean_updates_dict["fact_value"] = MemoryService.sanitize_fact_value(updates.fact_value.strip(), max_len=500)
    if updates.category is not None:
        clean_updates_dict["category"] = updates.category.strip()[:64]
    if updates.confidence is not None:
        clean_updates_dict["confidence"] = updates.confidence
    if updates.recall_count is not None:
        clean_updates_dict["recall_count"] = max(0, updates.recall_count)
    if updates.last_recalled_at is not None:
        clean_updates_dict["last_recalled_at"] = updates.last_recalled_at

    sanitized_updates = updates.model_copy(update=clean_updates_dict)

    async with get_db() as conn:
        try:
            updated = await crud.update_memory(conn, memory_id, sanitized_updates)
            if not updated:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Memory record with ID {memory_id} not found",
                )
            return updated
        except HTTPException:
            raise
        except Exception as exc:
            safe_err = sanitize_error_detail(exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update memory: {safe_err}",
            )


@router.delete("/{memory_id}", summary="Delete specific user memory")
async def delete_user_memory(memory_id: int):
    """
    Deletes a specific memory record by ID.
    """
    if memory_id < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="memory_id must be a positive integer >= 1",
        )
    async with get_db() as conn:
        try:
            deleted = await crud.delete_memory(conn, memory_id)
            if not deleted:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Memory record with ID {memory_id} not found",
                )
            return {"status": "deleted", "id": memory_id}
        except HTTPException:
            raise
        except Exception as exc:
            safe_err = sanitize_error_detail(exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete memory: {safe_err}",
            )


@router.delete("", summary="Clear all user memories")
async def clear_user_memories(
    user_id: str = Query(default="default_user", min_length=1, max_length=128, description="User ID"),
    character_id: Optional[int] = Query(default=None, ge=1, description="Character ID filter"),
):
    """
    Clears all memories for the specified user and optional character.
    """
    clean_user = user_id.strip()
    if not clean_user:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="user_id cannot be empty",
        )
    async with get_db() as conn:
        try:
            count = await crud.clear_memories(conn, user_id=clean_user, character_id=character_id)
            return {"status": "cleared", "deleted_count": count, "user_id": clean_user}
        except Exception as exc:
            safe_err = sanitize_error_detail(exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to clear memories: {safe_err}",
            )
