"""
TTS Scheduler and Single-Flight Coordination Module for galgame2voice.

Replaces naive global mutex contention with:
  1. Priority-aware scheduling (ACTIVE_PLAYING > NEXT_PREFETCH > BACKGROUND_PREHEAT).
  2. Generation-scoped stale task cancellation (drops old sentence syntheses when user interrupts).
  3. Single-Flight request coalescing to eliminate cache stampedes and duplicate GPU inference.
"""

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, AsyncGenerator, Callable, Coroutine, Dict, Optional, Set, TypeVar

logger = logging.getLogger("galgame2voice.services.tts_scheduler")

T = TypeVar("T")


class _StreamSentinel:
    """Sentinel indicating end of stream."""
    pass


class _StreamCancelled:
    """Sentinel indicating stream was cancelled."""
    pass


@dataclass
class _StreamError:
    exc: BaseException


_STREAM_EOF = _StreamSentinel()
_STREAM_CANCELLED = _StreamCancelled()


async def _safe_put_chunk(
    queue: asyncio.Queue,
    item: Any,
    stop_event: asyncio.Event,
) -> bool:
    """
    Puts item into bounded queue with backpressure.
    Returns False immediately if stop_event is set or if cancelled.
    """
    if stop_event.is_set():
        return False

    try:
        queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        pass

    put_task = asyncio.create_task(queue.put(item))
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        [put_task, stop_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for p in pending:
        p.cancel()
        try:
            await p
        except asyncio.CancelledError:
            pass
    return put_task in done and not stop_event.is_set()


class TtsPriority(IntEnum):
    HIGH = 0      # Active playing sentence (critical user-facing path)
    NORMAL = 1    # Next sentence prefetch
    LOW = 2       # Background character/emotion preheat

    @classmethod
    def from_options(
        cls,
        options: Optional[Dict[str, Any]] = None,
        default: Optional["TtsPriority"] = None,
    ) -> "TtsPriority":
        """
        Parses TTS scheduling priority from options dict (under '_priority' key).
        Safely falls back to default (NORMAL) on missing, invalid, or malformed values.
        """
        fallback = default if default is not None else cls.NORMAL
        if not options:
            return fallback
        raw_prio = options.get("_priority", fallback)
        try:
            return cls(int(raw_prio))
        except (ValueError, TypeError):
            return fallback


class SingleFlightCoordinator:
    """
    Coalesces concurrent operations on identical keys into a single in-flight execution.
    Subsequent callers await the single leader's result instead of duplicating GPU inference.
    """

    def __init__(self):
        self._flights: Dict[str, asyncio.Future] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _ensure_lock(self) -> asyncio.Lock:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if self._lock is None or self._loop is None or self._loop is not current_loop or self._loop.is_closed():
            self._loop = current_loop
            self._lock = asyncio.Lock()
            self._flights.clear()
        return self._lock

    async def execute(self, key: str, coro_fn: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """Executes coro_fn if key is not in flight; otherwise waits for existing flight."""
        if not key:
            return await coro_fn()

        lock = self._ensure_lock()
        async with lock:
            if key in self._flights:
                fut = self._flights[key]
                is_leader = False
            else:
                loop = asyncio.get_running_loop()
                fut = loop.create_future()
                self._flights[key] = fut
                is_leader = True

        if not is_leader:
            logger.debug("SingleFlight: joined existing in-flight task for key %s", key[:16])
            return await asyncio.shield(fut)

        try:
            result = await coro_fn()
            if not fut.done():
                fut.set_result(result)
            return result
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            async with lock:
                self._flights.pop(key, None)


@dataclass(order=True)
class ScheduledTtsTask:
    priority: int
    seq: int
    task_id: str = field(compare=False)
    generation_id: Optional[str] = field(compare=False)
    coro_fn: Optional[Callable[[], Coroutine[Any, Any, Any]]] = field(compare=False, default=None)
    future: asyncio.Future = field(compare=False, default=None)
    created_at: float = field(compare=False, default_factory=time.monotonic)
    cancelled: bool = field(compare=False, default=False)
    is_stream: bool = field(compare=False, default=False)
    stream_fn: Optional[Callable[[], Any]] = field(compare=False, default=None)
    stream_queue: Optional[asyncio.Queue] = field(compare=False, default=None)
    stop_event: Optional[asyncio.Event] = field(compare=False, default=None)
    stream_cancelled_counted: bool = field(compare=False, default=False)


class TtsScheduler:
    """
    Priority-driven async queue scheduler for TTS tasks.
    Serializes execution against the GPU while respecting priority tiers
    and enabling proactive elimination of stale tasks on dialogue interruptions.
    """

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.PriorityQueue[ScheduledTtsTask]] = None
        self._seq: int = 0
        self._lock: Optional[asyncio.Lock] = None
        self._sync_lock = threading.Lock()
        self._tasks_by_id: Dict[str, ScheduledTtsTask] = {}
        self._tasks_by_gen: Dict[str, Set[str]] = {}
        self._single_flight = SingleFlightCoordinator()
        self._worker_task: Optional[asyncio.Task] = None
        self._running: bool = False

        # Metrics telemetry
        self._active_streams: int = 0
        self._cancelled_streams: int = 0
        self._total_queue_wait_time: float = 0.0
        self._queue_wait_samples: int = 0
        self._last_queue_wait_time: float = 0.0

        self._init_loop_primitives()

    def _init_loop_primitives(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._queue = asyncio.PriorityQueue()
        self._lock = asyncio.Lock()
        self._worker_task = None
        self._running = False
        with self._sync_lock:
            self._tasks_by_id.clear()
            self._tasks_by_gen.clear()

    @property
    def single_flight(self) -> SingleFlightCoordinator:
        return self._single_flight

    @property
    def active_streams(self) -> int:
        return self._active_streams

    @property
    def active_stream_counts(self) -> int:
        return self._active_streams

    @property
    def cancelled_streams(self) -> int:
        return self._cancelled_streams

    @property
    def queue_wait_time(self) -> float:
        return self._last_queue_wait_time

    @property
    def last_queue_wait_time(self) -> float:
        return self._last_queue_wait_time

    @property
    def avg_queue_wait_time(self) -> float:
        return (self._total_queue_wait_time / self._queue_wait_samples) if self._queue_wait_samples > 0 else 0.0

    @property
    def total_queue_wait_time(self) -> float:
        return self._total_queue_wait_time

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "queue_depth": self.get_queue_depth(),
            "active_streams": self._active_streams,
            "cancelled_streams": self._cancelled_streams,
            "last_queue_wait_time": self._last_queue_wait_time,
            "avg_queue_wait_time": self.avg_queue_wait_time,
            "total_queue_wait_time": self._total_queue_wait_time,
        }

    def reset_metrics(self) -> None:
        self._active_streams = 0
        self._cancelled_streams = 0
        self._total_queue_wait_time = 0.0
        self._queue_wait_samples = 0
        self._last_queue_wait_time = 0.0

    def _ensure_worker(self) -> None:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        if self._loop is None or self._loop is not current_loop or self._loop.is_closed():
            self._init_loop_primitives()

        if self._worker_task is None or self._worker_task.done():
            self._running = True
            self._worker_task = asyncio.create_task(self._dispatch_loop())

    async def schedule(
        self,
        coro_fn: Callable[[], Coroutine[Any, Any, T]],
        priority: TtsPriority = TtsPriority.NORMAL,
        generation_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> T:
        """
        Schedules a TTS coroutine with the given priority and generation tracking.
        Awaits and returns the result once executed.
        """
        self._ensure_worker()
        tid = task_id or f"tts_{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        assert self._lock is not None and self._queue is not None
        async with self._lock:
            self._seq += 1
            task = ScheduledTtsTask(
                priority=int(priority),
                seq=self._seq,
                task_id=tid,
                generation_id=generation_id,
                coro_fn=coro_fn,
                future=fut,
            )
            self._tasks_by_id[tid] = task
            if generation_id:
                self._tasks_by_gen.setdefault(generation_id, set()).add(tid)
            await self._queue.put(task)

        return await fut

    async def schedule_stream(
        self,
        stream_fn: Callable[[], AsyncGenerator[bytes, None]],
        priority: TtsPriority = TtsPriority.NORMAL,
        generation_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> AsyncGenerator[bytes, None]:
        """
        Enqueues an asynchronous streaming TTS generator in the priority queue.
        When dispatched by the single GPU worker, chunks stream through a bounded
        buffer (asyncio.Queue) to the caller.
        """
        self._ensure_worker()
        tid = task_id or f"stream_{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        buffer_queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        stop_event = asyncio.Event()

        task = ScheduledTtsTask(
            priority=int(priority),
            seq=0,
            task_id=tid,
            generation_id=generation_id,
            future=fut,
            is_stream=True,
            stream_fn=stream_fn,
            stream_queue=buffer_queue,
            stop_event=stop_event,
        )

        async def _run_stream() -> None:
            if task.cancelled or task.future.cancelled() or stop_event.is_set():
                if not task.stream_cancelled_counted:
                    task.stream_cancelled_counted = True
                    self._cancelled_streams += 1
                return

            self._active_streams += 1
            stream_gen = None
            try:
                raw_gen = stream_fn()
                if asyncio.iscoroutine(raw_gen):
                    stream_gen = await raw_gen
                else:
                    stream_gen = raw_gen

                async for chunk in stream_gen:
                    if task.cancelled or task.future.cancelled() or stop_event.is_set():
                        if not task.stream_cancelled_counted:
                            task.stream_cancelled_counted = True
                            self._cancelled_streams += 1
                        break
                    put_ok = await _safe_put_chunk(buffer_queue, chunk, stop_event)
                    if not put_ok:
                        if not task.stream_cancelled_counted:
                            task.stream_cancelled_counted = True
                            self._cancelled_streams += 1
                        break
            except BaseException as exc:
                if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
                    if not task.stream_cancelled_counted:
                        task.stream_cancelled_counted = True
                        self._cancelled_streams += 1
                else:
                    await _safe_put_chunk(buffer_queue, _StreamError(exc), stop_event)
                raise
            finally:
                self._active_streams = max(0, self._active_streams - 1)
                if stream_gen is not None:
                    try:
                        await stream_gen.aclose()
                    except Exception:
                        pass
                await _safe_put_chunk(buffer_queue, _STREAM_EOF, stop_event)

        task.coro_fn = _run_stream

        assert self._lock is not None and self._queue is not None
        async with self._lock:
            self._seq += 1
            task.seq = self._seq
            self._tasks_by_id[tid] = task
            if generation_id:
                self._tasks_by_gen.setdefault(generation_id, set()).add(tid)
            await self._queue.put(task)

        is_normal_eof = False
        try:
            while True:
                item = await buffer_queue.get()
                if item is _STREAM_EOF:
                    buffer_queue.task_done()
                    is_normal_eof = True
                    break
                if item is _STREAM_CANCELLED or task.cancelled or task.future.cancelled():
                    buffer_queue.task_done()
                    break
                if isinstance(item, _StreamError):
                    buffer_queue.task_done()
                    raise item.exc
                buffer_queue.task_done()
                yield item
        finally:
            stop_event.set()
            if not is_normal_eof:
                task.cancelled = True
                if not task.stream_cancelled_counted:
                    task.stream_cancelled_counted = True
                    self._cancelled_streams += 1
                if not fut.done():
                    fut.cancel()

    def cancel_generation(self, generation_id: str) -> int:
        """Cancels all queued, unexecuted tasks associated with generation_id."""
        if not generation_id:
            return 0
        cancelled_count = 0
        with self._sync_lock:
            task_ids = self._tasks_by_gen.pop(generation_id, set())
            for tid in task_ids:
                task = self._tasks_by_id.pop(tid, None)
                if task and not task.future.done():
                    task.cancelled = True
                    task.future.cancel()
                    if getattr(task, "is_stream", False):
                        if task.stop_event:
                            task.stop_event.set()
                        if task.stream_queue:
                            try:
                                task.stream_queue.put_nowait(_STREAM_CANCELLED)
                            except Exception:
                                pass
                        if not getattr(task, "stream_cancelled_counted", False):
                            task.stream_cancelled_counted = True
                            self._cancelled_streams += 1
                    cancelled_count += 1
        if cancelled_count > 0:
            logger.info("TtsScheduler: Cancelled %d pending tasks for stale generation %s", cancelled_count, generation_id)
        return cancelled_count

    def cancel_task(self, task_id: str) -> bool:
        """Cancels a specific task by task_id."""
        if not task_id:
            return False
        with self._sync_lock:
            task = self._tasks_by_id.pop(task_id, None)
            if task and not task.future.done():
                task.cancelled = True
                task.future.cancel()
                if getattr(task, "is_stream", False):
                    if task.stop_event:
                        task.stop_event.set()
                    if task.stream_queue:
                        try:
                            task.stream_queue.put_nowait(_STREAM_CANCELLED)
                        except Exception:
                            pass
                    if not getattr(task, "stream_cancelled_counted", False):
                        task.stream_cancelled_counted = True
                        self._cancelled_streams += 1
                return True
        return False

    def get_queue_depth(self) -> int:
        return self._queue.qsize() if self._queue is not None else 0

    async def _dispatch_loop(self) -> None:
        """Single consumer worker executing queued tasks sequentially according to priority."""
        while self._running:
            try:
                task = await self._queue.get()
            except (asyncio.CancelledError, GeneratorExit, RuntimeError):
                break

            # Fast path: task was cancelled while sitting in queue
            if task.cancelled or task.future.cancelled():
                if task.is_stream and not task.stream_cancelled_counted:
                    task.stream_cancelled_counted = True
                    self._cancelled_streams += 1
                self._cleanup_task_tracking(task)
                self._queue.task_done()
                continue

            # Record queue wait time when task is popped for execution
            wait_time = max(0.0, time.monotonic() - task.created_at)
            self._total_queue_wait_time += wait_time
            self._queue_wait_samples += 1
            self._last_queue_wait_time = wait_time

            try:
                result = await task.coro_fn()
                if not task.future.done():
                    task.future.set_result(result)
            except (asyncio.CancelledError, GeneratorExit):
                if not task.future.done():
                    task.future.cancel()
                raise
            except Exception as exc:
                if not task.future.done():
                    task.future.set_exception(exc)
            finally:
                self._cleanup_task_tracking(task)
                self._queue.task_done()

    def _cleanup_task_tracking(self, task: ScheduledTtsTask) -> None:
        self._tasks_by_id.pop(task.task_id, None)
        if task.generation_id and task.generation_id in self._tasks_by_gen:
            self._tasks_by_gen[task.generation_id].discard(task.task_id)
            if not self._tasks_by_gen[task.generation_id]:
                self._tasks_by_gen.pop(task.generation_id, None)

    async def aclose(self) -> None:
        """Shuts down scheduler and worker task."""
        self._running = False
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass


_GLOBAL_TTS_SCHEDULER: Optional[TtsScheduler] = None


def get_tts_scheduler() -> TtsScheduler:
    """Returns application singleton TtsScheduler."""
    global _GLOBAL_TTS_SCHEDULER
    if _GLOBAL_TTS_SCHEDULER is None:
        _GLOBAL_TTS_SCHEDULER = TtsScheduler()
    return _GLOBAL_TTS_SCHEDULER
