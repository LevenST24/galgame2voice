"""
Character Package Manifest Schema v2 for Galgame2Voice.
Defines formal JSON schema validation for character packages, emotion audio definitions,
and package integrity verification.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from galgame2voice.services.tts_service import TtsService
from galgame2voice.utils.audio_spec import (
    REFERENCE_AUDIO_MAX_SECONDS,
    REFERENCE_AUDIO_MIN_SECONDS,
)

# A work id is the background pack folder name; characters declare which work they belong to.
CHARACTER_WORK_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def clean_character_work(value: object) -> str | None:
    """Return the work id when it matches the pack pattern, else None (absent)."""
    if isinstance(value, str) and CHARACTER_WORK_RE.fullmatch(value):
        return value
    return None


class VoiceParamsConfig(BaseModel):
    """Default voice generation parameters for a character."""
    speed: float = Field(default=1.0, ge=0.1, le=3.0, description="Default speech speed factor")
    temperature: float = Field(default=0.8, ge=0.0, le=2.0, description="Default sampling temperature")
    top_k: int | None = Field(default=15, ge=1, le=100, description="Top-k sampling parameter")
    top_p: float | None = Field(default=1.0, ge=0.0, le=1.0, description="Top-p sampling parameter")


class EmotionConfig(BaseModel):
    """Reference audio definition and metadata for a specific emotion."""
    audio: str = Field(..., min_length=1, description="Relative path to reference audio, e.g., 'refs/gentle.ogg'")
    text: str = Field(..., min_length=1, description="Transcript of the reference audio")
    lang: str = Field(default="ja", description="Language code of reference audio (ja, zh, en)")
    description: str | None = Field(default=None, description="Human-readable description of this emotion")
    voice_params: VoiceParamsConfig | None = Field(default=None, description="Optional custom voice parameters for this emotion")


class CharacterManifest(BaseModel):
    """Validated schema for character package manifest.json."""
    id: str = Field(..., min_length=1, max_length=100, description="Unique machine-readable character identifier")
    name: str = Field(..., min_length=1, max_length=100, description="Display name of the character")
    version: str = Field(default="1.0.0", description="Character package semantic version")
    description: str = Field(default="", description="Character background or lore description")
    system_prompt: str | None = Field(default="", description="Personality prompt template")
    default_voice_params: VoiceParamsConfig = Field(default_factory=VoiceParamsConfig)
    gpt_weights: str | None = Field(default=None, description="Path or pointer to GPT model weights")
    sovits_weights: str | None = Field(default=None, description="Path or pointer to SoVITS model weights")
    emotions: dict[str, EmotionConfig] = Field(default_factory=dict, description="Emotion to reference audio mapping")
    is_default: bool = Field(default=False, description="Whether this character is the default character")
    aliases: list[str] = Field(default_factory=list, description="Optional alternate names or aliases for matching")
    portrait: dict[str, Any] | None = Field(default=None, description="Optional portrait metadata")
    # Cleaned on read (clean_character_work): a bad optional value must not invalidate the package.
    work: str | None = Field(default=None, description="Background work/pack this character belongs to")


class CharacterManifestV2(CharacterManifest):
    """
    Formal Character Package Manifest Schema v2.
    Fully backward-compatible with v1 manifests while supporting schema versioning,
    enhanced voice parameters, and strict reference audio constraints.
    """
    manifest_version: str = Field(default="2.0", description="Manifest schema version (e.g. 2.0)")
    default_emotion: str | None = Field(default="gentle", description="Default emotion identifier")


def manifest_json_error(exc: Exception) -> str:
    return f"manifest.json JSON 解析错误: {exc}"


def manifest_schema_error(exc: Exception) -> str:
    return f"manifest.json 结构验证失败: {exc}"


def no_emotions_error() -> str:
    return "未定义任何 emotions 情感参考音频配置"


def emotion_audio_missing_error(emotion: str, audio: str) -> str:
    return f"情感 '{emotion}' 的参考音频不存在: {audio}"


def emotion_audio_unreadable_error(emotion: str, audio: str) -> str:
    return f"情感 '{emotion}' 的音频文件无法读取或时长未知: {audio}"


def emotion_audio_duration_error(emotion: str, duration: float, audio: str) -> str:
    return (
        f"情感 '{emotion}' 的参考音频时长 {duration:.2f} 秒，不在 "
        f"{REFERENCE_AUDIO_MIN_SECONDS:g} 到 {REFERENCE_AUDIO_MAX_SECONDS:g} 秒范围内: {audio}"
    )


def validate_character_package(character_dir: Path) -> tuple[bool, list[str]]:
    """
    Validates a character package directory on disk:
    1. Checks for presence of manifest.json
    2. Parses and validates JSON against CharacterManifestV2
    3. Verifies every emotion reference audio exists on disk
    4. Probes audio duration and verifies it lies inside the inclusive [3.0s, 10.0s] range
    Returns (is_valid, list_of_errors).
    """
    errors: list[str] = []
    char_dir = Path(character_dir).resolve()
    if not char_dir.is_dir():
        return False, [f"目录不存在: {char_dir}"]

    manifest_path = char_dir / "manifest.json"
    if not manifest_path.is_file():
        return False, [f"缺少 manifest.json 配置文件: {manifest_path}"]

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [manifest_json_error(exc)]

    try:
        manifest = CharacterManifestV2.model_validate(data)
    except Exception as exc:
        return False, [manifest_schema_error(exc)]

    if not manifest.emotions:
        errors.append(no_emotions_error())

    for emo_key, emo in manifest.emotions.items():
        ref_path = char_dir / emo.audio
        if not ref_path.is_file():
            errors.append(emotion_audio_missing_error(emo_key, emo.audio))
            continue

        try:
            duration = TtsService.get_audio_duration(ref_path)
        except Exception:
            duration = None
        if duration is None or duration <= 0.0:
            errors.append(emotion_audio_unreadable_error(emo_key, emo.audio))
        elif not (REFERENCE_AUDIO_MIN_SECONDS <= duration <= REFERENCE_AUDIO_MAX_SECONDS):
            errors.append(emotion_audio_duration_error(emo_key, duration, emo.audio))

    return not errors, errors


__all__ = [
    "VoiceParamsConfig",
    "EmotionConfig",
    "CharacterManifest",
    "CharacterManifestV2",
    "CHARACTER_WORK_RE",
    "clean_character_work",
    "emotion_audio_duration_error",
    "emotion_audio_missing_error",
    "emotion_audio_unreadable_error",
    "manifest_json_error",
    "manifest_schema_error",
    "no_emotions_error",
    "validate_character_package",
]
