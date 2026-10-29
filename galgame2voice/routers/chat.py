"""
Chat Router for galgame2voice.
Provides real-time SSE streaming endpoint (/api/chat/stream) and
backward-compatible synchronous chat endpoints (/api/chat, /ai/chat).
"""

import asyncio
import json
import logging
import time
from typing import Any, AsyncGenerator, Literal
import uuid
from anyio import CancelScope
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.requests import ClientDisconnect

from galgame2voice.services.chat_service import ChatService
from galgame2voice.services.chat_pipelines.tts_pipeline import TTS_JOB_TIMEOUT_SECONDS
from galgame2voice.services.chat_backup import export_chat_backup, import_timestamp, validate_adv_settings
from galgame2voice.services.gpt_sovits_client import validate_user_tts_options
from galgame2voice.database import crud
from galgame2voice.database.session import get_db, get_database_path, immediate_transaction
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.utils.sse import format_sse_frame
from galgame2voice.utils.error_diagnostics import ConfigurationSelectionError, EmptyReplyError, chat_error_payload
from galgame2voice.utils.async_tasks import await_protected, cancel_and_drain

logger = logging.getLogger("galgame2voice.routers.chat")

router = APIRouter(tags=["chat"])

PROMPT_MAX_LENGTH = 4000
SESSION_ID_MAX_LENGTH = 128

# Module-level ChatService instance
_chat_service: ChatService | None = None
_explicit_chat_service: ChatService | None = None


def get_chat_service() -> ChatService:
    """Returns this module's cached ChatService instance (not app-wide: the Telegram bot builds its own)."""
    global _chat_service, _explicit_chat_service
    if _explicit_chat_service is not None:
        return _explicit_chat_service
    db_path = get_database_path()
    if _chat_service is None or str(_chat_service.db_path) != str(db_path):
        _chat_service = ChatService(db_path=db_path)
    return _chat_service


def set_chat_service(service: ChatService | None) -> None:
    """Overrides the instance returned by get_chat_service (e.g. in tests); the Telegram bot keeps its own."""
    global _explicit_chat_service
    _explicit_chat_service = service


# ============================================================================
# Request Models
# ============================================================================

