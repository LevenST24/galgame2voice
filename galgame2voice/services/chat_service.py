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
import json
import logging
import math
import re
import time
import uuid
import wave
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union
from pathlib import Path

import aiosqlite

from galgame2voice.config import get_settings
from galgame2voice.adapters.base import ChatMessage, BaseLLMAdapter
from galgame2voice.adapters.registry import get_llm_adapter
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, immediate_transaction
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.session_manager import SessionManager
from galgame2voice.services.memory_service import MemoryService
from galgame2voice.services.affection_service import AffectionService
from galgame2voice.services.metrics_collector import get_metrics_collector, MetricsCollector
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.utils.text_splitter import split_japanese_sentences

logger = logging.getLogger("galgame2voice.services.chat_service")

# Internal sentinel marking "this pipeline stage has finished producing events".
_SENTINEL = object()
_CANCEL_SENTINEL = object()


# ============================================================================
# Emotion Taxonomy & Classifier (Decoupled Module)
# ============================================================================
from galgame2voice.services.emotion_classifier import (
    EMOTION_KEYWORDS,
    VALID_EMOTIONS,
    EMOTION_NAME_MAP,
    classify_emotion,
)

# ============================================================================
# Dynamic AI-Driven Voice Prosody & Emotion Constants
# ============================================================================
from galgame2voice.utils.prosody import (
    DYNAMIC_SPEED_MIN,
    DYNAMIC_SPEED_MAX,
    DYNAMIC_TEMP_MIN,
    DYNAMIC_TEMP_MAX,
    clamp_dynamic_speed,
    clamp_dynamic_temperature,
)


