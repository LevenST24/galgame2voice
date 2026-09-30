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
  - The event pump blocks on the queue (1s heartbeat only for cancel
    responsiveness) instead of busy-polling every 50ms.
  - WAV concatenation runs in a worker thread so the event loop never freezes.
  - Memory fact extraction runs in a true background task, off the TTFT path.
"""

import asyncio
import logging
from pathlib import Path
import time
from typing import Any, AsyncGenerator

import aiosqlite

from galgame2voice.adapters.base import ChatMessage, BaseLLMAdapter
from galgame2voice.adapters.registry import get_llm_adapter
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_database_path, get_db, immediate_transaction
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.session_manager import SessionManager
from galgame2voice.services.memory_service import MemoryService
from galgame2voice.services.affection_service import AffectionService
from galgame2voice.services.metrics_collector import get_metrics_collector, MetricsCollector
from galgame2voice.utils.async_tasks import drain_background_tasks
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.utils.sse import format_sse_frame
from galgame2voice.utils.text_splitter import split_japanese_sentences

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
        self.tts_service = tts_service or TtsService()
        self.db_path = str(db_path or get_database_path())
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
        )
        return adapter, model_name, actual_provider_id, messages

    @staticmethod
    def _format_sync_response(
        session_id: str,
        chinese: str,
        japanese: str,
        emotion: str,
        affection_res: dict[str, Any],
        metric_record: Any,
        audio_url: str,
        latency_ms: int,
        parser: Any,
        adaptive_enabled: bool,
    ) -> dict[str, Any]:
        """Formats standard response dictionary for chat_sync."""
        final_tts_params = {
            "speed": parser.tts_speed,
            "temperature": parser.tts_temperature,
            "emotion": parser.tts_emotion,
            "adaptive_enabled": bool(adaptive_enabled),
        } if (parser.tts_speed is not None or parser.tts_temperature is not None or parser.tts_emotion is not None) else None

        return {
            "session_id": session_id,
            "chinese": chinese,
            "japanese": japanese,
            "emotion": emotion,
            "affection": affection_res,
            "metrics": metric_record,
            "audio_url": audio_url,
            "audioUrl": audio_url,
            "latency_ms": latency_ms,
            "tts_params": final_tts_params,
        }

    @staticmethod
    def _affection_fallback(emotion: str) -> dict[str, Any]:
        """Neutral affection payload used when the affection update fails."""
        return AffectionService.get_fallback_payload(emotion)

    async def _get_active_llm_adapter(self, conn: aiosqlite.Connection | None = None, provider_id: str | None = None) -> tuple[BaseLLMAdapter, str, str]:
        """
        Loads the configured or requested LLM adapter, target chat model, and resolved provider ID from DB.
        """
        async def _fetch(active_conn: aiosqlite.Connection) -> tuple[BaseLLMAdapter, str, str]:
            if provider_id:
                provider = await crud.get_provider_raw(active_conn, provider_id)
            else:
                provider = await crud.get_active_provider_raw(active_conn)

            if provider:
                adapter = get_llm_adapter(provider)
                chat_model = provider.chat_model or "gpt-4o-mini"
                return adapter, chat_model, provider.id

            adapter = get_llm_adapter("openai")
            return adapter, "gpt-4o-mini", "openai"

        if conn is not None:
            return await _fetch(conn)

        async with get_db(self.db_path) as local_conn:
            return await _fetch(local_conn)

    async def get_active_llm_adapter(
        self,
        conn: aiosqlite.Connection | None = None,
        provider_id: str | None = None,
    ) -> tuple[BaseLLMAdapter, str, str]:
        """Public interface for getting the active LLM adapter.

        Returns (adapter, chat_model, provider_id). External callers (e.g. the
        Telegram bot) should use this instead of the private _get_active_llm_adapter.
        """
        return await self._get_active_llm_adapter(conn=conn, provider_id=provider_id)

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
        )

    async def prepare_messages(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        user_prompt: str,
        character_name: str | None = None,
        system_prompt_override: str | None = None,
        max_history_override: int | None = None,
        active_profile: Any | None = None,
        session: Any | None = None,
    ) -> list[ChatMessage]:
        """Public interface for preparing chat messages.

        External callers (e.g. the Telegram bot) should use this instead of the
        private _prepare_messages.
        """
        return await self._prepare_messages(
            conn, session_id, user_prompt, character_name,
            system_prompt_override=system_prompt_override,
            max_history_override=max_history_override,
            active_profile=active_profile,
            session=session,
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
        """Resolves active voice profile: explicit voice_profile_id -> tts_options -> session -> character_name -> global active."""
        target_profile_id = voice_profile_id or (tts_options or {}).get("voice_profile_id") or (sess_obj.voice_profile_id if sess_obj else None)
        active_prof = None
        if target_profile_id is not None:
            active_prof = await crud.get_voice_profile(conn, int(target_profile_id))
        if active_prof is None and character_name:
            active_prof = await crud.get_voice_profile_by_name(conn, character_name)
        if active_prof is None:
            active_prof = await crud.get_active_voice_profile(conn)
        return active_prof

    async def _init_turn_session_and_message(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        prompt: str,
        voice_profile_id: int | None,
        tts_options: dict[str, Any] | None,
        character_name: str | None,
    ) -> tuple[Any, Any, Any | None, str, int | None]:
        """Atomically initializes session, records user message, and resolves voice profile & user identifiers."""
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

    async def _prune_orphaned_user_message(self, user_msg_id: int | None, context_label: str = "") -> None:
        """Prunes orphaned user message when downstream processing fails before assistant reply persistence."""
        if not user_msg_id:
            return
        try:
            async with get_db(self.db_path) as conn:
                async with immediate_transaction(conn):
                    await crud.delete_message(conn, user_msg_id)
        except Exception as prune_err:
            logger.warning(
                "Failed to prune orphaned user message %s%s: %s",
                user_msg_id,
                f" in {context_label}" if context_label else "",
                prune_err,
            )

    async def _execute_sync_llm(
        self,
        adapter: BaseLLMAdapter,
        messages: list[ChatMessage],
        model_name: str,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        frequency_penalty: float | None = None,
        presence_penalty: float | None = None,
    ) -> tuple[str, float]:
        """Invokes LLM chat non-streaming and returns (completion_text, ttft_ms)."""
        t_llm_start = time.perf_counter()
        chat_kwargs: dict[str, Any] = {"model": model_name}
        if temperature is not None:
            chat_kwargs["temperature"] = temperature
        if top_p is not None:
            chat_kwargs["top_p"] = top_p
        if max_tokens is not None:
            chat_kwargs["max_tokens"] = max_tokens
        if frequency_penalty is not None:
            chat_kwargs["frequency_penalty"] = frequency_penalty
        if presence_penalty is not None:
            chat_kwargs["presence_penalty"] = presence_penalty
        llm_response = await adapter.chat(messages, **chat_kwargs)
        ttft_ms = (time.perf_counter() - t_llm_start) * 1000.0
        return llm_response.content, ttft_ms

    async def _synthesize_sync_audio(
        self,
        japanese: str,
        parser: StreamingBilingualParser,
        tts_options: dict[str, Any] | None,
        ai_adaptive_voice: bool,
        active_prof: Any | None,
    ) -> tuple[str, float, int, int]:
        """Synthesizes complete audio for sync chat response."""
        audio_url = ""
        tts_first_chunk_ms = 0.0
        tts_cached_chunks = 0
        tts_generated_chunks = 0
        if japanese.strip():
            try:
                t_tts_start = time.perf_counter()
                sync_opts = parser.get_dynamic_tts_options(
                    base_options=tts_options,
                    adaptive_enabled=bool(ai_adaptive_voice),
                    sentence_text=japanese,
                )
                if active_prof:
                    sync_opts.setdefault("voice_profile_id", active_prof.id)
                    sync_opts.setdefault("character_name", active_prof.name)
                user_split_method = (
                    (tts_options or {}).get("text_split_method")
                    or (tts_options or {}).get("cut_option")
                    or (tts_options or {}).get("how_to_cut")
                )
                if not user_split_method:
                    sync_opts["text_split_method"] = "cut0" if len(japanese.strip()) <= 80 else "cut2"
                audio_url, _, _ = await self.tts_service.synthesize_to_file(
                    japanese,
                    options=sync_opts,
                    filename_prefix="voice",
                )
                tts_first_chunk_ms = (time.perf_counter() - t_tts_start) * 1000.0
                if "/audio/cache/" in str(audio_url):
                    tts_cached_chunks = 1
                else:
                    tts_generated_chunks = 1
            except Exception as e:
                profile_name = getattr(active_prof, "name", "unknown") if active_prof else "unknown"
                logger.warning("TTS synthesis in chat_sync failed for profile '%s': %s", profile_name, e)
        return audio_url, tts_first_chunk_ms, tts_cached_chunks, tts_generated_chunks

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
        ai_adaptive_voice: bool | None = None,
        voice_profile_id: int | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        Asynchronously streams bilingual SSE events:
          - text: incremental Chinese delta tokens
          - audio_chunk: synthesized sentence audio URL and index
          - audio_chunk_error: a sentence failed to synthesize (frontend skips it)
          - done: final complete Chinese/Japanese text and full audio URL
          - error: error detail if an exception occurs

        All background tasks are guaranteed to be reaped in the finally block,
        even when the SSE consumer disconnects mid-stream.
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
                        conn, session_id, prompt, voice_profile_id, tts_options, character_name
                    )

                # Extract user memory facts and init character affection in a TRUE background task (off TTFT path)
                async def _bg_affection_and_memory() -> None:
                    try:
                        async with get_db(self.db_path) as conn_bg:
                            await crud.get_or_create_character_affection(
                                conn_bg, user_id=user_id, character_id=profile_id or 1
                            )
                    except Exception as exc:
                        logger.debug("Failed background affection update: %s", exc)
                    await self._extract_memory_safe(user_id, profile_id, prompt, user_msg.id)

                self._spawn_background(_bg_affection_and_memory())

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
                )

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
            )

            stream_iter = coordinator.stream()
            try:
                async for event in stream_iter:
                    yield event
            finally:
                await stream_iter.aclose()

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
        ai_adaptive_voice: bool | None = None,
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
            ai_adaptive_voice=ai_adaptive_voice,
        ):
            yield format_sse_frame(event)

    async def chat_sync(
        self,
        prompt: str,
        session_id: str = "default",
        character_name: str | None = None,
        provider_id: str | None = None,
        tts_options: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_context: int | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        frequency_penalty: float | None = None,
        presence_penalty: float | None = None,
        ai_adaptive_voice: bool | None = None,
        voice_profile_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Synchronous non-streaming bilingual completion and TTS synthesis.
        """
        adaptive_enabled = self._resolve_ai_adaptive_voice(ai_adaptive_voice, tts_options)

        t_start = time.perf_counter()
        user_msg = None
        persisted_assistant = False
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
                        conn, session_id, prompt, voice_profile_id, tts_options, character_name
                    )
                    await crud.get_or_create_character_affection(
                        conn, user_id=user_id, character_id=profile_id or 1
                    )

                # Extract user memory facts in background
                self._spawn_background(
                    self._extract_memory_safe(user_id, profile_id, prompt, user_msg.id)
                )

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
                )

            raw_text, ttft_ms = await self._execute_sync_llm(
                adapter=adapter,
                messages=messages,
                model_name=model_name,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
            )

            # Parse bilingual response
            chinese, japanese, parser = StreamingBilingualParser.parse_full_text(raw_text)

            final_emotion = classify_emotion(chinese, japanese, parser.emotion_extracted)

            # Affection update
            try:
                affection_res = await self.affection_service.handle_turn_affection(
                    user_id=user_id,
                    character_id=profile_id,
                    user_text=prompt,
                    assistant_text=chinese,
                    explicit_emotion=final_emotion,
                )
                final_emotion = affection_res.get("emotion", final_emotion)
            except Exception as aff_err:
                logger.warning("Affection update in chat_sync failed: %s", aff_err)
                affection_res = self._affection_fallback(final_emotion)

            # Synthesize full audio
            audio_url, tts_first_chunk_ms, tts_cached_chunks, tts_generated_chunks = await self._synthesize_sync_audio(
                japanese=japanese,
                parser=parser,
                tts_options=tts_options,
                ai_adaptive_voice=adaptive_enabled,
                active_prof=active_prof,
            )

            latency_ms = int((time.perf_counter() - t_start) * 1000)

            # Token and Latency Telemetry
            metric_record = await self.metrics_collector.record_chat_turn(
                session_id=session_id,
                channel="web",
                provider_id=actual_provider_id,
                model_name=model_name,
                messages=messages,
                chinese=chinese,
                japanese=japanese,
                ttft_ms=ttft_ms,
                tts_first_chunk_ms=tts_first_chunk_ms,
                total_latency_ms=float(latency_ms),
                tts_cached_chunks=tts_cached_chunks,
                tts_generated_chunks=tts_generated_chunks,
            )

            # Save assistant message to DB
            async with get_db(self.db_path) as conn:
                async with immediate_transaction(conn):
                    await crud.add_message(conn, MessageCreate(
                        session_id=session_id,
                        role="assistant",
                        content_chinese=chinese,
                        content_japanese=japanese,
                        audio_url=audio_url,
                        latency_ms=latency_ms,
                    ))
                persisted_assistant = True

            return self._format_sync_response(
                session_id=session_id,
                chinese=chinese,
                japanese=japanese,
                emotion=final_emotion,
                affection_res=affection_res,
                metric_record=metric_record,
                audio_url=audio_url,
                latency_ms=latency_ms,
                parser=parser,
                adaptive_enabled=adaptive_enabled,
            )
        finally:
            if not persisted_assistant and user_msg is not None:
                await self._prune_orphaned_user_message(getattr(user_msg, "id", None), context_label="chat_sync")

    async def resolve_message_japanese(
        self,
        text: str,
        session_id: str | None = None,
    ) -> str:
        """
        Resolves the Japanese original text used during backend synthesis for a given Chinese message.
        1. Checks database records for an exact or approximate match in messages table.
        2. If not found in database, invokes active LLM adapter to translate to spoken Japanese suitable for Galgame.
        """
        clean_text = (text or "").strip()
        if not clean_text:
            return ""

        async with get_db(self.db_path) as conn:
            async def _find_ja(sid: str | None) -> str | None:
                where_clause = "session_id = ? AND " if sid else ""
                query = f"""
                    SELECT content_japanese FROM messages
                    WHERE {where_clause}role = 'assistant' AND content_japanese != ''
                      AND (content_chinese = ? OR ? LIKE '%' || content_chinese || '%' OR content_chinese LIKE '%' || ? || '%')
                    ORDER BY id DESC LIMIT 1
                """
                params = (sid, clean_text, clean_text, clean_text) if sid else (clean_text, clean_text, clean_text)
                cursor = await conn.execute(query, params)
                row = await cursor.fetchone()
                if row and row[0] and str(row[0]).strip():
                    return str(row[0]).strip()
                return None

            # First attempt: match by session_id and exact/substring content_chinese
            if session_id:
                matched = await _find_ja(session_id)
                if matched:
                    return matched

            # Second attempt: search across all assistant messages in DB
            matched = await _find_ja(None)
            if matched:
                return matched

            # Third attempt: fallback to active LLM translation
            try:
                res = await self._get_active_llm_adapter(conn=conn)
                adapter, model_name, _ = await self._resolve_adapter_triple(res, conn)
                translation_prompt = (
                    "你是一个Galgame本地化配音翻译专家。请将以下中文台词直接翻译为适合配音朗读的口语化自然日文。"
                    "注意：仅输出翻译后的纯日文句子，严禁包含任何中文、拼音、假名注音或解释说明。\n\n"
                    f"中文台词：{clean_text}"
                )
                chat_msg = ChatMessage(role="user", content=translation_prompt)
                resp = await adapter.chat([chat_msg], model=model_name, temperature=0.3)
                raw_ja = resp.content if hasattr(resp, "content") else str(resp)
                return raw_ja.strip().strip('"\'`「」『』')
            except Exception as exc:
                logger.warning("LLM translation fallback in resolve_message_japanese failed: %s", exc)
                return ""


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
