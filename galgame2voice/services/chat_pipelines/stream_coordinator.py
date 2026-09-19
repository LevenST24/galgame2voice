"""
Stream Coordinator for Galgame2Voice Chat Pipeline.
Encapsulates real-time LLM streaming, token/sentence parsing, TTS synthesis dispatch,
SSE event pumping, WAV concatenation, and transactional message persistence.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Union

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, immediate_transaction
from galgame2voice.services.affection_service import AffectionService
from galgame2voice.services.emotion_classifier import classify_emotion
from galgame2voice.services.metrics_collector import MetricsCollector
from galgame2voice.services.tts_service import TtsService
from galgame2voice.utils.audio_concat import concat_wav_files
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.utils.profiler import ChatTurnProfiler

from .llm_pipeline import LlmStreamPipeline
from .text_pipeline import TextSegmentationPipeline
from .tts_pipeline import TtsStreamPipeline

logger = logging.getLogger("galgame2voice.services.chat_pipelines.stream_coordinator")

# Internal sentinels marking pipeline completion or cancellation
_SENTINEL = object()
_CANCEL_SENTINEL = object()


class SseKeepAlive(dict):
    """W3C Server-Sent Events keep-alive comment frame (: keep-alive\n\n)."""

    def __init__(self):
        super().__init__({"event": ":keep-alive", "data": {}, "comment": ": keep-alive\n\n"})

    def __str__(self) -> str:
        return ": keep-alive\n\n"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str) and other == ": keep-alive\n\n":
            return True
        return super().__eq__(other)


class StreamCoordinator:
    """
    Coordinates end-to-end streaming dialogue execution:
      - Worker and producer tasks (LLM token streaming & TTS synthesis)
      - Event queue pumping (text, audio_chunk, audio_chunk_error, done, error, SseKeepAlive)
      - Cancellation handling, timeout monitoring, and graceful task reaping
      - Multi-chunk WAV concatenation and atomic DB message persistence
    """

    def __init__(
        self,
        *,
        adapter: Any,
        messages: List[Any],
        model_name: str,
        actual_provider_id: str,
        session_id: str,
        prompt: str,
        user_msg: Optional[Any] = None,
        user_id: str = "default_user",
        profile_id: Optional[int] = None,
        active_prof: Optional[Any] = None,
        tts_service: TtsService,
        db_path: str,
        metrics_collector: MetricsCollector,
        affection_service: AffectionService,
        spawn_background: Optional[Callable[[Any], None]] = None,
        concat_wav_fn: Optional[Callable[..., bool]] = None,
        cancel_event: Optional[asyncio.Event] = None,
        tts_options: Optional[Dict[str, Any]] = None,
        ai_adaptive_voice: bool = True,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        t_start: Optional[float] = None,
    ):
        self.adapter = adapter
        self.messages = messages
        self.model_name = model_name
        self.actual_provider_id = actual_provider_id
        self.session_id = session_id
        self.prompt = prompt
        self.user_msg = user_msg
        self.user_id = user_id
        self.profile_id = profile_id
        self.active_prof = active_prof
        self.tts_service = tts_service
        self.db_path = db_path
        self.metrics_collector = metrics_collector
        self.affection_service = affection_service
        self.spawn_background = spawn_background
        self.concat_wav_fn = concat_wav_fn
        self.cancel_event = cancel_event
        self.tts_options = tts_options
        self.ai_adaptive_voice = ai_adaptive_voice
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.frequency_penalty = frequency_penalty
        self.presence_penalty = presence_penalty
        self.t_start = t_start if t_start is not None else time.perf_counter()

        self.persisted_assistant: bool = False

    def _spawn_bg(self, coro: Any) -> None:
        """Spawns background coroutine using callback or asyncio.create_task."""
        if self.spawn_background is not None:
            self.spawn_background(coro)
        else:
            asyncio.create_task(coro)

    def _concat_wav(
        self,
        chunk_paths: List[Union[str, Path]],
        output_path: Union[str, Path],
        pause_duration: float = 0.0,
    ) -> bool:
        """Concatenates audio WAV files."""
        if self.concat_wav_fn is not None:
            return self.concat_wav_fn(chunk_paths, output_path, pause_duration)
        return concat_wav_files(chunk_paths, output_path, pause_duration)

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

    def __aiter__(self) -> AsyncGenerator[Dict[str, Any], None]:
        return self.stream()

    async def stream(self) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Asynchronously streams bilingual SSE event dictionaries:
          - text: incremental Chinese delta tokens
          - audio_chunk: synthesized sentence audio URL and index
          - audio_chunk_error: sentence synthesis error notification
          - done: final complete text, affection update, telemetry and master audio
          - error: top-level error detail if an exception occurs
        """
        if self.cancel_event and self.cancel_event.is_set():
            logger.info("Stream chat cancelled before starting for session %s", self.session_id)
            return

        t_start = self.t_start
        ttft_ms = 0.0
        tts_first_chunk_ms = 0.0
        tts_cached_chunks = 0
        tts_generated_chunks = 0

        text_pipe = TextSegmentationPipeline()
        parser = text_pipe.parser
        audio_chunks: List[Dict[str, Any]] = []
        producer_task: Optional[asyncio.Task] = None
        worker_task: Optional[asyncio.Task] = None
        cancel_monitor: Optional[asyncio.Task] = None
        final_result: Dict[str, Any] = {}
        profiler = ChatTurnProfiler(turn_id=str(self.session_id))

        stream_gen_id = f"chat_{uuid.uuid4().hex[:8]}"
        tts_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        event_queue: asyncio.Queue = asyncio.Queue(maxsize=200)

        cancel_event = self.cancel_event

        if cancel_event:
            async def _watch_cancel():
                await cancel_event.wait()
                try:
                    from galgame2voice.services.tts_scheduler import get_tts_scheduler
                    get_tts_scheduler().cancel_generation(stream_gen_id)
                except Exception:
                    pass
                for t in (producer_task, worker_task):
                    if t is not None and not t.done():
                        t.cancel()
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

        tts_pipe = TtsStreamPipeline(self.tts_service, stream_gen_id)
        llm_pipe = LlmStreamPipeline(
            self.adapter,
            self.messages,
            self.model_name,
            cancel_event=cancel_event,
            temperature=self.temperature,
            top_p=self.top_p,
            max_tokens=self.max_tokens,
            frequency_penalty=self.frequency_penalty,
            presence_penalty=self.presence_penalty,
        )

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
                        if not tts_pipe.is_vocal_sentence(sentence):
                            logger.debug("Skipping non-vocal sentence chunk: '%s'", sentence)
                            continue
                        if cancel_event and cancel_event.is_set():
                            break

                        chunk_opts = text_pipe.get_dynamic_tts_options(
                            base_options=self.tts_options,
                            adaptive_enabled=bool(self.ai_adaptive_voice),
                            sentence_text=sentence,
                        )
                        chunk_opts = tts_pipe.prepare_chunk_options(
                            chunk_opts,
                            self.active_prof,
                            chunk_index,
                            sentence,
                        )
                        chunk_data, err_str = await tts_pipe.synthesize_chunk(
                            sentence=sentence,
                            chunk_index=chunk_index,
                            options=chunk_opts,
                            profiler=profiler,
                        )
                        if err_str:
                            await _put_with_cancel(event_queue, {
                                "event": "audio_chunk_error",
                                "data": {
                                    "index": chunk_index,
                                    "sentence": sentence,
                                    "error": err_str,
                                },
                            })
                        elif chunk_data:
                            if tts_first_chunk_ms == 0.0:
                                tts_first_chunk_ms = (time.perf_counter() - t_start) * 1000.0
                            if chunk_data["is_cached"]:
                                tts_cached_chunks += 1
                            else:
                                tts_generated_chunks += 1
                            audio_chunks.append(chunk_data)
                            await _put_with_cancel(event_queue, {
                                "event": "audio_chunk",
                                "data": {
                                    "index": chunk_index,
                                    "audio_url": chunk_data["audio_url"],
                                    "sentence": sentence,
                                },
                            })
                        chunk_index += 1
                    finally:
                        tts_queue.task_done()
            except asyncio.CancelledError:
                logger.debug("TTS worker received CancelledError, exiting worker loop.")
            finally:
                await _put_with_cancel(event_queue, _SENTINEL)

        # LLM Stream Producer
        async def llm_producer():
            nonlocal ttft_ms
            try:
                async for token in llm_pipe.stream_tokens():
                    if cancel_event and cancel_event.is_set():
                        break
                    delta_ch, completed_sentences, current_emo = text_pipe.feed_token(token)
                    if delta_ch:
                        profiler.record_llm_first_token()
                        if ttft_ms == 0.0:
                            ttft_ms = (time.perf_counter() - t_start) * 1000.0
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
                        profiler.record_first_sentence()
                        ok = await _put_with_cancel(tts_queue, sentence)
                        if not ok:
                            break

                full_ch, full_ja, rem_ch, rem_sentences, final_emo = text_pipe.finalize()
                final_result["chinese"] = full_ch
                final_result["japanese"] = full_ja
                if rem_ch and not (cancel_event and cancel_event.is_set()):
                    profiler.record_llm_first_token()
                    if ttft_ms == 0.0:
                        ttft_ms = (time.perf_counter() - t_start) * 1000.0
                    await _put_with_cancel(event_queue, {
                        "event": "text",
                        "data": {
                            "delta_chinese": rem_ch,
                            "emotion": final_emo,
                        }
                    })
                for sentence in rem_sentences:
                    if cancel_event and cancel_event.is_set():
                        break
                    profiler.record_first_sentence()
                    await _put_with_cancel(tts_queue, sentence)
            except Exception as exc:
                logger.error("LLM Producer error: %s", exc)
                try:
                    p_ch, p_ja, _, _, _ = text_pipe.finalize()
                    if p_ch and not final_result.get("chinese"):
                        final_result["chinese"] = p_ch
                    if p_ja and not final_result.get("japanese"):
                        final_result["japanese"] = p_ja
                except Exception:
                    pass
                safe_err = sanitize_error_detail(exc)
                await _put_with_cancel(event_queue, {"event": "error", "data": {"error": safe_err or "LLM generation failed"}})
            finally:
                await _put_with_cancel(tts_queue, None)
                await _put_with_cancel(event_queue, _SENTINEL)

        try:
            producer_task = asyncio.create_task(llm_producer())
            worker_task = asyncio.create_task(tts_worker())

            # Event pump: responsive wait on event queue and cancel event.
            sentinels_received = 0
            error_seen = False
            last_event_time = time.monotonic()
            keep_alive_interval = 5.0
            while sentinels_received < 2:
                if cancel_event and cancel_event.is_set():
                    break
                try:
                    event = event_queue.get_nowait()
                    last_event_time = time.monotonic()
                except asyncio.QueueEmpty:
                    get_task = asyncio.create_task(event_queue.get())
                    cancel_wait_task = asyncio.create_task(cancel_event.wait()) if cancel_event else None
                    wait_set = {get_task}
                    if cancel_wait_task:
                        wait_set.add(cancel_wait_task)

                    done, pending = await asyncio.wait(wait_set, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()

                    if cancel_event and cancel_event.is_set():
                        break

                    if get_task in done:
                        try:
                            event = get_task.result()
                            last_event_time = time.monotonic()
                        except asyncio.CancelledError:
                            continue
                    else:
                        now = time.monotonic()
                        if now - last_event_time >= keep_alive_interval:
                            yield SseKeepAlive()
                            last_event_time = now
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
                            error_seen, bool(cancel_event and cancel_event.is_set()), self.session_id)
                # Reap producer/worker before persisting so TTS synthesis stops
                # promptly and no task writes concurrently.
                for task in (producer_task, worker_task):
                    if task is not None and not task.done():
                        task.cancel()

                # Persist the partial reply the user already saw so the turn is
                # not silently dropped from history (the user message was saved).
                partial_ch = final_result.get("chinese") or parser.chinese_extracted
                partial_ja = final_result.get("japanese") or parser.japanese_extracted
                has_meaningful_content = bool(
                    (partial_ch and partial_ch.strip()) or (partial_ja and partial_ja.strip())
                )
                if has_meaningful_content:
                    async def _persist_partial():
                        try:
                            async with get_db(self.db_path) as conn:
                                await crud.add_message(conn, MessageCreate(
                                    session_id=self.session_id,
                                    role="assistant",
                                    content_chinese=partial_ch,
                                    content_japanese=partial_ja,
                                    audio_url="",
                                    latency_ms=int((time.perf_counter() - t_start) * 1000),
                                ))
                        except Exception as persist_err:
                            logger.warning("Failed to persist partial assistant message: %s", persist_err)

                    self._spawn_bg(_persist_partial())

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

            if cancel_event and cancel_event.is_set():
                logger.info("Stream chat ended early (error=False, cancelled=True) for session %s", self.session_id)
                partial_ch = final_result.get("chinese") or parser.chinese_extracted
                partial_ja = final_result.get("japanese") or parser.japanese_extracted
                has_meaningful_content = bool(
                    (partial_ch and partial_ch.strip()) or (partial_ja and partial_ja.strip())
                )
                if has_meaningful_content:
                    async def _persist_partial():
                        try:
                            async with get_db(self.db_path) as conn:
                                await crud.add_message(conn, MessageCreate(
                                    session_id=self.session_id,
                                    role="assistant",
                                    content_chinese=partial_ch,
                                    content_japanese=partial_ja,
                                    audio_url="",
                                    latency_ms=int((time.perf_counter() - t_start) * 1000),
                                ))
                        except Exception as persist_err:
                            logger.warning("Failed to persist partial assistant message: %s", persist_err)

                    self._spawn_bg(_persist_partial())

                yield {
                    "event": "done",
                    "data": {
                        "truncated": True,
                        "chinese": partial_ch or "",
                        "japanese": partial_ja or "",
                    }
                }
                return

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
                        pause_candidate = None
                        if self.tts_options:
                            pause_candidate = self.tts_options.get("fragment_interval")
                            if pause_candidate is None:
                                pause_candidate = self.tts_options.get("pause_duration")
                            if pause_candidate is None:
                                pause_candidate = self.tts_options.get("sentence_pause")
                        if pause_candidate is not None:
                            try:
                                pause_sec = float(pause_candidate)
                            except (ValueError, TypeError):
                                pause_sec = 0.3
                        else:
                            pause_sec = 0.3
                        pause_sec = max(0.0, min(5.0, pause_sec))
                        full_filename = f"full_{uuid.uuid4().hex[:12]}.wav"
                        full_path = self.tts_service.audio_dir / full_filename
                        ok = await asyncio.to_thread(
                            self._concat_wav,
                            [c.get("local_path", "") for c in audio_chunks],
                            full_path,
                            pause_sec,
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
            prompt_text = "".join([getattr(m, "content", "") for m in self.messages])
            prompt_tokens = self.metrics_collector.estimate_tokens(prompt_text)
            completion_tokens = self.metrics_collector.estimate_tokens(full_chinese + full_japanese)

            metric_record = await self.metrics_collector.record_metric(
                session_id=self.session_id,
                channel="web",
                provider_id=self.actual_provider_id,
                model_name=self.model_name,
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
                    async with immediate_transaction(conn):
                        await crud.add_message(conn, MessageCreate(
                            session_id=self.session_id,
                            role="assistant",
                            content_chinese=full_chinese,
                            content_japanese=full_japanese,
                            audio_url=total_audio_url,
                            latency_ms=total_latency,
                        ))
                    self.persisted_assistant = True
            elif self.user_msg is not None and getattr(self.user_msg, "id", None):
                # No meaningful assistant tokens were generated.
                # Prune the orphaned user message to prevent consecutive user turns in DB history.
                try:
                    async with get_db(self.db_path) as conn:
                        async with immediate_transaction(conn):
                            await conn.execute("DELETE FROM messages WHERE id = ?;", (self.user_msg.id,))
                except Exception as prune_err:
                    logger.warning("Failed to prune orphaned user message %s: %s", self.user_msg.id, prune_err)

            # Clean local_path from audio_chunks before emitting to frontend
            clean_chunks = [
                {"index": c.get("index", i), "audio_url": c.get("audio_url", ""), "sentence": c.get("sentence", "")}
                for i, c in enumerate(audio_chunks)
            ]

            # Affection State Machine update
            try:
                affection_res = await self.affection_service.handle_turn_affection(
                    user_id=self.user_id,
                    character_id=self.profile_id,
                    user_text=self.prompt,
                    assistant_text=full_chinese,
                    explicit_emotion=final_emotion,
                )
                final_emotion = affection_res.get("emotion", final_emotion)
            except Exception as aff_err:
                logger.warning("Affection update in stream_chat failed: %s", aff_err)
                affection_res = self._affection_fallback(final_emotion)

            final_tts_params = {
                "speed": parser.tts_speed,
                "temperature": parser.tts_temperature,
                "emotion": parser.tts_emotion,
                "adaptive_enabled": bool(self.ai_adaptive_voice),
            } if (parser.tts_speed is not None or parser.tts_temperature is not None or parser.tts_emotion is not None) else None

            # Emit final done event
            yield {
                "event": "done",
                "data": {
                    "truncated": bool((cancel_event and cancel_event.is_set()) or not has_meaningful_content),
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
                    "telemetry": profiler.to_dict(),
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
            try:
                from galgame2voice.services.tts_scheduler import get_tts_scheduler
                get_tts_scheduler().cancel_generation(stream_gen_id)
            except Exception:
                pass

            if cancel_monitor and not cancel_monitor.done():
                cancel_monitor.cancel()
                try:
                    await cancel_monitor
                except (asyncio.CancelledError, Exception):
                    pass

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
                    if cancel_event and cancel_event.is_set():
                        if not task.done():
                            task.cancel()
                        else:
                            await task
                    else:
                        await task
                except asyncio.CancelledError:
                    pass
                except Exception as task_exc:
                    logger.debug("Pipeline task ended with exception: %s", task_exc)

            # If the stream exited abruptly (e.g. client disconnect / GeneratorExit)
            # without having persisted the assistant message:
            if not self.persisted_assistant and self.user_msg is not None:
                partial_ch = final_result.get("chinese") or parser.chinese_extracted
                partial_ja = final_result.get("japanese") or parser.japanese_extracted
                has_meaningful_content = bool(
                    (partial_ch and partial_ch.strip()) or (partial_ja and partial_ja.strip())
                )
                if has_meaningful_content:
                    try:
                        async with get_db(self.db_path) as conn:
                            async with immediate_transaction(conn):
                                await crud.add_message(conn, MessageCreate(
                                    session_id=self.session_id,
                                    role="assistant",
                                    content_chinese=partial_ch,
                                    content_japanese=partial_ja,
                                    audio_url="",
                                    latency_ms=int((time.perf_counter() - t_start) * 1000),
                                ))
                            self.persisted_assistant = True
                    except Exception as persist_err:
                        logger.warning("Failed to persist partial assistant message in finally: %s", persist_err)
                elif getattr(self.user_msg, "id", None):
                    # No meaningful assistant tokens were generated before disconnect.
                    # Prune the orphaned user message to prevent consecutive user turns in DB history.
                    try:
                        async with get_db(self.db_path) as conn:
                            async with immediate_transaction(conn):
                                await conn.execute("DELETE FROM messages WHERE id = ?;", (self.user_msg.id,))
                    except Exception as prune_err:
                        logger.warning("Failed to prune orphaned user message %s: %s", self.user_msg.id, prune_err)

            profiler.print_waterfall()


__all__ = [
    "StreamCoordinator",
    "SseKeepAlive",
    "_SENTINEL",
    "_CANCEL_SENTINEL",
]
