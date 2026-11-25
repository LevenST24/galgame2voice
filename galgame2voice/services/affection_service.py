"""
Dynamic Affection State Machine & Galgame Easter Egg Service for galgame2voice.
Manages 5-tier intimacy levels, scoring rules, daily rate limits,
emotional state transitions, and interactive easter egg voicelines.
"""

import logging
import re
from typing import Any
from pathlib import Path

from galgame2voice.database import crud
from galgame2voice.database.session import get_database_path, get_db
from galgame2voice.services.emotion_classifier import VALID_EMOTIONS, EMOTION_NAME_MAP
from galgame2voice.services.emotion_references import EMOTION_SYNONYMS

logger = logging.getLogger("galgame2voice.services.affection_service")

_CLAUSE_SPLIT_RE = re.compile(r"[\s,，。！!?？~、；;：:…]+")
# A keyword right after one of these in the same clause is negated ("不喜欢你", "真不可爱", "谈不上好看",
# "并不是很可爱", "不是特别喜欢"); a negated negation ("不是不可爱", "不得不喜欢你") is not, and neither is
# "特别" ("especially"), whose 别 is not "don't".
_NEGATED_PREFIX_RE = re.compile(
    r"(?:(?<!不)(?<!并非)(?<!不是)(?<!不得)(?<!不能)"
    r"(?:不|没|没有|(?<!特)别|莫|勿|未|甭)"
    r"(?:是|算|觉得|喜欢|爱|太|很|真|挺|怎么|会|能|想|肯|配|要|许|准|特别){0,3}"
    r"|算不上|谈不上|称不上|谁说(?:你|她|他|我)?|讨厌|最烦|恨透(?:了)?)$"
)


def _has_unnegated_keyword(text: str, keywords: list[str]) -> bool:
    """Whether a keyword appears in ``text`` without a negation right before it in the same clause."""
    for clause in _CLAUSE_SPLIT_RE.split(text):
        for keyword in keywords:
            keyword = keyword.lower()
            pos = clause.find(keyword)
            while pos != -1:
                if not _NEGATED_PREFIX_RE.search(clause[max(0, pos - 10) : pos]):
                    return True
                pos = clause.find(keyword, pos + len(keyword))
    return False


# The preference keywords and easter eggs below come from one character's story (四季夏目 of
# 喫茶ステラ). Another character gets no bonus for "兼职" or "喝一杯" and no 女仆装 easter egg.
_LORE_CHARACTER_NAMES = ("夏目", "ナツメ", "natsume")


def _uses_character_lore(character_name: str | None) -> bool:
    """Whether the Natsume-specific keywords apply. ``None`` means the caller doesn't know the character."""
    if character_name is None:
        return True
    name = character_name.strip().lower()
    return any(lore_name in name for lore_name in _LORE_CHARACTER_NAMES)


