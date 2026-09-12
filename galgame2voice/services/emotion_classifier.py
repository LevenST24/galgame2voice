"""
Emotion Taxonomy & Deterministic Classifier for galgame2voice.
Decoupled from chat orchestration for clean architecture, high maintainability,
and isolated testing.
"""

import re
from typing import Dict, List, Optional, Tuple

# Archetype keyword lexicon mapping
EMOTION_KEYWORDS: Dict[str, List[str]] = {
    "tsundere": ["傲娇", "才不是", "才没有", "べ、別に", "勘違い", "ツン", "哼", "才不会", "別にあんた", "不要误会", "谁要你管"],
    "shy": ["害羞", "脸红", "照れ", "恥ずか", "///", "……///", "笨蛋", "讨厌", "えっと", "ばか"],
    "happy": ["开心", "高兴", "嬉し", "わーい", "やった", "笑", "喜ぶ", "大好き", "太好了", "ありがとう", "耶", "哈哈", "好棒"],
    "cool": ["冷淡", "高冷", "无聊", "くだらない", "別に", "静かに", "冷静", "ふん", "无所谓", "随你便"],
    "sad": ["难过", "伤心", "悲し", "泣く", "寂しい", "抱歉", "ごめん", "辛い", "对不起", "呜呜", "痛い"],
    "angry": ["生气", "愤怒", "气愤", "怒り", "怒る", "怒", "恼怒", "烦人", "讨厌死了", "吵死了", "うるさい", "ふざけるな", "怒りますよ"],
    "gentle": ["温柔", "ふふ", "大丈夫", "よしよし", "微笑", "慢点", "摸摸头", "乖", "優しい", "好的", "没关系", "请放心"],
}

VALID_EMOTIONS = {"gentle", "shy", "happy", "tsundere", "cool", "sad", "angry"}

EMOTION_NAME_MAP: Dict[str, str] = {
    # Tsundere
    "傲娇": "tsundere",
    "ツンデレ": "tsundere",
    "ツン": "tsundere",
    "娇蛮": "tsundere",
    "不服": "tsundere",
    "tsundere": "tsundere",
    # Shy
    "害羞": "shy",
    "羞怯": "shy",
    "羞涩": "shy",
    "脸红": "shy",
    "照れ": "shy",
    "恥ずかしい": "shy",
    "てれ": "shy",
    "shy": "shy",
    # Happy
    "开心": "happy",
    "高兴": "happy",
    "喜悦": "happy",
    "兴奋": "happy",
    "快乐": "happy",
    "得意": "happy",
    "うれしい": "happy",
    "楽しい": "happy",
    "happy": "happy",
    # Cool
    "高冷": "cool",
    "冷淡": "cool",
    "冷静": "cool",
    "冷漠": "cool",
    "平静": "cool",
    "cool": "cool",
    # Sad
    "难过": "sad",
    "伤心": "sad",
    "悲伤": "sad",
    "沮丧": "sad",
    "哭泣": "sad",
    "悲しい": "sad",
    "寂しい": "sad",
    "sad": "sad",
    # Angry
    "生气": "angry",
    "愤怒": "angry",
    "气愤": "angry",
    "恼怒": "angry",
    "怒": "angry",
    "怒り": "angry",
    "おこ": "angry",
    "暴怒": "angry",
    "angry": "angry",
    # Gentle
    "温柔": "gentle",
    "温和": "gentle",
    "柔和": "gentle",
    "亲切": "gentle",
    "微笑": "gentle",
    "安心": "gentle",
    "優しい": "gentle",
    "gentle": "gentle",
}


def extract_bracketed_emotion(text: str) -> Tuple[Optional[str], str]:
    """
    Extracts emotion archetype from bracketed stage cues such as:
    【傲娇】才不是因为喜欢你呢！ -> ("tsundere", "才不是因为喜欢你呢！")
    「【傲娇】才不是因为喜欢你呢！」 -> ("tsundere", "「才不是因为喜欢你呢！」")
    (happy) こんにちは！ -> ("happy", "こんにちは！")
    （害羞）あの…… -> ("shy", "あの……")
    [cool] くだらない。 -> ("cool", "くだらない。")
    才不是因为喜欢你呢！【傲娇】 -> ("tsundere", "才不是因为喜欢你呢！")
    那个……【害羞】手をつないでもいい？ -> ("shy", "那个……手をつないでもいい？")
    Returns (detected_emotion_or_none, cleaned_text).
    """
    if not text:
        return None, ""

    # 1. Primary check: Leading bracketed emotion tag (preserving dialogue quote wrappers)
    leading_pattern = r'^\s*([「『"\'“]?\s*)([（\(\[【〖〔])([^）\)\]】〗〕]+)([）\)\]】〗〕])\s*'
    m_lead = re.match(leading_pattern, text)
    if m_lead:
        quote_prefix = m_lead.group(1).strip()
        inner_content = m_lead.group(3).strip()
        candidate = re.sub(r'^(?:情绪|心情|状态|emotion|emo)[:：\s]*', '', inner_content, flags=re.IGNORECASE).strip().lower()
        detected = None
        if candidate in EMOTION_NAME_MAP:
            detected = EMOTION_NAME_MAP[candidate]
        elif candidate in VALID_EMOTIONS:
            detected = candidate
        else:
            for k, v in EMOTION_NAME_MAP.items():
                if k in candidate:
                    detected = v
                    break

        if detected in VALID_EMOTIONS:
            cleaned = text[m_lead.end():].strip()
            if quote_prefix and not cleaned.startswith(quote_prefix):
                cleaned = f"{quote_prefix}{cleaned}"
            return detected, cleaned

    # 2. Secondary check: Embedded or trailing bracketed emotion tag
    any_pattern = r'([（\(\[【〖〔])([^）\)\]】〗〕]+)([）\)\]】〗〕])'
    for m in re.finditer(any_pattern, text):
        inner_content = m.group(2).strip()
        candidate = re.sub(r'^(?:情绪|心情|状态|emotion|emo)[:：\s]*', '', inner_content, flags=re.IGNORECASE).strip().lower()
        detected = None
        if candidate in EMOTION_NAME_MAP:
            detected = EMOTION_NAME_MAP[candidate]
        elif candidate in VALID_EMOTIONS:
            detected = candidate
        else:
            for k, v in EMOTION_NAME_MAP.items():
                if k in candidate:
                    detected = v
                    break

        if detected in VALID_EMOTIONS:
            cleaned = f"{text[:m.start()]}{text[m.end():]}".strip()
            cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
            return detected, cleaned

    return None, text


def classify_emotion(
    chinese: str = "",
    japanese: str = "",
    explicit_emotion: Optional[str] = None,
) -> str:
    """
    Determines character emotion archetype ('gentle', 'shy', 'happy', 'tsundere', 'cool', 'sad', 'angry').
    Priority: explicit_emotion > leading bracketed emotion tag > deterministic keyword scan > 'gentle' fallback.
    """
    if explicit_emotion:
        clean = explicit_emotion.strip().lower()
        if clean in EMOTION_NAME_MAP:
            clean = EMOTION_NAME_MAP[clean]
        if clean in VALID_EMOTIONS:
            return clean

    # Check for leading bracketed emotion tags in chinese or japanese
    emo_ch, _ = extract_bracketed_emotion(chinese or "")
    if emo_ch:
        return emo_ch

    emo_ja, _ = extract_bracketed_emotion(japanese or "")
    if emo_ja:
        return emo_ja

    combined = f"{chinese or ''} {japanese or ''}".strip()
    if not combined:
        return "gentle"

    for emo, keywords in EMOTION_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return emo

    return "gentle"
