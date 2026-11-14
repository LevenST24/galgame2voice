"""
Chat Service and Streaming Bilingual Pipeline for galgame2voice.
Coordinates LLM streaming, incremental JSON bilingual parsing,
sentence boundary splitting, and low-latency TTS audio generation.

Pipeline hardening (v2.1):
  - Producer/worker tasks are ALWAYS reaped in a finally block (no orphan tasks
    leaking the inference lock after an SSE client disconnects).
  - TTS sentence failures emit an `audio_chunk_error` SSE event instead of
    being silently swallowed, so the frontend can skip that sentence and keep
    playing the rest.
  - The event pump waits on the queue in 0.5s cancel-responsive slices and emits
    a keep-alive comment frame after 5.0s of queue silence (no 50ms busy-poll).
  - WAV concatenation runs in a worker thread so the event loop never freezes.
  - Memory fact extraction runs in a true background task, off the TTFT path.
"""

import asyncio
import logging
from pathlib import Path
import time
from typing import Any, AsyncGenerator

import aiosqlite
from anyio import CancelScope

from galgame2voice.adapters.base import ChatMessage, BaseLLMAdapter, configured_reasoning_effort
from galgame2voice.adapters.registry import PROVIDER_PRESETS, get_llm_adapter, get_provider_preset
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_database_path, get_db, immediate_transaction
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.session_manager import SessionManager
from galgame2voice.services.memory_service import MemoryService
from galgame2voice.services.affection_service import AffectionService
from galgame2voice.services.metrics_collector import get_metrics_collector, MetricsCollector
from galgame2voice.services.character_identity import AmbiguousProfileName
from galgame2voice.services.voice_selection import select_voice_profile
from galgame2voice.utils.async_tasks import drain_background_tasks
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.utils.sse import format_sse_frame
from galgame2voice.utils.text_splitter import split_japanese_sentences
from galgame2voice.utils.error_diagnostics import (
    ConfigurationSelectionError,
    chat_error_payload,
)

from galgame2voice.services.emotion_classifier import (
    EMOTION_KEYWORDS,
    VALID_EMOTIONS,
    EMOTION_NAME_MAP,
    classify_emotion,
)
from galgame2voice.utils.prosody import (
    DYNAMIC_SPEED_MIN,
    DYNAMIC_SPEED_MAX,
    DYNAMIC_TEMP_MIN,
    DYNAMIC_TEMP_MAX,
    clamp_dynamic_speed,
    clamp_dynamic_temperature,
)
from galgame2voice.services.streaming_parser import StreamingBilingualParser
from galgame2voice.services.chat_pipelines import (
    StreamCoordinator,
    SseKeepAlive,
)
from galgame2voice.services.chat_pipelines.context_builder import build_chat_context
from galgame2voice.services.chat_pipelines.turn_persistence import TurnUnavailableError
from galgame2voice.services.chat_pipelines.turn_ownership import serialize_stream_turn
from galgame2voice.utils.audio_concat import concat_wav_files

logger = logging.getLogger("galgame2voice.services.chat_service")


# ============================================================================
# Chat Service (End-to-End Coordination)
# ============================================================================