class AffectionService:
    """
    State machine controlling character intimacy, emotion transitions,
    and milestone / easter egg dialogue unlocks.
    """

    @staticmethod
    def get_fallback_payload(emotion: str) -> dict[str, Any]:
        """Returns standard neutral affection payload used when an affection update fails."""
        return {
            "score": 0,
            "level": 1,
            "level_name": "初识/生疏",
            "emotion": emotion,
            "points_earned": 0,
        }

    # Keyword scoring rules
    COMPLIMENT_KEYWORDS = [
        "可爱", "辛苦了", "喜欢你", "谢谢你", "真棒", "夸你", "好看", "漂亮", "温柔", "爱你", "想你", "开心",
        "太棒了", "真好", "努力", "赞"
    ]

    CHARACTER_COMPLIMENT_KEYWORDS = ["喜欢夏目"]

    CHARACTER_PREFERENCE_KEYWORDS = [
        "星光咖啡馆", "女仆装", "甜鸡尾酒", "咖啡加糖", "大枣", "死神", "咖啡豆", "兼职", "高岭之花", "喝一杯",
        "死神之蝶", "喫茶", "ナツメ", "昂晴"
    ]

    # Galgame Easter Eggs Catalog
    EASTER_EGGS = {
        "easter_egg_zaozi": {
            "id": "easter_egg_zaozi",
            "title": "「枣子姐」的称呼抗议",
            "triggers": ["枣子姐", "大枣"],
            "chinese": "都说了不要叫那个名字啦！那是机翻的错……真是的，昂晴你又在取笑我。",
            "japanese": "その名前で呼ばないでって言ってるでしょ！誤訳のせいなんだから……もう、昂晴ったら。",
            "emotion": "tsundere",
            "min_level": 1,
        },
        "easter_egg_faka": {
            "id": "easter_egg_faka",
            "title": "「无情发卡姬」的温柔特例",
            "triggers": ["发卡姬", "无情发卡姬", "被拒绝"],
            "chinese": "……以前在学校确实拒绝过很多人呢。但是，现在的我，可不会随便给你发卡哦。",
            "japanese": "……昔はたくさん断ってきたけど。でも、今の私は、あなたを振ったりしないわ。",
            "emotion": "shy",
            "min_level": 2,
        },
        "easter_egg_maid": {
            "id": "easter_egg_maid",
            "title": "秘密的「女仆装」爱好",
            "triggers": ["女仆装", "女仆", "穿女仆装"],
            "chinese": "你怎么会知道……穿女仆装是我的个人爱好啦！不许到处乱说，知道了吗？",
            "japanese": "どうしてそれを……メイド服を着るのは私の趣味よ！誰にも言っちゃダメだからね？",
            "emotion": "shy",
            "min_level": 1,
        },
        "easter_egg_drunk": {
            "id": "easter_egg_drunk",
            "title": "微醺醉酒外向模式",
            "triggers": ["喝醉", "喝醉了", "微醺", "鸡尾酒", "再来一杯"],
            "chinese": "呼……头稍微有点晕呢。昂晴，要不要再陪我喝一杯？今晚……不许走哦。",
            "japanese": "ふぅ……ちょっとクラクラするかも。昂晴、もう一杯付き合ってくれる？今夜は……帰さないから。",
            "emotion": "gentle",
            "min_level": 3,
        },
    }

    # Milestone Voicelines per Level
    MILESTONES = {
        "milestone_lv1": {
            "id": "milestone_lv1",
            "level": 1,
            "title": "初识之缘",
            "chinese": "（保持着礼貌而略显生硬的距离）欢迎光临星光咖啡馆。请问需要点些什么？",
            "japanese": "喫茶ステラへようこそ。ご注文はお決まりですか？",
            "emotion": "normal",
        },
        "milestone_lv2": {
            "id": "milestone_lv2",
            "level": 2,
            "title": "同伴日常",
            "chinese": "又是你啊……好吧，今天也请多关照了。别总是一副心不在焉的样子哦。",
            "japanese": "またあなたね……まあいいわ、今日もよろしく。ぼーっとしてないでよね。",
            "emotion": "tsundere",
        },
        "milestone_lv3": {
            "id": "milestone_lv3",
            "level": 3,
            "title": "信任共鸣",
            "chinese": "和你在一起聊天，感觉心情平静了许多呢。谢谢你一直陪着我。",
            "japanese": "あなたと話していると、心が落ち着く気がするの。いつも付き合ってくれてありがとう。",
            "emotion": "gentle",
        },
        "milestone_lv4": {
            "id": "milestone_lv4",
            "level": 4,
            "title": "羞涩依恋",
            "chinese": "（脸颊微微泛红）真是的……你最近对我这么温柔，我都快要不习惯了……",
            "japanese": "もう……最近優しすぎて、なんだか調子が狂っちゃうわよ……",
            "emotion": "shy",
        },
        "milestone_lv5": {
            "id": "milestone_lv5",
            "level": 5,
            "title": "誓约永恒",
            "chinese": "昂晴……能够与你相遇、共同经历那些回溯与奇迹，是我一生中最幸福的事。无论未来如何，我都想一直留在你身边。",
            "japanese": "昂晴……あなたに出会えて、本当に幸せよ。どんな未来が待っていても、ずっとあなたの隣にいたいの。",
            "emotion": "gentle",
        },
    }

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(db_path) if db_path is not None else get_database_path()

    def calculate_turn_points(
        self, user_text: str, assistant_text: str = "", character_name: str | None = None,
    ) -> tuple[int, list[str]]:
        """Calculates turn affection points and reason tags."""
        if not user_text or not isinstance(user_text, str):
            return 1, ["base_turn", "base_interaction (+1)"]

        points = 1
        reasons = ["base_turn", "base_interaction (+1)"]

        combined = user_text[:4000].lower()

        uses_lore = _uses_character_lore(character_name)

        # Check compliments
        has_compliment = _has_unnegated_keyword(combined, self.COMPLIMENT_KEYWORDS) or (
            uses_lore and _has_unnegated_keyword(combined, self.CHARACTER_COMPLIMENT_KEYWORDS)
        )
        if has_compliment:
            points += 2
            reasons.extend(["compliment", "warm_compliment (+2)"])

        # Check character preferences
        has_preference = uses_lore and _has_unnegated_keyword(
            combined, self.CHARACTER_PREFERENCE_KEYWORDS,
        )
        if has_preference:
            points += 3
            reasons.extend(["character_preference", "character_preference (+3)"])

        return points, reasons

    def calculate_interaction_points(
        self, user_text: str, assistant_text: str = "", character_name: str | None = None,
    ) -> tuple[int, list[str]]:
        """Calculates affection points earned for a turn based on content analysis."""
        return self.calculate_turn_points(user_text, assistant_text, character_name)

    def check_easter_eggs(
        self, user_text: str, current_level: int = 1, character_name: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Checks if the user input triggers a specific Galgame easter egg dialogue.
        """
        if not user_text or not isinstance(user_text, str) or not _uses_character_lore(character_name):
            return None
        try:
            curr_lvl = int(current_level)
        except (TypeError, ValueError):
            curr_lvl = 1

        text_lower = user_text[:4000].lower()
        for egg in self.EASTER_EGGS.values():
            if curr_lvl >= egg.get("min_level", 1) and any(trigger.lower() in text_lower for trigger in egg["triggers"]):
                return egg
        return None

    def _resolve_turn_emotion(
        self,
        current: Any,
        explicit_emotion: str | None,
        triggered_egg: dict[str, Any] | None,
    ) -> str:
        """Resolves target emotion using explicit classification or easter egg triggers."""
        clean_exp = (explicit_emotion or "").strip().lower()
        if clean_exp and clean_exp in EMOTION_SYNONYMS:
            emotion = EMOTION_SYNONYMS[clean_exp]
        elif clean_exp and clean_exp in EMOTION_NAME_MAP:
            emotion = EMOTION_NAME_MAP[clean_exp]
        else:
            raw_clean = (current.current_emotion or "").strip().lower()
            emotion = EMOTION_SYNONYMS.get(raw_clean, EMOTION_NAME_MAP.get(raw_clean, raw_clean))

        if triggered_egg:
            egg_emo = (triggered_egg.get("emotion") or "").strip().lower()
            if egg_emo:
                emotion = EMOTION_SYNONYMS.get(egg_emo, EMOTION_NAME_MAP.get(egg_emo, egg_emo))

        if emotion not in VALID_EMOTIONS:
            emotion = "gentle"
        return emotion

    def _collect_new_dialogue_ids(
        self,
        updated: Any,
        triggered_egg: dict[str, Any] | None,
    ) -> list[str]:
        """Collects unlocked milestone and easter egg dialogue IDs for the current affection level."""
        new_dialogue_ids: list[str] = []
        for lvl in range(1, updated.affection_level + 1):
            milestone_id = f"milestone_lv{lvl}"
            if milestone_id in self.MILESTONES and milestone_id not in updated.unlocked_dialogues:
                new_dialogue_ids.append(milestone_id)

        if triggered_egg and triggered_egg["id"] not in updated.unlocked_dialogues:
            new_dialogue_ids.append(triggered_egg["id"])

        return new_dialogue_ids

    @staticmethod
    def _normalize_user_and_character_ids(user_id: Any, character_id: Any) -> tuple[str, int]:
        """Normalizes user_id string and character_id integer with safe defaults."""
        u_id = (user_id or "").strip() or "default_user"
        try:
            char_id = int(character_id) if character_id is not None and int(character_id) > 0 else 1
        except (TypeError, ValueError):
            char_id = 1
        return u_id, char_id

    async def handle_turn_affection(
        self,
        user_id: str = "default_user",
        character_id: int = 1,
        user_text: str = "",
        assistant_text: str = "",
        daily_limit: int = 15,
        explicit_emotion: str | None = None,
        character_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Processes a full turn of affection update:
        1. Calculate points
        2. Detect a triggered easter egg from the user text
        3. Resolve emotion (explicit classification, or the easter egg's emotion)
        4. Increment score and emotion in SQLite
        5. Unlock milestone and easter egg dialogues
        """
        u_id, char_id = self._normalize_user_and_character_ids(user_id, character_id)
        try:
            d_limit = max(0, int(daily_limit))
        except (TypeError, ValueError):
            d_limit = 15

        u_text = (user_text or "")[:4000] if isinstance(user_text, str) else ""
        a_text = (assistant_text or "")[:4000] if isinstance(assistant_text, str) else ""

        delta_points, reasons = self.calculate_interaction_points(u_text, a_text, character_name)

        async with get_db(self.db_path) as conn:
            current = await crud.get_or_create_character_affection(conn, u_id, char_id)
            triggered_egg = self.check_easter_eggs(u_text, current.affection_level, character_name)
            emotion = self._resolve_turn_emotion(current, explicit_emotion, triggered_egg)

            # Increment points atomically
            updated, actual_gain, level_up = await crud.increment_affection(
                conn=conn,
                user_id=u_id,
                character_id=char_id,
                delta_points=delta_points,
                emotion=emotion,
                daily_limit=d_limit,
            )

            # Collect new dialogues to unlock (unlock all milestone lines up to current level)
            new_dialogue_ids = self._collect_new_dialogue_ids(updated, triggered_egg)
            if new_dialogue_ids:
                await crud.unlock_character_dialogues(
                    conn=conn,
                    user_id=u_id,
                    character_id=char_id,
                    dialogue_ids_to_add=new_dialogue_ids,
                )
                updated = await crud.get_character_affection(conn, u_id, char_id) or updated

        return {
            "score": updated.affection_score,
            "level": updated.affection_level,
            "level_name": updated.level_name,
            "emotion": updated.current_emotion,
            "points_earned": actual_gain,
            "daily_points_earned": updated.daily_points_earned,
            "daily_limit": daily_limit,
            "interaction_count": updated.interaction_count,
            "level_up": level_up,
            "reasons": reasons,
            "easter_egg": triggered_egg,
            "unlocked_count": len(updated.unlocked_dialogues),
            "custom_nickname": updated.custom_nickname,
        }


EMOTION_KEYWORDS = {
    "tsundere": ["笨蛋", "才不是", "谁管你", "不要自作多情", "哼", "才没有", "别自以为是", "バカ"],
    "shy": ["脸红", "害羞", "唔……", "那个……", "别盯着我看", "秘密", "不好意思", "恥ずかしい"],
    "happy": ["太好了", "好高兴", "开心", "嘻嘻", "真棒", "笑", "うれしい", "よかった", "很高兴"],
    "sad": ["对不起", "难过", "伤心", "抱歉", "呜", "悲しい", "ごめん", "失落"],
    "gentle": ["微笑着", "没关系哦", "乖", "摸头", "辛苦了", "陪着你", "温柔", "大丈夫", "陪伴"],
    "cool": ["无聊", "……", "冷淡", "走开", "发卡", "別に"],
}

__all__ = [
    "AffectionService",
    "EMOTION_KEYWORDS",
]
