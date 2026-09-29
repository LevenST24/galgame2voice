"""
TTS options handling, validation, preset resolution, and voice profile weight specifications.

Provides:
- SLICING_METHODS: mapping of text slice method keys to human-readable labels
- TTS_PRESETS: default synthesis parameters for presets (high_quality, balanced, low_latency)
- _TTS_NUMERIC_RANGES, _TTS_STRING_MAXLEN: validation bounds for untrusted user options
- validate_user_tts_options(options): boundary validator raising ValueError on illegal params
- resolve_tts_options(options): merges defaults, normalizes keys, clamps numeric bounds
- VoiceProfileWeightSpec: Pydantic model for character voice profile weight specifications
- _extract_weight_spec(target): extracts and absolutizes weight paths from profile dicts/objects
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from galgame2voice.utils.path_guard import resolve_weight_file_path
from galgame2voice.utils.prosody import (
    clamp_dynamic_batch_size,
    clamp_dynamic_fragment_interval,
    clamp_dynamic_speed,
    clamp_dynamic_temperature,
    clamp_dynamic_top_k,
    clamp_dynamic_top_p,
)

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
    "emotion": 64,
}

_ALLOWED_TTS_KEYS = set(_TTS_NUMERIC_RANGES.keys()) | set(_TTS_STRING_MAXLEN.keys()) | {
    "preset", "temp", "ai_adaptive_voice", "aiAdaptiveVoice",
    # 内部音色路由键：routers/voice.py 与 tts_service.py 都会把它写进 options，
    # 不入白名单会被自己的边界校验判为非法参数。
    "voice_profile_id",
}

# voice_profile_id 是角色音色 ID，非 GPT-SoVITS 推理参数，仅需整数边界。
_INTERNAL_INT_KEYS = {"voice_profile_id": (1, 100000)}


class ChatTtsOptions(BaseModel):
    """Pydantic model validating dynamic TTS inference options parsed from chat turns."""

    model_config = ConfigDict(extra="forbid")

    speed_factor: Optional[float] = Field(default=None, ge=0.1, le=3.0)
    speed: Optional[float] = Field(default=None, ge=0.1, le=3.0)
    top_k: Optional[int] = Field(default=None, ge=1, le=100)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    temp: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    batch_size: Optional[int] = Field(default=None, ge=1, le=16)
    fragment_interval: Optional[float] = Field(default=None, ge=0.0, le=5.0)
    seed: Optional[int] = Field(default=None, ge=-1, le=2**31 - 1)
    text_lang: Optional[str] = Field(default=None, max_length=32)
    prompt_lang: Optional[str] = Field(default=None, max_length=32)
    text_language: Optional[str] = Field(default=None, max_length=32)
    prompt_language: Optional[str] = Field(default=None, max_length=32)
    refer_language: Optional[str] = Field(default=None, max_length=32)
    text_split_method: Optional[str] = Field(default=None, max_length=64)
    how_to_cut: Optional[str] = Field(default=None, max_length=64)
    cut_option: Optional[str] = Field(default=None, max_length=64)
    ref_audio_path: Optional[str] = Field(default=None, max_length=512)
    refer_audio_path: Optional[str] = Field(default=None, max_length=512)
    prompt_text: Optional[str] = Field(default=None, max_length=500)
    refer_text: Optional[str] = Field(default=None, max_length=500)
    emotion: Optional[str] = Field(default=None, max_length=64)
    preset: Optional[str] = Field(default=None, max_length=64)
    ai_adaptive_voice: Optional[bool] = None
    aiAdaptiveVoice: Optional[bool] = None
    voice_profile_id: Optional[int] = Field(default=None, ge=1, le=100000)


def validate_user_tts_options(options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Validates untrusted TTS options at the API boundary.
    Enforces extra="forbid" behavior, limits parameter counts, and raises ValueError
    on out-of-range numerics, oversized strings, or illegal control characters.
    """
    if not options:
        return {}
    if not isinstance(options, dict):
        raise ValueError("tts_options 必须是对象")
    if len(options) > 25:
        raise ValueError("tts_options 参数数量超限 (最多 25 项)")

    for key, value in options.items():
        if key not in _ALLOWED_TTS_KEYS:
            raise ValueError(f"禁止未知或非法的 TTS 参数: {key}")

        if key in _TTS_NUMERIC_RANGES:
            low, high, caster = _TTS_NUMERIC_RANGES[key]
            try:
                num = caster(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"TTS 参数 {key} 必须是数字") from exc
            if not (low <= num <= high):
                raise ValueError(f"TTS 参数 {key} 超出允许范围 [{low}, {high}]")
        elif key in _INTERNAL_INT_KEYS:
            low, high = _INTERNAL_INT_KEYS[key]
            try:
                num = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"TTS 参数 {key} 必须是整数") from exc
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

    top_k_val = int(_clamped("top_k", options.get("top_k", base_params.get("top_k", 15)), base_params.get("top_k", 15)))
    if is_adaptive:
        top_k_val = clamp_dynamic_top_k(top_k_val, fallback=15)

    top_p_val = _clamped("top_p", options.get("top_p", base_params.get("top_p", 1.0)), base_params.get("top_p", 1.0))
    if is_adaptive:
        top_p_val = clamp_dynamic_top_p(top_p_val, fallback=1.0)

    frag_val = _clamped("fragment_interval", options.get("fragment_interval", 0.3), 0.3)
    if is_adaptive:
        frag_val = clamp_dynamic_fragment_interval(frag_val, fallback=0.3)

    raw_batch = options.get("batch_size")
    batch_val = int(_clamped("batch_size", raw_batch if raw_batch is not None else base_params.get("batch_size", 1), base_params.get("batch_size", 1)))
    if is_adaptive and raw_batch is not None:
        batch_val = clamp_dynamic_batch_size(batch_val, fallback=1)

    merged = {
        "speed_factor": speed_val,
        "speed": speed_val,  # alias kept for backwards compatibility
        "top_k": top_k_val,
        "top_p": top_p_val,
        "temperature": temp_val,
        "text_lang": text_lang_val,
        "text_language": text_lang_val,  # alias
        "prompt_lang": prompt_lang_val,
        "prompt_language": prompt_lang_val,  # alias
        "text_split_method": split_val,
        "batch_size": batch_val,
        "seed": int(_clamped("seed", options.get("seed", -1), -1)),
        "fragment_interval": frag_val,
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
    """Normalized specification of model weight paths and reference audio metadata for a voice profile."""

    name: str
    gpt_weights_path: str
    sovits_weights_path: str
    refer_audio_path: str
    refer_text: str
    refer_language: str = "ja"
    prompt_language: str = "ja"
    text_language: str = "ja"


def _extract_weight_spec(target: Any) -> VoiceProfileWeightSpec:
    """Extracts weight paths and refer audio fields from various object types.
    Weight paths are absolutized here (single choke point) so the engine receives
    loadable absolute paths whether the profile stores project-relative package
    paths or engine-relative paths."""
    if isinstance(target, dict):
        return VoiceProfileWeightSpec(
            name=target.get("name", "Unnamed"),
            gpt_weights_path=resolve_weight_file_path(target.get("gpt_weights_path", "")),
            sovits_weights_path=resolve_weight_file_path(target.get("sovits_weights_path", "")),
            refer_audio_path=target.get("refer_audio_path") or target.get("ref_audio_path") or "",
            refer_text=target.get("refer_text") or target.get("prompt_text") or "",
            refer_language=target.get("refer_language") or target.get("prompt_lang") or "ja",
            prompt_language=target.get("prompt_language") or target.get("prompt_lang") or "ja",
            text_language=target.get("text_language") or target.get("text_lang") or "ja",
        )
    elif hasattr(target, "gpt_weights_path"):
        return VoiceProfileWeightSpec(
            name=getattr(target, "name", "Unnamed"),
            gpt_weights_path=resolve_weight_file_path(getattr(target, "gpt_weights_path", "")),
            sovits_weights_path=resolve_weight_file_path(getattr(target, "sovits_weights_path", "")),
            refer_audio_path=getattr(target, "refer_audio_path", getattr(target, "ref_audio_path", "")),
            refer_text=getattr(target, "refer_text", getattr(target, "prompt_text", "")),
            refer_language=getattr(target, "refer_language", getattr(target, "prompt_lang", "ja")),
            prompt_language=getattr(target, "prompt_language", getattr(target, "prompt_lang", "ja")),
            text_language=getattr(target, "text_language", getattr(target, "text_lang", "ja")),
        )
    else:
        raise ValueError(f"Cannot extract weight spec from object of type {type(target)}")


__all__ = [
    "SLICING_METHODS",
    "TTS_PRESETS",
    "_TTS_NUMERIC_RANGES",
    "_TTS_STRING_MAXLEN",
    "validate_user_tts_options",
    "resolve_tts_options",
    "VoiceProfileWeightSpec",
    "_extract_weight_spec",
]
