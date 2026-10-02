"""
Voice prosody, emotion parameter clamping, and fine-grained adaptive acoustics utilities.
Provides deterministic acoustic baselines for 7 standard emotion archetypes,
contextual micro-prosody modulation based on punctuation and utterance length,
and strict safety boundary clamping across all speech inference parameters.
"""

import math
import re
from typing import Any

# Dynamic AI-Driven Voice Prosody & Emotion Parameter Ranges
DYNAMIC_SPEED_MIN = 0.50
DYNAMIC_SPEED_MAX = 1.50
DYNAMIC_TEMP_MIN = 0.60
DYNAMIC_TEMP_MAX = 1.20

DYNAMIC_TOP_K_MIN = 1
DYNAMIC_TOP_K_MAX = 50
DYNAMIC_TOP_P_MIN = 0.50
DYNAMIC_TOP_P_MAX = 1.00

DYNAMIC_FRAGMENT_INTERVAL_MIN = 0.10
DYNAMIC_FRAGMENT_INTERVAL_MAX = 1.00

DYNAMIC_BATCH_SIZE_MIN = 1
DYNAMIC_BATCH_SIZE_MAX = 16

PARAM_SPEED = "speed"
PARAM_TEMPERATURE = "temperature"
PARAM_TOP_K = "top_k"
PARAM_TOP_P = "top_p"
PARAM_FRAGMENT_INTERVAL = "fragment_interval"

# ============================================================================
# 1. Clamping Functions
# ============================================================================

def _clamp_float(val: Any, min_val: float, max_val: float, fallback: float, round_digits: int = 4) -> float:
    try:
        num = float(val)
        if math.isnan(num) or math.isinf(num):
            return fallback
        return max(min_val, min(max_val, round(num, round_digits)))
    except (TypeError, ValueError):
        return fallback


def _clamp_int(val: Any, min_val: int, max_val: int, fallback: int) -> int:
    try:
        return max(min_val, min(max_val, int(val)))
    except (TypeError, ValueError):
        return fallback


def clamp_dynamic_speed(val: Any, fallback: float = 1.0) -> float:
    """Clamps dynamic voice inference speed into [0.50, 1.50]. Falls back if invalid."""
    return _clamp_float(val, DYNAMIC_SPEED_MIN, DYNAMIC_SPEED_MAX, fallback)


def clamp_dynamic_temperature(val: Any, fallback: float = 1.0) -> float:
    """Clamps dynamic voice inference temperature into [0.60, 1.20]. Falls back if invalid."""
    return _clamp_float(val, DYNAMIC_TEMP_MIN, DYNAMIC_TEMP_MAX, fallback)


def clamp_dynamic_top_k(val: Any, fallback: int = 15) -> int:
    """Clamps dynamic Top-K sampling parameter into [1, 50]. Falls back if invalid."""
    return _clamp_int(val, DYNAMIC_TOP_K_MIN, DYNAMIC_TOP_K_MAX, fallback)


def clamp_dynamic_top_p(val: Any, fallback: float = 1.0) -> float:
    """Clamps dynamic Top-P nucleus sampling parameter into [0.50, 1.00]. Falls back if invalid."""
    return _clamp_float(val, DYNAMIC_TOP_P_MIN, DYNAMIC_TOP_P_MAX, fallback)


def clamp_dynamic_fragment_interval(val: Any, fallback: float = 0.3) -> float:
    """Clamps inter-sentence pause fragment interval into [0.10, 1.00]. Falls back if invalid."""
    return _clamp_float(val, DYNAMIC_FRAGMENT_INTERVAL_MIN, DYNAMIC_FRAGMENT_INTERVAL_MAX, fallback)


def clamp_dynamic_batch_size(val: Any, fallback: int = 1) -> int:
    """Clamps inference batch size into [1, 16]. Falls back if invalid."""
    return _clamp_int(val, DYNAMIC_BATCH_SIZE_MIN, DYNAMIC_BATCH_SIZE_MAX, fallback)



# ============================================================================
# 2. Emotion Archetype Baseline Acoustic Profiles
# ============================================================================

