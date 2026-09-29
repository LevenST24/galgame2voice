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
    "嬉しい": "happy",
    "楽しい": "happy",
    "うれしい": "happy",
    "たのしい": "happy",
    "sad": "sad",
    "sorrow": "sad",
    "lonely": "sad",
    "depressed": "sad",
    "伤心": "sad",
    "悲伤": "sad",
    "难过": "sad",
    "失落": "sad",
    "悲しい": "sad",
    "寂しい": "sad",
    "さびしい": "sad",
    "かなしい": "sad",
    "tsundere": "tsundere",
    "pouty": "tsundere",
    "傲娇": "tsundere",
    "ツンデレ": "tsundere",
    "害羞傲娇": "tsundere",
    "angry": "angry",
    "生气": "angry",
    "愤怒": "angry",
    "气愤": "angry",
    "怒": "angry",
    "怒り": "angry",
    "怒る": "angry",
    "おこ": "angry",
    "shy": "shy",
    "embarrassed": "shy",
    "blushing": "shy",
    "害羞": "shy",
    "羞涩": "shy",
    "照れ": "shy",
    "恥ずかしい": "shy",
    "てれ": "shy",
    "cool": "cool",
    "cold": "cool",
    "indifferent": "cool",
    "kuudere": "cool",
    "高冷": "cool",
    "冷淡": "cool",
    "クール": "cool",
    "gentle": "gentle",
    "calm": "gentle",
    "normal": "gentle",
    "温柔": "gentle",
    "平稳": "gentle",
    "優しい": "gentle",
    "やさしい": "gentle",
}

# Dynamic data-driven emotion references proxy
def _load_manifest_emotion_references(
    character_name: Optional[str] = None,
    char_mgr: Optional[Any] = None,
) -> Dict[str, Dict[str, str]]:
    """
    Dynamically loads emotion references from the character package manifest.json.
    """
    try:
        from galgame2voice.services.character_manager import get_character_manager
        mgr = char_mgr or get_character_manager()
        pkg = mgr.get_character(character_name) if character_name else mgr.get_default_character()
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
    except Exception as exc:
        logger.debug("Failed dynamically loading emotion references from manifest: %s", exc)
    return {}


class _DynamicEmotionReferences(dict):
    """
    Data-driven dictionary proxy that reflects the character package manifest dynamically
    while maintaining 100% dictionary backward compatibility for legacy callers and tests.
    """
    def __init__(self) -> None:
        super().__init__()
        self._loaded: bool = False

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            data = _load_manifest_emotion_references()
            if data:
                self.update(data)
                self._loaded = True

    def __getitem__(self, item: str) -> Dict[str, str]:
        self._ensure_loaded()
        if item not in self:
            return super().get("gentle", {})
        return super().__getitem__(item)

    def get(self, item: str, default: Any = None) -> Any:
        self._ensure_loaded()
        return super().get(item, default)

    def __contains__(self, item: object) -> bool:
        self._ensure_loaded()
        return super().__contains__(item)

    def __iter__(self) -> Any:
        self._ensure_loaded()
        return super().__iter__()

    def __len__(self) -> int:
        self._ensure_loaded()
        return super().__len__()

    def items(self) -> Any:
        self._ensure_loaded()
        return super().items()

    def keys(self) -> Any:
        self._ensure_loaded()
        return super().keys()

    def values(self) -> Any:
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
    character_name: Optional[str],
    emotion: Optional[str],
    base_dir: Optional[Path] = None,
) -> Optional[Dict[str, str]]:
    """
    Data-driven resolution of emotion reference audio file path, prompt text, and prompt lang.
    Queries the CharacterManager for the active/named character package manifest.
    Falls back to default character package if specified character is not found.

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
        if character_name:
            return mgr.resolve_emotion_audio_path(character_name, emotion, base_dir=base_dir)

        # Fallback to default character package only when no character_name was specified
        return mgr.resolve_emotion_audio_path("default", emotion, base_dir=base_dir)
    except Exception as exc:
        logger.debug("Data-driven emotion resolution encountered exception: %s", exc)
        return None
