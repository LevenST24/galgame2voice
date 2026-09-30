"""
Latency-aware and throughput-driven dynamic batch size scheduler for GPT-SoVITS synthesis.
Measures real-time factor (RTF) and character synthesis velocity (chars/sec) to dynamically
scale inference batch size for multi-sentence synthesis while protecting against OOM
and maintaining low TTFA for streaming.
"""

import collections
import logging
import re
import threading
import time
from typing import Any

from galgame2voice.utils.prosody import clamp_dynamic_batch_size

logger = logging.getLogger("galgame2voice.services.dynamic_batcher")

_RE_SPLIT_PUNCT_COMMA = re.compile(r'[。！？\.\!\?，,、\n]+')
_RE_SPLIT_PUNCT_PERIOD = re.compile(r'[。！？\.\!\?\n]+')


class SynthesisSpeedRecord:
    """Historical snapshot of a single speech synthesis operation."""
    __slots__ = ("timestamp", "char_count", "elapsed_s", "audio_dur_s", "rtf", "chars_per_sec")

    def __init__(
        self,
        char_count: int,
        elapsed_s: float,
        audio_dur_s: float | None = None,
    ) -> None:
        self.timestamp = time.time()
        self.char_count = max(1, char_count)
        self.elapsed_s = max(0.001, elapsed_s)
        # If audio duration is unknown, estimate ~7.0 characters per second for Japanese/Chinese
        self.audio_dur_s = max(0.05, audio_dur_s if (audio_dur_s and audio_dur_s > 0) else (self.char_count / 7.0))
        self.rtf = self.elapsed_s / self.audio_dur_s
        self.chars_per_sec = self.char_count / self.elapsed_s


class SynthesisSpeedTracker:
    """
    Thread-safe moving-window tracker for TTS inference throughput and latency.
    Computes rolling averages of RTF (Real-Time Factor) and character throughput.
    """

    def __init__(self, max_history: int = 15) -> None:
        self._max_history = max_history
        self._history: collections.deque[SynthesisSpeedRecord] = collections.deque(maxlen=max_history)
        self._lock = threading.Lock()

    def record(
        self,
        char_count: int,
        elapsed_s: float,
        audio_dur_s: float | None = None,
    ) -> SynthesisSpeedRecord:
        """Records a completed synthesis operation into the moving window."""
        rec = SynthesisSpeedRecord(char_count=char_count, elapsed_s=elapsed_s, audio_dur_s=audio_dur_s)
        with self._lock:
            self._history.append(rec)
        logger.debug(
            "Recorded TTS synthesis speed: %d chars in %.3fs (%.1f chars/s, RTF=%.2f)",
            rec.char_count, rec.elapsed_s, rec.chars_per_sec, rec.rtf,
        )
        return rec

    def get_metrics(self) -> dict[str, Any]:
        """Returns statistical metrics over recent synthesis operations."""
        with self._lock:
            items = list(self._history)

        if not items:
            return {
                "sample_count": 0,
                "avg_rtf": 1.0,
                "avg_chars_per_sec": 15.0,
                "is_resource_constrained": False,
                "is_high_throughput": False,
            }

        avg_rtf = sum(item.rtf for item in items) / len(items)
        avg_cps = sum(item.chars_per_sec for item in items) / len(items)

        return {
            "sample_count": len(items),
            "avg_rtf": round(avg_rtf, 3),
            "avg_chars_per_sec": round(avg_cps, 2),
            "is_resource_constrained": avg_rtf > 1.2 or avg_cps < 9.0,
            "is_high_throughput": avg_rtf < 0.5 and avg_cps >= 20.0,
        }

    @property
    def sample_count(self) -> int:
        with self._lock:
            return len(self._history)

    def is_resource_constrained(self) -> bool:
        return bool(self.get_metrics()["is_resource_constrained"])

    def is_high_throughput(self) -> bool:
        return bool(self.get_metrics()["is_high_throughput"])

    def get_telemetry(self) -> dict[str, Any]:
        """Returns comprehensive telemetry dictionary for API endpoints."""
        metrics = self.get_metrics()
        scheduler = get_batch_scheduler()
        rec_batch_size = scheduler.compute_batch_size(
            "测试一段话。第二句话。第三句话。第四句话。",
            split_method="cut2",
        )
        return {
            "status": "ready" if metrics["sample_count"] > 0 else "cold_start",
            "sample_count": metrics["sample_count"],
            "avg_rtf": metrics["avg_rtf"],
            "chars_per_sec": metrics["avg_chars_per_sec"],
            "is_resource_constrained": metrics["is_resource_constrained"],
            "is_high_throughput": metrics["is_high_throughput"],
            "dynamic_batch_size": rec_batch_size,
        }

    def clear(self) -> None:
        with self._lock:
            self._history.clear()


