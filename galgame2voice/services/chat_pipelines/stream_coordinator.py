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
from typing import Any, AsyncGenerator, Callable

from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, immediate_transaction
from galgame2voice.services.affection_service import AffectionService
from galgame2voice.services.emotion_classifier import classify_emotion
from galgame2voice.services.metrics_collector import MetricsCollector
from galgame2voice.services.tts_scheduler import get_tts_scheduler
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


def _consume_background_failure(task: asyncio.Task) -> None:
    """Logs a fire-and-forget task's failure instead of deferring it to GC.

    A task whose exception is never retrieved makes the interpreter print a bare
    "Task exception was never retrieved" when it is collected, losing both the
    context and the timing of the original failure.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "Background streaming task failed: %s: %s",
            type(exc).__name__,
            exc,
            exc_info=exc,
        )


class SseKeepAlive(dict):
    """W3C Server-Sent Events keep-alive comment frame (: keep-alive\n\n)."""

    def __init__(self) -> None:
        super().__init__({"event": ":keep-alive", "data": {}, "comment": ": keep-alive\n\n"})

    def __str__(self) -> str:
        return ": keep-alive\n\n"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str) and other == ": keep-alive\n\n":
            return True
        return super().__eq__(other)


def _has_meaningful_text(chinese: str | None, japanese: str | None) -> bool:
    """Returns True if either Chinese or Japanese string contains non-whitespace text."""
    return bool((chinese and chinese.strip()) or (japanese and japanese.strip()))


class _StreamRun:
    """Holds per-stream runtime state, pipelines, task handles, and telemetry."""

    def __init__(
        self,
        *,
        t_start: float,
        session_id: str,
        stream_gen_id: str,
        text_pipe: TextSegmentationPipeline,
        tts_pipe: TtsStreamPipeline,
        llm_pipe: LlmStreamPipeline,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        self.t_start: float = t_start
        self.session_id: str = session_id
        self.stream_gen_id: str = stream_gen_id
        self.cancel_event: asyncio.Event | None = cancel_event

        self.text_pipe: TextSegmentationPipeline = text_pipe
        self.parser = text_pipe.parser
        self.tts_pipe: TtsStreamPipeline = tts_pipe
        self.llm_pipe: LlmStreamPipeline = llm_pipe

        self.tts_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=200)

        self.producer_task: asyncio.Task | None = None
        self.worker_task: asyncio.Task | None = None
        self.cancel_monitor: asyncio.Task | None = None

        self.final_result: dict[str, Any] = {}
        self.audio_chunks: list[dict[str, Any]] = []
        self.profiler: ChatTurnProfiler = ChatTurnProfiler(turn_id=str(session_id))

        self.ttft_ms: float = 0.0
        self.tts_first_chunk_ms: float = 0.0
        self.tts_cached_chunks: int = 0
        self.tts_generated_chunks: int = 0
        self.error_seen: bool = False


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
        messages: list[Any],
        model_name: str,
        actual_provider_id: str,
        session_id: str,
        prompt: str,
        user_msg: Any | None = None,
        user_id: str = "default_user",
        profile_id: int | None = None,
        active_prof: Any | None = None,
        tts_service: TtsService,
        db_path: str,
        metrics_collector: MetricsCollector,
        affection_service: AffectionService,
        spawn_background: Callable[[Any], None] | None = None,
        concat_wav_fn: Callable[..., bool] | None = None,
        cancel_event: asyncio.Event | None = None,
        tts_options: dict[str, Any] | None = None,
        ai_adaptive_voice: bool = True,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        frequency_penalty: float | None = None,
        presence_penalty: float | None = None,
        t_start: float | None = None,
    ) -> None:
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
            return
        task = asyncio.create_task(coro)
        task.add_done_callback(_consume_background_failure)

    def _concat_wav(
        self,
        chunk_paths: list[str | Path],
        output_path: str | Path,
        pause_duration: float = 0.0,
    ) -> bool:
        """Concatenates audio WAV files."""
        if self.concat_wav_fn is not None:
            return self.concat_wav_fn(chunk_paths, output_path, pause_duration)
        return concat_wav_files(chunk_paths, output_path, pause_duration)

    @staticmethod
    def _affection_fallback(emotion: str) -> dict[str, Any]:
        """Neutral affection payload used when the affection update fails."""
        return AffectionService.get_fallback_payload(emotion)

    def __aiter__(self) -> AsyncGenerator[dict[str, Any], None]:
        return self.stream()

    def _init_stream_run(self) -> _StreamRun:
        """Initializes a new per-stream context object holding state, pipelines, and queues."""
        stream_gen_id = f"chat_{uuid.uuid4().hex[:8]}"
        text_pipe = TextSegmentationPipeline()
        tts_pipe = TtsStreamPipeline(self.tts_service, stream_gen_id)
        llm_pipe = LlmStreamPipeline(
            self.adapter,
            self.messages,
            self.model_name,
            cancel_event=self.cancel_event,
            temperature=self.temperature,
            top_p=self.top_p,
            max_tokens=self.max_tokens,
            frequency_penalty=self.frequency_penalty,
            presence_penalty=self.presence_penalty,
        )
        return _StreamRun(
            t_start=self.t_start,
            session_id=str(self.session_id),
            stream_gen_id=stream_gen_id,
            text_pipe=text_pipe,
            tts_pipe=tts_pipe,
            llm_pipe=llm_pipe,
            cancel_event=self.cancel_event,
        )

    def _start_subtasks(self, run: _StreamRun) -> None:
        """Starts background producer, consumer, and cancel monitor tasks."""
        run.producer_task = asyncio.create_task(self._llm_producer(run))
        run.worker_task = asyncio.create_task(self._tts_worker(run))
        if run.cancel_event:
            run.cancel_monitor = asyncio.create_task(self._watch_cancel(run))

    async def _watch_cancel(self, run: _StreamRun) -> None:
        """Watches for external cancellation and signals pipeline subtasks and queues."""
        if not run.cancel_event:
            return
        await run.cancel_event.wait()
        try:
            get_tts_scheduler().cancel_generation(run.stream_gen_id)
        except Exception as cancel_err:
            logger.debug("Non-critical: error cancelling generation in _watch_cancel: %s", cancel_err)
        for t in (run.producer_task, run.worker_task):
            if t is not None and not t.done():
                t.cancel()
        try:
            run.tts_queue.put_nowait(None)
        except (asyncio.QueueFull, Exception):
            pass
        try:
            run.event_queue.put_nowait(_CANCEL_SENTINEL)
        except (asyncio.QueueFull, Exception):
            pass

    async def _put_with_cancel(
        self,
        queue: asyncio.Queue,
        item: Any,
        cancel_event: asyncio.Event | None = None,
    ) -> bool:
        """Puts an item into a bounded queue with ultra-low latency while remaining responsive to cancel_event."""
        ce = cancel_event if cancel_event is not None else self.cancel_event
        if ce and ce.is_set():
            return False
        try:
            # Fast path: non-blocking enqueue without coroutine suspension
            queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            # Queue at capacity; fall back to short polling loop responsive to cancel_event
            pass
        while True:
            if ce and ce.is_set():
                return False
            try:
                await asyncio.wait_for(queue.put(item), timeout=0.1)
                return True
            except asyncio.TimeoutError:
                # Slot wait timed out; re-evaluate cancel_event before retrying
                continue

    async def _tts_worker(self, run: _StreamRun) -> None:
        """Background worker consuming text sentences from tts_queue and synthesizing audio chunks."""
        chunk_index = 0
        cancel_event = run.cancel_event
        tts_pipe = run.tts_pipe
        text_pipe = run.text_pipe
        tts_queue = run.tts_queue
        event_queue = run.event_queue
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
                        profiler=run.profiler,
                    )
                    if err_str:
                        await self._put_with_cancel(event_queue, {
                            "event": "audio_chunk_error",
                            "data": {
                                "index": chunk_index,
                                "sentence": sentence,
                                "error": err_str,
                            },
                        }, cancel_event)
                    elif chunk_data:
                        if run.tts_first_chunk_ms == 0.0:
                            run.tts_first_chunk_ms = (time.perf_counter() - run.t_start) * 1000.0
                        if chunk_data["is_cached"]:
                            run.tts_cached_chunks += 1
                        else:
                            run.tts_generated_chunks += 1
                        run.audio_chunks.append(chunk_data)
                        await self._put_with_cancel(event_queue, {
                            "event": "audio_chunk",
                            "data": {
                                "index": chunk_index,
                                "audio_url": chunk_data["audio_url"],
                                "sentence": sentence,
                                # 立绘按这句话的语境切换。TTS 韵律仍用整条消息的
                                # 统一情绪（见 chunk_opts），逐句分类只影响画面，
                                # 不动声音 —— 音频在此之前已经合成完毕。
                                "emotion": classify_emotion(sentence, "", None)
                                    or chunk_opts.get("emotion"),
                            },
                        }, cancel_event)
                    chunk_index += 1
                finally:
                    tts_queue.task_done()
        except asyncio.CancelledError:
            logger.debug("TTS worker received CancelledError, exiting worker loop.")
        finally:
            await self._put_with_cancel(event_queue, _SENTINEL, cancel_event)

    async def _llm_producer(self, run: _StreamRun) -> None:
        """Producer task streaming LLM tokens, extracting sentences, and feeding TTS and event queues."""
        cancel_event = run.cancel_event
        llm_pipe = run.llm_pipe
        text_pipe = run.text_pipe
        profiler = run.profiler
        event_queue = run.event_queue
        tts_queue = run.tts_queue
        t_start = run.t_start
        try:
            async for token in llm_pipe.stream_tokens():
                if cancel_event and cancel_event.is_set():
                    break
                delta_ch, completed_sentences, current_emo = text_pipe.feed_token(token)
                if delta_ch:
                    profiler.record_llm_first_token()
                    if run.ttft_ms == 0.0:
                        run.ttft_ms = (time.perf_counter() - t_start) * 1000.0
                    ok = await self._put_with_cancel(event_queue, {
                        "event": "text",
                        "data": {
                            "delta_chinese": delta_ch,
                            "emotion": current_emo,
                        }
                    }, cancel_event)
                    if not ok:
                        break
                for sentence in completed_sentences:
                    profiler.record_first_sentence()
                    ok = await self._put_with_cancel(tts_queue, sentence, cancel_event)
                    if not ok:
                        break

            full_ch, full_ja, rem_ch, rem_sentences, final_emo = text_pipe.finalize()
            run.final_result["chinese"] = full_ch
            run.final_result["japanese"] = full_ja
            if rem_ch and not (cancel_event and cancel_event.is_set()):
                profiler.record_llm_first_token()
                if run.ttft_ms == 0.0:
                    run.ttft_ms = (time.perf_counter() - t_start) * 1000.0
                await self._put_with_cancel(event_queue, {
                    "event": "text",
                    "data": {
                        "delta_chinese": rem_ch,
                        "emotion": final_emo,
                    }
                }, cancel_event)
            for sentence in rem_sentences:
                if cancel_event and cancel_event.is_set():
                    break
                profiler.record_first_sentence()
                await self._put_with_cancel(tts_queue, sentence, cancel_event)
        except Exception as exc:
            logger.error("LLM Producer error: %s", exc)
            try:
                p_ch, p_ja, _, _, _ = text_pipe.finalize()
                if p_ch and not run.final_result.get("chinese"):
                    run.final_result["chinese"] = p_ch
                if p_ja and not run.final_result.get("japanese"):
                    run.final_result["japanese"] = p_ja
            except Exception as finalize_err:
                logger.debug("Non-critical: error finalizing text_pipe on LLM error: %s", finalize_err)
            safe_err = sanitize_error_detail(exc)
            await self._put_with_cancel(
                event_queue,
                {"event": "error", "data": {"error": safe_err or "LLM generation failed"}},
                cancel_event,
            )
        finally:
            await self._put_with_cancel(tts_queue, None, cancel_event)
            await self._put_with_cancel(event_queue, _SENTINEL, cancel_event)

    async def _pump_events(
        self,
        run: _StreamRun,
    ) -> AsyncGenerator[dict[str, Any] | SseKeepAlive, None]:
        """Event pump loop: responsive wait on event queue, keep-alive frames, and cancel event."""
        sentinels_received = 0
        last_event_time = time.monotonic()
        keep_alive_interval = 5.0
        cancel_event = run.cancel_event
        event_queue = run.event_queue

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
                run.error_seen = True
                break

    async def _persist_partial_message(
        self,
        partial_ch: str | None,
        partial_ja: str | None,
        t_start: float,
    ) -> None:
        """Persists partial assistant response to the database in background."""
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

    async def _prune_orphaned_user_message(self) -> None:
        """Prunes orphaned user message when no meaningful assistant response was generated."""
        if self.user_msg is not None and getattr(self.user_msg, "id", None):
            try:
                async with get_db(self.db_path) as conn:
                    async with immediate_transaction(conn):
                        await crud.delete_message(conn, self.user_msg.id)
            except Exception as prune_err:
                logger.warning("Failed to prune orphaned user message %s: %s", self.user_msg.id, prune_err)

    async def _finalize_early_exit(
        self,
        run: _StreamRun,
        error_seen: bool,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Handles early termination due to pipeline errors or client cancellation."""
        cancel_event = run.cancel_event
        is_cancelled = bool(cancel_event and cancel_event.is_set())
        logger.info(
            "Stream chat ended early (error=%s, cancelled=%s) for session %s",
            error_seen,
            is_cancelled,
            self.session_id,
        )
        # Reap producer/worker before persisting so TTS synthesis stops
        # promptly and no task writes concurrently.
        for task in (run.producer_task, run.worker_task):
            if task is not None and not task.done():
                task.cancel()

        # Persist the partial reply the user already saw so the turn is
        # not silently dropped from history (the user message was saved).
        partial_ch = run.final_result.get("chinese") or run.parser.chinese_extracted
        partial_ja = run.final_result.get("japanese") or run.parser.japanese_extracted
        if _has_meaningful_text(partial_ch, partial_ja):
            self._spawn_bg(self._persist_partial_message(partial_ch, partial_ja, run.t_start))

        if is_cancelled:
            # Explicit truncated done so the client can distinguish
            # "user stopped" from a broken connection.
            yield {
                "event": "done",
                "data": {
                    "truncated": True,
                    "chinese": partial_ch or "",
                    "japanese": partial_ja or "",
                },
            }

    async def _concat_audio_chunks(self, audio_chunks: list[dict[str, Any]]) -> str:
        """Concatenates chunk WAVs into a master WAV file in a worker thread."""
        if not audio_chunks:
            return ""
        if len(audio_chunks) == 1:
            return audio_chunks[0]["audio_url"]

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
            return f"/audio/{full_filename}" if ok else audio_chunks[0]["audio_url"]
        except Exception as cat_err:
            logger.warning("Failed to concatenate audio chunks: %s", cat_err)
            return audio_chunks[0]["audio_url"]

    @staticmethod
    def _clean_audio_chunks(audio_chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Cleans local_path from audio_chunks before emitting to frontend."""
        return [
            {"index": c.get("index", i), "audio_url": c.get("audio_url", ""), "sentence": c.get("sentence", "")}
            for i, c in enumerate(audio_chunks)
        ]

    @staticmethod
    def _build_final_tts_params(parser: Any, ai_adaptive_voice: Any) -> dict[str, Any] | None:
        """Constructs final TTS parameter payload from parser attributes if available."""
        if parser.tts_speed is not None or parser.tts_temperature is not None or parser.tts_emotion is not None:
            return {
                "speed": parser.tts_speed,
                "temperature": parser.tts_temperature,
                "emotion": parser.tts_emotion,
                "adaptive_enabled": bool(ai_adaptive_voice),
            }
        return None

    async def _persist_turn_message(
        self,
        full_chinese: str,
        full_japanese: str,
        total_audio_url: str,
        total_latency: int,
    ) -> bool:
        """Persists the assistant message to the database, or prunes orphaned user message if empty."""
        has_meaningful_content = _has_meaningful_text(full_chinese, full_japanese)
        if has_meaningful_content:
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
        else:
            await self._prune_orphaned_user_message()
        return has_meaningful_content

    async def _update_turn_affection(
        self,
        full_chinese: str,
        final_emotion: str,
    ) -> tuple[dict[str, Any], str]:
        """Updates character affection state machine based on user prompt and assistant response."""
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
        return affection_res, final_emotion

    async def _finalize_success(
        self,
        run: _StreamRun,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Performs success-path finalization: WAV concat, metrics, DB persist, affection, and done event."""
        parser = run.parser
        audio_chunks = run.audio_chunks
        t_start = run.t_start
        cancel_event = run.cancel_event

        full_chinese = run.final_result.get("chinese") or parser.chinese_extracted
        full_japanese = run.final_result.get("japanese") or parser.japanese_extracted
        final_emotion = classify_emotion(full_chinese, full_japanese, parser.emotion_extracted)

        # Concatenate chunks into a master WAV in a worker thread.
        total_audio_url = await self._concat_audio_chunks(audio_chunks)

        total_latency = int((time.perf_counter() - t_start) * 1000)
        ttft_ms = run.ttft_ms if run.ttft_ms != 0.0 else float(total_latency)
        tts_first_chunk_ms = run.tts_first_chunk_ms if run.tts_first_chunk_ms != 0.0 else float(total_latency)

        metric_record = await self.metrics_collector.record_chat_turn(
            session_id=self.session_id,
            channel="web",
            provider_id=self.actual_provider_id,
            model_name=self.model_name,
            messages=self.messages,
            chinese=full_chinese,
            japanese=full_japanese,
            ttft_ms=ttft_ms,
            tts_first_chunk_ms=tts_first_chunk_ms,
            total_latency_ms=float(total_latency),
            tts_cached_chunks=run.tts_cached_chunks,
            tts_generated_chunks=run.tts_generated_chunks,
        )

        has_meaningful_content = await self._persist_turn_message(
            full_chinese=full_chinese,
            full_japanese=full_japanese,
            total_audio_url=total_audio_url,
            total_latency=total_latency,
        )

        clean_chunks = self._clean_audio_chunks(audio_chunks)
        affection_res, final_emotion = await self._update_turn_affection(full_chinese, final_emotion)
        final_tts_params = self._build_final_tts_params(parser, self.ai_adaptive_voice)

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
                "telemetry": run.profiler.to_dict(),
            }
        }

    async def _teardown(self, run: _StreamRun) -> None:
        """Finally-block teardown: cancels scheduled generation, reaps background tasks, ensures DB persistence."""
        try:
            get_tts_scheduler().cancel_generation(run.stream_gen_id)
        except Exception as cancel_err:
            logger.debug("Non-critical: error cancelling generation in _teardown: %s", cancel_err)

        if run.cancel_monitor and not run.cancel_monitor.done():
            run.cancel_monitor.cancel()
            try:
                await run.cancel_monitor
            except (asyncio.CancelledError, Exception):
                pass

        # Reap producer/worker no matter how we exited (normal end, error,
        # client disconnect, or cancellation). This prevents orphan tasks
        # from holding the TTS inference lock forever.
        for task in (run.producer_task, run.worker_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (run.producer_task, run.worker_task):
            if task is None:
                continue
            try:
                if run.cancel_event and run.cancel_event.is_set():
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
            partial_ch = run.final_result.get("chinese") or run.parser.chinese_extracted
            partial_ja = run.final_result.get("japanese") or run.parser.japanese_extracted
            if _has_meaningful_text(partial_ch, partial_ja):
                try:
                    async with get_db(self.db_path) as conn:
                        async with immediate_transaction(conn):
                            await crud.add_message(conn, MessageCreate(
                                session_id=self.session_id,
                                role="assistant",
                                content_chinese=partial_ch,
                                content_japanese=partial_ja,
                                audio_url="",
                                latency_ms=int((time.perf_counter() - run.t_start) * 1000),
                            ))
                        self.persisted_assistant = True
                except Exception as persist_err:
                    logger.warning("Failed to persist partial assistant message in finally: %s", persist_err)
            else:
                await self._prune_orphaned_user_message()

        run.profiler.print_waterfall()

    async def stream(self) -> AsyncGenerator[dict[str, Any], None]:
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

        run = self._init_stream_run()

        try:
            self._start_subtasks(run)

            async for event in self._pump_events(run):
                yield event

            if run.error_seen or (run.cancel_event and run.cancel_event.is_set()):
                async for event in self._finalize_early_exit(run, error_seen=run.error_seen):
                    yield event
                return

            # Ensure both tasks are fully finished before touching shared state.
            await asyncio.gather(run.producer_task, run.worker_task, return_exceptions=True)

            if run.cancel_event and run.cancel_event.is_set():
                async for event in self._finalize_early_exit(run, error_seen=False):
                    yield event
                return

            async for event in self._finalize_success(run):
                yield event

        except Exception as exc:
            logger.error("Error in stream_chat pipeline: %s", exc, exc_info=True)
            safe_err = sanitize_error_detail(exc)
            yield {
                "event": "error",
                "data": {"error": safe_err or "Chat service stream pipeline error"}
            }
        finally:
            await self._teardown(run)


__all__ = [
    "StreamCoordinator",
    "SseKeepAlive",
    "_SENTINEL",
    "_CANCEL_SENTINEL",
]
