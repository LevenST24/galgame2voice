"""
Audio specification probing, LRU caching, and reference audio validation for galgame2voice.

Provides:
- AudioSpec dataclass (duration_s, sample_rate, channels)
- AudioSpecCache LRU cache keyed by (resolved_path, mtime_ns, size)
- Singleton _AUDIO_SPEC_CACHE (and _GLOBAL_AUDIO_SPEC_CACHE alias)
- probe_audio_spec(path_or_bytes) with soundfile/wave and raw WAV RIFF header fallback
- probe_audio_duration_seconds(path_or_bytes)
- async_probe_audio_duration_seconds(path_or_bytes)
- validate_reference_audio(path_or_bytes) enforcing the 3.0s ~ 10.0s hard constraint
- extract_wav_duration(audio: bytes)
- resolve_reference_audio_path(path: str)
- SILENT_AUDIO_ERROR message for FP16 clamping / all-zero audio
- wav_peak_amplitude(audio: bytes)
- is_silent_audio(audio: bytes) and wav_is_silent alias
"""

from __future__ import annotations

import array
import asyncio
import io
import struct
import sys
import threading
import wave
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

REFERENCE_AUDIO_MIN_SECONDS: float = 3.0
REFERENCE_AUDIO_MAX_SECONDS: float = 10.0
WAV_HEADER_BYTES: int = 44
PCM_32KHZ_16BIT_MONO_BYTE_RATE: float = 64000.0


def _safe_resolve_path(path_val: str | Path) -> Path:
    """Safely resolves path, falling back to absolute() on Windows permission errors."""
    p = Path(path_val)
    try:
        return p.resolve()
    except (OSError, PermissionError):
        return p.absolute()


@dataclass(frozen=True)
class AudioSpec:
    """Audio specification containing duration in seconds, sample rate, and channel count."""

    duration_s: float
    sample_rate: int = 0
    channels: int = 1