EMOTION_PROSODY_MATRIX: dict[str, dict[str, Any]] = {
    "gentle": {
        PARAM_SPEED: 0.98,
        PARAM_TEMPERATURE: 0.78,
        PARAM_TOP_K: 15,
        PARAM_TOP_P: 0.85,
        PARAM_FRAGMENT_INTERVAL: 0.32,
    },
    "happy": {
        PARAM_SPEED: 1.12,
        PARAM_TEMPERATURE: 0.95,
        PARAM_TOP_K: 20,
        PARAM_TOP_P: 0.95,
        PARAM_FRAGMENT_INTERVAL: 0.22,
    },
    "tsundere": {
        PARAM_SPEED: 1.15,
        PARAM_TEMPERATURE: 0.92,
        PARAM_TOP_K: 14,
        PARAM_TOP_P: 0.88,
        PARAM_FRAGMENT_INTERVAL: 0.20,
    },
    "shy": {
        PARAM_SPEED: 0.88,
        PARAM_TEMPERATURE: 0.72,
        PARAM_TOP_K: 10,
        PARAM_TOP_P: 0.78,
        PARAM_FRAGMENT_INTERVAL: 0.38,
    },
    "sad": {
        PARAM_SPEED: 0.82,
        PARAM_TEMPERATURE: 0.65,
        PARAM_TOP_K: 8,
        PARAM_TOP_P: 0.70,
        PARAM_FRAGMENT_INTERVAL: 0.42,
    },
    "angry": {
        PARAM_SPEED: 1.22,
        PARAM_TEMPERATURE: 0.90,
        PARAM_TOP_K: 12,
        PARAM_TOP_P: 0.85,
        PARAM_FRAGMENT_INTERVAL: 0.16,
    },
    "cool": {
        PARAM_SPEED: 0.94,
        PARAM_TEMPERATURE: 0.68,
        PARAM_TOP_K: 10,
        PARAM_TOP_P: 0.72,
        PARAM_FRAGMENT_INTERVAL: 0.28,
    },
}

# Emotion name normalization mapping
_EMO_NORM_MAP: dict[str, str] = {
    "gentle": "gentle", "温柔": "gentle", "温和": "gentle", "柔和": "gentle", "微笑": "gentle",
    "happy": "happy", "开心": "happy", "高兴": "happy", "喜悦": "happy", "兴奋": "happy",
    "tsundere": "tsundere", "傲娇": "tsundere", "ツンデレ": "tsundere", "娇蛮": "tsundere",
    "shy": "shy", "害羞": "shy", "羞怯": "shy", "脸红": "shy", "照れ": "shy",
    "sad": "sad", "难过": "sad", "伤心": "sad", "悲伤": "sad", "哭泣": "sad", "沮丧": "sad",
    "angry": "angry", "生气": "angry", "愤怒": "angry", "怒": "angry", "恼怒": "angry",
    "cool": "cool", "高冷": "cool", "冷淡": "cool", "冷静": "cool", "冷漠": "cool",
}


# Pre-compiled prosody regex patterns
_RE_HESITATION = re.compile(r'[…\.]{2,}|[〜~～]')
_RE_EXCLAMATION = re.compile(r'[！!]+')
_RE_QUESTION = re.compile(r'[？\?]+')
_RE_VOCAL_CHARS = re.compile(r'[\w\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]')
_RE_STUTTER_REPEAT = re.compile(r'([^\s、，,]{1,2})[、，,]\1')
_RE_STUTTER_START = re.compile(r'^[あえそな][、，,]')


# ============================================================================
# 3. Contextual Micro-Prosody & Adaptive Acoustics Calculator
# ============================================================================

def _apply_text_prosody_modulations(
    cleaned_text: str,
    speed: float,
    temperature: float,
    top_k: int,
    top_p: float,
    frag_interval: float,
    base_opts: dict[str, Any],
) -> tuple[float, float, int, float, float]:
    """Applies sentence-level micro-prosody cues (hesitation, exclamation, question, length, stutter)."""
    has_custom_speed = PARAM_SPEED in base_opts or "speed_factor" in base_opts
    has_custom_temp = PARAM_TEMPERATURE in base_opts or "temp" in base_opts
    has_custom_top_k = PARAM_TOP_K in base_opts
    has_custom_top_p = PARAM_TOP_P in base_opts
    has_custom_frag = PARAM_FRAGMENT_INTERVAL in base_opts

    # A. Trailing / embedded ellipsis, wave dashes, hesitation: '…', '...', '〜', '~'
    if _RE_HESITATION.search(cleaned_text):
        if not has_custom_speed:
            speed -= 0.04
        if not has_custom_temp:
            temperature -= 0.03
        if not has_custom_top_p:
            top_p -= 0.04
        if not has_custom_frag:
            frag_interval += 0.06

    # B. Strong exclamations: '！', '!', '!?', '！？'
    if _RE_EXCLAMATION.search(cleaned_text):
        if not has_custom_speed:
            speed += 0.05
        if not has_custom_temp:
            temperature += 0.04
        if not has_custom_top_k:
            top_k += 3
        if not has_custom_frag:
            frag_interval -= 0.04

    # C. Interrogative intonation: '？', '?'
    if _RE_QUESTION.search(cleaned_text):
        if not has_custom_top_k:
            top_k += 2
        if not has_custom_temp:
            temperature += 0.02

    # D. Utterance length modulation
    vocal_chars_count = len(_RE_VOCAL_CHARS.findall(cleaned_text))
    if vocal_chars_count > 0:
        if vocal_chars_count < 6 and not has_custom_speed and speed > 0.90:
            speed -= 0.05
        elif vocal_chars_count > 45 and not has_custom_speed and speed < 1.15:
            speed += 0.03

    # E. Stutter / Repetition detection (e.g. 'べ、別に', 'あ、あの', 'そ、そんな')
    if _RE_STUTTER_REPEAT.search(cleaned_text) or _RE_STUTTER_START.search(cleaned_text):
        if not has_custom_temp:
            temperature += 0.04
        if not has_custom_speed:
            speed += 0.03

    return speed, temperature, top_k, top_p, frag_interval


