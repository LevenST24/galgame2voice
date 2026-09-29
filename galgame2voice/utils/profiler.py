"""
Performance & Latency Profiler for Galgame2Voice.
Provides ASCII waterfall charts tracking TTFT, first sentence splitting,
TTS dispatch, TTS synthesis, and TTFA (Time To First Audio).
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional


def is_profiling_enabled() -> bool:
    """Returns True if profiling mode is activated via env var or CLI flag."""
    val = os.environ.get("GALGAME2VOICE_PROFILE", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def set_profiling_enabled(enabled: bool = True) -> None:
    """Explicitly enables or disables profiling mode."""
    if enabled:
        os.environ["GALGAME2VOICE_PROFILE"] = "1"
    else:
        os.environ.pop("GALGAME2VOICE_PROFILE", None)


class ChatTurnProfiler:
    """
    Tracks and visualizes latency checkpoints for a single chat turn (T0 ~ T7).
    T0: Request received
    T1: LLM first token (TTFT)
    T2: First sentence segmented
    T3: TTS scheduler dispatch
    T4: Upstream TTS engine first audio chunk arrived
    T5: Application first audio chunk yielded (TTFA)
    T6: Frontend delivery over SSE
    T7: Audio playback started
    """

    def __init__(self, turn_id: Optional[str] = None, enabled: Optional[bool] = None) -> None:
        self.turn_id = turn_id or str(int(time.time() * 1000) % 10000)
        self.enabled = is_profiling_enabled() if enabled is None else enabled
        self.t_start = time.perf_counter()

        self.t_llm_first_token: Optional[float] = None
        self.t_first_sentence: Optional[float] = None
        self.t_tts_dispatch: Optional[float] = None
        self.t_upstream_first_byte: Optional[float] = None
        self.t_tts_inference_done: Optional[float] = None
        self.t_first_audio: Optional[float] = None
        self.t_frontend_delivered: Optional[float] = None
        self.t_playback_started: Optional[float] = None

        self.cache_hits: List[int] = []
        self.cache_misses: List[int] = []
        self.chunk_count: int = 0

    def record_llm_first_token(self) -> None:
        if self.t_llm_first_token is None:
            self.t_llm_first_token = time.perf_counter()

    def record_first_sentence(self) -> None:
        if self.t_first_sentence is None:
            self.t_first_sentence = time.perf_counter()

    def record_tts_dispatch(self, chunk_index: int = 0) -> None:
        if self.t_tts_dispatch is None:
            self.t_tts_dispatch = time.perf_counter()

    def record_upstream_first_byte(self) -> None:
        if self.t_upstream_first_byte is None:
            self.t_upstream_first_byte = time.perf_counter()

    def record_tts_inference(self, chunk_index: int = 0, cached: bool = False) -> None:
        if self.t_tts_inference_done is None:
            self.t_tts_inference_done = time.perf_counter()
        if cached:
            self.cache_hits.append(chunk_index)
        else:
            self.cache_misses.append(chunk_index)
        self.chunk_count += 1

    def record_first_audio(self) -> None:
        if self.t_first_audio is None:
            self.t_first_audio = time.perf_counter()

    def record_app_first_chunk(self) -> None:
        self.record_first_audio()

    def record_frontend_delivered(self) -> None:
        if self.t_frontend_delivered is None:
            self.t_frontend_delivered = time.perf_counter()

    def record_playback_started(self) -> None:
        if self.t_playback_started is None:
            self.t_playback_started = time.perf_counter()

    @property
    def app_first_chunk_ts(self) -> Optional[float]:
        return self.t_first_audio

    def _diff_ms(self, t: Optional[float]) -> float:
        return (t - self.t_start) * 1000.0 if t is not None else 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Returns structured JSON-serializable telemetry data."""
        now = time.perf_counter()
        ttft_ms = self._diff_ms(self.t_llm_first_token)
        sent_ms = self._diff_ms(self.t_first_sentence)
        disp_ms = self._diff_ms(self.t_tts_dispatch)
        upstream_ms = self._diff_ms(self.t_upstream_first_byte)
        infer_ms = self._diff_ms(self.t_tts_inference_done)
        ttfa_ms = self._diff_ms(self.t_first_audio) or (now - self.t_start) * 1000.0
        delivered_ms = self._diff_ms(self.t_frontend_delivered)
        playback_ms = self._diff_ms(self.t_playback_started)

        cached_count = len(self.cache_hits)
        generated_count = len(self.cache_misses)
        hit_rate = (cached_count / self.chunk_count) if self.chunk_count > 0 else 0.0

        return {
            "turn_id": self.turn_id,
            "t0_start_s": self.t_start,
            "t1_ttft_ms": round(ttft_ms, 1),
            "t2_first_sentence_ms": round(sent_ms, 1),
            "t3_tts_dispatch_ms": round(disp_ms, 1),
            "t4_upstream_first_byte_ms": round(upstream_ms, 1),
            "t5_app_ttfa_ms": round(ttfa_ms, 1),
            "t6_frontend_delivered_ms": round(delivered_ms, 1),
            "t7_playback_ms": round(playback_ms, 1),
            "tts_infer_done_ms": round(infer_ms, 1),
            "cache_hits": list(self.cache_hits),
            "cache_misses": list(self.cache_misses),
            "cached_chunks": cached_count,
            "generated_chunks": generated_count,
            "cache_hit_rate": round(hit_rate, 2),
            "chunk_count": self.chunk_count,
            "passed_target": ttfa_ms < 1000.0,
        }

    def _format_bar(self, ms: float, max_ms: float, bar_width: int = 20) -> str:
        if max_ms <= 0:
            return ""
        fraction = min(1.0, max(0.05, ms / max_ms))
        count = int(round(fraction * bar_width))
        return "█" * count

    def render_ascii(self) -> str:
        """Renders the ASCII waterfall diagram regardless of enabled flag."""
        now = time.perf_counter()
        ttft_ms = self._diff_ms(self.t_llm_first_token)
        sent_ms = self._diff_ms(self.t_first_sentence)
        disp_ms = self._diff_ms(self.t_tts_dispatch)
        upstream_ms = self._diff_ms(self.t_upstream_first_byte)
        infer_ms = self._diff_ms(self.t_tts_inference_done)
        ttfa_ms = self._diff_ms(self.t_first_audio) or (now - self.t_start) * 1000.0
        deliv_ms = self._diff_ms(self.t_frontend_delivered)
        play_ms = self._diff_ms(self.t_playback_started)

        max_metric = max(100.0, ttfa_ms, infer_ms, deliv_ms, play_ms)
        status = "PASS (<1000ms)" if ttfa_ms < 1000.0 else "WARN (>=1000ms)"

        lines = [
            f"\n┌── [Chat Turn #{self.turn_id} Profile Waterfall (T0~T7)] ──────────────────",
            f"│  T0 Request Sent:        0.0ms",
            f"│  T1 LLM First Token / LLM TTFT: {ttft_ms:6.1f}ms  {self._format_bar(ttft_ms, max_metric)}",
            f"│  T2 First Sentence:    {sent_ms:6.1f}ms  {self._format_bar(sent_ms, max_metric)}",
            f"│  T3 TTS Dispatch:      {disp_ms:6.1f}ms  {self._format_bar(disp_ms, max_metric)}",
        ]
        if self.t_upstream_first_byte is not None:
            lines.append(f"│  T4 Upstream First Byte: {upstream_ms:6.1f}ms  {self._format_bar(upstream_ms, max_metric)}")
        lines.extend([
            f"│  T5 App Chunk 0 Emitted: {ttfa_ms:6.1f}ms  {self._format_bar(ttfa_ms, max_metric)}",
            f"│  TTS Inference:         {infer_ms:6.1f}ms  {self._format_bar(infer_ms, max_metric)}",
        ])
        if self.t_frontend_delivered is not None:
            lines.append(f"│  T6 SSE Delivered:       {deliv_ms:6.1f}ms  {self._format_bar(deliv_ms, max_metric)}")
        if self.t_playback_started is not None:
            lines.append(f"│  T7 Playback Start:      {play_ms:6.1f}ms  {self._format_bar(play_ms, max_metric)}")

        lines.extend([
            f"├─────────────────────────────────────────────────────────────",
            f"│  TTFA:                 {ttfa_ms:6.1f}ms (Target: <1000ms) -> {status}",
        ])

        if self.cache_hits:
            saved_approx = len(self.cache_hits) * 350
            lines.append(f"│  Cache Hit:            Chunks {self.cache_hits} (saved ~{saved_approx}ms)")
        else:
            lines.append("│  Cache Hit:            None (full neural synthesis)")

        lines.append("└─────────────────────────────────────────────────────────────\n")
        return "\n".join(lines)

    def generate_report(self) -> str:
        if not self.enabled:
            return ""
        return self.render_ascii()

    def print_waterfall(self) -> None:
        if self.enabled:
            report = self.generate_report()
            if report:
                print(report, flush=True)


__all__ = [
    "is_profiling_enabled",
    "set_profiling_enabled",
    "ChatTurnProfiler",
]
