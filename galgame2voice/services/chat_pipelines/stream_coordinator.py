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
from anyio import CancelScope

from galgame2voice.adapters.base import configured_reasoning_effort
from galgame2voice.database import crud
from galgame2voice.database.models import MessageCreate
from galgame2voice.database.session import get_db, immediate_transaction
from galgame2voice.services.affection_service import AffectionService
from galgame2voice.services.emotion_classifier import classify_emotion, detect_emotion_cue
from galgame2voice.services.metrics_collector import MetricsCollector
from galgame2voice.services.tts_scheduler import get_tts_scheduler
from galgame2voice.services.tts_service import TtsService
from galgame2voice.utils.audio_concat import concat_wav_files
from galgame2voice.utils.error_diagnostics import (
    EMPTY_REPLY_DEFAULT,
    EMPTY_REPLY_STOPPED_ON_LENGTH,
    EmptyReplyError,
    chat_error_payload,
)
from galgame2voice.utils.logger import sanitize_error_detail
from galgame2voice.utils.profiler import ChatTurnProfiler
from galgame2voice.utils.text_splitter import split_japanese_sentences

from .acting_menu import sanitize_act
from .line_builder import LineAligner
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
        self.shutdown_event = asyncio.Event()

        self.text_pipe: TextSegmentationPipeline = text_pipe
        self.parser = text_pipe.parser
        self.tts_pipe: TtsStreamPipeline = tts_pipe
        self.llm_pipe: LlmStreamPipeline = llm_pipe

        self.tts_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=200)

        self.producer_task: asyncio.Task | None = None
        self.worker_task: asyncio.Task | None = None
        self.cancel_monitor: asyncio.Task | None = None
        self.cancel_requested: set[asyncio.Task] = set()

        self.final_result: dict[str, Any] = {}
        self.audio_chunks: list[dict[str, Any]] = []
        self.aligner: LineAligner = LineAligner()
        self.lines: list[dict[str, Any]] = []
        self.announced_lines: list[dict[str, Any]] = []
        self.line_audio_states: dict[int, dict[str, Any]] = {}
        self.line_audio_lock = asyncio.Lock()
        self.next_line_audio_index: int = 0
        self.ja_chunk_cursor: int = 0
        self.chunk_counter: int = 0
        self.chunk_s_map: dict[int, int] = {}
        self.chunk_line_map: dict[int, int | None] = {}
        self.chunk_results: dict[int, dict[str, Any]] = {}
        self.persisted_lines: list[dict[str, Any]] | None = None
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
        reply_format: str = "old",
        reasoning_effort: str | None = None,
        character_name: str | None = None,
        acting_menu: dict | None = None,
    ) -> None:
        self.adapter = adapter
        self.reply_format = reply_format
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
        self.reasoning_effort = reasoning_effort
        if self.reasoning_effort is None:
            self.reasoning_effort = configured_reasoning_effort(self.adapter)

        self.persisted_assistant: bool = False
        self.acting_menu = acting_menu if acting_menu is not None else getattr(messages, "menu", None)
        self.character_name = (
            character_name
            or getattr(self.active_prof, "name", None)
            or "Character"
        )

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
            reasoning_effort=self.reasoning_effort,
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
        self._request_pipeline_stop(run)
        try:
            run.tts_queue.put_nowait(None)
        except (asyncio.QueueFull, Exception):
            pass
        try:
            run.event_queue.put_nowait(_CANCEL_SENTINEL)
        except (asyncio.QueueFull, Exception):
            pass

    @staticmethod
    def _request_pipeline_stop(run: _StreamRun) -> None:
        """Request cancellation once, so a later stop cannot interrupt finally cleanup."""
        run.shutdown_event.set()
        for task in (run.producer_task, run.worker_task):
            if task is not None and not task.done() and task not in run.cancel_requested:
                run.cancel_requested.add(task)
                task.cancel()

    async def _put_with_cancel(
        self,
        queue: asyncio.Queue,
        item: Any,
        cancel_event: asyncio.Event | None = None,
        shutdown_event: asyncio.Event | None = None,
    ) -> bool:
        """Puts an item into a bounded queue with ultra-low latency while remaining responsive to cancel_event."""
        ce = cancel_event if cancel_event is not None else self.cancel_event
        if (ce and ce.is_set()) or (shutdown_event and shutdown_event.is_set()):
            return False
        try:
            # Fast path: non-blocking enqueue without coroutine suspension
            queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            # Queue at capacity; fall back to short polling loop responsive to cancel_event
            pass
        while True:
            if (ce and ce.is_set()) or (shutdown_event and shutdown_event.is_set()):
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
                item = await tts_queue.get()
                try:
                    if item is None:
                        break
                    line_item = item if isinstance(item, dict) else None
                    if line_item is not None:
                        sentence = line_item["ja"]
                    else:
                        sentence, s_idx = item
                    self._attribute_chunk(run, chunk_index)

                    if not tts_pipe.is_vocal_sentence(sentence):
                        logger.debug("Skipping non-vocal sentence chunk: '%s'", sentence)
                        self._settle_chunk(run, chunk_index)
                        await self._try_emit_line_audio(run)
                        chunk_index += 1
                        continue

                    if cancel_event and cancel_event.is_set():
                        break

                    chunk_opts = text_pipe.get_dynamic_tts_options(
                        base_options=self.tts_options,
                        adaptive_enabled=bool(self.ai_adaptive_voice),
                        sentence_text=sentence,
                    )
                    if line_item is not None:
                        chunk_opts["emotion"] = line_item["emotion"]
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
                    # Chinese may have completed while synthesis was awaiting.
                    L = self._attribute_chunk(run, chunk_index)
                    if err_str:
                        await self._put_with_cancel(event_queue, {
                            "event": "audio_chunk_error",
                            "data": {
                                "index": chunk_index,
                                "sentence": sentence,
                                "error": err_str,
                                **({"line": L} if line_item is not None else {}),
                            },
                        }, cancel_event, run.shutdown_event)
                    elif chunk_data:
                        if run.tts_first_chunk_ms == 0.0:
                            run.tts_first_chunk_ms = (time.perf_counter() - run.t_start) * 1000.0
                        if chunk_data["is_cached"]:
                            run.tts_cached_chunks += 1
                        else:
                            run.tts_generated_chunks += 1
                        run.audio_chunks.append(chunk_data)
                        emotion = line_item["emotion"] if line_item is not None else (
                            detect_emotion_cue(sentence) or chunk_opts.get("emotion") or text_pipe.current_emotion()
                        )
                        if L is not None:
                            chunk_data["line"] = L
                        payload: dict[str, Any] = {
                            "index": chunk_index,
                            "audio_url": chunk_data["audio_url"],
                            "sentence": sentence,
                            "emotion": emotion,
                        }
                        if L is not None:
                            payload["line"] = L
                        await self._put_with_cancel(event_queue, {
                            "event": "audio_chunk",
                            "data": payload,
                        }, cancel_event, run.shutdown_event)
                    # Keep the chunk pending through event-queue backpressure:
                    # a producer must not enqueue line_audio ahead of audio_chunk.
                    self._settle_chunk(
                        run, chunk_index, chunk=chunk_data,
                        error=err_str or ("TTS synthesis returned no audio" if not chunk_data else None),
                    )
                    await self._try_emit_line_audio(run)
                    chunk_index += 1
                finally:
                    tts_queue.task_done()
        except asyncio.CancelledError:
            logger.debug("TTS worker received CancelledError, exiting worker loop.")
        except Exception as exc:
            logger.exception("TTS worker failed")
            await self._put_with_cancel(event_queue, {
                "event": "error", "data": {"error": sanitize_error_detail(exc) or "TTS worker failed"},
            }, cancel_event, run.shutdown_event)
        finally:
            await self._put_with_cancel(event_queue, _SENTINEL, cancel_event, run.shutdown_event)

    def _attribute_chunk(self, run: _StreamRun, chunk_index: int) -> int | None:
        """Attach an independent chunk result once Chinese establishes its line."""
        line = run.chunk_line_map.get(chunk_index)
        if line is not None or not run.aligner.has_chinese:
            return line
        m = len(run.aligner.chinese_sentences or [])
        sentence_index = run.chunk_s_map[chunk_index]
        line = sentence_index if m == 0 else min(sentence_index, m - 1)
        run.chunk_line_map[chunk_index] = line
        result = run.chunk_results[chunk_index]
        state = self._line_audio_state(run, line)
        if not result["settled"]:
            state["pending"] += 1
        else:
            self._attach_chunk_result(run, chunk_index, line)
        return line

    def _attach_chunk_result(self, run: _StreamRun, chunk_index: int, line: int) -> None:
        result = run.chunk_results[chunk_index]
        state = self._line_audio_state(run, line)
        if result["error"]:
            state["errors"].append(result["error"])
        elif result["chunk"]:
            chunk = result["chunk"]
            state["chunks"].append({
                "index": chunk_index, "line": line,
                "audio_url": chunk["audio_url"], "sentence": chunk["sentence"],
            })

    def _settle_chunk(
        self, run: _StreamRun, chunk_index: int, *,
        chunk: dict[str, Any] | None = None, error: str | None = None,
    ) -> int | None:
        result = run.chunk_results[chunk_index]
        result.update(settled=True, chunk=chunk, error=error)
        line = run.chunk_line_map.get(chunk_index)
        if line is None:
            return self._attribute_chunk(run, chunk_index)
        self._line_audio_state(run, line)["pending"] -= 1
        self._attach_chunk_result(run, chunk_index, line)
        return line

    @staticmethod
    def _line_audio_state(run: _StreamRun, index: int) -> dict[str, Any]:
        return run.line_audio_states.setdefault(index, {
            "line": index,
            "sealed": False,
            "pending": 0,
            "chunks": [],
            "errors": [],
            "emitted": False,
            "status": None,
        })

    async def _try_emit_line_audio(self, run: _StreamRun) -> None:
        # Both producer and worker can settle a line. Hold the lock across the
        # queue await so neither task can advance the other's shared cursor.
        async with run.line_audio_lock:
            await self._emit_line_audio_locked(run)

    async def _emit_line_audio_locked(self, run: _StreamRun) -> None:
        while run.next_line_audio_index < len(run.lines):
            idx = run.next_line_audio_index
            state = self._line_audio_state(run, idx)
            if not state["sealed"] or state["pending"] > 0:
                break
            if not state["emitted"]:
                status = "failed" if state["errors"] else ("ready" if state["chunks"] else "skipped")
                data: dict[str, Any] = {
                    "line": idx,
                    "status": status,
                    "chunks": list(state["chunks"]),
                }
                if status == "failed":
                    data["error"] = "; ".join(dict.fromkeys(state["errors"]))
                if not await self._put_with_cancel(run.event_queue, {
                    "event": "line_audio",
                    "data": data,
                }, run.cancel_event, run.shutdown_event):
                    return
                state["emitted"] = True
                state["status"] = status
            run.next_line_audio_index += 1

    async def _publish_line(self, run: _StreamRun, line: dict[str, Any]) -> None:
        if "act" in line:
            line["act"] = sanitize_act(
                line["act"],
                self.acting_menu,
                line_index=line.get("index", 0),
                character=self.character_name,
            )
        run.lines.append(line)
        await self._put_with_cancel(
            run.event_queue, {"event": "line", "data": line}, run.cancel_event, run.shutdown_event,
        )

    @staticmethod
    def _find_containing_sentence_idx(japanese_text: str, chunk: str, cursor: int) -> tuple[int, int]:
        strict_sentences = split_japanese_sentences(japanese_text, is_first_chunk=False)
        if not strict_sentences:
            raise ValueError("Cannot attribute a chunk without a Japanese sentence offset")
        start_pos = japanese_text.find(chunk, cursor)
        if start_pos == -1:
            raise ValueError("TTS chunk has no matching Japanese text offset")
        new_cursor = start_pos + len(chunk)

        cur = 0
        for idx, s in enumerate(strict_sentences):
            pos = japanese_text.find(s, cur)
            if pos == -1:
                raise ValueError("Japanese sentence has no matching text offset")
            s_end = pos + len(s)
            if pos <= start_pos < s_end:
                if new_cursor > s_end:
                    raise ValueError("TTS chunk spans Japanese sentence offsets")
                return idx, new_cursor
            cur = s_end

        raise ValueError("TTS chunk offset lies outside Japanese sentences")

    async def _check_freeze_lines(self, run: _StreamRun, *, final: bool = False) -> None:
        if run.parser.line_scanner.format == "lines":
            await self._queue_lines(run)
            return
        if not run.aligner.has_chinese and (run.parser.chinese_complete or final):
            run.aligner.set_chinese(run.parser.chinese_extracted)
            for c_idx in run.chunk_results:
                self._attribute_chunk(run, c_idx)

        if not run.aligner.has_chinese:
            return

        # An evolving whole-reply inference is not a stable line fallback.
        # Without declared/lead metadata, the aligner waits for final inference.
        run.aligner.default_emotion = run.parser.emotion_extracted or None
        is_final_step = final or (run.parser.japanese_complete and bool(run.aligner.default_emotion))
        new_lines = run.aligner.feed_japanese(run.parser.japanese_extracted, final=is_final_step)
        for line in new_lines:
            await self._publish_line(run, line)

        m = len(run.aligner.chinese_sentences or [])
        for line in run.lines:
            idx = line["index"]
            if is_final_step or m == 0 or idx < m - 1:
                self._line_audio_state(run, idx)["sealed"] = True

        await self._try_emit_line_audio(run)

    async def _queue_lines(self, run: _StreamRun) -> None:
        """Publish completed lines before queueing one synthesis job per line."""
        for line in run.parser.normalized_lines(start_index=len(run.lines)):
            index = line["index"]
            state = self._line_audio_state(run, index)
            # Reserve before publishing: the worker may finish a previous line
            # while the producer is waiting for event-queue space.
            state["pending"] = 1
            state["sealed"] = True
            c_idx = run.chunk_counter
            run.chunk_counter += 1
            run.chunk_line_map[c_idx] = index
            run.chunk_results[c_idx] = {"settled": False, "chunk": None, "error": None}
            await self._publish_line(run, line)
            run.profiler.record_first_sentence()
            if not await self._put_with_cancel(run.tts_queue, {
                "line": index, "ja": line["ja"], "emotion": line["emotion"],
            }, run.cancel_event, run.shutdown_event):
                return

    async def _queue_sentences(self, run: _StreamRun, chunks: list[str], *, final: bool = False) -> None:
        """Attribute sentence chunks with real offsets and feed TTS and line aligner."""
        if run.parser.line_scanner.format == "lines":
            await self._queue_lines(run)
            return
        japanese_text = run.parser.japanese_extracted
        for chunk in chunks:
            if not chunk:
                continue
            run.profiler.record_first_sentence()
            try:
                s_idx, run.ja_chunk_cursor = self._find_containing_sentence_idx(
                    japanese_text, chunk, run.ja_chunk_cursor
                )
            except ValueError as exc:
                # Finalize can rewrite the Japanese text (e.g. strip a bracketed
                # emotion), moving offsets. Placement only decides which line
                # owns the audio, so it must never fail the turn.
                s_idx = run.chunk_s_map.get(run.chunk_counter - 1, 0)
                logger.warning("TTS chunk kept on sentence %d: %s", s_idx, exc)
            c_idx = run.chunk_counter
            run.chunk_counter += 1
            run.chunk_s_map[c_idx] = s_idx
            run.chunk_line_map[c_idx] = None
            run.chunk_results[c_idx] = {"settled": False, "chunk": None, "error": None}
            self._attribute_chunk(run, c_idx)

            if not await self._put_with_cancel(run.tts_queue, (chunk, s_idx), run.cancel_event, run.shutdown_event):
                return

        await self._check_freeze_lines(run, final=final)

    def _snapshot_lines_with_audio(
        self,
        run: _StreamRun,
        *,
        mark_unfinished_failed: bool = False,
    ) -> list[dict[str, Any]]:
        result = []
        for line in run.announced_lines:
            line_copy = dict(line)
            idx = line["index"]
            state = run.line_audio_states.get(idx)
            if state is not None:
                if state["emitted"]:
                    status = state["status"]
                elif mark_unfinished_failed:
                    status = "failed"
                else:
                    status = "failed" if state["errors"] else ("ready" if state["chunks"] else "skipped")
                chunks = list(state.get("chunks", []))
            else:
                status = "failed" if mark_unfinished_failed else "skipped"
                chunks = []
            line_copy["audio"] = {
                "status": status,
                "chunks": chunks,
            }
            result.append(line_copy)
        return result

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
                    }, cancel_event, run.shutdown_event)
                    if not ok:
                        break
                if completed_sentences:
                    await self._queue_sentences(run, completed_sentences)
                else:
                    await self._check_freeze_lines(run, final=False)

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
                }, cancel_event, run.shutdown_event)
            if not (cancel_event and cancel_event.is_set()):
                await self._queue_sentences(run, rem_sentences, final=True)
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
            # Provider and connection failures reach the user as a Chinese next step
            # (missing key, private endpoint, quota...) instead of the raw upstream text.
            payload = chat_error_payload(
                exc,
                provider_id=self.actual_provider_id,
                api_key_configured=bool(str(getattr(self.adapter, "api_key", "") or "").strip()),
            )
            await self._put_with_cancel(event_queue, {"event": "error", "data": payload}, cancel_event, run.shutdown_event)
        finally:
            await self._put_with_cancel(tts_queue, None, cancel_event, run.shutdown_event)
            await self._put_with_cancel(event_queue, _SENTINEL, cancel_event, run.shutdown_event)

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

                try:
                    done, _ = await asyncio.wait(wait_set, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    # A disconnected consumer can cancel us while waiting. Neither
                    # queue.get nor Event.wait may outlive the stream in that case.
                    for waiter in wait_set:
                        if not waiter.done():
                            waiter.cancel()
                    await asyncio.gather(*wait_set, return_exceptions=True)

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
        lines: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist the interrupted turn once, before its stream teardown finishes."""
        try:
            async with get_db(self.db_path) as conn:
                await crud.add_message(conn, MessageCreate(
                    session_id=self.session_id,
                    role="assistant",
                    content_chinese=partial_ch,
                    content_japanese=partial_ja,
                    lines=lines,
                    audio_url="",
                    latency_ms=int((time.perf_counter() - t_start) * 1000),
                ))
            self.persisted_assistant = True
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
        # Request stop immediately; teardown reaps both tasks before persistence.
        self._request_pipeline_stop(run)

        snapshot_lines = self._snapshot_lines_with_audio(run, mark_unfinished_failed=True)
        run.persisted_lines = snapshot_lines or None

        if is_cancelled:
            partial_ch = run.final_result.get("chinese") or run.parser.chinese_extracted
            partial_ja = run.final_result.get("japanese") or run.parser.japanese_extracted
            yield {
                "event": "done",
                "data": {
                    "truncated": True,
                    "chinese": partial_ch or "",
                    "japanese": partial_ja or "",
                    "lines": snapshot_lines,
                    "reply_format": run.parser.line_scanner.format,
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
    def _clean_audio_chunks(
        audio_chunks: list[dict[str, Any]], *, include_line: bool = False,
    ) -> list[dict[str, Any]]:
        """Cleans local_path from audio_chunks before emitting to frontend."""
        return [
            {
                "index": c.get("index", i), "audio_url": c.get("audio_url", ""), "sentence": c.get("sentence", ""),
                **({"line": c["line"]} if include_line and "line" in c else {}),
            }
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
        lines: list[dict[str, Any]] | None = None,
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
                        lines=lines,
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

        final_lines = self._snapshot_lines_with_audio(run, mark_unfinished_failed=False)
        run.persisted_lines = final_lines

        has_meaningful_content = (
            _has_meaningful_text(full_chinese, full_japanese)
            or bool(final_lines)
        )

        if not has_meaningful_content and not bool(cancel_event and cancel_event.is_set()):
            stopped_on_length = bool(
                getattr(parser, "lines_truncated", False)
                or getattr(self.adapter, "last_finish_reason", None) in ("length", "MAX_TOKENS", "max_tokens")
            )
            text = EMPTY_REPLY_STOPPED_ON_LENGTH if stopped_on_length else EMPTY_REPLY_DEFAULT
            await self._prune_orphaned_user_message()
            run.error_seen = True
            yield {
                "event": "error",
                "data": chat_error_payload(EmptyReplyError(text, stopped_on_length=stopped_on_length)),
            }
            return

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
            lines=final_lines,
        )

        clean_chunks = self._clean_audio_chunks(audio_chunks, include_line=parser.line_scanner.format == "lines")
        if has_meaningful_content:
            affection_res, final_emotion = await self._update_turn_affection(full_chinese, final_emotion)
        else:
            # The empty reply withdrew the user's message, so the turn earns nothing and
            # sending it again cannot count twice.
            affection_res = self._affection_fallback(final_emotion)
        final_tts_params = self._build_final_tts_params(parser, self.ai_adaptive_voice)

        # Emit final done event
        yield {
            "event": "done",
            "data": {
                "truncated": bool((cancel_event and cancel_event.is_set()) or not has_meaningful_content or parser.lines_truncated),
                "chinese": full_chinese,
                "japanese": full_japanese,
                "lines": final_lines,
                "reply_format": parser.line_scanner.format,
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
        # A disconnected request's AnyIO scope cancels every unshielded cleanup
        # await, including history writes. This scope contains no yielded events.
        with CancelScope(shield=True):
            await self._finish_teardown(run)

    async def _finish_teardown(self, run: _StreamRun) -> None:
        """Release owned pipeline tasks and settle this turn's database state."""
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
        self._request_pipeline_stop(run)
        for task in (run.producer_task, run.worker_task):
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
        if not self.persisted_assistant and self.user_msg is not None:
            partial_ch = run.final_result.get("chinese") or run.parser.chinese_extracted
            partial_ja = run.final_result.get("japanese") or run.parser.japanese_extracted
            if _has_meaningful_text(partial_ch, partial_ja):
                lines_to_save = getattr(run, "persisted_lines", None)
                if lines_to_save is None and run.announced_lines:
                    lines_to_save = self._snapshot_lines_with_audio(run, mark_unfinished_failed=True)
                await self._persist_partial_message(partial_ch, partial_ja, run.t_start, lines=lines_to_save)
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
                if run.cancel_event and run.cancel_event.is_set():
                    break
                if event.get("event") == "line":
                    run.announced_lines.append(event["data"])
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
            if run.error_seen or (run.cancel_event and run.cancel_event.is_set()):
                return
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