class DynamicBatchScheduler:
    """
    Determines the optimal VITS inference batch_size based on:
    1. Streaming status (always 1 for lowest TTFA)
    2. Explicit user parameter override (if specified)
    3. Sentence/fragment count in target text
    4. Hardware runtime throughput & RTF feedback
    """

    def __init__(self, tracker: SynthesisSpeedTracker | None = None) -> None:
        self.tracker = tracker or get_speed_tracker()

    @staticmethod
    def estimate_slice_count(text: str, split_method: str = "cut0") -> int:
        """
        Estimates the number of slice fragments that GPT-SoVITS will produce
        from the given text according to the text splitting method.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return 1

        method = (split_method or "cut0").lower()
        if method == "cut0":
            # No cut or single sentence
            return 1

        # Common punctuation boundaries in Japanese and Chinese
        # cut1: 凑四句 / 四句切
        # cut2: 按句号逗号切
        # cut3: 按句号切
        # cut4: 凑50字切
        # cut5: 按标点切
        if method in ("cut2", "cut5"):
            parts = [p for p in _RE_SPLIT_PUNCT_COMMA.split(cleaned) if p.strip()]
        elif method in ("cut1", "cut3"):
            parts = [p for p in _RE_SPLIT_PUNCT_PERIOD.split(cleaned) if p.strip()]
        else:
            # Approx 25-30 chars per fragment
            parts = [cleaned[i:i + 30] for i in range(0, len(cleaned), 30)]

        return max(1, len(parts))

    @staticmethod
    def _scale_batch_size_for_throughput(slice_count: int, is_high_throughput: bool) -> int:
        """Scales batch size according to slice count and GPU throughput tier."""
        if is_high_throughput:
            if slice_count >= 8:
                return 8
            if slice_count >= 4:
                return 4
            return min(2, slice_count)

        if slice_count >= 6:
            return 4
        if slice_count >= 3:
            return 3
        return min(2, slice_count)

    def compute_batch_size(
        self,
        text: str,
        is_streaming: bool = False,
        split_method: str = "cut0",
        user_batch_size: Any | None = None,
    ) -> int:
        """
        Computes the optimal batch size for GPT-SoVITS inference.
        """
        # 1. Explicit user override
        if user_batch_size is not None:
            try:
                ubs = int(user_batch_size)
                # If user passed a positive integer that isn't sentinel 0
                if ubs > 0:
                    return clamp_dynamic_batch_size(ubs)
            except (TypeError, ValueError):
                pass

        # 2. Streaming mode priority: lowest TTFA is paramount
        if is_streaming:
            return 1

        # 3. Fragment count estimation
        slice_count = self.estimate_slice_count(text, split_method=split_method)
        if slice_count <= 1:
            return 1

        # 4. Latency & Throughput-aware dynamic scaling
        metrics = self.tracker.get_metrics()
        samples = metrics["sample_count"]

        # Cold start (few or no samples) or resource-constrained: conservative parallelism
        if samples < 2 or metrics["is_resource_constrained"]:
            return min(2, slice_count)

        return self._scale_batch_size_for_throughput(slice_count, metrics["is_high_throughput"])


# Global singletons
_GLOBAL_TRACKER: SynthesisSpeedTracker | None = None
_GLOBAL_SCHEDULER: DynamicBatchScheduler | None = None
_INIT_LOCK = threading.RLock()


def get_speed_tracker() -> SynthesisSpeedTracker:
    """Returns the process-wide SynthesisSpeedTracker singleton."""
    global _GLOBAL_TRACKER
    if _GLOBAL_TRACKER is None:
        with _INIT_LOCK:
            if _GLOBAL_TRACKER is None:
                _GLOBAL_TRACKER = SynthesisSpeedTracker()
    return _GLOBAL_TRACKER


def reset_speed_tracker() -> None:
    """Resets the process-wide SynthesisSpeedTracker singleton."""
    global _GLOBAL_TRACKER
    with _INIT_LOCK:
        _GLOBAL_TRACKER = SynthesisSpeedTracker()


def get_batch_scheduler() -> DynamicBatchScheduler:
    """Returns the process-wide DynamicBatchScheduler singleton."""
    global _GLOBAL_SCHEDULER
    if _GLOBAL_SCHEDULER is None:
        with _INIT_LOCK:
            if _GLOBAL_SCHEDULER is None:
                _GLOBAL_SCHEDULER = DynamicBatchScheduler(tracker=get_speed_tracker())
    return _GLOBAL_SCHEDULER


__all__ = [
    "SynthesisSpeedRecord",
    "SynthesisSpeedTracker",
    "DynamicBatchScheduler",
    "get_speed_tracker",
    "reset_speed_tracker",
    "get_batch_scheduler",
]