class ChatRequest(BaseModel):
    """Request payload for initiating streaming or synchronous chat generation."""

    reply_format: Literal["old", "lines"] = "old"
    prompt: str = Field(..., min_length=1, max_length=PROMPT_MAX_LENGTH, description="User prompt text")
    session_id: str = Field(default="default", max_length=SESSION_ID_MAX_LENGTH, description="Conversation session identifier")
    stream: bool | None = Field(default=True, description="Whether client requested streaming")
    character_name: str | None = Field(default=None, max_length=100, description="Character persona name override")
    voice_profile_id: int | None = Field(default=None, description="Active voice profile ID for character")
    provider_id: str | None = Field(default=None, max_length=64, description="LLM provider ID override")
    tts_options: dict[str, Any] | None = Field(default=None, description="Inference parameters (speed, top_k, etc.)")
    preset: str | None = Field(default=None, max_length=64, description="TTS Preset name (high_quality, balanced, low_latency)")
    system_prompt: str | None = Field(default=None, max_length=PROMPT_MAX_LENGTH, description="Per-request system prompt override (frontend session persona)")
    temperature: float | None = Field(default=None, ge=0.0, le=2.0, description="Per-request LLM temperature override")
    max_context: int | None = Field(default=None, ge=2, le=100, description="Per-request max history messages override")
    top_p: float | None = Field(default=None, ge=0.0, le=1.0, description="Per-request nucleus sampling override")
    max_tokens: int | None = Field(default=None, ge=16, le=32768, description="Per-request max generated tokens override")
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0, description="Per-request frequency penalty override")
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0, description="Per-request presence penalty override")
    ai_adaptive_voice: bool | None = Field(default=True, description="Whether AI-driven dynamic voice inference is enabled")
    outfit: str | None = Field(default=None, description="Active character outfit override")

    @field_validator("outfit", mode="before")
    @classmethod
    def _validate_outfit(cls, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        val = value.strip()
        if not val or len(val) > 64:
            return None
        if "/" in val or "\\" in val or ".." in val:
            return None
        return val


# ============================================================================
# Streaming SSE Endpoint
# ============================================================================

class ChatStreamingResponse(StreamingResponse):
    """Own the SSE iterator even when ASGI send fails after a yielded chunk."""

    def __init__(self, content: Any, *, cancel_event: asyncio.Event, **kwargs: Any) -> None:
        self.cancel_event = cancel_event
        super().__init__(content, **kwargs)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # One response-owned listener consumes ASGI receive in all protocol
        # versions; it also covers disconnects while the next event is pending.
        sender = asyncio.create_task(self.stream_response(send))
        watcher = asyncio.create_task(self.listen_for_disconnect(receive))
        try:
            done, _ = await asyncio.wait((sender, watcher), return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                try:
                    await sender
                except OSError as exc:
                    raise ClientDisconnect() from exc
            else:
                await watcher
        finally:
            self.cancel_event.set()
            try:
                await cancel_and_drain((sender, watcher))
            finally:
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    await await_protected(close())

    async def stream_response(self, send: Any) -> None:
        try:
            await super().stream_response(send)
        finally:
            # ASGI 2.4 reports disconnect through send(); older servers cancel
            # the request scope. Both paths must close the yielded SSE iterator.
            with CancelScope(shield=True):
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    await close()


async def sse_event_formatter(
    event_generator: AsyncGenerator[dict[str, Any] | str, None],
    cancel_event: asyncio.Event | None = None,
    cleanup_task: asyncio.Task | None = None,
) -> AsyncGenerator[str, None]:
    """Formats event dictionaries into standard Server-Sent Events SSE text stream."""
    try:
        async for event in event_generator:
            if cancel_event and cancel_event.is_set():
                break
            yield format_sse_frame(event)
    except (asyncio.CancelledError, GeneratorExit):
        if cancel_event:
            cancel_event.set()
        logger.info("SSE client disconnected from stream.")
    except Exception as exc:
        logger.error("Error during SSE streaming: %s", exc, exc_info=True)
        safe_err = sanitize_error_detail(exc)
        err_data = json.dumps({"error": safe_err or "Streaming encountered an internal error"}, ensure_ascii=False)
        yield f"event: error\ndata: {err_data}\n\n"
    finally:
        if cancel_event:
            cancel_event.set()
        # async-for does not close a generator when the consumer breaks out.
        # Close it explicitly so pipeline locks and history are settled now.
        try:
            close = getattr(event_generator, "aclose", None)
            if close is not None:
                await close()
        except Exception as exc:
            logger.warning("Failed to close SSE source: %s", sanitize_error_detail(exc))
        finally:
            if cleanup_task and not cleanup_task.done():
                cleanup_task.cancel()
                try:
                    await cleanup_task
                except (asyncio.CancelledError, Exception):
                    pass


def _validate_chat_request(req: ChatRequest) -> None:
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Prompt cannot be empty",
        )
    # Defensively normalize and validate session_id
    req.session_id = req.session_id.strip() or "default"
    if len(req.session_id) > SESSION_ID_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Session ID exceeds maximum length of {SESSION_ID_MAX_LENGTH} characters",
        )
    if req.character_name is not None:
        req.character_name = req.character_name.strip() or None
    if req.provider_id is not None:
        req.provider_id = req.provider_id.strip() or None

    try:
        validate_user_tts_options(req.tts_options)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


def _resolve_request_tts_options(req: ChatRequest) -> dict[str, Any]:
    tts_opts = dict(req.tts_options or {})
    if req.preset:
        tts_opts["preset"] = req.preset
    if req.ai_adaptive_voice is not None:
        tts_opts["ai_adaptive_voice"] = req.ai_adaptive_voice
    return tts_opts


