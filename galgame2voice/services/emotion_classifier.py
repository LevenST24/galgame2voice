"""
Emotion Taxonomy & Deterministic Classifier for galgame2voice.
Decoupled from chat orchestration for clean architecture, high maintainability,
and isolated testing.
"""

from typing import Dict, List, Optional

# Archetype keyword lexicon mapping
EMOTION_KEYWORDS: Dict[str, List[str]] = {
    "tsundere": ["傲娇", "才不是", "才没有", "べ、別に", "勘違い", "ツン", "哼", "才不会", "別にあんた", "不要误会", "谁要你管"],
    "shy": ["害羞", "脸红", "照れ", "恥ずか", "///", "……///", "笨蛋", "讨厌", "えっと", "ばか"],
    "happy": ["开心", "高兴", "嬉し", "わーい", "やった", "笑", "喜ぶ", "大好き", "太好了", "ありがとう", "耶", "哈哈", "好棒"],
    "cool": ["冷淡", "高冷", "无聊", "くだらない", "別に", "静かに", "冷静", "ふん", "无所谓", "随你便"],
    "sad": ["难过", "伤心", "悲し", "泣く", "寂しい", "抱歉", "ごめん", "辛い", "对不起", "呜呜", "痛い"],
    "gentle": ["温柔", "ふふ", "大丈夫", "よしよし", "微笑", "慢点", "摸摸头", "乖", "優しい", "好的", "没关系", "请放心"],
}

VALID_EMOTIONS = {"gentle", "shy", "happy", "tsundere", "cool", "sad"}

EMOTION_NAME_MAP: Dict[str, str] = {
    "傲娇": "tsundere",
    "害羞": "shy",
    "开心": "happy",
    "高兴": "happy",
    "高冷": "cool",
    "冷淡": "cool",
    "难过": "sad",
    "伤心": "sad",
    "温柔": "gentle",
}


def classify_emotion(
    chinese: str = "",
    japanese: str = "",
    explicit_emotion: Optional[str] = None,
) -> str:
    """
    Determines character emotion archetype ('gentle', 'shy', 'happy', 'tsundere', 'cool', 'sad').
    Priority: explicit_emotion > deterministic keyword scan > 'gentle' fallback.
    """
    if explicit_emotion:
        clean = explicit_emotion.strip().lower()
        if clean in EMOTION_NAME_MAP:
            clean = EMOTION_NAME_MAP[clean]
        if clean in VALID_EMOTIONS:
            return clean

    combined = f"{chinese or ''} {japanese or ''}".strip()
    if not combined:
        return "gentle"

    for emo, keywords in EMOTION_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return emo

    return "gentle"
