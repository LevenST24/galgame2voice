"""
Voice prosody, emotion parameter clamping, and fine-grained adaptive acoustics utilities.
Provides deterministic acoustic baselines for 7 standard emotion archetypes,
contextual micro-prosody modulation based on punctuation and utterance length,
and strict safety boundary clamping across all speech inference parameters.
"""

import math
import re
from typing import Any, Dict, Optional

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

# ============================================================================
# 1. Clamping Functions
# ============================================================================

def clamp_dynamic_speed(val: Any, fallback: float = 1.0) -> float:
    """Clamps dynamic voice inference speed into [0.50, 1.50]. Falls back if invalid."""
    try:
        num = float(val)
        if math.isnan(num) or math.isinf(num):
            return fallback
        return max(DYNAMIC_SPEED_MIN, min(DYNAMIC_SPEED_MAX, round(num, 4)))
    except (TypeError, ValueError):
        return fallback


def clamp_dynamic_temperature(val: Any, fallback: float = 1.0) -> float:
    """Clamps dynamic voice inference temperature into [0.60, 1.20]. Falls back if invalid."""
    try:
        num = float(val)
        if math.isnan(num) or math.isinf(num):
            return fallback
        return max(DYNAMIC_TEMP_MIN, min(DYNAMIC_TEMP_MAX, round(num, 4)))
    except (TypeError, ValueError):
        return fallback


def clamp_dynamic_top_k(val: Any, fallback: int = 15) -> int:
    """Clamps dynamic Top-K sampling parameter into [1, 50]. Falls back if invalid."""
    try:
        num = int(val)
        return max(DYNAMIC_TOP_K_MIN, min(DYNAMIC_TOP_K_MAX, num))
    except (TypeError, ValueError):
        return fallback


def clamp_dynamic_top_p(val: Any, fallback: float = 1.0) -> float:
    """Clamps dynamic Top-P nucleus sampling parameter into [0.50, 1.00]. Falls back if invalid."""
    try:
        num = float(val)
        if math.isnan(num) or math.isinf(num):
            return fallback
        return max(DYNAMIC_TOP_P_MIN, min(DYNAMIC_TOP_P_MAX, round(num, 4)))
    except (TypeError, ValueError):
        return fallback


def clamp_dynamic_fragment_interval(val: Any, fallback: float = 0.3) -> float:
    """Clamps inter-sentence pause fragment interval into [0.10, 1.00]. Falls back if invalid."""
    try:
        num = float(val)
        if math.isnan(num) or math.isinf(num):
            return fallback
        return max(DYNAMIC_FRAGMENT_INTERVAL_MIN, min(DYNAMIC_FRAGMENT_INTERVAL_MAX, round(num, 4)))
    except (TypeError, ValueError):
        return fallback


def clamp_dynamic_batch_size(val: Any, fallback: int = 1) -> int:
    """Clamps inference batch size into [1, 16]. Falls back if invalid."""
    try:
        num = int(val)
        return max(DYNAMIC_BATCH_SIZE_MIN, min(DYNAMIC_BATCH_SIZE_MAX, num))
    except (TypeError, ValueError):
        return fallback


# ============================================================================
# 2. Emotion Archetype Baseline Acoustic Profiles
# ============================================================================

EMOTION_PROSODY_MATRIX: Dict[str, Dict[str, Any]] = {
    "gentle": {
        "speed": 0.98,
        "temperature": 0.78,
        "top_k": 15,
        "top_p": 0.85,
        "fragment_interval": 0.32,
    },
    "happy": {
        "speed": 1.12,
        "temperature": 0.95,
        "top_k": 20,
        "top_p": 0.95,
        "fragment_interval": 0.22,
    },
    "tsundere": {
        "speed": 1.15,
        "temperature": 0.92,
        "top_k": 14,
        "top_p": 0.88,
        "fragment_interval": 0.20,
    },
    "shy": {
        "speed": 0.88,
        "temperature": 0.72,
        "top_k": 10,
        "top_p": 0.78,
        "fragment_interval": 0.38,
    },
    "sad": {
        "speed": 0.82,
        "temperature": 0.65,
        "top_k": 8,
        "top_p": 0.70,
        "fragment_interval": 0.42,
    },
    "angry": {
        "speed": 1.22,
        "temperature": 0.90,
        "top_k": 12,
        "top_p": 0.85,
        "fragment_interval": 0.16,
    },
    "cool": {
        "speed": 0.94,
        "temperature": 0.68,
        "top_k": 10,
        "top_p": 0.72,
        "fragment_interval": 0.28,
    },
}