@router.post("/api/chat/stream", summary="Real-time SSE bilingual streaming chat")
async def chat_stream_endpoint(req: ChatRequest, request: Request) -> StreamingResponse:
    """
    Server-Sent Events endpoint streaming real-time Chinese delta text
    and synthesized Japanese audio chunks.
    """
    _validate_chat_request(req)
    tts_opts = _resolve_request_tts_options(req)

    cancel_event = asyncio.Event()
    service = get_chat_service()
    event_gen = service.stream_chat(
        prompt=req.prompt.strip(),
        session_id=req.session_id,
        character_name=req.character_name,
        voice_profile_id=req.voice_profile_id,
        provider_id=req.provider_id,
        tts_options=tts_opts,
        cancel_event=cancel_event,
        system_prompt=req.system_prompt,
        temperature=req.temperature,
        max_context=req.max_context,
        top_p=req.top_p,
        max_tokens=req.max_tokens,
        frequency_penalty=req.frequency_penalty,
        presence_penalty=req.presence_penalty,
        ai_adaptive_voice=req.ai_adaptive_voice,
        reply_format=req.reply_format,
        outfit=req.outfit,
    )

    return ChatStreamingResponse(
        sse_event_formatter(event_gen, cancel_event=cancel_event),
        cancel_event=cancel_event,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-G2V-Progress-Policy": "1",
            "X-G2V-Voice-Job-Timeout-Ms": str(round(TTS_JOB_TIMEOUT_SECONDS * 1000)),
        },
    )


# ============================================================================
# Synchronous Non-Streaming Endpoints
# ============================================================================

@router.post("/api/chat", summary="Synchronous chat completion")
async def chat_sync_endpoint(req: ChatRequest) -> dict[str, Any]:
    """
    Non-streaming synchronous chat completion returning bilingual text and audio URL.
    """
    _validate_chat_request(req)

    tts_opts = _resolve_request_tts_options(req)

    service = get_chat_service()
    try:
        return await service.chat_sync(
            prompt=req.prompt.strip(),
            session_id=req.session_id,
            character_name=req.character_name,
            voice_profile_id=req.voice_profile_id,
            provider_id=req.provider_id,
            tts_options=tts_opts,
            system_prompt=req.system_prompt,
            temperature=req.temperature,
            max_context=req.max_context,
            top_p=req.top_p,
            max_tokens=req.max_tokens,
            frequency_penalty=req.frequency_penalty,
            presence_penalty=req.presence_penalty,
            ai_adaptive_voice=req.ai_adaptive_voice,
            reply_format=req.reply_format,
            outfit=req.outfit,
        )
    except HTTPException:
        raise
    except ConfigurationSelectionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=chat_error_payload(exc)) from exc
    except EmptyReplyError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": str(exc), "error_code": "empty_reply", "kind": "empty_reply", "message": str(exc)},
        ) from exc
    except Exception as exc:
        logger.error(
            "Error in synchronous chat completion (session_id=%s, character=%s, provider=%s): %s",
            req.session_id,
            req.character_name,
            req.provider_id,
            exc,
            exc_info=True,
        )
        safe_err = sanitize_error_detail(exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=safe_err or "Chat completion encountered an internal error",
        ) from exc