class ChatService:
    """
    Coordinates multi-turn dialogue, LLM adapter streaming, incremental bilingual
    parsing, and low-latency sentence-by-sentence TTS audio generation.
    """

    def __init__(
        self,
        tts_service: TtsService | None = None,
        db_path: str | Path | None = None,
        metrics_collector: MetricsCollector | None = None,
    ):
        self.db_path = str(db_path or get_database_path())
        self.tts_service = tts_service or TtsService(db_path=self.db_path)
        self.session_manager = SessionManager(db_path=self.db_path)
        self.memory_service = MemoryService(db_path=self.db_path)
        self.affection_service = AffectionService(db_path=self.db_path)
        self.metrics_collector = metrics_collector or get_metrics_collector(db_path=self.db_path)
        # Strong references for fire-and-forget background tasks (prevent GC mid-flight).
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    def _spawn_background(self, coro: Any) -> None:
        """Runs a coroutine in the background with strong ref + error logging."""
        try:
            task = asyncio.create_task(coro)
            self._bg_tasks.add(task)

            def _on_done(t: asyncio.Task[Any]) -> None:
                self._bg_tasks.discard(t)
                if not t.cancelled():
                    exc = t.exception()
                    if exc:
                        logger.warning("Background task raised unhandled exception: %s", exc)

            task.add_done_callback(_on_done)
        except RuntimeError:
            pass

    async def aclose(self) -> None:
        """Waits for pending background tasks (memory extraction, etc.) to finish."""
        await drain_background_tasks(self._bg_tasks, timeout=3.0)

    async def _extract_memory_safe(
        self, user_id: str, profile_id: int | None, message_text: str, message_id: int
    ) -> None:
        """Background memory fact extraction that never raises."""
        try:
            await self.memory_service.process_user_message(
                user_id=user_id,
                character_id=profile_id,
                message_text=message_text,
                source_message_id=message_id,
            )
        except Exception as mem_err:
            logger.warning("Memory fact extraction failed: %s", mem_err)

    async def _resolve_adapter_triple(
        self,
        res: tuple[BaseLLMAdapter, str, str | None],
        conn: aiosqlite.Connection,
        provider_id: str | None = None,
    ) -> tuple[BaseLLMAdapter, str, str]:
        """Normalizes the adapter-factory result into (adapter, model, provider_id)."""
        if isinstance(res, (tuple, list)) and len(res) >= 3:
            return res[0], res[1], res[2] or "custom"
        adapter, model_name = res[0], res[1]
        active_p = await crud.get_active_provider_raw(conn)
        actual_provider_id = provider_id or getattr(adapter, "provider_type", None) or (active_p.id if active_p else "custom")
        return adapter, model_name, actual_provider_id

    async def _resolve_adapter_and_messages(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        prompt: str,
        character_name: str | None = None,
        provider_id: str | None = None,
        system_prompt: str | None = None,
        max_context: int | None = None,
        active_prof: Any | None = None,
        session: Any | None = None,
        reply_format: str = "lines",
        outfit: str | None = None,
    ) -> tuple[BaseLLMAdapter, str, str, list[ChatMessage]]:
        """Resolves active LLM adapter, model name, provider ID, and prepared chat messages."""
        res = await self._get_active_llm_adapter(conn=conn, provider_id=provider_id)
        adapter, model_name, actual_provider_id = await self._resolve_adapter_triple(
            res, conn, provider_id
        )
        messages = await self._prepare_messages(
            conn,
            session_id,
            prompt,
            character_name,
            system_prompt_override=system_prompt,
            max_history_override=max_context,
            active_profile=active_prof,
            session=session,
            outfit=outfit,
            reply_format="lines",
        )
        return adapter, model_name, actual_provider_id, messages

    async def _get_active_llm_adapter(self, conn: aiosqlite.Connection | None = None, provider_id: str | None = None) -> tuple[BaseLLMAdapter, str, str]:
        """
        Loads the configured or requested LLM adapter, target chat model, and resolved provider ID from DB.
        """
        async def _fetch(active_conn: aiosqlite.Connection) -> tuple[BaseLLMAdapter, str, str]:
            settings = await crud.get_settings_raw(active_conn)
            allow_private = bool(getattr(settings, "allow_private_llm_endpoints", False))
            if provider_id:
                provider = await crud.get_provider_raw(active_conn, provider_id)
                if provider is None:
                    raise ConfigurationSelectionError(
                        "指定的模型配置已不存在，请在「设置中心 → 对话模型」重新选择并启用模型。",
                        error_code="PROVIDER_NOT_FOUND",
                    )
            else:
                provider = await crud.get_active_provider_raw(active_conn)

            openai_default = PROVIDER_PRESETS["openai"]["default_chat_model"]
            if provider:
                adapter = get_llm_adapter(provider, allow_private=allow_private)
                chat_model = (provider.chat_model or "").strip()
                if not chat_model:
                    preset = get_provider_preset(provider.id)
                    chat_model = (
                        (preset.get("default_chat_model") if preset else None)
                        or getattr(adapter, "default_model", None)
                        or openai_default
                    )
                return adapter, chat_model, provider.id

            adapter = get_llm_adapter("openai", allow_private=allow_private)
            return adapter, openai_default, "openai"

        if conn is not None:
            return await _fetch(conn)

        async with get_db(self.db_path) as local_conn:
            return await _fetch(local_conn)

    async def _prepare_messages(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        user_prompt: str,
        character_name: str | None = None,
        system_prompt_override: str | None = None,
        max_history_override: int | None = None,
        active_profile: Any | None = None,
        session: Any | None = None,
        reply_format: str = "lines",
        outfit: str | None = None,
    ) -> list[ChatMessage]:
        """
        Constructs system prompt and conversation history messages for LLM using SessionManager.
        Injects dynamically recalled memories and character affection status into prompt context.
        """
        return await build_chat_context(
            conn=conn,
            session_id=session_id,
            user_prompt=user_prompt,
            session_manager=self.session_manager,
            memory_service=self.memory_service,
            character_name=character_name,
            system_prompt_override=system_prompt_override,
            max_history_override=max_history_override,
            active_profile=active_profile,
            session=session,
            outfit=outfit,
            reply_format="lines",
        )

    def _concat_wav_files(
        self,
        chunk_paths: list[str | Path],
        output_path: str | Path,
        pause_duration: float = 0.0,
    ) -> bool:
        """Synchronous WAV concatenation with parameter validation and streaming frames — ALWAYS run via asyncio.to_thread()."""
        return concat_wav_files(chunk_paths, output_path, pause_duration)

    @staticmethod
    def _resolve_ai_adaptive_voice(
        ai_adaptive_voice: bool | None,
        tts_options: dict[str, Any] | None,
    ) -> bool:
        """Resolves whether AI adaptive voice prosody is enabled."""
        if ai_adaptive_voice is not None:
            return bool(ai_adaptive_voice)
        opts_map = tts_options or {}
        return bool(opts_map.get("ai_adaptive_voice", opts_map.get("aiAdaptiveVoice", True)))

    @staticmethod
    async def _resolve_voice_profile(
        conn: aiosqlite.Connection,
        voice_profile_id: int | None,
        tts_options: dict[str, Any] | None,
        sess_obj: Any | None,
        character_name: str | None,
    ) -> Any | None:
        """Explicit ID/name selectors precede a session binding and global default."""
        name = character_name.strip() if character_name else None
        target_profile_id = voice_profile_id
        if target_profile_id is None:
            target_profile_id = (tts_options or {}).get("voice_profile_id")
        if target_profile_id is None and not name:
            target_profile_id = getattr(sess_obj, "voice_profile_id", None)
        try:
            profile = await select_voice_profile(conn, target_profile_id, name)
        except AmbiguousProfileName as exc:
            raise ConfigurationSelectionError(str(exc), error_code="VOICE_PROFILE_AMBIGUOUS", status_code=409) from exc
        if profile is None and (target_profile_id is not None or name):
            raise ConfigurationSelectionError(
                "所选音色已不存在，请在「设置中心 → 角色与会话」重新选择音色。",
                error_code="VOICE_PROFILE_NOT_FOUND",
            )
        return profile

    async def _init_turn_session_and_message(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        prompt: str,
        voice_profile_id: int | None,
        tts_options: dict[str, Any] | None,
        character_name: str | None,
        require_existing_session: bool = False,
    ) -> tuple[Any, Any, Any | None, str, int | None]:
        """Atomically initializes session, records user message, and resolves voice profile & user identifiers."""
        # The caller holds an immediate transaction, so deletion cannot race this check and insertion.
        if require_existing_session and await crud.get_session(conn, session_id) is None:
            raise TurnUnavailableError("对话已被删除，请重新选择对话后再发送。")
        sess_obj = await crud.get_or_create_session(conn, session_id)
        user_msg = await crud.add_message(conn, MessageCreate(
            session_id=session_id,
            role="user",
            content_chinese=prompt,
            content_japanese="",
            audio_url="",
            latency_ms=0,
        ))

        active_prof = await self._resolve_voice_profile(
            conn, voice_profile_id, tts_options, sess_obj, character_name
        )

        user_id = sess_obj.user_id if sess_obj and sess_obj.user_id else "default_user"
        profile_id = active_prof.id if active_prof else None
        return sess_obj, user_msg, active_prof, user_id, profile_id

    async def _prune_orphaned_user_message(self, user_msg_id: int | None) -> None:
        """Prunes orphaned user message when downstream processing fails before assistant reply persistence."""
        if not user_msg_id:
            return
        try:
            async with get_db(self.db_path) as conn:
                async with immediate_transaction(conn):
                    await crud.delete_message(conn, user_msg_id)
        except Exception as prune_err:
            logger.warning("Failed to prune orphaned user message %s: %s", user_msg_id, prune_err)

    @serialize_stream_turn
    async def stream_chat(
        self,
        prompt: str,
        session_id: str = "default",
        character_name: str | None = None,
        provider_id: str | None = None,
        tts_options: dict[str, Any] | None = None,
        cancel_event: asyncio.Event | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_context: int | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        frequency_penalty: float | None = None,
        presence_penalty: float | None = None,
        reasoning_effort: str | None = None,
        ai_adaptive_voice: bool | None = None,
        voice_profile_id: int | None = None,
        reply_format: str = "lines",
        outfit: str | None = None,
        _require_existing_session: bool = False,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        Asynchronously streams bilingual SSE events:
          - text: incremental Chinese delta tokens
          - audio_chunk: synthesized sentence audio URL and index
          - audio_chunk_error: a sentence failed to synthesize (frontend skips it)
          - done: final complete Chinese/Japanese text and full audio URL
          - error: error detail if an exception occurs

        The pipeline producer/worker tasks are reaped by the coordinator's teardown
        finally block, even when the SSE consumer disconnects mid-stream.
        """
        adaptive_enabled = self._resolve_ai_adaptive_voice(ai_adaptive_voice, tts_options)

        t_start = time.perf_counter()

        if cancel_event and cancel_event.is_set():
            logger.info("Stream chat cancelled before starting for session %s", session_id)
            return

        coordinator: StreamCoordinator | None = None
        user_msg: Any | None = None

        try:
            async with get_db(self.db_path) as conn:
                async with immediate_transaction(conn):
                    (
                        sess_obj,
                        user_msg,
                        active_prof,
                        user_id,
                        profile_id,
                    ) = await self._init_turn_session_and_message(
                        conn, session_id, prompt, voice_profile_id, tts_options, character_name,
                        _require_existing_session,
                    )

                # Extract user memory facts and init character affection in a TRUE background task (off TTFT path)
                async def _bg_affection_and_memory() -> None:
                    try:
                        async with get_db(self.db_path) as conn_bg:
                            await crud.get_or_create_character_affection(
                                conn_bg, user_id=user_id, character_id=profile_id or 1
                            )
                    except Exception as exc:
                        logger.debug("Failed background affection record init: %s", exc)
                    await self._extract_memory_safe(user_id, profile_id, prompt, user_msg.id)

                (
                    adapter,
                    model_name,
                    actual_provider_id,
                    messages,
                ) = await self._resolve_adapter_and_messages(
                    conn=conn,
                    session_id=session_id,
                    prompt=prompt,
                    character_name=character_name,
                    provider_id=provider_id,
                    system_prompt=system_prompt,
                    max_context=max_context,
                    active_prof=active_prof,
                    session=sess_obj,
                    outfit=outfit,
                    reply_format="lines",
                )
                self._spawn_background(_bg_affection_and_memory())

            coordinator = StreamCoordinator(
                adapter=adapter,
                messages=messages,
                model_name=model_name,
                actual_provider_id=actual_provider_id,
                session_id=session_id,
                prompt=prompt,
                user_msg=user_msg,
                user_id=user_id,
                profile_id=profile_id,
                active_prof=active_prof,
                tts_service=self.tts_service,
                db_path=self.db_path,
                metrics_collector=self.metrics_collector,
                affection_service=self.affection_service,
                spawn_background=self._spawn_background,
                concat_wav_fn=self._concat_wav_files,
                cancel_event=cancel_event,
                tts_options=tts_options,
                ai_adaptive_voice=adaptive_enabled,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
                t_start=t_start,
                reasoning_effort=reasoning_effort if reasoning_effort is not None else configured_reasoning_effort(adapter),
                reply_format="lines",
                character_name=character_name or (active_prof.name if active_prof else None),
                acting_menu=getattr(messages, "menu", None),
            )

            stream_iter = coordinator.stream()
            try:
                async for event in stream_iter:
                    yield event
            finally:
                await stream_iter.aclose()

        except ConfigurationSelectionError as exc:
            yield {"event": "error", "data": chat_error_payload(exc)}
        except Exception as exc:
            logger.error(
                "Error in stream_chat pipeline (session_id=%s, character=%s, provider=%s): %s",
                session_id,
                character_name,
                provider_id,
                exc,
                exc_info=True,
            )
            safe_err = sanitize_error_detail(exc)
            yield {
                "event": "error",
                "data": {"error": safe_err or "Chat service stream pipeline error"}
            }
        finally:
            if coordinator is None and user_msg is not None:
                # The request scope may be cancelled; the prune still has to land.
                with CancelScope(shield=True):
                    await self._prune_orphaned_user_message(getattr(user_msg, "id", None))


    async def stream_chat_events(
        self,
        prompt: str,
        session_id: str = "default",
        character_name: str | None = None,
        provider_id: str | None = None,
        tts_options: dict[str, Any] | None = None,
        cancel_event: asyncio.Event | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_context: int | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        frequency_penalty: float | None = None,
        presence_penalty: float | None = None,
        reasoning_effort: str | None = None,
        ai_adaptive_voice: bool | None = None,
        voice_profile_id: int | None = None,
        reply_format: str = "lines",
        outfit: str | None = None,
    ) -> AsyncGenerator[str | dict[str, Any], None]:
        """
        Asynchronously streams bilingual SSE formatted event strings.
        Yields standard W3C SSE frames (event: <name>\ndata: <json>\n\n) and emits
        W3C SSE comment frames ': keep-alive\n\n' every 5.0 seconds of queue silence.
        """
        async for event in self.stream_chat(
            prompt=prompt,
            session_id=session_id,
            character_name=character_name,
            provider_id=provider_id,
            tts_options=tts_options,
            cancel_event=cancel_event,
            system_prompt=system_prompt,
            temperature=temperature,
            max_context=max_context,
            top_p=top_p,
            max_tokens=max_tokens,
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
            reasoning_effort=reasoning_effort,
            ai_adaptive_voice=ai_adaptive_voice,
            voice_profile_id=voice_profile_id,
            outfit=outfit,
            reply_format="lines",
        ):
            yield format_sse_frame(event)



__all__ = [
    "StreamingBilingualParser",
    "ChatService",
    "SseKeepAlive",
    "split_japanese_sentences",
    "classify_emotion",
    "EMOTION_KEYWORDS",
    "VALID_EMOTIONS",
    "EMOTION_NAME_MAP",
    "DYNAMIC_SPEED_MIN",
    "DYNAMIC_SPEED_MAX",
    "DYNAMIC_TEMP_MIN",
    "DYNAMIC_TEMP_MAX",
    "clamp_dynamic_speed",
    "clamp_dynamic_temperature",
]
