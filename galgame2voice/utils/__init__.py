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
from galgame2voice.utils.path_guard import (
    PathTraversalError,
    is_windows_device_name,
    contains_traversal_payload,
    is_safe_filename,
    get_authorized_roots,
    validate_path_containment,
    is_path_safe,
    safe_resolve_audio_path,
    validate_voice_profile_paths,
)
from galgame2voice.utils.audio_converter import (
    find_ffmpeg,
    reset_ffmpeg_cache,
    is_ffmpeg_available,
    is_target_wav_pcm,
    run_ffmpeg_command,
    convert_ogg_to_wav,
    convert_wav_to_ogg,
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
    "PathTraversalError",
    "is_windows_device_name",
    "contains_traversal_payload",
    "is_safe_filename",
    "get_authorized_roots",
    "validate_path_containment",
    "is_path_safe",
    "safe_resolve_audio_path",
    "validate_voice_profile_paths",
    "find_ffmpeg",
    "reset_ffmpeg_cache",
    "is_ffmpeg_available",
    "is_target_wav_pcm",
    "run_ffmpeg_command",
    "convert_ogg_to_wav",
    "convert_wav_to_ogg",
]
