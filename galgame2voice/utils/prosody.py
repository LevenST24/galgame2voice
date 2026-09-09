"""
Voice prosody and emotion parameter clamping utilities.
"""

import math
from typing import Any

# Dynamic AI-Driven Voice Prosody & Emotion Constants
DYNAMIC_SPEED_MIN = 0.50
DYNAMIC_SPEED_MAX = 1.50
DYNAMIC_TEMP_MIN = 0.60
DYNAMIC_TEMP_MAX = 1.20


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


__all__ = [
    "DYNAMIC_SPEED_MIN",
    "DYNAMIC_SPEED_MAX",
    "DYNAMIC_TEMP_MIN",
    "DYNAMIC_TEMP_MAX",
    "clamp_dynamic_speed",
    "clamp_dynamic_temperature",
]
