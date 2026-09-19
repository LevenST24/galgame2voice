"""
Galgame2Voice schemas package.
"""

from galgame2voice.schemas.character_manifest import (
    CharacterManifest,
    CharacterManifestV2,
    EmotionConfig,
    VoiceParamsConfig,
    validate_character_package,
)

__all__ = [
    "CharacterManifest",
    "CharacterManifestV2",
    "EmotionConfig",
    "VoiceParamsConfig",
    "validate_character_package",
]