def calculate_adaptive_prosody(
    text: str,
    emotion: str | None = None,
    base_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Computes fine-grained voice synthesis parameters based on emotion archetype
    and sentence-level micro-prosody cues (punctuation, utterance length, stuttering).

    Priority:
      1. Explicit fields in base_params (user/manifest overrides)
      2. Emotion archetype baseline from EMOTION_PROSODY_MATRIX
      3. Contextual adjustments based on text semantics
      4. Safe boundary clamping for all values
    """
    raw_emo = (emotion or "").strip().lower()
    norm_emo = _EMO_NORM_MAP.get(raw_emo, raw_emo if raw_emo in EMOTION_PROSODY_MATRIX else "gentle")
    archetype = EMOTION_PROSODY_MATRIX.get(norm_emo, EMOTION_PROSODY_MATRIX["gentle"])

    base_opts = dict(base_params or {})

    # Start with baseline archetype or explicit base values
    speed = float(base_opts.get(PARAM_SPEED, base_opts.get("speed_factor", archetype[PARAM_SPEED])))
    temperature = float(base_opts.get(PARAM_TEMPERATURE, base_opts.get("temp", archetype[PARAM_TEMPERATURE])))
    top_k = int(base_opts.get(PARAM_TOP_K, archetype[PARAM_TOP_K]))
    top_p = float(base_opts.get(PARAM_TOP_P, archetype[PARAM_TOP_P]))
    frag_interval = float(base_opts.get(PARAM_FRAGMENT_INTERVAL, archetype[PARAM_FRAGMENT_INTERVAL]))

    cleaned_text = (text or "").strip()
    if cleaned_text:
        speed, temperature, top_k, top_p, frag_interval = _apply_text_prosody_modulations(
            cleaned_text, speed, temperature, top_k, top_p, frag_interval, base_opts
        )

    # Safely clamp all results
    final_speed = clamp_dynamic_speed(speed)
    final_temp = clamp_dynamic_temperature(temperature)
    final_top_k = clamp_dynamic_top_k(top_k)
    final_top_p = clamp_dynamic_top_p(top_p)
    final_frag = clamp_dynamic_fragment_interval(frag_interval)

    return {
        PARAM_SPEED: final_speed,
        "speed_factor": final_speed,
        PARAM_TEMPERATURE: final_temp,
        "temp": final_temp,
        PARAM_TOP_K: final_top_k,
        PARAM_TOP_P: final_top_p,
        PARAM_FRAGMENT_INTERVAL: final_frag,
        "emotion": norm_emo,
    }


__all__ = [
    "DYNAMIC_SPEED_MIN",
    "DYNAMIC_SPEED_MAX",
    "DYNAMIC_TEMP_MIN",
    "DYNAMIC_TEMP_MAX",
    "DYNAMIC_TOP_K_MIN",
    "DYNAMIC_TOP_K_MAX",
    "DYNAMIC_TOP_P_MIN",
    "DYNAMIC_TOP_P_MAX",
    "DYNAMIC_FRAGMENT_INTERVAL_MIN",
    "DYNAMIC_FRAGMENT_INTERVAL_MAX",
    "DYNAMIC_BATCH_SIZE_MIN",
    "DYNAMIC_BATCH_SIZE_MAX",
    "clamp_dynamic_speed",
    "clamp_dynamic_temperature",
    "clamp_dynamic_top_k",
    "clamp_dynamic_top_p",
    "clamp_dynamic_fragment_interval",
    "clamp_dynamic_batch_size",
    "EMOTION_PROSODY_MATRIX",
    "calculate_adaptive_prosody",
]
