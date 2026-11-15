"""
Unit and integration tests for Dynamic Affection State Machine & Easter Eggs (AffectionService).
Covers 5-tier level transitions, scoring rules (+1 base, +2 compliments, +3 character keywords),
daily points cap, 7 emotion taxonomy classifiers, Galgame easter egg triggers,
and milestone line unlocks.
"""

import pytest
import aiosqlite
from galgame2voice.database import crud
from galgame2voice.database.models import CharacterAffectionUpdate
from galgame2voice.database.session import get_db
from galgame2voice.services.affection_service import AffectionService


@pytest.fixture
def affection_service(isolate_test_database):
    return AffectionService(db_path=isolate_test_database)


class TestAffectionLevelAndScoring:
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

    @pytest.mark.parametrize(
        "text",
        [
            "我一点都不喜欢你，真讨厌",
            "你真不可爱",
            "并不觉得漂亮",
            "别夸你了",
            "我讨厌女仆装，也不喜欢咖啡加糖",
            "你并不是很可爱",
            "我不太喜欢女仆装",
            "不是特别喜欢咖啡加糖",
        ],
    )
    def test_negated_compliments_and_preferences_earn_only_the_base_point(self, affection_service, text):
        assert affection_service.calculate_turn_points(user_text=text) == (1, ["base_turn", "base_interaction (+1)"])

    @pytest.mark.parametrize(
        "text",
        ["你也不是不可爱啦", "不得不喜欢你", "我从来没这么喜欢你", "虽然今天心情不好，但夏目真可爱", "你今天特别可爱", "我特别喜欢你"],
    )
    def test_double_negatives_and_positive_clauses_still_count_as_compliments(self, affection_service, text):
        points, reasons = affection_service.calculate_turn_points(user_text=text)
        assert points == 1 + 2
        assert "compliment" in reasons

    def test_intensifier_before_a_preference_keeps_its_bonus(self, affection_service):
        # 「特别」 ends in 「别」 ("don't"), but here it means "especially".
        points, reasons = affection_service.calculate_turn_points(user_text="我特别喜欢女仆装")
        assert points == 1 + 3
        assert "character_preference" in reasons


class TestEmotionClassification:
    def test_classify_all_emotions(self, affection_service):
        assert affection_service.classify_emotion("……你这个笨蛋///，才没有脸红呢！", "……ばか///") == "shy"
        assert affection_service.classify_emotion("哼，我才不是特意为你泡咖啡的！别误会了！", "べ、別に…") == "tsundere"
        assert affection_service.classify_emotion("安心吧，我会一直陪在你身边的，请放松一些。", "大丈夫ですよ") == "gentle"
        assert affection_service.classify_emotion("太好啦！今天真是超级开心的一天呢！", "やったー！嬉しい！") == "happy"
        assert affection_service.classify_emotion("好难过，为什么会变成这样……对不起……", "悲しい…ごめんなさい") == "sad"
        assert affection_service.classify_emotion("……无聊。请不要浪费彼此的时间。", "くだらない…") == "cool"
        assert affection_service.classify_emotion("今天的天气预报说是晴天。", "今日は晴れです。") == "normal"

    @pytest.mark.asyncio
    async def test_explicit_emotion_normalization(self, affection_service):
        res = await affection_service.handle_turn_affection(
            user_id="u_emo_norm",
            character_id=1,
            user_text="你好",
            assistant_text="你好啊",
            explicit_emotion="  Happy  ",
        )
        assert res["emotion"] == "happy"

        res2 = await affection_service.handle_turn_affection(
            user_id="u_emo_norm",
            character_id=1,
            user_text="你好",
            assistant_text="你好啊",
            explicit_emotion="傲娇 ",
        )
        assert res2["emotion"] == "tsundere"


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

    @pytest.mark.asyncio
    async def test_all_five_level_up_transitions(self, isolate_test_database):
        async with get_db(isolate_test_database) as conn:
            await crud.update_character_affection(
                conn, user_id="u_tier", character_id=1,
                updates=CharacterAffectionUpdate(affection_score=19),
            )
            aff, _gain, level_up = await crud.increment_affection(
                conn, user_id="u_tier", character_id=1, delta_points=1, daily_limit=100,
            )
            assert aff.affection_score == 20
            assert aff.affection_level == 2
            assert level_up is True

            await crud.update_character_affection(
                conn, user_id="u_tier", character_id=1,
                updates=CharacterAffectionUpdate(affection_score=39),
            )
            aff, _gain, level_up = await crud.increment_affection(
                conn, user_id="u_tier", character_id=1, delta_points=1, daily_limit=100,
            )
            assert aff.affection_score == 40
            assert aff.affection_level == 3
            assert level_up is True

            await crud.update_character_affection(
                conn, user_id="u_tier", character_id=1,
                updates=CharacterAffectionUpdate(affection_score=59),
            )
            aff, _gain, level_up = await crud.increment_affection(
                conn, user_id="u_tier", character_id=1, delta_points=1, daily_limit=100,
            )
            assert aff.affection_score == 60
            assert aff.affection_level == 4
            assert level_up is True

            await crud.update_character_affection(
                conn, user_id="u_tier", character_id=1,
                updates=CharacterAffectionUpdate(affection_score=79),
            )
            aff, _gain, level_up = await crud.increment_affection(
                conn, user_id="u_tier", character_id=1, delta_points=1, daily_limit=100,
            )
            assert aff.affection_score == 80
            assert aff.affection_level == 5
            assert level_up is True

    @pytest.mark.asyncio
    async def test_score_max_cap_at_100(self, isolate_test_database):
        async with get_db(isolate_test_database) as conn:
            await crud.update_character_affection(
                conn, user_id="u_cap", character_id=1,
                updates=CharacterAffectionUpdate(affection_score=98),
            )
            aff, gain, _level_up = await crud.increment_affection(
                conn, user_id="u_cap", character_id=1, delta_points=10, daily_limit=100,
            )
            assert aff.affection_score == 100
            assert gain == 10
            assert aff.affection_level == 5

    @pytest.mark.asyncio
    async def test_daily_cap_and_midnight_rollover(self, isolate_test_database):
        async with get_db(isolate_test_database) as conn:
            day1 = "2026-08-23"
            aff1, gain1, _ = await crud.increment_affection(
                conn, user_id="u_daily", character_id=1, delta_points=10, daily_limit=15, today_date_str=day1,
            )
            assert gain1 == 10
            assert aff1.daily_points_earned == 10

            aff2, gain2, _ = await crud.increment_affection(
                conn, user_id="u_daily", character_id=1, delta_points=10, daily_limit=15, today_date_str=day1,
            )
            assert gain2 == 5
            assert aff2.daily_points_earned == 15
            assert aff2.affection_score == 15

            aff3, gain3, _ = await crud.increment_affection(
                conn, user_id="u_daily", character_id=1, delta_points=3, daily_limit=15, today_date_str=day1,
            )
            assert gain3 == 0
            assert aff3.daily_points_earned == 15
            assert aff3.affection_score == 15

            day2 = "2026-08-24"
            aff4, gain4, _ = await crud.increment_affection(
                conn, user_id="u_daily", character_id=1, delta_points=8, daily_limit=15, today_date_str=day2,
            )
            assert gain4 == 8
            assert aff4.daily_points_earned == 8
            assert aff4.affection_score == 23
            assert aff4.affection_level == 2
