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

logger = logging.getLogger("galgame2voice.routers.memory")

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.get("", response_model=List[UserMemoryResponse], summary="List user memories")
async def list_user_memories(
    user_id: str = Query(default="default_user", description="User ID"),
    character_id: Optional[int] = Query(default=None, description="Character Voice Profile ID filter"),
    category: Optional[str] = Query(default=None, description="Memory category filter (nickname, preference, promise, etc.)"),
    limit: int = Query(default=100, ge=1, le=500, description="Max items to return"),
):
    """
    Returns list of stored user long-term facts and memories.
    """
    async with get_db() as conn:
        memories = await crud.list_memories(
            conn,
            user_id=user_id,
            character_id=character_id,
            category=category,
            limit=limit,
        )
        return memories


@router.post("", response_model=UserMemoryResponse, status_code=status.HTTP_201_CREATED, summary="Create or upsert memory")
async def create_user_memory(mem: UserMemoryCreate):
    """
    Manually creates or upserts a fact memory for a user and character.
    """
    if not mem.fact_key or not mem.fact_key.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="fact_key cannot be empty",
        )
    if not mem.fact_value or not mem.fact_value.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="fact_value cannot be empty",
        )

    async with get_db() as conn:
        created = await crud.upsert_memory(conn, mem)
        return created


@router.put("/{memory_id}", response_model=UserMemoryResponse, summary="Update user memory")
async def update_user_memory(memory_id: int, updates: UserMemoryUpdate):
    """
    Updates an existing memory record by its ID.
    """
    async with get_db() as conn:
        updated = await crud.update_memory(conn, memory_id, updates)
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Memory record with ID {memory_id} not found",
            )
        return updated


@router.delete("/{memory_id}", summary="Delete specific user memory")
async def delete_user_memory(memory_id: int):
    """
    Deletes a specific memory record by ID.
    """
    async with get_db() as conn:
        deleted = await crud.delete_memory(conn, memory_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Memory record with ID {memory_id} not found",
            )
        return {"status": "deleted", "id": memory_id}


@router.delete("", summary="Clear all user memories")
async def clear_user_memories(
    user_id: str = Query(default="default_user", description="User ID"),
    character_id: Optional[int] = Query(default=None, description="Character ID filter"),
):
    """
    Clears all memories for the specified user and optional character.
    """
    async with get_db() as conn:
        count = await crud.clear_memories(conn, user_id=user_id, character_id=character_id)
        return {"status": "cleared", "deleted_count": count, "user_id": user_id}
