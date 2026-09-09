"""
GPT-SoVITS API Client for galgame2voice.

Single shared async client with:
  - Persistent httpx connection pool (keep-alive, no per-request TCP churn)
  - Tiered timeouts (fast connect fail, long GPU inference read)
  - Transient-failure retry for TTS synthesis
  - asyncio.Lock inference mutex shared across the whole application
  - 3-step atomic model switching with rollback
  - Hot base_url reload (settings console changes take effect immediately)
"""

import asyncio
import array
import logging
import math
import os
import re
import struct
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx
from pydantic import BaseModel

logger = logging.getLogger("galgame2voice.services.gpt_sovits_client")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# GPT-SoVITS hard-rejects reference audio outside 3~10 seconds with a raw 400
# ("参考音频在3~10秒范围外"). Validate here so the user gets a fallback voice
# instead of a silent synthesis failure (e.g. the 2.47s cool.ogg incident).
REFERENCE_AUDIO_MIN_SECONDS = 3.0
REFERENCE_AUDIO_MAX_SECONDS = 10.0

_BUNDLED_REF_AUDIO = _PROJECT_ROOT / "audio" / "references" / "natsume" / "gentle.ogg"
_BUNDLED_REF_TEXT = "とりあえず、今日見たことは忘れて、わかった?"
_BUNDLED_REF_LANG = "ja"


# ============================================================================
# Japanese Parentheses Cleaner (Stage Direction Stripper)
# ============================================================================

def clean_japanese_parentheses(text: str, max_passes: int = 5) -> str:
    """
    Strips stage cues and action directions enclosed in fullwidth （...） or ASCII (...) parentheses.
    Applies multi-pass regex sanitization (up to max_passes) to handle nested brackets like （（ため息））.
    """
    if not text:
        return ""

    cleaned = text
    for _ in range(max_passes):
        prev = cleaned
        cleaned = re.sub(r'（[^（）]*）', '', cleaned)
        cleaned = re.sub(r'\([^()]*\)', '', cleaned)
        if cleaned == prev:
            break

    cleaned = cleaned.replace('（', '').replace('）', '').replace('(', '').replace(')', '')
    return cleaned.strip()


# ============================================================================
# Reference Audio Validation (3~10s pre-flight guard)
# ============================================================================

@lru_cache(maxsize=256)
def probe_audio_duration_seconds(path: str) -> Optional[float]:
    """
    Returns audio duration in seconds for WAV (stdlib wave) and OGG
    (soundfile or Vorbis/Opus via last OggS page granule position) files, or None if
    the duration cannot be determined.
    """
    try:
        p = Path(path)
        if not p.is_file() and (_PROJECT_ROOT / path).is_file():
            p = _PROJECT_ROOT / path

        # 1. Try soundfile first for exact header/granule parsing
        try:
            import soundfile as sf
            info = sf.info(str(p))
            return float(info.duration)
        except Exception:
            pass

        suffix = p.suffix.lower()
        if suffix == ".wav":
            import wave
            with wave.open(str(p), "rb") as w:
                framerate = w.getframerate()
                return (w.getnframes() / framerate) if framerate > 0 else None
        if suffix in (".ogg", ".opus"):
            data = p.read_bytes()
            idx = data.rfind(b"OggS")
            if idx < 0 or idx + 14 > len(data):
                return None
            granule = int.from_bytes(data[idx + 6:idx + 14], "little")
            if granule <= 0:
                return None
            rate = 0
            if suffix == ".opus" or b"OpusHead" in data[:64]:
                rate = 48000  # Opus granule positions are always 48kHz-based
            else:
                h = data.find(b"\x01vorbis")
                if h > 0:
                    rate = int.from_bytes(data[h + 12:h + 16], "little")
            if rate <= 0:
                return None
            return granule / rate
    except Exception:
        return None
    return None


