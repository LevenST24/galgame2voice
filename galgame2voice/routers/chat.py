"""
Chat Router for galgame2voice.
Provides real-time SSE streaming endpoint (/api/chat/stream) and
backward-compatible synchronous chat endpoints (/api/chat, /ai/chat).
"""

import asyncio
import json
import logging
from typing import Any, AsyncGenerator, Dict, Optional
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from galgame2voice.services.chat_service import ChatService
from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db, get_database_path

logger = logging.getLogger("galgame2voice.routers.chat")

router = APIRouter(tags=["chat"])

# Module-level ChatService instance
_chat_service: Optional[ChatService] = None
_explicit_chat_service: Optional[ChatService] = None


def get_chat_service() -> ChatService:
    """Returns singleton ChatService instance."""
    global _chat_service, _explicit_chat_service
    if _explicit_chat_service is not None:
        return _explicit_chat_service
    db_path = get_database_path()
    if _chat_service is None or str(_chat_service.db_path) != str(db_path):
        _chat_service = ChatService(db_path=db_path)
    return _chat_service


def set_chat_service(service: Optional[ChatService]) -> None:
    """Overrides singleton ChatService instance (e.g. in tests)."""
    global _explicit_chat_service
    _explicit_chat_service = service


# ============================================================================
# Request Models
# ============================================================================

class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="User prompt text")
    session_id: str = Field(default="default", description="Conversation session identifier")
    character_name: Optional[str] = Field(default=None, description="Character persona name override")
    provider_id: Optional[str] = Field(default=None, description="LLM provider ID override")
    tts_options: Optional[Dict[str, Any]] = Field(default=None, description="Inference parameters (speed, top_k, etc.)")
    preset: Optional[str] = Field(default=None, description="TTS Preset name (high_quality, balanced, low_latency)")


# ============================================================================
# Streaming SSE Endpoint
# ============================================================================

async def sse_event_formatter(event_generator: AsyncGenerator[Dict[str, Any], None]) -> AsyncGenerator[str, None]:
    """Formats event dictionaries into standard Server-Sent Events SSE text stream."""
    try:
        async for event in event_generator:
            event_name = event.get("event", "message")
            event_data = json.dumps(event.get("data", {}), ensure_ascii=False)
            yield f"event: {event_name}\ndata: {event_data}\n\n"
    except asyncio.CancelledError:
        logger.info("SSE client disconnected from stream.")
    except Exception as exc:
        logger.error("Error during SSE streaming: %s", exc, exc_info=True)
        err_data = json.dumps({"error": str(exc)}, ensure_ascii=False)
        yield f"event: error\ndata: {err_data}\n\n"


@router.post("/api/chat/stream", summary="Real-time SSE bilingual streaming chat")
async def chat_stream_endpoint(req: ChatRequest):
    """
    Server-Sent Events endpoint streaming real-time Chinese delta text
    and synthesized Japanese audio chunks.
    """
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Prompt cannot be empty",
        )

    tts_opts = dict(req.tts_options or {})
    if req.preset:
        tts_opts["preset"] = req.preset

    service = get_chat_service()
    event_gen = service.stream_chat(
        prompt=req.prompt.strip(),
        session_id=req.session_id,
        character_name=req.character_name,
        provider_id=req.provider_id,
        tts_options=tts_opts,
    )

    return StreamingResponse(
        sse_event_formatter(event_gen),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ============================================================================
# Synchronous Non-Streaming Endpoints
# ============================================================================

@router.post("/api/chat", summary="Synchronous chat completion")
async def chat_sync_endpoint(req: ChatRequest):
    """
    Non-streaming synchronous chat completion returning bilingual text and audio URL.
    """
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Prompt cannot be empty",
        )

    tts_opts = dict(req.tts_options or {})
    if req.preset:
        tts_opts["preset"] = req.preset

    service = get_chat_service()
    result = await service.chat_sync(
        prompt=req.prompt.strip(),
        session_id=req.session_id,
        character_name=req.character_name,
        provider_id=req.provider_id,
        tts_options=tts_opts,
    )
    return result


@router.get("/ai/chat", summary="Legacy GET chat completion endpoint")
async def legacy_get_chat(
    prompt: str = Query(..., min_length=1, description="Prompt text"),
    session_id: str = Query(default="default", description="Session ID"),
    character_name: Optional[str] = Query(default=None, description="Character name"),
    preset: Optional[str] = Query(default=None, description="TTS preset"),
):
    """
    Legacy backward-compatible GET endpoint for simple chat queries.
    """
    if not prompt or not prompt.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Prompt cannot be empty",
        )

    service = get_chat_service()
    tts_opts = {"preset": preset} if preset else {}
    result = await service.chat_sync(
        prompt=prompt.strip(),
        session_id=session_id,
        character_name=character_name,
        tts_options=tts_opts,
    )
    return result


@router.post("/ai/chat", summary="Legacy POST chat completion endpoint")
async def legacy_post_chat(req: ChatRequest):
    """
    Legacy backward-compatible POST endpoint.
    """
    return await chat_sync_endpoint(req)


# ============================================================================
# Session & Message History Endpoints
# ============================================================================

@router.get("/api/chat/history", summary="Get session message history")
async def get_chat_history(
    session_id: str = Query(default="default", description="Conversation session ID"),
    limit: int = Query(default=50, ge=1, le=200, description="Max message count to return"),
):
    """
    Returns chronological list of previous messages in the session for UI restoration.
    """
    async with get_db() as conn:
        messages = await crud.get_recent_messages(conn, session_id=session_id, limit=limit)
        return {
            "session_id": session_id,
            "count": len(messages),
            "messages": [m.model_dump() for m in messages],
        }


@router.delete("/api/chat/history", summary="Clear session message history")
async def clear_chat_history(
    session_id: str = Query(..., description="Conversation session ID to reset"),
):
    """
    Deletes all messages associated with the specified session ID.
    """
    async with get_db() as conn:
        cleared = await crud.clear_session_messages(conn, session_id=session_id)
        return {
            "status": "cleared",
            "session_id": session_id,
            "success": cleared,
        }