# ============================================================================
# Streaming Bilingual Parser (Decoupled Module)
# ============================================================================
from galgame2voice.services.streaming_parser import StreamingBilingualParser


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
        tts_service: Optional[TtsService] = None,
        db_path: Optional[Union[str, Path]] = None,
        metrics_collector: Optional[MetricsCollector] = None,
    ):
        from galgame2voice.database.session import get_database_path
        self.tts_service = tts_service or TtsService()
        self.db_path = str(db_path or get_database_path())
        self.session_manager = SessionManager(db_path=self.db_path)
        self.memory_service = MemoryService(db_path=self.db_path)
        self.affection_service = AffectionService(db_path=self.db_path)
        self.metrics_collector = metrics_collector or get_metrics_collector(db_path=self.db_path)
        # Strong references for fire-and-forget background tasks (prevent GC mid-flight).
        self._bg_tasks: set = set()

    def _spawn_background(self, coro) -> None:
        """Runs a coroutine in the background with strong ref + error logging."""
        try:
            task = asyncio.create_task(coro)
            self._bg_tasks.add(task)

            def _on_done(t: asyncio.Task) -> None:
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
        pending = [t for t in self._bg_tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=3.0)
        stragglers = [t for t in self._bg_tasks if not t.done()]
        for t in stragglers:
            t.cancel()
        if stragglers:
            await asyncio.gather(*stragglers, return_exceptions=True)
        self._bg_tasks.clear()

    async def _extract_memory_safe(
        self, user_id: str, profile_id: Optional[int], message_text: str, message_id: int
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
        res: Tuple[BaseLLMAdapter, str, Optional[str]],
        conn: aiosqlite.Connection,
        provider_id: Optional[str] = None,
    ) -> Tuple[BaseLLMAdapter, str, str]:
        """Normalizes the adapter-factory result into (adapter, model, provider_id)."""
        if isinstance(res, (tuple, list)) and len(res) >= 3:
            return res[0], res[1], res[2]
        adapter, model_name = res[0], res[1]
        active_p = await crud.get_active_provider_raw(conn)
        actual_provider_id = provider_id or getattr(adapter, "provider_type", None) or (active_p.id if active_p else "custom")
        return adapter, model_name, actual_provider_id

    @staticmethod
    def _affection_fallback(emotion: str) -> Dict[str, Any]:
        """Neutral affection payload used when the affection update fails."""
        return {
            "score": 0,
            "level": 1,
            "level_name": "初识/生疏",
            "emotion": emotion,
            "points_earned": 0,
        }

    async def _get_active_llm_adapter(self, conn: Optional[aiosqlite.Connection] = None, provider_id: Optional[str] = None) -> Tuple[BaseLLMAdapter, str, str]:
        """
        Loads the configured or requested LLM adapter, target chat model, and resolved provider ID from DB.
        """
        if conn is not None:
            if provider_id:
                provider = await crud.get_provider_raw(conn, provider_id)
            else:
                provider = await crud.get_active_provider_raw(conn)

            if provider:
                adapter = get_llm_adapter(provider)
                chat_model = provider.chat_model or "gpt-4o-mini"
                return adapter, chat_model, provider.id

            adapter = get_llm_adapter("openai")
            return adapter, "gpt-4o-mini", "openai"

        async with get_db(self.db_path) as local_conn:
            if provider_id:
                provider = await crud.get_provider_raw(local_conn, provider_id)
            else:
                provider = await crud.get_active_provider_raw(local_conn)

            if provider:
                adapter = get_llm_adapter(provider)
                chat_model = provider.chat_model or "gpt-4o-mini"
                return adapter, chat_model, provider.id

            adapter = get_llm_adapter("openai")
            return adapter, "gpt-4o-mini", "openai"

    async def get_active_llm_adapter(
        self,
        conn: Optional[aiosqlite.Connection] = None,
        provider_id: Optional[str] = None,
    ) -> Tuple[BaseLLMAdapter, str, str]:
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
        character_name: Optional[str] = None,
        system_prompt_override: Optional[str] = None,
        max_history_override: Optional[int] = None,
    ) -> List[ChatMessage]:
        """
        Constructs system prompt and conversation history messages for LLM using SessionManager.
        Injects dynamically recalled memories and character affection status into prompt context.
        """
        active_profile = await crud.get_active_voice_profile(conn)
        if system_prompt_override and system_prompt_override.strip():
            system_prompt = system_prompt_override.strip()
        else:
            system_prompt = (
                active_profile.system_prompt
                if active_profile and active_profile.system_prompt
                else self.session_manager.DEFAULT_SYSTEM_TEMPLATE
            )

        # Auto-upgrade legacy prompt formats that lack dynamic tts instructions
        if system_prompt and '"tts":' not in system_prompt and '{"chinese":' in system_prompt:
            system_prompt = system_prompt.replace(
                '{"chinese": "显示给玩家的中文台词", "japanese": "对应的口语化日文台词"}',
                '{"tts": {"speed": 1.05, "temp": 0.95, "emotion": "gentle"}, "chinese": "显示给玩家的中文台词", "japanese": "对应的口语化日文台词"}'
            )
            if "动态决定语音推理参数" not in system_prompt:
                system_prompt = system_prompt.replace(
                    "你必须严格输出如下 JSON 格式",
                    "你必须严格输出如下 JSON 格式，在最开头根据语境动态决定语音推理参数（speed 语速: 0.5~1.5 请大胆调节！激动时可设为1.3以上，低落时设为0.7以下, temp 温度: 0.60~1.20, emotion 情绪: gentle|shy|happy|tsundere|cool|sad）"
                )

        char_name = character_name or (active_profile.name if active_profile else "四季夏目")

        settings_raw = await crud.get_settings_raw(conn)
        max_history = max_history_override or (settings_raw.max_history_messages if settings_raw else 10)

        session = await crud.get_session(conn, session_id)
        user_id = session.user_id if session and session.user_id else "default_user"
        profile_id = active_profile.id if active_profile else 1

        # RAG memory retrieval & affection context injection
        try:
            recalled_memories = await self.memory_service.retrieve_relevant_memories(
                user_id=user_id,
                character_id=profile_id,
                prompt=user_prompt,
                top_k=5,
                conn=conn,
            )
            affection = await crud.get_or_create_character_affection(conn, user_id=user_id, character_id=profile_id)
            aff_info = {
                "level": affection.affection_level,
                "level_name": affection.level_name,
                "emotion": affection.current_emotion,
                "nickname": affection.custom_nickname,
            }
            memory_block = self.memory_service.format_memory_prompt_block(recalled_memories, aff_info)
        except Exception as e:
            logger.warning("Failed to retrieve memories for prompt injection: %s", e)
            memory_block = None

        return await self.session_manager.build_chat_messages(
            session_id=session_id,
            user_prompt=user_prompt,
            character_name=char_name,
            custom_system_prompt=system_prompt,
            max_messages=max_history,
            memory_prompt_block=memory_block,
            conn=conn,
        )

    async def prepare_messages(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        user_prompt: str,
        character_name: Optional[str] = None,
        system_prompt_override: Optional[str] = None,
        max_history_override: Optional[int] = None,
    ) -> List[ChatMessage]:
        """Public interface for preparing chat messages.

        External callers (e.g. the Telegram bot) should use this instead of the
        private _prepare_messages.
        """
        return await self._prepare_messages(
            conn, session_id, user_prompt, character_name,
            system_prompt_override=system_prompt_override,
            max_history_override=max_history_override,
        )

    def _concat_wav_files(self, chunk_paths: List[str], output_path: Path) -> bool:
        """Synchronous WAV concatenation with parameter validation and streaming frames — ALWAYS run via asyncio.to_thread()."""
        if not chunk_paths:
            return False

        valid_files: List[Path] = []
        base_params = None

        for local_p_str in chunk_paths:
            if not local_p_str:
                continue
            p = Path(local_p_str)
            if not p.is_file():
                continue
            try:
                with wave.open(str(p), "rb") as w:
                    cur_params = w.getparams()
                    if base_params is None:
                        base_params = cur_params
                        valid_files.append(p)
                    else:
                        if (
                            w.getnchannels() == base_params.nchannels
                            and w.getsampwidth() == base_params.sampwidth
                            and w.getframerate() == base_params.framerate
                        ):
                            valid_files.append(p)
                        else:
                            logger.warning(
                                "Skipping WAV chunk %s: mismatched audio parameters (channels=%d, sampwidth=%d, framerate=%d vs base channels=%d, sampwidth=%d, framerate=%d)",
                                p, w.getnchannels(), w.getsampwidth(), w.getframerate(),
                                base_params.nchannels, base_params.sampwidth, base_params.framerate,
                            )
            except Exception as exc:
                logger.debug("Skipping unreadable WAV chunk %s: %s", p, exc)

        if not valid_files or base_params is None:
            return False

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(output_path), "wb") as w_out:
                w_out.setparams(base_params)
                for p in valid_files:
                    try:
                        with wave.open(str(p), "rb") as w_in:
                            while True:
                                frames = w_in.readframes(4096)
                                if not frames:
                                    break
                                w_out.writeframes(frames)
                    except Exception as err:
                        logger.warning("Error reading frames from chunk %s: %s", p, err)
            return True
        except Exception as exc:
            logger.error("Failed to write concatenated WAV to %s: %s", output_path, exc)
            return False

    async def stream_chat(
        self,
        prompt: str,
        session_id: str = "default",
        character_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        tts_options: Optional[Dict[str, Any]] = None,
        cancel_event: Optional[asyncio.Event] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_context: Optional[int] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        ai_adaptive_voice: Optional[bool] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
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
        # Resolve AI adaptive voice mode (session setting or parameter)
        if ai_adaptive_voice is None:
            opts_map = tts_options or {}
            ai_adaptive_voice = opts_map.get("ai_adaptive_voice", opts_map.get("aiAdaptiveVoice", True))

        t_start = time.perf_counter()
        ttft_ms = 0.0
        tts_first_chunk_ms = 0.0
        tts_cached_chunks = 0
        tts_generated_chunks = 0

        parser = StreamingBilingualParser()
        audio_chunks: List[Dict[str, Any]] = []
        producer_task: Optional[asyncio.Task] = None
        worker_task: Optional[asyncio.Task] = None
        cancel_monitor: Optional[asyncio.Task] = None
        persisted_assistant: bool = False
        user_msg: Optional[Any] = None

        if cancel_event and cancel_event.is_set():
            logger.info("Stream chat cancelled before starting for session %s", session_id)
            return

        try:
            async with get_db(self.db_path) as conn:
                # Ensure session exists and record user message
                sess_obj = await crud.get_or_create_session(conn, session_id)
                user_msg = await crud.add_message(conn, MessageCreate(
                    session_id=session_id,
                    role="user",
                    content_chinese=prompt,
                    content_japanese="",
                    audio_url="",
                    latency_ms=0,
                ))

                active_prof = await crud.get_active_voice_profile(conn)
                user_id = sess_obj.user_id if sess_obj and sess_obj.user_id else "default_user"
                profile_id = active_prof.id if active_prof else None

                # Extract user memory facts in a TRUE background task (off TTFT path)
                self._spawn_background(
                    self._extract_memory_safe(user_id, profile_id, prompt, user_msg.id)
                )

                res = await self._get_active_llm_adapter(conn=conn, provider_id=provider_id)
                adapter, model_name, actual_provider_id = await self._resolve_adapter_triple(
                    res, conn, provider_id
                )
                messages = await self._prepare_messages(
                    conn, session_id, prompt, character_name,
                    system_prompt_override=system_prompt,
                    max_history_override=max_context,
                )

            # Bounded queues provide real backpressure: a slow SSE consumer
            # throttles the pipeline instead of growing memory without limit.
            tts_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
            event_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
            final_result: Dict[str, Any] = {}

            if cancel_event:
                async def _watch_cancel():
                    await cancel_event.wait()
                    try:
                        tts_queue.put_nowait(None)
                    except Exception:
                        pass
                    try:
                        event_queue.put_nowait(_CANCEL_SENTINEL)
                    except Exception:
                        pass

                cancel_monitor = asyncio.create_task(_watch_cancel())

            async def _put_with_cancel(q: asyncio.Queue, item: Any) -> bool:
                """Puts an item into a bounded queue with ultra-low latency while remaining responsive to cancel_event."""
                if cancel_event and cancel_event.is_set():
                    return False
                try:
                    q.put_nowait(item)
                    return True
                except asyncio.QueueFull:
                    pass
                while True:
                    if cancel_event and cancel_event.is_set():
                        return False
                    try:
                        await asyncio.wait_for(q.put(item), timeout=0.1)
                        return True
                    except asyncio.TimeoutError:
                        continue

            # Background TTS Consumer Worker
            async def tts_worker():
                nonlocal tts_first_chunk_ms, tts_cached_chunks, tts_generated_chunks
                chunk_index = 0
                try:
                    while True:
                        if cancel_event and cancel_event.is_set():
                            break
                        sentence = await tts_queue.get()
                        try:
                            if sentence is None:
                                break
                            if not sentence.strip():
                                continue
                            if cancel_event and cancel_event.is_set():
                                break

                            try:
                                chunk_opts = parser.get_dynamic_tts_options(
                                    base_options=tts_options,
                                    adaptive_enabled=bool(ai_adaptive_voice),
                                )
                                audio_url, local_path, _ = await self.tts_service.synthesize_to_file(
                                    sentence,
                                    options=chunk_opts,
                                    filename_prefix=f"chunk_{chunk_index}",
                                )
                                if tts_first_chunk_ms == 0.0:
                                    tts_first_chunk_ms = (time.perf_counter() - t_start) * 1000.0
                                if "/audio/cache/" in str(audio_url):
                                    tts_cached_chunks += 1
                                else:
                                    tts_generated_chunks += 1

                                chunk_data = {
                                    "index": chunk_index,
                                    "audio_url": audio_url,
                                    "sentence": sentence,
                                    "local_path": str(local_path),
                                }
                                audio_chunks.append(chunk_data)
                                await _put_with_cancel(event_queue, {
                                    "event": "audio_chunk",
                                    "data": {
                                        "index": chunk_index,
                                        "audio_url": audio_url,
                                        "sentence": sentence,
                                    },
                                })
                            except Exception as tts_err:
                                # Surface the failure to the frontend instead of
                                # silently dropping the sentence.
                                logger.warning(
                                    "Failed to synthesize audio chunk %d for sentence '%s': %s",
                                    chunk_index, sentence, tts_err,
                                )
                                safe_err = sanitize_error_detail(str(tts_err)[:200])
                                await _put_with_cancel(event_queue, {
                                    "event": "audio_chunk_error",
                                    "data": {
                                        "index": chunk_index,
                                        "sentence": sentence,
                                        "error": safe_err,
                                    },
                                })
                            chunk_index += 1
                        finally:
                            tts_queue.task_done()
                finally:
                    await _put_with_cancel(event_queue, _SENTINEL)

            # LLM Stream Producer
            async def llm_producer():
                nonlocal ttft_ms
                stream_gen = None
                try:
                    stream_kwargs: Dict[str, Any] = {"model": model_name}
                    if temperature is not None:
                        stream_kwargs["temperature"] = temperature
                    if top_p is not None:
                        stream_kwargs["top_p"] = top_p
                    if max_tokens is not None:
                        stream_kwargs["max_tokens"] = max_tokens
                    if frequency_penalty is not None:
                        stream_kwargs["frequency_penalty"] = frequency_penalty
                    if presence_penalty is not None:
                        stream_kwargs["presence_penalty"] = presence_penalty
                    stream_gen = adapter.stream_chat(messages, **stream_kwargs)
                    async for token in stream_gen:
                        if cancel_event and cancel_event.is_set():
                            break
                        delta_ch, completed_sentences = parser.feed_chunk(token)
                        if delta_ch:
                            if ttft_ms == 0.0:
                                ttft_ms = (time.perf_counter() - t_start) * 1000.0
                            current_emo = parser.emotion_extracted or classify_emotion(parser.chinese_extracted, parser.japanese_extracted)
                            ok = await _put_with_cancel(event_queue, {
                                "event": "text",
                                "data": {
                                    "delta_chinese": delta_ch,
                                    "emotion": current_emo,
                                }
                            })
                            if not ok:
                                break
                        for sentence in completed_sentences:
                            ok = await _put_with_cancel(tts_queue, sentence)
                            if not ok:
                                break

                    full_ch, full_ja, rem_sentences = parser.finalize()
                    final_result["chinese"] = full_ch
                    final_result["japanese"] = full_ja
                    if len(full_ch) > parser.emitted_chinese_len and not (cancel_event and cancel_event.is_set()):
                        rem_ch = full_ch[parser.emitted_chinese_len:]
                        if ttft_ms == 0.0:
                            ttft_ms = (time.perf_counter() - t_start) * 1000.0
                        current_emo = parser.emotion_extracted or classify_emotion(full_ch, full_ja)
                        await _put_with_cancel(event_queue, {
                            "event": "text",
                            "data": {
                                "delta_chinese": rem_ch,
                                "emotion": current_emo,
                            }
                        })
                    for sentence in rem_sentences:
                        if cancel_event and cancel_event.is_set():
                            break
                        await _put_with_cancel(tts_queue, sentence)
                except Exception as exc:
                    logger.error("LLM Producer error: %s", exc)
                    safe_err = sanitize_error_detail(exc)
                    await _put_with_cancel(event_queue, {"event": "error", "data": {"error": safe_err or "LLM generation failed"}})
                finally:
                    if stream_gen is not None and hasattr(stream_gen, "aclose"):
                        try:
                            await asyncio.wait_for(stream_gen.aclose(), timeout=0.05)
                        except (asyncio.TimeoutError, Exception):
                            pass
                    await _put_with_cancel(tts_queue, None)
                    await _put_with_cancel(event_queue, _SENTINEL)

            producer_task = asyncio.create_task(llm_producer())
            worker_task = asyncio.create_task(tts_worker())

            # Event pump: responsive wait on event queue and cancel event.
            sentinels_received = 0
            error_seen = False
            while sentinels_received < 2:
                if cancel_event and cancel_event.is_set():
                    break
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    if cancel_event and cancel_event.is_set():
                        break
                    continue

                if event is _CANCEL_SENTINEL or (cancel_event and cancel_event.is_set()):
                    break

                if event is _SENTINEL:
                    sentinels_received += 1
                    continue

                yield event
                if event.get("event") == "error":
                    error_seen = True
                    break

            if error_seen or (cancel_event and cancel_event.is_set()):
                logger.info("Stream chat ended early (error=%s, cancelled=%s) for session %s",
                            error_seen, bool(cancel_event and cancel_event.is_set()), session_id)
                # Reap producer/worker before persisting so TTS synthesis stops
                # promptly and no task writes concurrently.
                for task in (producer_task, worker_task):
                    if task is not None and not task.done():
                        task.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.gather(producer_task, worker_task, return_exceptions=True),
                        timeout=0.1,
                    )
                except asyncio.TimeoutError:
                    pass



                # Persist the partial reply the user already saw so the turn is
                # not silently dropped from history (the user message was saved).
                partial_ch = final_result.get("chinese") or parser.chinese_extracted
                partial_ja = final_result.get("japanese") or parser.japanese_extracted
                has_meaningful_content = bool(
                    (partial_ch and partial_ch.strip()) or (partial_ja and partial_ja.strip())
                )
                if has_meaningful_content:
                    try:
                        async with get_db(self.db_path) as conn:
                            await crud.add_message(conn, MessageCreate(
                                session_id=session_id,
                                role="assistant",
                                content_chinese=partial_ch,
                                content_japanese=partial_ja,
                                audio_url="",
                                latency_ms=int((time.perf_counter() - t_start) * 1000),
                            ))
                            persisted_assistant = True
                    except Exception as persist_err:
                        logger.warning("Failed to persist partial assistant message: %s", persist_err)
                if cancel_event and cancel_event.is_set():
                    # Explicit truncated done so the client can distinguish
                    # "user stopped" from a broken connection.
                    yield {
                        "event": "done",
                        "data": {
                            "truncated": True,
                            "chinese": partial_ch or "",
                            "japanese": partial_ja or "",
                        }
                    }
                return

            # Ensure both tasks are fully finished before touching shared state.
            await asyncio.gather(producer_task, worker_task, return_exceptions=True)

            full_chinese = final_result.get("chinese") or parser.chinese_extracted
            full_japanese = final_result.get("japanese") or parser.japanese_extracted
            final_emotion = classify_emotion(full_chinese, full_japanese, parser.emotion_extracted)

            # Concatenate chunks into a master WAV in a worker thread.
            total_audio_url = ""
            if audio_chunks:
                if len(audio_chunks) == 1:
                    total_audio_url = audio_chunks[0]["audio_url"]
                else:
                    try:
                        full_filename = f"full_{uuid.uuid4().hex[:12]}.wav"
                        full_path = self.tts_service.audio_dir / full_filename
                        ok = await asyncio.to_thread(
                            self._concat_wav_files,
                            [c.get("local_path", "") for c in audio_chunks],
                            full_path,
                        )
                        total_audio_url = f"/audio/{full_filename}" if ok else audio_chunks[0]["audio_url"]
                    except Exception as cat_err:
                        logger.warning("Failed to concatenate audio chunks: %s", cat_err)
                        total_audio_url = audio_chunks[0]["audio_url"]

            total_latency = int((time.perf_counter() - t_start) * 1000)
            if ttft_ms == 0.0:
                ttft_ms = float(total_latency)
            if tts_first_chunk_ms == 0.0:
                tts_first_chunk_ms = float(total_latency)

            # Calculate and record token and latency metrics
            prompt_text = "".join([getattr(m, "content", "") for m in messages])
            prompt_tokens = self.metrics_collector.estimate_tokens(prompt_text)
            completion_tokens = self.metrics_collector.estimate_tokens(full_chinese + full_japanese)

            metric_record = await self.metrics_collector.record_metric(
                session_id=session_id,
                channel="web",
                provider_id=actual_provider_id,
                model_name=model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                ttft_ms=ttft_ms,
                tts_first_chunk_ms=tts_first_chunk_ms,
                total_latency_ms=float(total_latency),
                tts_cached_chunks=tts_cached_chunks,
                tts_generated_chunks=tts_generated_chunks,
            )

            has_meaningful_content = bool(
                (full_chinese and full_chinese.strip()) or (full_japanese and full_japanese.strip())
            )
            if has_meaningful_content:
                # Persist assistant message in DB
                async with get_db(self.db_path) as conn:
                    await crud.add_message(conn, MessageCreate(
                        session_id=session_id,
                        role="assistant",
                        content_chinese=full_chinese,
                        content_japanese=full_japanese,
                        audio_url=total_audio_url,
                        latency_ms=total_latency,
                    ))
                    persisted_assistant = True
            elif user_msg is not None and getattr(user_msg, "id", None):
                # No meaningful assistant tokens were generated.
                # Prune the orphaned user message to prevent consecutive user turns in DB history.
                try:
                    async with get_db(self.db_path) as conn:
                        async with immediate_transaction(conn):
                            await conn.execute("DELETE FROM messages WHERE id = ?;", (user_msg.id,))
                except Exception as prune_err:
                    logger.warning("Failed to prune orphaned user message %s: %s", user_msg.id, prune_err)

            # Clean local_path from audio_chunks before emitting to frontend
            clean_chunks = [
                {"index": c.get("index", i), "audio_url": c.get("audio_url", ""), "sentence": c.get("sentence", "")}
                for i, c in enumerate(audio_chunks)
            ]

            # Affection State Machine update
            try:
                affection_res = await self.affection_service.handle_turn_affection(
                    user_id=user_id,
                    character_id=profile_id,
                    user_text=prompt,
                    assistant_text=full_chinese,
                )
                final_emotion = affection_res.get("emotion", final_emotion)
            except Exception as aff_err:
                logger.warning("Affection update in stream_chat failed: %s", aff_err)
                affection_res = self._affection_fallback(final_emotion)

            final_tts_params = {
                "speed": parser.tts_speed,
                "temperature": parser.tts_temperature,
                "emotion": parser.tts_emotion,
                "adaptive_enabled": bool(ai_adaptive_voice),
            } if (parser.tts_speed is not None or parser.tts_temperature is not None or parser.tts_emotion is not None) else None

            # Emit final done event
            yield {
                "event": "done",
                "data": {
                    "truncated": not has_meaningful_content,
                    "chinese": full_chinese,
                    "japanese": full_japanese,
                    "emotion": final_emotion,
                    "affection": affection_res,
                    "metrics": metric_record,
                    "audio_url": total_audio_url,
                    "total_audio_url": total_audio_url,
                    "chunks": clean_chunks,
                    "latency_ms": total_latency,
                    "tts_params": final_tts_params,
                }
            }

        except Exception as exc:
            logger.error("Error in stream_chat pipeline: %s", exc, exc_info=True)
            safe_err = sanitize_error_detail(exc)
            yield {
                "event": "error",
                "data": {"error": safe_err or "Chat service stream pipeline error"}
            }
        finally:
            if cancel_monitor and not cancel_monitor.done():
                cancel_monitor.cancel()

            # Reap producer/worker no matter how we exited (normal end, error,
            # client disconnect, or cancellation). This prevents orphan tasks
            # from holding the TTS inference lock forever.
            for task in (producer_task, worker_task):
                if task is not None and not task.done():
                    task.cancel()
            for task in (producer_task, worker_task):
                if task is None:
                    continue
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as task_exc:
                    logger.debug("Pipeline task ended with exception: %s", task_exc)

            # If the stream exited abruptly (e.g. client disconnect / GeneratorExit)
            # without having persisted the assistant message:
            if not persisted_assistant and user_msg is not None:
                partial_ch = final_result.get("chinese") or parser.chinese_extracted
                partial_ja = final_result.get("japanese") or parser.japanese_extracted
                has_meaningful_content = bool(
                    (partial_ch and partial_ch.strip()) or (partial_ja and partial_ja.strip())
                )
                if has_meaningful_content:
                    try:
                        async with get_db(self.db_path) as conn:
                            await crud.add_message(conn, MessageCreate(
                                session_id=session_id,
                                role="assistant",
                                content_chinese=partial_ch,
                                content_japanese=partial_ja,
                                audio_url="",
                                latency_ms=int((time.perf_counter() - t_start) * 1000),
                            ))
                            persisted_assistant = True
                    except Exception as persist_err:
                        logger.warning("Failed to persist partial assistant message in finally: %s", persist_err)
                elif getattr(user_msg, "id", None):
                    # No meaningful assistant tokens were generated before disconnect.
                    # Prune the orphaned user message to prevent consecutive user turns in DB history.
                    try:
                        async with get_db(self.db_path) as conn:
                            async with immediate_transaction(conn):
                                await conn.execute("DELETE FROM messages WHERE id = ?;", (user_msg.id,))
                    except Exception as prune_err:
                        logger.warning("Failed to prune orphaned user message %s: %s", user_msg.id, prune_err)

    async def chat_sync(
        self,
        prompt: str,
        session_id: str = "default",
        character_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        tts_options: Optional[Dict[str, Any]] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_context: Optional[int] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        ai_adaptive_voice: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Synchronous non-streaming bilingual completion and TTS synthesis.
        """
        if ai_adaptive_voice is None:
            opts_map = tts_options or {}
            ai_adaptive_voice = opts_map.get("ai_adaptive_voice", opts_map.get("aiAdaptiveVoice", True))

        t_start = time.perf_counter()
        user_msg = None
        persisted_assistant = False
        try:
            async with get_db(self.db_path) as conn:
                sess_obj = await crud.get_or_create_session(conn, session_id)
                user_msg = await crud.add_message(conn, MessageCreate(
                    session_id=session_id,
                    role="user",
                    content_chinese=prompt,
                    content_japanese="",
                    audio_url="",
                    latency_ms=0,
                ))

                active_prof = await crud.get_active_voice_profile(conn)
                user_id = sess_obj.user_id if sess_obj and sess_obj.user_id else "default_user"
                profile_id = active_prof.id if active_prof else None

                # Extract user memory facts in background
                self._spawn_background(
                    self._extract_memory_safe(user_id, profile_id, prompt, user_msg.id)
                )

                res = await self._get_active_llm_adapter(conn=conn, provider_id=provider_id)
                adapter, model_name, actual_provider_id = await self._resolve_adapter_triple(
                    res, conn, provider_id
                )
                messages = await self._prepare_messages(
                    conn, session_id, prompt, character_name,
                    system_prompt_override=system_prompt,
                    max_history_override=max_context,
                )

            t_llm_start = time.perf_counter()
            chat_kwargs: Dict[str, Any] = {"model": model_name}
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
            raw_text = llm_response.content

            # Parse bilingual response
            parser = StreamingBilingualParser()
            parser.feed_chunk(raw_text)
            chinese, japanese, _ = parser.finalize()

            if not chinese:
                chinese = raw_text
            if not japanese:
                japanese = chinese

            final_emotion = classify_emotion(chinese, japanese, parser.emotion_extracted)

            # Affection update
            try:
                affection_res = await self.affection_service.handle_turn_affection(
                    user_id=user_id,
                    character_id=profile_id,
                    user_text=prompt,
                    assistant_text=chinese,
                )
                final_emotion = affection_res.get("emotion", final_emotion)
            except Exception as aff_err:
                logger.warning("Affection update in chat_sync failed: %s", aff_err)
                affection_res = self._affection_fallback(final_emotion)

            # Synthesize full audio
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
                    )
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
                    logger.warning("TTS synthesis in chat_sync failed: %s", e)

            latency_ms = int((time.perf_counter() - t_start) * 1000)

            # Token and Latency Telemetry
            prompt_text = "".join([getattr(m, "content", "") for m in messages])
            prompt_tokens = self.metrics_collector.estimate_tokens(prompt_text)
            completion_tokens = self.metrics_collector.estimate_tokens(chinese + japanese)

            metric_record = await self.metrics_collector.record_metric(
                session_id=session_id,
                channel="web",
                provider_id=actual_provider_id,
                model_name=model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                ttft_ms=ttft_ms,
                tts_first_chunk_ms=tts_first_chunk_ms,
                total_latency_ms=float(latency_ms),
                tts_cached_chunks=tts_cached_chunks,
                tts_generated_chunks=tts_generated_chunks,
            )

            # Save assistant message to DB
            async with get_db(self.db_path) as conn:
                await crud.add_message(conn, MessageCreate(
                    session_id=session_id,
                    role="assistant",
                    content_chinese=chinese,
                    content_japanese=japanese,
                    audio_url=audio_url,
                    latency_ms=latency_ms,
                ))
                persisted_assistant = True

            final_tts_params = {
                "speed": parser.tts_speed,
                "temperature": parser.tts_temperature,
                "emotion": parser.tts_emotion,
                "adaptive_enabled": bool(ai_adaptive_voice),
            } if (parser.tts_speed is not None or parser.tts_temperature is not None or parser.tts_emotion is not None) else None

            return {
                "session_id": session_id,
                "chinese": chinese,
                "japanese": japanese,
                "emotion": final_emotion,
                "affection": affection_res,
                "metrics": metric_record,
                "audio_url": audio_url,
                "audioUrl": audio_url,
                "latency_ms": latency_ms,
                "tts_params": final_tts_params,
            }
        finally:
            if not persisted_assistant and user_msg is not None and getattr(user_msg, "id", None):
                try:
                    async with get_db(self.db_path) as conn:
                        async with immediate_transaction(conn):
                            await conn.execute("DELETE FROM messages WHERE id = ?;", (user_msg.id,))
                except Exception as prune_err:
                    logger.warning("Failed to prune orphaned user message %s in chat_sync: %s", user_msg.id, prune_err)

    async def resolve_message_japanese(
        self,
        text: str,
        session_id: Optional[str] = None,
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
            # First attempt: match by session_id and exact/substring content_chinese
            if session_id:
                cursor = await conn.execute(
                    """
                    SELECT content_japanese FROM messages
                    WHERE session_id = ? AND role = 'assistant' AND content_japanese != ''
                      AND (content_chinese = ? OR ? LIKE '%' || content_chinese || '%' OR content_chinese LIKE '%' || ? || '%')
                    ORDER BY id DESC LIMIT 1
                    """,
                    (session_id, clean_text, clean_text, clean_text),
                )
                row = await cursor.fetchone()
                if row and row[0] and str(row[0]).strip():
                    return str(row[0]).strip()

            # Second attempt: search across all assistant messages in DB
            cursor = await conn.execute(
                """
                SELECT content_japanese FROM messages
                WHERE role = 'assistant' AND content_japanese != ''
                  AND (content_chinese = ? OR ? LIKE '%' || content_chinese || '%' OR content_chinese LIKE '%' || ? || '%')
                ORDER BY id DESC LIMIT 1
                """,
                (clean_text, clean_text, clean_text),
            )
            row = await cursor.fetchone()
            if row and row[0] and str(row[0]).strip():
                return str(row[0]).strip()

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
                ja = raw_ja.strip().strip('"\'`「」『』')
                return ja
            except Exception as exc:
                logger.warning("LLM translation fallback in resolve_message_japanese failed: %s", exc)
                return ""


__all__ = [
    "StreamingBilingualParser",
    "ChatService",
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