def validate_reference_audio(ref_audio: str) -> Tuple[bool, str]:
    """Checks a reference audio path against GPT-SoVITS's 3~10s hard constraint."""
    if not ref_audio:
        return False, "empty reference audio path"
    p = Path(ref_audio)
    if not p.is_file():
        if (_PROJECT_ROOT / ref_audio).is_file():
            p = (_PROJECT_ROOT / ref_audio).resolve()
        else:
            return False, f"reference audio file not found: {ref_audio}"
    duration = probe_audio_duration_seconds(str(p))
    if duration is None:
        # Undeterminable (exotic container) — let the engine decide rather than block.
        return True, ""
    if not (REFERENCE_AUDIO_MIN_SECONDS <= duration <= REFERENCE_AUDIO_MAX_SECONDS):
        return False, (
            f"reference audio duration {duration:.2f}s is outside the "
            f"{REFERENCE_AUDIO_MIN_SECONDS:.0f}~{REFERENCE_AUDIO_MAX_SECONDS:.0f}s range: {ref_audio}"
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

def _fallback_reference() -> Optional[Tuple[str, str, str]]:
    """Bundled baseline reference (5.03s gentle voice) usable on any machine."""
    if _BUNDLED_REF_AUDIO.is_file():
        return str(_BUNDLED_REF_AUDIO.resolve()), _BUNDLED_REF_TEXT, _BUNDLED_REF_LANG
    alt_nat = _PROJECT_ROOT / "audio" / "nat002_032.ogg"
    if alt_nat.is_file():
        return str(alt_nat.resolve()), _BUNDLED_REF_TEXT, _BUNDLED_REF_LANG
    return None


# ============================================================================
# Silent Audio Invariant: an all-zero WAV is a FAILED synthesis, not a success
# ============================================================================

SILENT_AUDIO_ERROR = (
    "TTS synthesis produced all-zero silent audio. This almost always means the GPU's "
    "half-precision (FP16) inference is defective (e.g. NVIDIA MX450 / GTX 16-series / TU117): "
    "the vocoder overflowed to NaN and was clamped to silence. "
    "Fix: restart via 启动.bat, which automatically enables FP32 single-precision (is_half=False) "
    "via clean process environment isolation. See logs/gpt_sovits.log."
)


def wav_peak_amplitude(audio: bytes) -> Optional[float]:
    """
    Returns the peak absolute sample value (normalized 0.0~1.0) of a RIFF/WAVE
    payload (PCM16 or float32), or None if the container/samples cannot be parsed.
    A zero-length data chunk counts as undeterminable (None), not silent.
    """
    try:
        if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            return None
        fmt: Optional[bytes] = None
        data: Optional[bytes] = None
        pos = 12
        while pos + 8 <= len(audio):
            chunk_id = audio[pos:pos + 4]
            chunk_size = int.from_bytes(audio[pos + 4:pos + 8], "little")
            if chunk_id == b"fmt ":
                fmt = audio[pos + 8:pos + 8 + chunk_size]
            elif chunk_id == b"data":
                data = audio[pos + 8:pos + 8 + chunk_size]
                break
            pos += 8 + chunk_size + (chunk_size & 1)
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
            samples.frombytes(data[:count * 2])
            if sys.byteorder == "big":
                samples.byteswap()
            return max(abs(s) for s in samples) / 32768.0
        if audio_format == 3 and bits == 32:
            count = len(data) // 4
            if count == 0:
                return None
            samples = array.array("f")
            samples.frombytes(data[:count * 4])
            if sys.byteorder == "big":
                samples.byteswap()
            return max(abs(s) for s in samples)
        return None
    except Exception:
        return None


def wav_is_silent(audio: bytes) -> bool:
    """True only when the WAV parses and every sample is exactly zero."""
    peak = wav_peak_amplitude(audio)
    return peak is not None and peak == 0.0


# ============================================================================
# Presets & Slicing Methods
# ============================================================================

SLICING_METHODS = {
    "cut0": "No slice / 不切",
    "cut1": "Slice by 4 sentences / 凑四句切",
    "cut2": "Slice by 50 characters / 凑50字切",
    "cut3": "Slice by Chinese punctuation / 按中文句号。切",
    "cut4": "Slice by English punctuation / 按英文句号.切",
    "cut5": "Slice by punctuation / 按标点符号切",
}

TTS_PRESETS: Dict[str, Dict[str, Any]] = {
    "high_quality": {
        "name": "High Quality",
        "speed": 0.9,
        "speed_factor": 0.9,
        "top_k": 20,
        "top_p": 1.0,
        "temperature": 0.8,
        "text_split_method": "cut5",
        "batch_size": 1,
    },
    "balanced": {
        "name": "Balanced",
        "speed": 1.0,
        "speed_factor": 1.0,
        "top_k": 15,
        "top_p": 1.0,
        "temperature": 1.0,
        "text_split_method": "cut5",
        "batch_size": 1,
    },
    "low_latency": {
        "name": "Low Latency",
        "speed": 1.2,
        "speed_factor": 1.2,
        "top_k": 5,
        "top_p": 0.9,
        "temperature": 0.5,
        "text_split_method": "cut5",
        "batch_size": 1,
    },
}


# Range constraints for user-supplied TTS options (mirrors TtsOptions model).
_TTS_NUMERIC_RANGES = {
    "speed_factor": (0.1, 3.0, float),
    "speed": (0.1, 3.0, float),
    "top_k": (1, 100, int),
    "top_p": (0.0, 1.0, float),
    "temperature": (0.0, 2.0, float),
    "batch_size": (1, 16, int),
    "fragment_interval": (0.0, 5.0, float),
    "seed": (-1, 2**31 - 1, int),
}
_TTS_STRING_MAXLEN = {
    "text_lang": 32,
    "prompt_lang": 32,
    "text_language": 32,
    "prompt_language": 32,
    "refer_language": 32,
    "text_split_method": 64,
    "how_to_cut": 64,
    "cut_option": 64,
    "ref_audio_path": 512,
    "refer_audio_path": 512,
    "prompt_text": 500,
    "refer_text": 500,
}

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


def validate_user_tts_options(options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Validates untrusted TTS options at the API boundary.
    Raises ValueError on out-of-range numerics, oversized/控制字符 strings.
    Returns the input unchanged when valid.
    """
    if not options:
        return {}
    if not isinstance(options, dict):
        raise ValueError("tts_options 必须是对象")
    for key, value in options.items():
        if key in _TTS_NUMERIC_RANGES:
            low, high, caster = _TTS_NUMERIC_RANGES[key]
            try:
                num = caster(value)
            except (TypeError, ValueError):
                raise ValueError(f"TTS 参数 {key} 必须是数字")
            if not (low <= num <= high):
                raise ValueError(f"TTS 参数 {key} 超出允许范围 [{low}, {high}]")
        elif key in _TTS_STRING_MAXLEN:
            s = str(value)
            if len(s) > _TTS_STRING_MAXLEN[key]:
                raise ValueError(f"TTS 参数 {key} 长度超限 (最多 {_TTS_STRING_MAXLEN[key]} 字符)")
            if "\x00" in s:
                raise ValueError(f"TTS 参数 {key} 含有非法控制字符")
    return options


def resolve_tts_options(options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Merges preset defaults with user-supplied TTS inference options.
    Normalizes parameter keys to official GPT-SoVITS api_v2.py format.
    Numeric values are clamped to their legal ranges as defense in depth.
    """
    options = options or {}
    preset_key = str(options.get("preset", "balanced")).lower().replace(" ", "_")
    base_params = dict(TTS_PRESETS.get(preset_key, TTS_PRESETS["balanced"]))

    def _clamped(key: str, value: Any, fallback: Any) -> Any:
        low, high, caster = _TTS_NUMERIC_RANGES[key]
        try:
            num = caster(value)
        except (TypeError, ValueError):
            num = caster(fallback)
        return max(low, min(high, num))

    speed_val = _clamped(
        "speed_factor",
        options.get("speed_factor", options.get("speed", base_params.get("speed_factor", 1.0))),
        base_params.get("speed_factor", 1.0),
    )
    is_adaptive = bool(options.get("ai_adaptive_voice", options.get("aiAdaptiveVoice", False)))
    if is_adaptive:
        speed_val = clamp_dynamic_speed(speed_val, fallback=1.0)

    text_lang_val = str(options.get("text_lang", options.get("text_language", "ja")))[:32]
    prompt_lang_val = str(options.get("prompt_lang", options.get("prompt_language", options.get("refer_language", "ja"))))[:32]
    split_val = str(options.get("text_split_method", options.get("how_to_cut", options.get("cut_option", base_params.get("text_split_method", "cut5")))))[:64]

    temp_val = _clamped("temperature", options.get("temperature", options.get("temp", base_params.get("temperature", 1.0))), base_params.get("temperature", 1.0))
    if is_adaptive:
        temp_val = clamp_dynamic_temperature(temp_val, fallback=1.0)

    merged = {
        "speed_factor": speed_val,
        "speed": speed_val,  # alias kept for backwards compatibility
        "top_k": int(_clamped("top_k", options.get("top_k", base_params.get("top_k", 15)), base_params.get("top_k", 15))),
        "top_p": _clamped("top_p", options.get("top_p", base_params.get("top_p", 1.0)), base_params.get("top_p", 1.0)),
        "temperature": temp_val,
        "text_lang": text_lang_val,
        "text_language": text_lang_val,  # alias
        "prompt_lang": prompt_lang_val,
        "prompt_language": prompt_lang_val,  # alias
        "text_split_method": split_val,
        "batch_size": int(_clamped("batch_size", options.get("batch_size", base_params.get("batch_size", 1)), base_params.get("batch_size", 1))),
        "seed": int(_clamped("seed", options.get("seed", -1), -1)),
        "fragment_interval": _clamped("fragment_interval", options.get("fragment_interval", 0.3), 0.3),
        "ref_audio_path": str(options.get("ref_audio_path", options.get("refer_audio_path", "")))[:512],
        "prompt_text": str(options.get("prompt_text", options.get("refer_text", "")))[:500],
        "ai_adaptive_voice": is_adaptive,
    }

    # streaming_mode: accept bool or int (1/2/3 presets), normalized to bool later.
    streaming_raw = options.get("streaming_mode", options.get("stream_mode"))
    if streaming_raw is not None:
        merged["streaming_mode"] = streaming_raw

    return merged


# ============================================================================
# Voice Profile Data Representation Helper
# ============================================================================

class VoiceProfileWeightSpec(BaseModel):
    name: str
    gpt_weights_path: str
    sovits_weights_path: str
    refer_audio_path: str
    refer_text: str
    refer_language: str = "ja"
    prompt_language: str = "ja"
    text_language: str = "ja"


def _extract_weight_spec(target: Any) -> VoiceProfileWeightSpec:
    """Extracts weight paths and refer audio fields from various object types."""
    if isinstance(target, dict):
        return VoiceProfileWeightSpec(
            name=target.get("name", "Unnamed"),
            gpt_weights_path=target.get("gpt_weights_path", ""),
            sovits_weights_path=target.get("sovits_weights_path", ""),
            refer_audio_path=target.get("refer_audio_path") or target.get("ref_audio_path") or "",
            refer_text=target.get("refer_text") or target.get("prompt_text") or "",
            refer_language=target.get("refer_language") or target.get("prompt_lang") or "ja",
            prompt_language=target.get("prompt_language") or target.get("prompt_lang") or "ja",
            text_language=target.get("text_language") or target.get("text_lang") or "ja",
        )
    elif hasattr(target, "gpt_weights_path"):
        return VoiceProfileWeightSpec(
            name=getattr(target, "name", "Unnamed"),
            gpt_weights_path=getattr(target, "gpt_weights_path", ""),
            sovits_weights_path=getattr(target, "sovits_weights_path", ""),
            refer_audio_path=getattr(target, "refer_audio_path", getattr(target, "ref_audio_path", "")),
            refer_text=getattr(target, "refer_text", getattr(target, "prompt_text", "")),
            refer_language=getattr(target, "refer_language", getattr(target, "prompt_lang", "ja")),
            prompt_language=getattr(target, "prompt_language", getattr(target, "prompt_lang", "ja")),
            text_language=getattr(target, "text_language", getattr(target, "text_lang", "ja")),
        )
    else:
        raise ValueError(f"Cannot extract weight spec from object of type {type(target)}")


# ============================================================================
# GPT-SoVITS Client
# ============================================================================

# Tiered timeout profile: fail fast on connect, allow long GPU synthesis reads.
# NOTE: connect is capped at 1s because some VPN/TUN proxy stacks delay even
# loopback connection-refused to ~2s; a healthy local engine connects in <50ms.
TTS_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=15.0, pool=15.0)
SWITCH_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=15.0, pool=15.0)
HEALTH_TIMEOUT = httpx.Timeout(connect=1.0, read=2.5, write=2.5, pool=2.5)


