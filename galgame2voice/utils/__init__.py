"""
Utilities module for galgame2voice.
"""

from galgame2voice.utils.logger import (
    MaskingFilter,
    MaskingFormatter,
    sanitize_error_detail,
    setup_logger,
)
from galgame2voice.utils.text_splitter import (
    split_japanese_sentences,
    normalize_dialogue_prosody,
    MODAL_PARTICLES_PATTERN,
    GREETING_PREFIX_PATTERN,
    is_natural_clause_boundary,
)
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
    get_system_memory_status,
)
from galgame2voice.utils.precision import (
    read_precision_cache,
    resolve_initial_is_half,
    write_precision_cache,
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
from galgame2voice.utils.japanese_phonetics import (
    normalize_japanese_furigana,
    normalize_galgame_names_and_readings,
    normalize_japanese_for_tts,
    clean_japanese_parentheses,
    extract_stage_directions_and_emotion,
)
from galgame2voice.utils.audio_spec import (
    REFERENCE_AUDIO_MIN_SECONDS,
    REFERENCE_AUDIO_MAX_SECONDS,
    AudioSpec,
    AudioSpecCache,
    _AUDIO_SPEC_CACHE,
    probe_audio_spec,
    probe_audio_duration_seconds,
    async_probe_audio_duration_seconds,
    validate_reference_audio,
    resolve_reference_audio_path,
    extract_wav_duration,
)

__all__ = [
    "MaskingFilter",
    "MaskingFormatter",
    "sanitize_error_detail",
    "setup_logger",
    "split_japanese_sentences",
    "normalize_dialogue_prosody",
    "MODAL_PARTICLES_PATTERN",
    "GREETING_PREFIX_PATTERN",
    "is_natural_clause_boundary",
    "DYNAMIC_SPEED_MIN",
    "DYNAMIC_SPEED_MAX",
    "DYNAMIC_TEMP_MIN",
    "DYNAMIC_TEMP_MAX",
    "clamp_dynamic_speed",
    "clamp_dynamic_temperature",
    "detect_gpu_capability",
    "get_system_memory_status",
    "read_precision_cache",
    "resolve_initial_is_half",
    "write_precision_cache",
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
    "normalize_japanese_furigana",
    "normalize_galgame_names_and_readings",
    "normalize_japanese_for_tts",
    "clean_japanese_parentheses",
    "extract_stage_directions_and_emotion",
    "REFERENCE_AUDIO_MIN_SECONDS",
    "REFERENCE_AUDIO_MAX_SECONDS",
    "AudioSpec",
    "AudioSpecCache",
    "_AUDIO_SPEC_CACHE",
    "probe_audio_spec",
    "probe_audio_duration_seconds",
    "async_probe_audio_duration_seconds",
    "validate_reference_audio",
    "resolve_reference_audio_path",
    "extract_wav_duration",
]