@router.get("/ai/chat", summary="Legacy GET chat completion endpoint")
async def legacy_get_chat(
    prompt: str = Query(..., min_length=1, max_length=PROMPT_MAX_LENGTH, description="Prompt text"),
    session_id: str = Query(default="default", max_length=SESSION_ID_MAX_LENGTH, description="Session ID"),
    character_name: str | None = Query(default=None, max_length=100, description="Character name"),
    preset: str | None = Query(default=None, max_length=64, description="TTS preset"),
) -> dict[str, Any]:
    """
    Legacy backward-compatible GET endpoint for simple chat queries.
    """
    if not prompt or not prompt.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Prompt cannot be empty",
        )

    clean_session_id = (session_id or "").strip() or "default"
    clean_char_name = character_name.strip() if character_name else None
    service = get_chat_service()
    tts_opts = {"preset": preset} if preset else {}
    try:
        return await service.chat_sync(
            prompt=prompt.strip(),
            session_id=clean_session_id,
            character_name=clean_char_name,
            tts_options=tts_opts,
        )
    except HTTPException:
        raise
    except ConfigurationSelectionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=chat_error_payload(exc)) from exc
    except EmptyReplyError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": str(exc), "error_code": "empty_reply", "kind": "empty_reply", "message": str(exc)},
        ) from exc
    except Exception as exc:
        logger.error(
            "Error in legacy GET chat completion (session_id=%s, character=%s): %s",
            clean_session_id,
            clean_char_name,
            exc,
            exc_info=True,
        )
        safe_err = sanitize_error_detail(exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=safe_err or "Chat completion encountered an internal error",
        ) from exc


@router.post("/ai/chat", summary="Legacy POST chat completion endpoint")
async def legacy_post_chat(req: ChatRequest) -> dict[str, Any]:
    """
    Legacy backward-compatible POST endpoint.
    """
    return await chat_sync_endpoint(req)


# ============================================================================
# Session & Message History Endpoints
# ============================================================================

class SessionUpsertRequest(BaseModel):
    """Request payload for creating or modifying a chat session and its settings."""

    id: str | None = Field(default=None, max_length=SESSION_ID_MAX_LENGTH, description="Session ID (auto-generated if omitted)")
    title: str | None = Field(default=None, max_length=200, description="Session display title")
    voice_profile_id: int | None = Field(default=None, description="Bound voice profile ID")
    custom_system_prompt: str | None = Field(default=None, max_length=PROMPT_MAX_LENGTH, description="Custom persona system prompt")
    settings: dict[str, Any] | None = Field(default=None, description="Session-specific generation & voice parameters")


ALLOWED_ACT_KEYS = frozenset({"face", "pose", "motion", "blush", "fx"})


class ImportedLine(BaseModel):
    index: int = Field(ge=0)
    zh: str = Field(default="", max_length=10000)
    ja: str = Field(default="", max_length=10000)
    emotion: str = Field(default="gentle", max_length=32)
    act: dict[str, Any] = Field(default_factory=dict)
    audio: dict[str, Any] | None = None

    @field_validator("act", mode="before")
    @classmethod
    def filter_act_keys(cls, v: Any) -> dict[str, Any]:
        if not isinstance(v, dict):
            return {}
        return {k: val for k, val in v.items() if k in ALLOWED_ACT_KEYS}

    def to_persisted_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "zh": self.zh,
            "ja": self.ja,
            "emotion": self.emotion,
            "act": self.act,
            "audio": {"status": "skipped", "chunks": []},
        }


class ImportedMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(max_length=100000)
    japanese: str = Field(default="", max_length=100000)
    lines: list[ImportedLine] | None = Field(default=None, max_length=2000)
    ts: int | None = Field(default=None, ge=0, le=253402300799000)


