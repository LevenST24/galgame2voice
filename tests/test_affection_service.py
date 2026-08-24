"""
Unit and integration tests for Dynamic Affection State Machine & Easter Eggs (AffectionService).
Covers 5-tier level transitions, scoring rules (+1 base, +2 compliments, +3 character keywords),
daily points cap, 7 emotion taxonomy classifiers, Galgame easter egg triggers,
milestone line unlocks, and dialogue gallery queries.
"""

import pytest
import aiosqlite
from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.services.affection_service import AffectionService


@pytest.fixture
def affection_service(isolate_test_database):
    return AffectionService(db_path=isolate_test_database)


class TestAffectionLevelAndScoring:
    def test_level_calculation(self, affection_service):
        assert affection_service.calculate_level(0) == (1, "初识/生疏")
        assert affection_service.calculate_level(19) == (1, "初识/生疏")
        assert affection_service.calculate_level(20) == (2, "熟悉/同伴")
        assert affection_service.calculate_level(39) == (2, "熟悉/同伴")
        assert affection_service.calculate_level(40) == (3, "友好/信任")
        assert affection_service.calculate_level(59) == (3, "友好/信任")
        assert affection_service.calculate_level(60) == (4, "亲密/依赖")
        assert affection_service.calculate_level(79) == (4, "亲密/依赖")
        assert affection_service.calculate_level(80) == (5, "恋慕/誓约")
        assert affection_service.calculate_level(100) == (5, "恋慕/誓约")

    def test_calculate_turn_points(self, affection_service):
        # Base point
        pts, reasons = affection_service.calculate_turn_points(
            user_text="今天天气不错", assistant_text="是的呢"
        )
        assert pts == 1
        assert "base_turn" in reasons

        # Compliment point
        pts_comp, reasons_comp = affection_service.calculate_turn_points(
            user_text="夏目你好可爱好漂亮呀", assistant_text="……笨蛋"
        )
        assert pts_comp == 1 + 2
        assert "compliment" in reasons_comp

        # Preference point
        pts_pref, reasons_pref = affection_service.calculate_turn_points(
            user_text="我们一起喝杯咖啡加糖吧", assistant_text="好的"
        )
        assert pts_pref == 1 + 3
        assert "character_preference" in reasons_pref

        # Both compliment + preference point
        pts_both, reasons_both = affection_service.calculate_turn_points(
            user_text="夏目真可爱，我们一起喝咖啡加糖吧", assistant_text="……唔"
        )
        assert pts_both == 1 + 2 + 3
        assert "compliment" in reasons_both
        assert "character_preference" in reasons_both


class TestEmotionClassification:
    def test_classify_all_emotions(self, affection_service):
        assert affection_service.classify_emotion("……你这个笨蛋///，才没有脸红呢！", "……ばか///") == "shy"
        assert affection_service.classify_emotion("哼，我才不是特意为你泡咖啡的！别误会了！", "べ、別に…") == "tsundere"
        assert affection_service.classify_emotion("安心吧，我会一直陪在你身边的，请放松一些。", "大丈夫ですよ") == "gentle"
        assert affection_service.classify_emotion("太好啦！今天真是超级开心的一天呢！", "やったー！嬉しい！") == "happy"
        assert affection_service.classify_emotion("好难过，为什么会变成这样……对不起……", "悲しい…ごめんなさい") == "sad"
        assert affection_service.classify_emotion("……无聊。请不要浪费彼此的时间。", "くだらない…") == "cold"
        assert affection_service.classify_emotion("今天的天气预报说是晴天。", "今日は晴れです。") == "normal"


class TestEasterEggTriggers:
    def test_match_easter_egg_triggers(self, affection_service):
        # 1. 枣子姐
        egg1 = affection_service.check_easter_egg("枣子姐天下第一！")
        assert egg1 is not None
        assert egg1["id"] == "easter_egg_zaozi"
        assert "枣子姐" in egg1["triggers"]
        assert egg1["emotion"] == "tsundere"

        # 2. 无情发卡姬 (min_level 2)
        egg2 = affection_service.check_easter_egg("你真是无情发卡姬啊", current_level=2)
        assert egg2 is not None
        assert egg2["id"] == "easter_egg_faka"
        assert egg2["emotion"] == "shy"

        # 3. 女仆装 (min_level 1)
        egg3 = affection_service.check_easter_egg("夏目穿女仆装肯定很好看", current_level=1)
        assert egg3 is not None
        assert egg3["id"] == "easter_egg_maid"
        assert egg3["emotion"] == "shy"

        # 4. 喝醉了 (min_level 3)
        egg4 = affection_service.check_easter_egg("我今天喝醉了，头好晕", current_level=3)
        assert egg4 is not None
        assert egg4["id"] == "easter_egg_drunk"
        assert egg4["emotion"] == "gentle"

        # Normal text -> None
        assert affection_service.check_easter_egg("早安，吃早餐了吗？") is None