# Emotion name normalization mapping
_EMO_NORM_MAP: Dict[str, str] = {
    "gentle": "gentle", "温柔": "gentle", "温和": "gentle", "柔和": "gentle", "微笑": "gentle",
    "happy": "happy", "开心": "happy", "高兴": "happy", "喜悦": "happy", "兴奋": "happy",
    "tsundere": "tsundere", "傲娇": "tsundere", "ツンデレ": "tsundere", "娇蛮": "tsundere",
    "shy": "shy", "害羞": "shy", "羞怯": "shy", "脸红": "shy", "照れ": "shy",
    "sad": "sad", "难过": "sad", "伤心": "sad", "悲伤": "sad", "哭泣": "sad", "沮丧": "sad",
    "angry": "angry", "生气": "angry", "愤怒": "angry", "怒": "angry", "恼怒": "angry",
    "cool": "cool", "高冷": "cool", "冷淡": "cool", "冷静": "cool", "冷漠": "cool",
}


# ============================================================================
# 3. Contextual Micro-Prosody & Adaptive Acoustics Calculator
# ============================================================================

def calculate_adaptive_prosody(
    text: str,
    emotion: Optional[str] = None,
    base_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
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
    speed = float(base_opts.get("speed", base_opts.get("speed_factor", archetype["speed"])))
    temperature = float(base_opts.get("temperature", base_opts.get("temp", archetype["temperature"])))
    top_k = int(base_opts.get("top_k", archetype["top_k"]))
    top_p = float(base_opts.get("top_p", archetype["top_p"]))
    frag_interval = float(base_opts.get("fragment_interval", archetype["fragment_interval"]))

    cleaned_text = (text or "").strip()

    # Apply contextual micro-prosody if user didn't explicitly lock every parameter
    has_custom_speed = "speed" in base_opts or "speed_factor" in base_opts
    has_custom_temp = "temperature" in base_opts or "temp" in base_opts
    has_custom_top_k = "top_k" in base_opts
    has_custom_top_p = "top_p" in base_opts
    has_custom_frag = "fragment_interval" in base_opts

    if cleaned_text:
        # A. Trailing / embedded ellipsis, wave dashes, hesitation: '…', '...', '〜', '~'
        has_trailing_hesitation = bool(re.search(r'[…\.]{2,}|[〜~～]', cleaned_text))
        if has_trailing_hesitation:
            if not has_custom_speed:
                speed -= 0.04
            if not has_custom_temp:
                temperature -= 0.03
            if not has_custom_top_p:
                top_p -= 0.04
            if not has_custom_frag:
                frag_interval += 0.06

        # B. Strong exclamations: '！', '!', '!?', '！？'
        has_exclamation = bool(re.search(r'[！!]+', cleaned_text))
        if has_exclamation:
            if not has_custom_speed:
                speed += 0.05
            if not has_custom_temp:
                temperature += 0.04
            if not has_custom_top_k:
                top_k += 3
            if not has_custom_frag:
                frag_interval -= 0.04

        # C. Interrogative intonation: '？', '?'
        has_question = bool(re.search(r'[？\?]+', cleaned_text))
        if has_question:
            if not has_custom_top_k:
                top_k += 2
            if not has_custom_temp:
                temperature += 0.02

        # D. Utterance length modulation
        vocal_chars_count = len(re.findall(r'[\w\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]', cleaned_text))
        if vocal_chars_count > 0:
            if vocal_chars_count < 6 and not has_custom_speed and speed > 0.90:
                # Very short interjection or single word (e.g., 'バカ...', 'うん', 'えっ')
                # Moderately decelerate to prevent rushed audio and ensure clear articulation
                speed -= 0.05
            elif vocal_chars_count > 45 and not has_custom_speed and speed < 1.15:
                # Extra-long monologue: slight acceleration to avoid sluggish delivery
                speed += 0.03

        # E. Stutter / Repetition detection (e.g. 'べ、別に', 'あ、あの', 'そ、そんな')
        has_stutter = bool(re.search(r'([^\s、，,]{1,2})[、，,]\1', cleaned_text) or re.search(r'^[あえそな][、，,]', cleaned_text))
        if has_stutter:
            if not has_custom_temp:
                temperature += 0.04
            if not has_custom_speed:
                speed += 0.03

    # Safely clamp all results
    final_speed = clamp_dynamic_speed(speed)
    final_temp = clamp_dynamic_temperature(temperature)
    final_top_k = clamp_dynamic_top_k(top_k)
    final_top_p = clamp_dynamic_top_p(top_p)
    final_frag = clamp_dynamic_fragment_interval(frag_interval)

    return {
        "speed": final_speed,
        "speed_factor": final_speed,
        "temperature": final_temp,
        "temp": final_temp,
        "top_k": final_top_k,
        "top_p": final_top_p,
        "fragment_interval": final_frag,
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