def _parse_wav_chunks(
    data: bytes,
    include_data_bytes: bool = False,
) -> tuple[bytes | None, int | None, bytes | None]:
    """Scans RIFF/WAVE chunks and returns (fmt_bytes, data_len, data_bytes)."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None, None, None
    try:
        pos = 12
        fmt: bytes | None = None
        data_len: int | None = None
        data_bytes: bytes | None = None
        while pos + 8 <= len(data):
            chunk_id = data[pos : pos + 4]
            chunk_size = int.from_bytes(data[pos + 4 : pos + 8], "little")
            if chunk_id == b"fmt ":
                fmt = data[pos + 8 : pos + 8 + chunk_size]
            elif chunk_id == b"data":
                data_len = chunk_size
                if include_data_bytes:
                    data_bytes = data[pos + 8 : pos + 8 + chunk_size]
                break
            pos += 8 + chunk_size + (chunk_size & 1)
        return fmt, data_len, data_bytes
    except (IndexError, TypeError, ValueError):
        return None, None, None


def _probe_wav_riff_header(data: bytes) -> AudioSpec | None:
    """
    Parses raw WAV RIFF header and fmt chunk to determine sample rate, channels,
    and duration without requiring third-party libraries.
    """
    fmt, data_len, _ = _parse_wav_chunks(data)
    if fmt and len(fmt) >= 16:
        try:
            channels = struct.unpack_from("<H", fmt, 2)[0]
            sample_rate = struct.unpack_from("<I", fmt, 4)[0]
            byte_rate = struct.unpack_from("<I", fmt, 8)[0]
            effective_data_len = data_len if data_len is not None else max(0, len(data) - WAV_HEADER_BYTES)
            if byte_rate > 0:
                duration_s = max(0.05, effective_data_len / float(byte_rate))
            elif sample_rate > 0 and channels > 0:
                duration_s = max(0.05, effective_data_len / float(sample_rate * channels * 2))
            else:
                duration_s = max(0.05, (len(data) - WAV_HEADER_BYTES) / PCM_32KHZ_16BIT_MONO_BYTE_RATE)
            return AudioSpec(
                duration_s=duration_s,
                sample_rate=sample_rate if sample_rate > 0 else 32000,
                channels=channels if channels > 0 else 1,
            )
        except (struct.error, IndexError, TypeError, ZeroDivisionError):
            pass
    return None


def _probe_ogg_granule(data: bytes, suffix: str = ".ogg") -> AudioSpec | None:
    """Parses OggS container granules to estimate duration and sample rate."""
    idx = data.rfind(b"OggS")
    if idx >= 0 and idx + 14 <= len(data):
        try:
            granule = int.from_bytes(data[idx + 6 : idx + 14], "little")
            if granule > 0:
                rate = 48000 if suffix == ".opus" or b"OpusHead" in data[:64] else 0
                if rate == 0:
                    h = data.find(b"\x01vorbis")
                    if h > 0:
                        rate = int.from_bytes(data[h + 12 : h + 16], "little")
                if rate > 0:
                    return AudioSpec(
                        duration_s=granule / rate,
                        sample_rate=rate,
                        channels=1,
                    )
        except (IndexError, TypeError, ValueError, ZeroDivisionError):
            pass
    return None


def _spec_from_wave(w: Any) -> AudioSpec | None:
    """Constructs AudioSpec from an opened stdlib wave reader."""
    framerate = w.getframerate()
    channels = w.getnchannels()
    nframes = w.getnframes()
    if framerate > 0:
        return AudioSpec(
            duration_s=nframes / framerate,
            sample_rate=framerate,
            channels=channels,
        )
    return None


def _probe_soundfile(source: Any) -> AudioSpec | None:
    """Probes AudioSpec from path string or file-like buffer using soundfile."""
    try:
        import soundfile as sf

        info = sf.info(source)
        return AudioSpec(
            duration_s=float(info.duration),
            sample_rate=int(info.samplerate),
            channels=int(info.channels),
        )
    except (RuntimeError, ValueError, TypeError, OSError, ImportError):
        return None


def _probe_from_bytes(data: bytes) -> AudioSpec | None:
    """Probes AudioSpec from in-memory byte buffer using soundfile, wave, and fallback parsers."""
    if not data or len(data) < 12:
        return None

    # 1. Try soundfile
    spec = _probe_soundfile(io.BytesIO(data))
    if spec is not None:
        return spec

    # 2. Try stdlib wave
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            spec = _spec_from_wave(w)
            if spec is not None:
                return spec
    except (wave.Error, EOFError, struct.error, OSError):
        pass

    # 3. Raw WAV RIFF header parsing fallback
    spec = _probe_wav_riff_header(data)
    if spec is not None:
        return spec

    # 4. Raw OGG / Opus granule parsing fallback
    spec = _probe_ogg_granule(data)
    if spec is not None:
        return spec

    return None


class AudioSpecCache:
    """
    In-memory LRU cache for audio file specifications (duration, sample rate, channels).
    Keyed by (resolved_path, mtime_ns, size) so modifications on disk automatically invalidate stale specs.
    """

    def __init__(self, maxsize: int = 512) -> None:
        self._maxsize = maxsize
        self._cache: OrderedDict[tuple[str, int, int], AudioSpec] = OrderedDict()
        self._lock = threading.Lock()

    def get_spec(self, path: str | Path | bytes | bytearray | memoryview) -> AudioSpec | None:
        if isinstance(path, (bytes, bytearray, memoryview)):
            return _probe_from_bytes(bytes(path))
        try:
            p = Path(path)
            if not p.is_file() and (_PROJECT_ROOT / path).is_file():
                p = _PROJECT_ROOT / path
            if not p.is_file():
                return None
            st = p.stat()
            key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
            with self._lock:
                if key in self._cache:
                    self._cache.move_to_end(key)
                    return self._cache[key]
        except (OSError, TypeError, ValueError):
            return None

        spec = self._probe(p)
        if spec is not None:
            with self._lock:
                self._cache[key] = spec
                self._cache.move_to_end(key)
                while len(self._cache) > self._maxsize:
                    self._cache.popitem(last=False)
        return spec

    def get_duration(self, path: str | Path | bytes | bytearray | memoryview) -> float | None:
        spec = self.get_spec(path)
        return spec.duration_s if spec else None

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    @staticmethod
    def _probe(p: Path) -> AudioSpec | None:
        # 1. Try soundfile first
        spec = _probe_soundfile(str(p))
        if spec is not None:
            return spec

        suffix = p.suffix.lower()
        if suffix == ".wav":
            try:
                with wave.open(str(p), "rb") as w:
                    spec = _spec_from_wave(w)
                    if spec is not None:
                        return spec
            except (wave.Error, EOFError, struct.error, OSError):
                pass

        if suffix in (".ogg", ".opus"):
            try:
                data = p.read_bytes()
                spec = _probe_ogg_granule(data, suffix=suffix)
                if spec is not None:
                    return spec
            except (OSError, ValueError, TypeError, IndexError):
                pass

        # Fallback: read bytes and attempt raw header probing
        try:
            data = p.read_bytes()
            return _probe_from_bytes(data)
        except (OSError, ValueError, TypeError, IndexError):
            pass

        return None


_AUDIO_SPEC_CACHE = AudioSpecCache()
_GLOBAL_AUDIO_SPEC_CACHE = _AUDIO_SPEC_CACHE


def probe_audio_spec(
    path_or_bytes: str | Path | bytes | bytearray | memoryview | None,
) -> AudioSpec | None:
    """
    Probes full audio specification (duration_s, sample_rate, channels).
    Accepts a file path (str or Path) or raw audio bytes/bytearray/memoryview.
    Uses LRU cache for file paths; executes byte reading and raw WAV RIFF header parsing fallback for bytes.
    """
    if path_or_bytes is None:
        return None
    if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
        return _probe_from_bytes(bytes(path_or_bytes))
    return _AUDIO_SPEC_CACHE.get_spec(path_or_bytes)


def probe_audio_duration_seconds(
    path_or_bytes: str | Path | bytes | bytearray | memoryview | None,
) -> float | None:
    """
    Returns audio duration in seconds for WAV, OGG, Opus, etc., or None if
    the duration cannot be determined. Uses AudioSpecCache for file paths.
    """
    if not path_or_bytes:
        return None
    spec = probe_audio_spec(path_or_bytes)
    return spec.duration_s if spec else None


async def async_probe_audio_duration_seconds(
    path_or_bytes: str | Path | bytes | bytearray | memoryview | None,
) -> float | None:
    """
    Asynchronously probes audio duration in seconds.
    If the spec is already cached by stat (mtime_ns, size), returns immediately
    without offloading. On cache miss, delegates file inspection to asyncio.to_thread().
    """
    if not path_or_bytes:
        return None
    if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
        return probe_audio_duration_seconds(path_or_bytes)

    # Fast path: check in-memory stat cache first
    try:
        p = Path(path_or_bytes)
        if not p.is_file() and (_PROJECT_ROOT / path_or_bytes).is_file():
            p = _PROJECT_ROOT / path_or_bytes
        if not p.is_file():
            return None
        st = p.stat()
        key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
        with _AUDIO_SPEC_CACHE._lock:
            if key in _AUDIO_SPEC_CACHE._cache:
                _AUDIO_SPEC_CACHE._cache.move_to_end(key)
                return _AUDIO_SPEC_CACHE._cache[key].duration_s
    except OSError:
        return None

    # Offload disk I/O probe to thread pool
    return await asyncio.to_thread(probe_audio_duration_seconds, path_or_bytes)


def validate_reference_audio(
    path_or_bytes: str | Path | bytes | bytearray | memoryview | None,
) -> tuple[bool, str]:
    """
    Checks reference audio (file path or raw bytes) against GPT-SoVITS's 3~10s hard constraint.
    Returns (True, "") on success, or (False, error_message) on violation.
    """
    if path_or_bytes is None:
        return False, "empty reference audio path"

    if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
        if len(path_or_bytes) == 0:
            return False, "empty reference audio data"
        spec = probe_audio_spec(path_or_bytes)
        if spec is None:
            # Undeterminable (exotic container) — let the engine decide rather than block.
            return True, ""
        if not (REFERENCE_AUDIO_MIN_SECONDS <= spec.duration_s <= REFERENCE_AUDIO_MAX_SECONDS):
            return False, (
                f"reference audio duration {spec.duration_s:.2f}s is outside the "
                f"{REFERENCE_AUDIO_MIN_SECONDS:.0f}~{REFERENCE_AUDIO_MAX_SECONDS:.0f}s range"
            )
        return True, ""

    ref_audio_str = str(path_or_bytes)
    if not ref_audio_str.strip():
        return False, "empty reference audio path"

    p = Path(path_or_bytes)
    if not p.is_file():
        if (_PROJECT_ROOT / path_or_bytes).is_file():
            p = (_PROJECT_ROOT / path_or_bytes).resolve()
        else:
            return False, f"reference audio file not found: {ref_audio_str}"

    duration = probe_audio_duration_seconds(str(p))
    if duration is None:
        # Undeterminable (exotic container) — let the engine decide rather than block.
        return True, ""
    if not (REFERENCE_AUDIO_MIN_SECONDS <= duration <= REFERENCE_AUDIO_MAX_SECONDS):
        return False, (
            f"reference audio duration {duration:.2f}s is outside the "
            f"{REFERENCE_AUDIO_MIN_SECONDS:.0f}~{REFERENCE_AUDIO_MAX_SECONDS:.0f}s range: {ref_audio_str}"
        )
    return True, ""


def resolve_reference_audio_path(path: str) -> str:
    """Converts a relative project reference audio path to an absolute path for GPT-SoVITS."""
    if not path:
        return ""
    p = Path(path)
    if not p.is_file() and (_PROJECT_ROOT / path).is_file():
        return str((_PROJECT_ROOT / path).resolve())
    elif p.is_file():
        return str(p.resolve())
    return path


def extract_wav_duration(audio: bytes) -> float | None:
    """
    Extracts exact audio duration in seconds from RIFF/WAVE header and fmt chunk byte_rate,
    falling back to 32kHz 16-bit mono PCM estimation.
    """
    if not audio or len(audio) <= WAV_HEADER_BYTES:
        return None
    try:
        fmt, data_len, _ = _parse_wav_chunks(audio)
        if fmt and len(fmt) >= 16:
            _byte_rate = struct.unpack_from("<I", fmt, 8)[0]
            if _byte_rate > 0:
                effective_data_len = data_len if data_len is not None else max(0, len(audio) - WAV_HEADER_BYTES)
                return max(0.05, effective_data_len / float(_byte_rate))
    except (struct.error, IndexError, TypeError, ZeroDivisionError):
        pass
    # Fallback: assume 32000Hz 16-bit mono PCM (64000 bytes/sec)
    return max(0.05, (len(audio) - WAV_HEADER_BYTES) / PCM_32KHZ_16BIT_MONO_BYTE_RATE)


# ============================================================================
# Silent Audio Invariant: an all-zero WAV is a FAILED synthesis, not a success
# ============================================================================

SILENT_AUDIO_ERROR = (
    "TTS synthesis produced all-zero silent audio. This almost always means the GPU's "
    "half-precision (FP16) inference is defective (e.g. NVIDIA MX450 / GTX 16-series / TU117): "
    "the vocoder overflowed to NaN and was clamped to silence. "
    "Fix: restart the GPT-SoVITS engine with FP32 single precision (is_half=False) — "
    "on Windows simply re-run 启动.bat (it probes and calibrates precision automatically); "
    "on Linux/container set the environment variable is_half=false (or GPT_SOVITS_PRECISION=fp32 "
    "and let the launcher calibrate) before starting api_v2.py. See logs/gpt_sovits.log."
)


def wav_peak_amplitude(audio: bytes) -> float | None:
    """
    Returns the peak absolute sample value (normalized 0.0~1.0) of a RIFF/WAVE
    payload (PCM16 or float32), or None if the container/samples cannot be parsed.
    A zero-length data chunk counts as undeterminable (None), not silent.
    """
    try:
        if len(audio) < WAV_HEADER_BYTES:
            return None
        fmt, _, data = _parse_wav_chunks(audio, include_data_bytes=True)
        if not fmt or len(fmt) < 16 or not data:
            return None
        audio_format, _channels, _rate, _byte_rate, _align, bits = struct.unpack_from("<HHIIHH", fmt, 0)
        if audio_format == 0xFFFE and len(fmt) >= 26:  # WAVE_FORMAT_EXTENSIBLE
            audio_format = struct.unpack_from("<H", fmt, 24)[0]
        if audio_format == 1 and bits == 16:
            count = len(data) // 2
            if count == 0:
                return None
            samples = array.array("h")
            samples.frombytes(data[: count * 2])
            if sys.byteorder == "big":
                samples.byteswap()
            return max(abs(s) for s in samples) / 32768.0
        if audio_format == 3 and bits == 32:
            count = len(data) // 4
            if count == 0:
                return None
            float_samples = array.array("f")
            float_samples.frombytes(data[: count * 4])
            if sys.byteorder == "big":
                float_samples.byteswap()
            return max(abs(s) for s in float_samples)
        return None
    except Exception:
        return None


def is_silent_audio(audio: bytes) -> bool:
    """True only when the WAV parses and every sample is exactly zero."""
    peak = wav_peak_amplitude(audio)
    return peak is not None and peak == 0.0


# Backward compatibility alias
wav_is_silent = is_silent_audio


__all__ = [
    "AudioSpec",
    "AudioSpecCache",
    "_AUDIO_SPEC_CACHE",
    "_GLOBAL_AUDIO_SPEC_CACHE",
    "REFERENCE_AUDIO_MIN_SECONDS",
    "REFERENCE_AUDIO_MAX_SECONDS",
    "probe_audio_spec",
    "probe_audio_duration_seconds",
    "async_probe_audio_duration_seconds",
    "validate_reference_audio",
    "resolve_reference_audio_path",
    "extract_wav_duration",
    "SILENT_AUDIO_ERROR",
    "wav_peak_amplitude",
    "is_silent_audio",
    "wav_is_silent",
]