class GptSovitsClient:
    """
    Asynchronous client for the GPT-SoVITS api_v2 service.

    One instance = one persistent httpx connection pool + one inference mutex.
    The whole application should share a single instance (see get_gpt_sovits_client)
    so that synthesis and model switching are globally serialized against the
    single GPU inference engine.

    Mock-server mode (`server=`) is preserved for the test suite.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9880",
        timeout: float = 300.0,
        client: Optional[httpx.AsyncClient] = None,
        server: Optional[Any] = None,
    ):
        self.base_url = str(base_url).rstrip("/")
        self.timeout = timeout
        self._client = client
        self.server = server  # MockGptSovitsServer in tests
        self.lock = asyncio.Lock()

        # State tracking
        self.active_profile: Optional[Any] = None
        self.is_switching: bool = False
        self.current_gpt_weights: Optional[str] = None
        self.current_sovits_weights: Optional[str] = None
        self.current_refer_audio: Optional[str] = None
        self.current_refer_text: Optional[str] = None
        self.current_refer_language: Optional[str] = None

        # In-flight request tracking for hot URL swaps: the old connection
        # pool is closed once in-flight requests drain or the grace period
        # expires, whichever comes first (read timeout is up to 300s).
        self._inflight_requests = 0
        self._close_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Connection pool lifecycle
    # ------------------------------------------------------------------

    def _get_http_client(self) -> httpx.AsyncClient:
        """Lazily creates and reuses a pooled httpx.AsyncClient (keep-alive)."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                trust_env=False,
                timeout=TTS_TIMEOUT,
                limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            )
        return self._client

    async def aclose(self) -> None:
        """Closes the pooled HTTP client. Safe to call multiple times."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def set_base_url(self, new_url: str) -> None:
        """Hot-reloads the GPT-SoVITS endpoint URL, recreating the connection pool."""
        new_url = str(new_url).strip().rstrip("/")
        if not new_url:
            return
        if new_url == self.base_url:
            return
        logger.info("GPT-SoVITS base URL changing: %s -> %s", self.base_url, new_url)
        self.base_url = new_url
        # Swap the client atomically; drain in-flight requests (or give up
        # after a grace period) before closing the old pool so ongoing
        # synthesis streams are not cut off mid-read.
        old_client = self._client
        self._client = None
        if old_client is not None and not old_client.is_closed:
            grace = float(os.getenv("GALGAME2VOICE_CLIENT_CLOSE_GRACE_SECONDS", "30") or 30)

            async def _close_when_drained():
                try:
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + grace
                    while self._inflight_requests > 0 and loop.time() < deadline:
                        await asyncio.sleep(0.25)
                except Exception:
                    pass
                try:
                    await old_client.aclose()
                except Exception:
                    pass

            # Keep a strong reference so the task cannot be garbage collected.
            if self._close_task is not None and not self._close_task.done():
                self._close_task.cancel()
            self._close_task = asyncio.create_task(_close_when_drained())

    async def _request(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[httpx.Timeout] = None,
    ) -> httpx.Response:
        """Internal HTTP request dispatcher supporting mock server or pooled httpx."""
        if self.server is not None and hasattr(self.server, "handle_request"):
            return await self.server.handle_request(method, path, json_data=json_data, params=params)

        url = f"{self.base_url}{path}"
        client = self._get_http_client()
        return await client.request(
            method, url, json=json_data, params=params, timeout=timeout
        )

    # ------------------------------------------------------------------
    # Health & Diagnostic Endpoints
    # ------------------------------------------------------------------

    async def check_health(self) -> Dict[str, Any]:
        """
        Probes GPT-SoVITS reachability via GET /control (api_v2 control endpoint returns 400 when active).
        HTTP 200/400 proves the engine is alive and listening; other codes / network errors are unreachable.
        Dispatches through the mock server when one is configured (tests).
        """
        try:
            if self.server is not None and hasattr(self.server, "handle_request"):
                try:
                    resp = await self.server.handle_request("GET", "/control")
                except Exception:
                    resp = await self.server.handle_request("GET", "/")
            else:
                client = self._get_http_client()
                url = f"{self.base_url}/control"
                try:
                    resp = await client.get(url, timeout=HEALTH_TIMEOUT)
                except Exception:
                    # Fallback to GET / if /control fails
                    resp = await client.get(f"{self.base_url}/", timeout=HEALTH_TIMEOUT)

            if resp.status_code in (200, 400):
                return {
                    "connected": True,
                    "status": "running",
                    "url": self.base_url,
                    "http_status": resp.status_code,
                    "current_gpt_weights": self.current_gpt_weights,
                    "current_sovits_weights": self.current_sovits_weights,
                }
            return {
                "connected": False,
                "status": "unreachable",
                "url": self.base_url,
                "http_status": resp.status_code,
                "error": f"Unexpected status code: {resp.status_code}",
                "current_gpt_weights": self.current_gpt_weights,
                "current_sovits_weights": self.current_sovits_weights,
            }
        except Exception as exc:
            return {
                "connected": False,
                "status": "unreachable",
                "url": self.base_url,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def control(self, command: str = "restart") -> Dict[str, Any]:
        """Sends control command to GPT-SoVITS service."""
        resp = await self._request("POST", "/control", json_data={"command": command})
        if resp.status_code == 200:
            return resp.json()
        raise RuntimeError(f"Control command failed with status {resp.status_code}: {resp.text}")

    # ------------------------------------------------------------------
    # Individual Weight Endpoints
    # ------------------------------------------------------------------

    async def set_gpt_weights(self, weights_path: str) -> bool:
        """Sets GPT weights path. Loading onto GPU may take tens of seconds."""
        resp = await self._request("GET", "/set_gpt_weights", params={"weights_path": weights_path}, timeout=SWITCH_TIMEOUT)
        if resp.status_code == 200:
            self.current_gpt_weights = weights_path
            return True
        logger.error("Failed to set GPT weights (%s): HTTP %d %s", weights_path, resp.status_code, resp.text)
        return False

    async def set_sovits_weights(self, weights_path: str) -> bool:
        """Sets SoVITS weights path. Loading onto GPU may take tens of seconds."""
        resp = await self._request("GET", "/set_sovits_weights", params={"weights_path": weights_path}, timeout=SWITCH_TIMEOUT)
        if resp.status_code == 200:
            self.current_sovits_weights = weights_path
            return True
        logger.error("Failed to set SoVITS weights (%s): HTTP %d %s", weights_path, resp.status_code, resp.text)
        return False

    async def set_refer_audio(
        self,
        refer_audio_path: str,
        refer_text: str = "",
        refer_language: str = "ja",
    ) -> bool:
        """Sets reference audio."""
        p = Path(refer_audio_path)
        if not p.is_file() and (_PROJECT_ROOT / refer_audio_path).is_file():
            refer_audio_path = str((_PROJECT_ROOT / refer_audio_path).resolve())
        elif p.is_file():
            refer_audio_path = str(p.resolve())

        resp = await self._request("GET", "/set_refer_audio", params={"refer_audio_path": refer_audio_path})
        if resp.status_code == 200:
            self.current_refer_audio = refer_audio_path
            self.current_refer_text = refer_text
            self.current_refer_language = refer_language
            return True
        logger.error("Failed to set refer audio (%s): HTTP %d %s", refer_audio_path, resp.status_code, resp.text)
        return False

    # ------------------------------------------------------------------
    # 3-Step Atomic Model Switching with Auto-Rollback
    # ------------------------------------------------------------------

    async def switch_voice_profile(self, target: Any, force: bool = False) -> bool:
        """
        Switches GPT-SoVITS voice profile in 3 transactional steps:
          Step 1: GET /set_gpt_weights?weights_path=... (skipped if identical weights already loaded)
          Step 2: GET /set_sovits_weights?weights_path=... (skipped if identical weights already loaded)
          Step 3: GET /set_refer_audio?refer_audio_path=...

        If any step fails, automatically rolls back previous steps to restore
        the prior working state. Mutex protected with asyncio.Lock.
        """
        async with self.lock:
            self.is_switching = True
            spec = _extract_weight_spec(target)
            prev_profile = self.active_profile
            prev_spec = _extract_weight_spec(prev_profile) if prev_profile else None

            logger.info("Switching voice profile to '%s' (GPT: %s, SoVITS: %s)...",
                        spec.name, spec.gpt_weights_path, spec.sovits_weights_path)

            try:
                # Step 1: GPT weights (skip if identical weights already loaded and not force)
                if force or not (self.current_gpt_weights and self.current_gpt_weights == spec.gpt_weights_path):
                    r1 = await self._request("GET", "/set_gpt_weights", params={"weights_path": spec.gpt_weights_path}, timeout=SWITCH_TIMEOUT)
                    if r1.status_code != 200:
                        logger.error("Switch failed at Step 1 (GPT weights): %s", r1.text)
                        return False
                    self.current_gpt_weights = spec.gpt_weights_path
                else:
                    logger.debug("Skipping /set_gpt_weights: '%s' already loaded", spec.gpt_weights_path)

                # Step 2: SoVITS weights (skip if identical weights already loaded and not force)
                if force or not (self.current_sovits_weights and self.current_sovits_weights == spec.sovits_weights_path):
                    r2 = await self._request("GET", "/set_sovits_weights", params={"weights_path": spec.sovits_weights_path}, timeout=SWITCH_TIMEOUT)
                    if r2.status_code != 200:
                        logger.error("Switch failed at Step 2 (SoVITS weights): %s. Initiating rollback...", r2.text)
                        if prev_spec and prev_spec.gpt_weights_path and prev_spec.gpt_weights_path != spec.gpt_weights_path:
                            await self._request("GET", "/set_gpt_weights", params={"weights_path": prev_spec.gpt_weights_path}, timeout=SWITCH_TIMEOUT)
                            self.current_gpt_weights = prev_spec.gpt_weights_path
                        return False
                    self.current_sovits_weights = spec.sovits_weights_path
                else:
                    logger.debug("Skipping /set_sovits_weights: '%s' already loaded", spec.sovits_weights_path)

                # Step 3: Reference Audio
                resolved_ref_audio = resolve_reference_audio_path(spec.refer_audio_path)
                if force or not (self.current_refer_audio and self.current_refer_audio == resolved_ref_audio and self.current_refer_text == spec.refer_text and self.current_refer_language == spec.refer_language):
                    r3 = await self._request("GET", "/set_refer_audio", params={"refer_audio_path": resolved_ref_audio})
                    if r3.status_code != 200:
                        logger.error("Switch failed at Step 3 (Refer Audio): %s. Initiating rollback...", r3.text)
                        if prev_spec:
                            if prev_spec.sovits_weights_path and prev_spec.sovits_weights_path != spec.sovits_weights_path:
                                await self._request("GET", "/set_sovits_weights", params={"weights_path": prev_spec.sovits_weights_path}, timeout=SWITCH_TIMEOUT)
                                self.current_sovits_weights = prev_spec.sovits_weights_path
                            if prev_spec.gpt_weights_path and prev_spec.gpt_weights_path != spec.gpt_weights_path:
                                await self._request("GET", "/set_gpt_weights", params={"weights_path": prev_spec.gpt_weights_path}, timeout=SWITCH_TIMEOUT)
                                self.current_gpt_weights = prev_spec.gpt_weights_path
                            if prev_spec.refer_audio_path:
                                rollback_ref = resolve_reference_audio_path(prev_spec.refer_audio_path)
                                await self._request("GET", "/set_refer_audio", params={"refer_audio_path": rollback_ref})
                        return False

                self.current_refer_audio = resolved_ref_audio
                self.current_refer_text = spec.refer_text
                self.current_refer_language = spec.refer_language
                self.active_profile = target
                logger.info("Successfully switched voice profile to '%s'", spec.name)
                return True

            except Exception as exc:
                logger.error("Exception during voice profile switch: %s. Rolling back...", exc, exc_info=True)
                if prev_spec:
                    try:
                        if prev_spec.sovits_weights_path:
                            await self._request("GET", "/set_sovits_weights", params={"weights_path": prev_spec.sovits_weights_path}, timeout=SWITCH_TIMEOUT)
                        if prev_spec.gpt_weights_path:
                            await self._request("GET", "/set_gpt_weights", params={"weights_path": prev_spec.gpt_weights_path}, timeout=SWITCH_TIMEOUT)
                        if prev_spec.refer_audio_path:
                            rollback_ref = resolve_reference_audio_path(prev_spec.refer_audio_path)
                            await self._request("GET", "/set_refer_audio", params={"refer_audio_path": rollback_ref})
                    except Exception as rollback_exc:
                        # Rollback failure leaves server state diverged from local state — surface it loudly.
                        logger.error("ROLLBACK FAILED after switch error (server state may diverge): %s", rollback_exc)
                return False
            finally:
                self.is_switching = False

    # ------------------------------------------------------------------
    # Synthesis Endpoints (/tts)
    # ------------------------------------------------------------------

    def _build_tts_payload(self, text: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Builds standardized GPT-SoVITS official /tts request payload."""
        resolved = resolve_tts_options(options)
        ref_audio = resolved.get("ref_audio_path") or self.current_refer_audio or ""
        ref_text = resolved.get("prompt_text") or self.current_refer_text or ""
        ref_lang = resolved.get("prompt_lang") or self.current_refer_language or "ja"

        ok, reason = validate_reference_audio(ref_audio)
        if not ok:
            fallback = _fallback_reference()
            if fallback:
                logger.warning(
                    "Reference audio rejected (%s) — falling back to bundled baseline reference", reason
                )
                ref_audio, ref_text, ref_lang = fallback
            else:
                logger.error("Reference audio rejected (%s) and no bundled fallback available", reason)
        else:
            p = Path(ref_audio)
            if not p.is_file() and (_PROJECT_ROOT / ref_audio).is_file():
                ref_audio = str((_PROJECT_ROOT / ref_audio).resolve())
            elif p.is_file():
                ref_audio = str(p.resolve())

        return {
            "text": text,
            "text_lang": resolved["text_lang"],
            "ref_audio_path": ref_audio,
            "prompt_text": ref_text,
            "prompt_lang": ref_lang,
            "top_k": resolved["top_k"],
            "top_p": resolved["top_p"],
            "temperature": resolved["temperature"],
            "text_split_method": resolved["text_split_method"],
            "batch_size": resolved["batch_size"],
            "speed_factor": resolved["speed_factor"],
            "streaming_mode": resolved.get("streaming_mode", False),
            "seed": resolved["seed"],
        }

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """Heuristic: network-level errors are transient; HTTP 4xx are not."""
        if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout,
                            httpx.PoolTimeout, httpx.RemoteProtocolError)):
            return True
        if isinstance(exc, RuntimeError) and "status 5" in str(exc):
            return True
        return False

    async def synthesize(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        retries: int = 1,
    ) -> bytes:
        """
        Synthesizes text into complete WAV audio bytes.
        Cleans Japanese stage cues before synthesis.
        Guarded by the shared inference mutex lock.
        Retries once on transient network failures.
        """
        async with self.lock:
            cleaned_text = clean_japanese_parentheses(text)
            if not cleaned_text:
                raise ValueError("Text is empty after cleaning stage directions")

            opts = dict(options or {})
            opts["streaming_mode"] = False
            payload = self._build_tts_payload(cleaned_text, opts)
            payload["streaming_mode"] = False

            attempt = 0
            while True:
                attempt += 1
                try:
                    self._inflight_requests += 1
                    try:
                        resp = await self._request("POST", "/tts", json_data=payload)
                    finally:
                        self._inflight_requests -= 1
                    if resp.status_code != 200:
                        raise RuntimeError(f"TTS synthesis failed with status {resp.status_code}: {resp.text[:300]}")
                    if not resp.content:
                        raise RuntimeError("TTS synthesis returned empty audio payload")
                    if wav_is_silent(resp.content):
                        raise RuntimeError(SILENT_AUDIO_ERROR)
                    return resp.content
                except Exception as exc:
                    if attempt <= retries and self._is_transient(exc):
                        logger.warning(
                            "TTS transient failure (attempt %d/%d) for '%s...': %s — retrying",
                            attempt, attempt + retries, cleaned_text[:20], exc,
                        )
                        await asyncio.sleep(0.8 * attempt)
                        continue
                    raise

    async def stream_tts(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        chunk_size: int = 4096,
    ) -> AsyncGenerator[bytes, None]:
        """
        Streams synthesized audio in binary chunks.

        The inference lock is only held while the synthesis response is being
        downloaded into a spooled buffer; the chunks are then yielded to the
        caller AFTER the lock is released, so a slow HTTP consumer can no
        longer stall the global GPU serialization point.
        """
        cleaned_text = clean_japanese_parentheses(text)
        if not cleaned_text:
            raise ValueError("Text is empty after cleaning stage directions")

        opts = dict(options or {})
        opts["streaming_mode"] = True
        payload = self._build_tts_payload(cleaned_text, opts)
        payload["streaming_mode"] = True

        buffer = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        try:
            if self.server is not None and hasattr(self.server, "handle_request"):
                async with self.lock:
                    resp = await self.server.handle_request("POST", "/tts", json_data=payload)
                    if resp.status_code != 200:
                        raise RuntimeError(f"TTS synthesis failed with status {resp.status_code}: {resp.text}")
                    buffer.write(resp.content)
            else:
                url = f"{self.base_url}/tts"
                client = self._get_http_client()
                async with self.lock:
                    self._inflight_requests += 1
                    try:
                        async with client.stream("POST", url, json=payload, timeout=TTS_TIMEOUT) as resp:
                            if resp.status_code != 200:
                                err_bytes = await resp.aread()
                                raise RuntimeError(
                                    f"TTS synthesis failed with status {resp.status_code}: {err_bytes.decode('utf-8', errors='ignore')[:300]}"
                                )
                            async for chunk in resp.aiter_bytes(chunk_size=65536):
                                if chunk:
                                    buffer.write(chunk)
                    finally:
                        self._inflight_requests -= 1

            buffer.seek(0)
            audio_bytes = buffer.read()
            if wav_is_silent(audio_bytes):
                raise RuntimeError(SILENT_AUDIO_ERROR)
            for i in range(0, len(audio_bytes), chunk_size):
                yield audio_bytes[i:i + chunk_size]
        finally:
            buffer.close()