class ImportedSession(BaseModel):
    id: str = Field(min_length=1, max_length=SESSION_ID_MAX_LENGTH, pattern=r"^s_import_[a-zA-Z0-9_]+$")
    title: str = Field(max_length=200)
    messages: list[ImportedMessage] = Field(max_length=10000)
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: int | None = Field(default=None, ge=0, le=253402300799000)
    updated_at: int | None = Field(default=None, ge=0, le=253402300799000)

    @field_validator("settings")
    @classmethod
    def validate_settings(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_adv_settings(value)


class ChatImportRequest(BaseModel):
    sessions: list[ImportedSession] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def bounded_unique_sessions(self):
        ids = [session.id for session in self.sessions]
        count = sum(len(session.messages) for session in self.sessions)
        size = sum(
            len(message.content) + len(message.japanese)
            + (len(json.dumps([line.to_persisted_dict() for line in message.lines], ensure_ascii=False)) if message.lines is not None else 0)
            for session in self.sessions for message in session.messages
        )
        if len(set(ids)) != len(ids) or count > 50000 or size > 10_000_000:
            raise ValueError("备份编号重复或数据过大，请拆分后导入，当前记录未修改。")
        return self


@router.post("/api/chat/import", summary="Import chat history into new sessions atomically")
async def import_chat_sessions(req: ChatImportRequest) -> dict[str, Any]:
    """Restore model context as well as browser display, without overwriting IDs."""
    async with get_db() as conn, immediate_transaction(conn):
        for session in req.sessions:
            existing = await (await conn.execute("SELECT id FROM sessions WHERE id = ?", (session.id,))).fetchone()
            if existing:
                raise HTTPException(409, "导入的会话已存在，原记录未修改。请刷新查看后再决定是否导入另一份副本。")
            # Profile IDs are installation-specific; a restored session must
            # choose its voice explicitly rather than binding a different person.
            settings = {**session.settings, "voiceProfileId": None}
            system_prompt = settings.get("systemPrompt")
            if system_prompt is not None and not isinstance(system_prompt, str):
                raise HTTPException(422, "人设提示词应为文字，当前记录未修改。")
            await conn.execute(
                "INSERT INTO sessions (id, channel, title, settings_json, custom_system_prompt, created_at, updated_at) "
                "VALUES (?, 'web', ?, ?, ?, ?, ?)",
                (session.id, session.title, json.dumps(settings, ensure_ascii=False), system_prompt,
                 import_timestamp(session.created_at), import_timestamp(session.updated_at)),
            )
            await conn.executemany(
                "INSERT INTO messages (session_id, role, content_chinese, content_japanese, created_at, lines_json) VALUES (?, ?, ?, ?, ?, ?)",
                [(session.id, message.role, message.content, message.japanese, import_timestamp(message.ts),
                  json.dumps([line.to_persisted_dict() for line in message.lines], ensure_ascii=False) if message.lines is not None else None)
                 for message in session.messages],
            )
    return {"status": "ok", "count": len(req.sessions)}


@router.get("/api/chat/export", summary="Export complete portable chat history")
async def export_chat_sessions() -> dict[str, Any]:
    return await export_chat_backup()


@router.get("/api/chat/sessions", summary="List all chat sessions with metadata")
async def list_chat_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """
    Returns list of all conversation sessions from SQLite database in reverse-chronological order,
    including inferred human-readable title, message count, and last message preview.
    """
    async with get_db() as conn:
        sessions = await crud.list_sessions_overview(conn, limit=limit + 1, offset=offset)
        has_more = len(sessions) > limit
        sessions = sessions[:limit]
        results = []
        for s in sessions:
            settings_dict = {}
            if s.get("settings_json"):
                try:
                    settings_dict = json.loads(s["settings_json"])
                except (json.JSONDecodeError, TypeError, ValueError):
                    settings_dict = {}
            results.append({
                "id": s["id"],
                "title": s.get("title") or "新对话",
                "voice_profile_id": s.get("voice_profile_id"),
                "custom_system_prompt": s.get("custom_system_prompt"),
                "created_at": s.get("created_at"),
                "updated_at": s.get("updated_at"),
                "message_count": s.get("message_count", 0),
                "last_message": s.get("last_message"),
                "settings": settings_dict,
            })
        return {
            "count": len(results),
            "has_more": has_more,
            "sessions": results,
        }


def _validate_non_empty_session_id(session_id: str) -> str:
    clean_id = (session_id or "").strip()
    if not clean_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="session_id cannot be empty",
        )
    return clean_id


