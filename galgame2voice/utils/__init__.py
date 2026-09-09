"""
Utilities module for galgame2voice.
"""

from galgame2voice.utils.logger import (
    MaskingFilter,
    MaskingFormatter,
    sanitize_error_detail,
    setup_logger,
)
from galgame2voice.utils.text_splitter import split_japanese_sentences
from galgame2voice.utils.prosody import (
    DYNAMIC_SPEED_MIN,
    DYNAMIC_SPEED_MAX,
    DYNAMIC_TEMP_MIN,
    DYNAMIC_TEMP_MAX,
    clamp_dynamic_speed,
    clamp_dynamic_temperature,
)
from galgame2voice.utils.hardware import (
    detect_gpu_capability,
    is_turing_tu116_tu117_gpu,
    get_system_memory_status,
)

__all__ = [
    "MaskingFilter",
    "MaskingFormatter",
    "sanitize_error_detail",
    "setup_logger",
    "split_japanese_sentences",
    "DYNAMIC_SPEED_MIN",
    "DYNAMIC_SPEED_MAX",
    "DYNAMIC_TEMP_MIN",
    "DYNAMIC_TEMP_MAX",
    "clamp_dynamic_speed",
    "clamp_dynamic_temperature",
    "detect_gpu_capability",
    "is_turing_tu116_tu117_gpu",
    "get_system_memory_status",
]