# ============================================================================
# Application-Level Singleton
# ============================================================================

_global_gpt_sovits_client: Optional[GptSovitsClient] = None


def get_gpt_sovits_client() -> GptSovitsClient:
    """
    Returns the application-wide singleton GptSovitsClient.
    All services (TtsService, VoiceManager, Telegram, routers) MUST share this
    instance so the inference mutex actually serializes GPU access globally.
    """
    global _global_gpt_sovits_client
    if _global_gpt_sovits_client is None:
        settings = None
        try:
            from galgame2voice.config import get_settings
            settings = get_settings()
        except Exception:
            pass
        base_url = settings.gpt_sovits_base_url if settings else "http://127.0.0.1:9880"
        _global_gpt_sovits_client = GptSovitsClient(base_url=base_url)
    return _global_gpt_sovits_client


async def reload_gpt_sovits_client_base_url(new_url: str) -> None:
    """Hot-updates the singleton's endpoint (called when settings console saves gpt_sovits_url)."""
    client = get_gpt_sovits_client()
    await client.set_base_url(new_url)


def set_gpt_sovits_client(client: Optional[GptSovitsClient]) -> None:
    """Replaces or resets the singleton (used by tests)."""
    global _global_gpt_sovits_client
    _global_gpt_sovits_client = client


async def close_gpt_sovits_client() -> None:
    """Closes the singleton's connection pool (called during app shutdown)."""
    global _global_gpt_sovits_client
    if _global_gpt_sovits_client is not None:
        await _global_gpt_sovits_client.aclose()
        _global_gpt_sovits_client = None


__all__ = [
    "GptSovitsClient",
    "get_gpt_sovits_client",
    "set_gpt_sovits_client",
    "reload_gpt_sovits_client_base_url",
    "close_gpt_sovits_client",
    "clean_japanese_parentheses",
    "resolve_tts_options",
    "SLICING_METHODS",
    "TTS_PRESETS",
    "DYNAMIC_SPEED_MIN",
    "DYNAMIC_SPEED_MAX",
    "DYNAMIC_TEMP_MIN",
    "DYNAMIC_TEMP_MAX",
    "clamp_dynamic_speed",
    "clamp_dynamic_temperature",
    "resolve_reference_audio_path",
]
