"""
Dynamic Emotional Reference Audio Mapper for Galgame2Voice.

Data-driven emotion resolution querying active/discovered Character Packages via CharacterManager.
Provides seamless backward compatibility for legacy callers.
"""

from pathlib import Path
import logging

logger = logging.getLogger("galgame2voice.services.emotion_references")

# Emotion synonym normalizer (maps emotional keywords and Japanese/Chinese terms to canonical archetypes)
EMOTION_SYNONYMS: dict[str, str] = {
    "happy": "happy",
    "cheerful": "happy",
    "joy": "happy",
    "excited": "happy",
    "laugh": "happy",
    "fun": "happy",
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
    "anger": "angry",
    "生气": "angry",
    "愤怒": "angry",
    "气愤": "angry",
    "怒": "angry",
    "怒り": "angry",
    "怒る": "angry",
    "おこ": "angry",
    "shy": "shy",
    "shyness": "shy",
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

def normalize_emotion(emotion: str | None) -> str:
    """Normalizes an emotion string to one of the canonical archetypes."""
    if not emotion:
        return "gentle"
    cleaned = str(emotion).strip().lower()
    return EMOTION_SYNONYMS.get(cleaned, "gentle")


def resolve_emotion_reference(
    character_name: str | None,
    emotion: str | None,
    base_dir: Path | None = None,
) -> dict[str, str] | None:
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
        target = character_name if character_name else "default"
        return mgr.resolve_emotion_audio_path(target, emotion, base_dir=base_dir)
    except Exception as exc:
        logger.debug("Data-driven emotion resolution encountered exception: %s", exc)
        return None
