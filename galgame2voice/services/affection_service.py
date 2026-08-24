"""
Dynamic Affection State Machine & Galgame Easter Egg Service for galgame2voice.
Manages 5-tier intimacy levels, scoring rules, daily rate limits,
emotional state transitions, and interactive easter egg voicelines.
"""

import json
import logging
from typing import List, Dict, Any, Optional, Tuple, Union
from pathlib import Path
import aiosqlite

from galgame2voice.database import crud
from galgame2voice.database.models import CharacterAffectionResponse, CharacterAffectionUpdate
from galgame2voice.database.session import get_database_path, get_db

logger = logging.getLogger("galgame2voice.services.affection_service")


class AffectionService:
    """
    State machine controlling character intimacy, emotion transitions,
    and milestone / easter egg dialogue unlocks.
    """

    # 5 Intimacy Tiers Definition
    LEVEL_TIERS = {
        1: {"min_score": 0, "max_score": 19, "name": "初识/生疏", "desc": "保持礼貌距离，略显生硬与客套"},
        2: {"min_score": 20, "max_score": 39, "name": "熟悉/同伴", "desc": "咖啡厅日常同伴，日常吐槽与调侃"},
        3: {"min_score": 40, "max_score": 59, "name": "友好/信任", "desc": "建立深厚信任，主动倾听心事，温柔关心"},
        4: {"min_score": 60, "max_score": 79, "name": "亲密/依赖", "desc": "害羞傲娇、依赖撒娇，偶现脸红与占有欲"},
        5: {"min_score": 80, "max_score": 100, "name": "恋慕/誓约", "desc": "专属心意与誓约羁绊，解锁全部隐藏告白台词"},
    }

    # Keyword scoring rules
    COMPLIMENT_KEYWORDS = [
        "可爱", "辛苦了", "喜欢你", "谢谢你", "真棒", "夸你", "好看", "漂亮", "温柔", "爱你", "想你", "开心",
        "太棒了", "真好", "努力", "赞", "喜欢夏目"
    ]

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

    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        self.db_path = str(db_path) if db_path is not None else get_database_path()

    def calculate_level(self, score: int) -> Tuple[int, str]:
        """Calculates intimacy level and tier name based on total affection score."""
        score = max(0, min(100, score))
        if score >= 80:
            return 5, "恋慕/誓约"
        elif score >= 60:
            return 4, "亲密/依赖"
        elif score >= 40:
            return 3, "友好/信任"
        elif score >= 20:
            return 2, "熟悉/同伴"
        return 1, "初识/生疏"

    def calculate_turn_points(self, user_text: str, assistant_text: str = "") -> Tuple[int, List[str]]:
        """Calculates turn affection points and reason tags."""
        points = 1
        reasons = ["base_turn", "base_interaction (+1)"]

        combined = f"{user_text} {assistant_text}".lower()

        # Check compliments
        has_compliment = any(kw.lower() in combined for kw in self.COMPLIMENT_KEYWORDS)
        if has_compliment:
            points += 2
            reasons.extend(["compliment", "warm_compliment (+2)"])

        # Check character preferences
        has_preference = any(kw.lower() in combined for kw in self.CHARACTER_PREFERENCE_KEYWORDS)
        if has_preference:
            points += 3
            reasons.extend(["character_preference", "character_preference (+3)"])

        return points, reasons

    def calculate_interaction_points(self, user_text: str, assistant_text: str = "") -> Tuple[int, List[str]]:
        """Calculates affection points earned for a turn based on content analysis."""
        return self.calculate_turn_points(user_text, assistant_text)

    def check_easter_egg(self, user_text: str, current_level: int = 1) -> Optional[Dict[str, Any]]:
        """Alias to check_easter_eggs."""
        return self.check_easter_eggs(user_text, current_level)

    def classify_emotion(
        self,
        assistant_text: str,
        user_text: str = "",
        current_emotion: str = "normal",
        affection_level: int = 1,
    ) -> str:
        """
        Classifies emotion based on dialogue keywords and context.
        """
        text = f"{assistant_text} {user_text}".lower()

        # Shy keywords
        if any(k in text for k in ["脸红", "害羞", "///", "唔……", "那个……", "别盯着我看", "秘密", "不好意思", "恥ずかしい", "照れ"]):
            return "shy"

        # Tsundere keywords
        if any(k in text for k in ["才不是", "谁管你", "不要自作多情", "哼", "才没有", "别自以为是", "バカ", "笨蛋", "べ、別に", "别误会"]):
            return "tsundere"

        # Happy keywords
        if any(k in text for k in ["太好了", "好高兴", "开心", "嘻嘻", "真棒", "笑", "うれしい", "よかった", "很高兴"]):
            return "happy"

        # Sad keywords
        if any(k in text for k in ["对不起", "难过", "伤心", "抱歉", "呜", "悲しい", "ごめん", "失落"]):
            return "sad"

        # Gentle keywords
        if any(k in text for k in ["微笑着", "没关系哦", "乖", "摸头", "辛苦了", "陪着你", "温柔", "大丈夫", "陪伴"]):
            return "gentle"

        # Cold keywords
        if any(k in text for k in ["无聊", "……", "冷淡", "走开", "发卡", "別に"]):
            return "cold"

        # Default by intimacy tier
        if affection_level >= 4:
            return "gentle"
        return current_emotion or "normal"

    def check_easter_eggs(self, user_text: str, current_level: int = 1) -> Optional[Dict[str, Any]]:
        """
        Checks if the user input triggers a specific Galgame easter egg dialogue.
        """
        text_lower = user_text.lower()
        for egg_id, egg in self.EASTER_EGGS.items():
            if current_level >= egg.get("min_level", 1):
                if any(trigger.lower() in text_lower for trigger in egg["triggers"]):
                    return egg
        return None

    async def handle_turn_affection(
        self,
        user_id: str = "default_user",
        character_id: int = 1,
        user_text: str = "",
        assistant_text: str = "",
        daily_limit: int = 15,
    ) -> Dict[str, Any]:
        """
        Processes a full turn of affection update:
        1. Calculate points
        2. Detect emotion
        3. Check easter eggs and unlock milestones
        4. Update SQLite state machine
        """
        delta_points, reasons = self.calculate_interaction_points(user_text, assistant_text)

        async with get_db(self.db_path) as conn:
            current = await crud.get_or_create_character_affection(conn, user_id, character_id)
            emotion = self.classify_emotion(
                assistant_text=assistant_text,
                user_text=user_text,
                current_emotion=current.current_emotion,
                affection_level=current.affection_level,
            )

            # Check easter egg
            triggered_egg = self.check_easter_eggs(user_text, current.affection_level)
            if triggered_egg:
                emotion = triggered_egg["emotion"]

            # Increment points
            updated, actual_gain, level_up = await crud.increment_affection(
                conn=conn,
                user_id=user_id,
                character_id=character_id,
                delta_points=delta_points,
                emotion=emotion,
                daily_limit=daily_limit,
            )

            # Update unlocked dialogues
            unlocked_set = set(updated.unlocked_dialogues)

            # Unlock milestone for current level
            milestone_id = f"milestone_lv{updated.affection_level}"
            if milestone_id in self.MILESTONES and milestone_id not in unlocked_set:
                unlocked_set.add(milestone_id)

            if triggered_egg and triggered_egg["id"] not in unlocked_set:
                unlocked_set.add(triggered_egg["id"])

            if len(unlocked_set) != len(updated.unlocked_dialogues):
                await crud.update_character_affection(
                    conn,
                    user_id=user_id,
                    character_id=character_id,
                    updates=CharacterAffectionUpdate(unlocked_dialogues=list(unlocked_set)),
                )
                updated = await crud.get_character_affection(conn, user_id, character_id) or updated

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

    async def get_dialogue_gallery(
        self,
        user_id: str = "default_user",
        character_id: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        Returns full list of milestone and easter egg dialogues with unlock status.
        """
        db_path = self.db_path or get_database_path()
        async with get_db(db_path) as conn:
            aff = await crud.get_or_create_character_affection(conn, user_id, character_id)

        unlocked_set = set(aff.unlocked_dialogues)
        gallery: List[Dict[str, Any]] = []

        # 1. Add Milestones
        for m_id, m in self.MILESTONES.items():
            is_unlocked = m_id in unlocked_set or aff.affection_level >= m["level"]
            gallery.append({
                "id": m["id"],
                "type": "milestone",
                "title": f"Lv.{m['level']} 阶段台词 - {m['title']}",
                "chinese": m["chinese"],
                "japanese": m["japanese"],
                "emotion": m["emotion"],
                "is_unlocked": is_unlocked,
                "unlock_condition": f"好感度达到 Lv.{m['level']}",
            })

        # 2. Add Easter Eggs
        for e_id, e in self.EASTER_EGGS.items():
            is_unlocked = e_id in unlocked_set
            gallery.append({
                "id": e["id"],
                "type": "easter_egg",
                "title": f"隐藏彩蛋 - {e['title']}",
                "chinese": e["chinese"],
                "japanese": e["japanese"],
                "emotion": e["emotion"],
                "is_unlocked": is_unlocked,
                "unlock_condition": f"触发关键词: {', '.join(e['triggers'])} (需好感度 Lv.{e['min_level']}+)",
            })

        return gallery


# Module-level aliases for convenience and backwards-compatibility
EASTER_EGG_TRIGGERS = AffectionService.EASTER_EGGS
MILESTONE_VOICELINES = AffectionService.MILESTONES
EMOTION_KEYWORDS = {
    "tsundere": ["笨蛋", "才不是", "谁管你", "不要自作多情", "哼", "才没有", "别自以为是", "バカ"],
    "shy": ["脸红", "害羞", "唔……", "那个……", "别盯着我看", "秘密", "不好意思", "恥ずかしい"],
    "happy": ["太好了", "好高兴", "开心", "嘻嘻", "真棒", "笑", "うれしい", "よかった", "很高兴"],
    "sad": ["对不起", "难过", "伤心", "抱歉", "呜", "悲しい", "ごめん", "失落"],
    "gentle": ["微笑着", "没关系哦", "乖", "摸头", "辛苦了", "陪着你", "温柔", "大丈夫", "陪伴"],
    "cold": ["无聊", "……", "冷淡", "走开", "发卡", "別に"],
}

__all__ = [
    "AffectionService",
    "EASTER_EGG_TRIGGERS",
    "MILESTONE_VOICELINES",
    "EMOTION_KEYWORDS",
]
