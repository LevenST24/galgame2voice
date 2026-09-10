"""
Dynamic Emotional Reference Audio Mapper for Galgame2Voice.

Data-driven emotion resolution querying active/discovered Character Packages via CharacterManager.
Provides seamless backward compatibility for legacy callers.
"""

from pathlib import Path
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger("galgame2voice.services.emotion_references")

# Emotion synonym normalizer (maps emotional keywords and Japanese/Chinese terms to canonical archetypes)
EMOTION_SYNONYMS: Dict[str, str] = {
    "happy": "happy",
    "cheerful": "happy",
    "joy": "happy",
    "excited": "happy",
    "laugh": "happy",
    "开心": "happy",
    "喜悦": "happy",
    "高兴": "happy",
    "sad": "sad",
    "sorrow": "sad",
    "lonely": "sad",
    "depressed": "sad",
    "伤心": "sad",
    "悲伤": "sad",
    "难过": "sad",
    "失落": "sad",
    "tsundere": "tsundere",
    "angry": "tsundere",
    "pouty": "tsundere",
    "傲娇": "tsundere",
    "生气": "tsundere",
    "害羞傲娇": "tsundere",
    "shy": "shy",
    "embarrassed": "shy",
    "blushing": "shy",
    "害羞": "shy",
    "羞涩": "shy",
    "cool": "cool",
    "cold": "cool",
    "indifferent": "cool",
    "kuudere": "cool",
    "高冷": "cool",
    "冷淡": "cool",
    "gentle": "gentle",
    "calm": "gentle",
    "normal": "gentle",
    "温柔": "gentle",
    "平稳": "gentle",
}

# Dynamic data-driven emotion references proxy
def _load_manifest_emotion_references(character_name: str = "四季夏目") -> Dict[str, Dict[str, str]]:
    """Dynamically extracts emotion references dictionary from character package manifest."""
    try:
        from galgame2voice.services.character_manager import get_character_manager
        mgr = get_character_manager()
        pkg = mgr.get_character(character_name)
        if pkg and pkg.manifest and pkg.manifest.emotions:
            res: Dict[str, Dict[str, str]] = {}
            for k, emo in pkg.manifest.emotions.items():
                res[k] = {
                    "audio_name": Path(emo.audio).name,
                    "prompt_text": emo.text,
                    "prompt_lang": emo.lang,
                    "description": emo.description or f"Dynamic {k} emotion",
                }
            return res
    except Exception:
        pass
    return {}


class _DynamicEmotionReferences(dict):
    """
    Data-driven dictionary proxy that reflects the character package manifest dynamically
    while maintaining 100% dictionary backward compatibility for legacy callers and tests.
    """
    def __init__(self):
        super().__init__()
        self._loaded = False

    def _ensure_loaded(self):
        if not self._loaded:
            data = _load_manifest_emotion_references("四季夏目")
            if data:
                self.update(data)
                self._loaded = True

    def __getitem__(self, item):
        self._ensure_loaded()
        if item not in self:
            return super().get("gentle", {})
        return super().__getitem__(item)

    def get(self, item, default=None):
        self._ensure_loaded()
        return super().get(item, default)

    def __contains__(self, item):
        self._ensure_loaded()
        return super().__contains__(item)

    def __iter__(self):
        self._ensure_loaded()
        return super().__iter__()

    def __len__(self):
        self._ensure_loaded()
        return super().__len__()

    def items(self):
        self._ensure_loaded()
        return super().items()

    def keys(self):
        self._ensure_loaded()
        return super().keys()

    def values(self):
        self._ensure_loaded()
        return super().values()


NATSUME_EMOTION_REFERENCES: Dict[str, Dict[str, str]] = _DynamicEmotionReferences()


def normalize_emotion(emotion: Optional[str]) -> str:
    """Normalizes an emotion string to one of the canonical archetypes."""
    if not emotion:
        return "gentle"
    cleaned = str(emotion).strip().lower()
    return EMOTION_SYNONYMS.get(cleaned, "gentle")


def resolve_emotion_reference(
    character_name: str,
    emotion: Optional[str],
    base_dir: Optional[Path] = None,
) -> Optional[Dict[str, str]]:
    """
    Data-driven resolution of emotion reference audio file path, prompt text, and prompt lang.
    Queries the CharacterManager for the active/named character package manifest.
    Falls back gracefully if the emotion is missing or audio is invalid.

    Returns:
        {
            'ref_audio_path': str,
            'prompt_text': str,
            'prompt_lang': str,
            'emotion': str,
        }
        or None if no matching character/reference audio is found.
    """
    try:
        from galgame2voice.services.character_manager import get_character_manager
        mgr = get_character_manager()
        result = mgr.resolve_emotion_audio_path(character_name, emotion, base_dir=base_dir)
        if result is not None:
            return result

        # Backward compatibility fallback: check if character is Shiki Natsume or default
        is_natsume = (
            not character_name
            or "夏目" in character_name
            or "natsume" in character_name.lower()
            or "siki" in character_name.lower()
            or character_name.strip().lower() == "default"
        )
        if not is_natsume:
            return None

        return mgr.resolve_emotion_audio_path("default", emotion, base_dir=base_dir)
    except Exception as exc:
        logger.debug("Data-driven emotion resolution encountered exception: %s", exc)
        return None