class TestAffectionTurnHandlingAndDailyCap:
    @pytest.mark.asyncio
    async def test_turn_affection_increment_and_easter_egg(self, affection_service, isolate_test_database):
        user_id = "u_aff_1"
        char_id = 1

        # Turn 1: Normal dialogue
        res1 = await affection_service.handle_turn_affection(
            user_id=user_id,
            character_id=char_id,
            user_text="你好呀夏目",
            assistant_text="你好。",
        )
        assert res1["level"] == 1
        assert res1["score"] == 1
        assert res1["points_earned"] == 1

        # Turn 2: Easter egg text "枣子姐"
        res2 = await affection_service.handle_turn_affection(
            user_id=user_id,
            character_id=char_id,
            user_text="枣子姐最可爱了！",
            assistant_text="……才不是枣子姐！",
        )
        assert res2["score"] == 1 + (1 + 2)  # base + compliment = 3 pts -> total 4
        assert res2["easter_egg"] is not None
        assert res2["easter_egg"]["id"] == "easter_egg_zaozi"
        assert res2["emotion"] == "tsundere"

        # Verify unlocked dialogues stored in DB
        async with get_db(isolate_test_database) as conn:
            aff_db = await crud.get_character_affection(conn, user_id=user_id, character_id=char_id)
            assert "easter_egg_zaozi" in aff_db.unlocked_dialogues

    @pytest.mark.asyncio
    async def test_daily_points_cap_enforcement(self, affection_service, isolate_test_database):
        user_id = "u_aff_cap"
        char_id = 1

        # Earn points up to cap (15 points)
        for i in range(5):
            await affection_service.handle_turn_affection(
                user_id=user_id,
                character_id=char_id,
                user_text="夏目真可爱，我们一起喝咖啡加糖吧！", # +6 pts each turn
                assistant_text="……好的。",
            )

        async with get_db(isolate_test_database) as conn:
            aff_db = await crud.get_character_affection(conn, user_id=user_id, character_id=char_id)
            assert aff_db.daily_points_earned == 15
            assert aff_db.affection_score == 15

        # 6th turn on the same day: should gain 0 points due to cap
        res_capped = await affection_service.handle_turn_affection(
            user_id=user_id,
            character_id=char_id,
            user_text="夏目太棒了",
            assistant_text="谢谢",
        )
        assert res_capped["points_earned"] == 0
        assert res_capped["score"] == 15


class TestDialogueGallery:
    @pytest.mark.asyncio
    async def test_get_dialogue_gallery_unlock_states(self, affection_service, isolate_test_database):
        user_id = "u_gallery"
        char_id = 1

        # Initial state: Only Lv.1 milestone is unlocked
        gallery1 = await affection_service.get_dialogue_gallery(user_id=user_id, character_id=char_id)
        assert len(gallery1) == len(AffectionService.MILESTONES) + len(AffectionService.EASTER_EGGS)

        lv1_item = next(d for d in gallery1 if d["id"] == "milestone_lv1")
        assert lv1_item["is_unlocked"] is True

        lv5_item = next(d for d in gallery1 if d["id"] == "milestone_lv5")
        assert lv5_item["is_unlocked"] is False

        zaozi_item = next(d for d in gallery1 if d["id"] == "easter_egg_zaozi")
        assert zaozi_item["is_unlocked"] is False

        # Trigger easter egg
        await affection_service.handle_turn_affection(
            user_id=user_id,
            character_id=char_id,
            user_text="枣子姐！",
            assistant_text="……",
        )

        gallery2 = await affection_service.get_dialogue_gallery(user_id=user_id, character_id=char_id)
        zaozi_item_after = next(d for d in gallery2 if d["id"] == "easter_egg_zaozi")
        assert zaozi_item_after["is_unlocked"] is True
