"""
Character Package Manifest Schema v2 for Galgame2Voice.
Defines formal JSON schema validation for character packages, emotion audio definitions,
and package integrity verification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from galgame2voice.services.tts_service import TtsService


class VoiceParamsConfig(BaseModel):
    """Default voice generation parameters for a character."""
    speed: float = Field(default=1.0, ge=0.1, le=3.0, description="Default speech speed factor")
    temperature: float = Field(default=0.8, ge=0.0, le=2.0, description="Default sampling temperature")
    top_k: Optional[int] = Field(default=15, ge=1, le=100, description="Top-k sampling parameter")
    top_p: Optional[float] = Field(default=1.0, ge=0.0, le=1.0, description="Top-p sampling parameter")


class EmotionConfig(BaseModel):
    """Reference audio definition and metadata for a specific emotion."""
    audio: str = Field(..., min_length=1, description="Relative path to reference audio, e.g., 'refs/gentle.ogg'")
    text: str = Field(..., min_length=1, description="Transcript of the reference audio")
    lang: str = Field(default="ja", description="Language code of reference audio (ja, zh, en)")
    description: Optional[str] = Field(default=None, description="Human-readable description of this emotion")
    voice_params: Optional[VoiceParamsConfig] = Field(default=None, description="Optional custom voice parameters for this emotion")


class CharacterManifest(BaseModel):
    """Validated schema for character package manifest.json."""
    id: str = Field(..., min_length=1, max_length=100, description="Unique machine-readable character identifier")
    name: str = Field(..., min_length=1, max_length=100, description="Display name of the character")
    version: str = Field(default="1.0.0", description="Character package semantic version")
    description: str = Field(default="", description="Character background or lore description")
    system_prompt: Optional[str] = Field(default="", description="Personality prompt template")
    default_voice_params: VoiceParamsConfig = Field(default_factory=VoiceParamsConfig)
    gpt_weights: Optional[str] = Field(default=None, description="Path or pointer to GPT model weights")
    sovits_weights: Optional[str] = Field(default=None, description="Path or pointer to SoVITS model weights")
    emotions: Dict[str, EmotionConfig] = Field(default_factory=dict, description="Emotion to reference audio mapping")
    is_default: bool = Field(default=False, description="Whether this character is the default character")
    aliases: List[str] = Field(default_factory=list, description="Optional alternate names or aliases for matching")
    portrait: Optional[Dict[str, Any]] = Field(default=None, description="Optional portrait metadata")


class CharacterManifestV2(CharacterManifest):
    """
    Formal Character Package Manifest Schema v2.
    Fully backward-compatible with v1 manifests while supporting schema versioning,
    enhanced voice parameters, and strict reference audio constraints.
    """
    manifest_version: str = Field(default="2.0", description="Manifest schema version (e.g. 2.0)")
    default_emotion: Optional[str] = Field(default="gentle", description="Default emotion identifier")


def validate_character_package(character_dir: Path) -> Tuple[bool, List[str]]:
    """
    Validates a character package directory on disk:
    1. Checks for presence of manifest.json
    2. Parses and validates JSON against CharacterManifestV2
    3. Verifies every emotion reference audio exists on disk
    4. Probes and verifies reference audio duration is strictly within [3.0s, 10.0s]
    Returns (is_valid, list_of_errors).
    """
    errors: List[str] = []
    char_dir = Path(character_dir).resolve()
    if not char_dir.is_dir():
        return False, [f"目录不存在: {char_dir}"]

    manifest_path = char_dir / "manifest.json"
    if not manifest_path.is_file():
        return False, [f"缺少 manifest.json 配置文件: {manifest_path}"]

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"manifest.json JSON 解析错误: {exc}"]

    try:
        manifest = CharacterManifestV2.model_validate(data)
    except Exception as exc:
        return False, [f"manifest.json 结构验证失败: {exc}"]

    if not manifest.emotions:
        errors.append("未定义任何 emotions 情感参考音频配置")

    for emo_key, emo in manifest.emotions.items():
        ref_path = char_dir / emo.audio
        if not ref_path.is_file():
            errors.append(f"情感 '{emo_key}' 的参考音频不存在: {emo.audio}")
            continue

        try:
            duration = TtsService.get_audio_duration(ref_path)
        except Exception:
            duration = None
        if duration is None or duration <= 0.0:
            errors.append(f"情感 '{emo_key}' 的音频文件无法读取或时长未知: {emo.audio}")
        elif duration < 3.0 or duration > 10.0:
            errors.append(
                f"情感 '{emo_key}' 音频时长 ({duration:.2f}s) 超出标准范围 [3.0s, 10.0s]: {emo.audio}"
            )

    return len(errors) == 0, errors


__all__ = [
    "VoiceParamsConfig",
    "EmotionConfig",
    "CharacterManifest",
    "CharacterManifestV2",
    "validate_character_package",
]