@router.post("/api/chat/sessions", summary="Create or update conversation session")
async def upsert_chat_session(req: SessionUpsertRequest) -> dict[str, Any]:
    """
    Creates or updates a conversation session in SQLite database,
    persisting title and custom generation parameters.
    """
    sess_id = (req.id or "").strip()
    if not sess_id:
        sess_id = f"s_{int(time.time()):x}_{uuid.uuid4().hex[:6]}"

    settings_str = json.dumps(req.settings, ensure_ascii=False) if req.settings is not None else None
    async with get_db() as conn:
        session_obj = await crud.upsert_session(
            conn=conn,
            session_id=sess_id,
            title=req.title,
            voice_profile_id=req.voice_profile_id,
            custom_system_prompt=req.custom_system_prompt,
            settings_json=settings_str,
            update_voice_binding="voice_profile_id" in req.model_fields_set,
        )
        return {
            "status": "ok",
            "session": session_obj.model_dump(),
        }


@router.delete("/api/chat/sessions/{session_id}", summary="Delete conversation session")
async def delete_chat_session_by_id(session_id: str) -> dict[str, Any]:
    """
    Deletes a conversation session and all its cascading messages.
    """
    clean_id = _validate_non_empty_session_id(session_id)
    async with get_db() as conn:
        success = await crud.delete_session(conn, clean_id)
        return {
            "status": "deleted" if success else "not_found",
            "session_id": clean_id,
            "success": success,
        }


@router.get("/api/chat/history", summary="Get session message history")
async def get_chat_history(
    session_id: str = Query(default="default", max_length=SESSION_ID_MAX_LENGTH, description="Conversation session ID"),
    limit: int = Query(default=100, ge=1, le=500, description="Max message count to return"),
    before_id: int | None = Query(default=None, ge=1, description="Return messages older than this message ID"),
) -> dict[str, Any]:
    """
    Returns chronological list of previous messages in the session for UI restoration.
    Supports cursor pagination via before_id to browse arbitrarily long conversation history.
    """
    clean_session_id = (session_id or "").strip() or "default"
    async with get_db() as conn:
        fetched = await crud.get_recent_messages(
            conn, session_id=clean_session_id, limit=limit + 1, before_id=before_id
        )
        has_more = len(fetched) > limit
        messages = fetched[-limit:] if has_more else fetched
        oldest_id = messages[0].id if messages else None
        return {
            "session_id": clean_session_id,
            "count": len(messages),
            "has_more": has_more,
            "oldest_id": oldest_id,
            "messages": [m.model_dump() for m in messages],
        }


@router.delete("/api/chat/history", summary="Clear session message history")
async def clear_chat_history(
    session_id: str = Query(..., max_length=SESSION_ID_MAX_LENGTH, description="Conversation session ID to reset"),
) -> dict[str, Any]:
    """
    Deletes all messages associated with the specified session ID.
    """
    clean_session_id = _validate_non_empty_session_id(session_id)
    async with get_db() as conn:
        cleared = await crud.clear_session_messages(conn, session_id=clean_session_id)
        return {
            "status": "cleared",
            "session_id": clean_session_id,
            "success": cleared,
        }


class MessageJapaneseRequest(BaseModel):
    """Request payload for retrieving or translating Japanese speech script for a message."""

    text: str = Field(..., min_length=1, max_length=PROMPT_MAX_LENGTH, description="Chinese text of the assistant message")
    session_id: str | None = Field(default=None, max_length=SESSION_ID_MAX_LENGTH, description="Session ID if known")


@router.post("/api/chat/japanese", summary="Get or resolve backend synthesized Japanese text")
async def get_message_japanese_endpoint(req: MessageJapaneseRequest) -> dict[str, Any]:
    """
    Returns the Japanese original text used for TTS synthesis of this message.
    Looks up database history first, falling back to LLM translation.
    """
    service = get_chat_service()
    ja = await service.resolve_message_japanese(req.text, session_id=req.session_id)
    return {"japanese": ja}


