"""
Dynamic Emotional Reference Audio Mapper for Galgame2Voice.
Maps AI-classified emotions (gentle, happy, sad, tsundere, shy, cool)
to curated reference audio files and verified Japanese transcripts.
"""

from pathlib import Path
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger("galgame2voice.services.emotion_references")

# Reference audio definitions for Shiki Natsume (四季夏目)
# Listened, verified, and mapped from original game audio assets.
NATSUME_EMOTION_REFERENCES: Dict[str, Dict[str, str]] = {
    "gentle": {
        "audio_name": "gentle.ogg",
        "prompt_text": "とりあえず、今日見たことは忘れて、わかった?",
        "prompt_lang": "ja",
        "description": "平稳温和，微带疏离与关切（常规基准）",
    },
    "happy": {
        "audio_name": "happy.ogg",
        "prompt_text": "今日は来てくれてありがとう。楽しかった。",
        "prompt_lang": "ja",
        "description": "轻快甜美，带笑意的喜悦语气",
    },
    "sad": {
        "audio_name": "sad.ogg",
        "prompt_text": "その時少し、ほんの少し、寂しいって思った。",
        "prompt_lang": "ja",
        "description": "低沉放缓，微弱气声的委屈落寞语气",
    },
    "tsundere": {
        "audio_name": "tsundere.ogg",
        "prompt_text": "今さらそんな確認しないでよ、バカ",
        "prompt_lang": "ja",
        "description": "娇嗔急促、嘴硬气恼的傲娇语气",
    },
    "shy": {
        "audio_name": "shy.ogg",
        "prompt_text": "だから、無言で凝視されると恥ずかしいんだってば。",
        "prompt_lang": "ja",
        "description": "羞涩颤音、慌乱难为情的语气",
    },
    "cool": {
        "audio_name": "cool.ogg",
        "prompt_text": "勝手に仲間にしないでください。",
        "prompt_lang": "ja",
        "description": "平淡冷静、果断拉开距离的高冷语气",
    },
}

# Emotion synonym normalizer
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


def normalize_emotion(emotion: Optional[str]) -> str:
    """Normalizes an emotion string to one of the 6 canonical archetypes."""
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
    Resolves emotion reference audio file path and prompt text.
    Returns:
        {
            'ref_audio_path': str,
            'prompt_text': str,
            'prompt_lang': str,
            'emotion': str,
        }
        or None if no matching reference audio is found.
    """
    canonical_emo = normalize_emotion(emotion)

    # Check if character is Shiki Natsume (or default fallback)
    is_natsume = (
        "夏目" in character_name
        or "natsume" in character_name.lower()
        or "siki" in character_name.lower()
        or character_name == "default"
    )

    if not is_natsume:
        return None

    if canonical_emo not in NATSUME_EMOTION_REFERENCES:
        return None

    ref_info = NATSUME_EMOTION_REFERENCES[canonical_emo]

    # Resolve local file path
    if base_dir is None:
        try:
            from galgame2voice.config import get_settings
            settings = get_settings()
            base_dir = Path(settings.audio_dir) / "references" / "natsume"
        except Exception:
            base_dir = Path("audio/references/natsume")

    candidate_file = Path(base_dir) / ref_info["audio_name"]
    if candidate_file.exists():
        return {
            "ref_audio_path": str(candidate_file.resolve()),
            "prompt_text": ref_info["prompt_text"],
            "prompt_lang": ref_info["prompt_lang"],
            "emotion": canonical_emo,
        }

    # Fallback to E: drive if original folder is still accessible
    e_fallback = Path(r"E:\yuzusoft\cafeStella\sikivoice")
    legacy_file_map = {
        "gentle.ogg": "nat002_032.ogg",
        "happy.ogg": "nat212_099.ogg",
        "sad.ogg": "nat207_125.ogg",
        "tsundere.ogg": "nat208_083.ogg",
        "shy.ogg": "nat208_101.ogg",
        "cool.ogg": "nat201_146.ogg",
    }
    legacy_file = e_fallback / legacy_file_map.get(ref_info["audio_name"], "")
    if legacy_file.exists():
        return {
            "ref_audio_path": str(legacy_file.resolve()),
            "prompt_text": ref_info["prompt_text"],
            "prompt_lang": ref_info["prompt_lang"],
            "emotion": canonical_emo,
        }

    return None
