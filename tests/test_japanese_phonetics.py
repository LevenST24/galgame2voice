"""
Tests for Japanese phonetics normalizer and Galgame proper noun pronunciations.
Validates that:
1. "乃愛" is normalized to "のあ" (Noa), preventing pyopenjtalk's erroneous "rino" pronunciation.
2. "お兄" vocatives are normalized to "おにい", preventing "oani" mispronunciations.
3. Other Yuzusoft character names (天音, 来海, 輝耶, 七海, 羽月, 叢雨, 栞那, etc.) are properly normalized.
4. Inline furigana brackets 漢字（ふりがな） are resolved to kana.
5. Stage directions remain properly stripped while preserving dialogue.
"""

import pytest
from galgame2voice.utils.japanese_phonetics import (
    normalize_japanese_furigana,
    normalize_galgame_names_and_readings,
    normalize_japanese_for_tts,
    clean_japanese_parentheses,
)
from galgame2voice.services.gpt_sovits_client import (
    normalize_japanese_for_tts as client_normalize,
    clean_japanese_parentheses as client_clean,
)
from galgame2voice.services.tts_cache_manager import TtsCacheManager


def test_shirayuki_noa_phonetic_normalization():
    """Verifies 白雪乃愛 and 乃愛 are normalized to のあ, never 'rino'."""
    raw1 = "乃愛？"
    assert normalize_japanese_for_tts(raw1) == "のあ？"

    raw2 = "白雪乃愛先輩、こんにちは！"
    assert normalize_japanese_for_tts(raw2) == "しらゆきのあ先輩、こんにちは！"

    raw3 = "乃愛ちゃん、お昼ご飯食べに行こう！"
    assert normalize_japanese_for_tts(raw3) == "のあちゃん、お昼ご飯食べに行こう！"

    raw4 = "乃爱？"  # Simplified Chinese input
    assert normalize_japanese_for_tts(raw4) == "のあ？"


def test_user_dialogue_screenshot_sentence():
    """
    Verifies the exact user dialogue sentence from the screenshot with Tanikaze Amane:
    '乃愛？おや、お兄にしては気にかけてるじゃん。最近は元気みたいだよ、部活で忙しいみたいで、LINEの返信ものんびりしてるけど。……何、お兄って乃愛のこと気になるわけ？'
    """
    raw_sentence = (
        "乃愛？おや、お兄にしては気にかけてるじゃん。最近は元気みたいだよ、部活で忙しいみたいで、"
        "LINEの返信ものんびりしてるけど。……何、お兄って乃愛のこと気になるわけ？"
    )
    normalized = normalize_japanese_for_tts(raw_sentence)

    # 乃愛 must be replaced with のあ
    assert "乃愛" not in normalized
    assert "のあ？" in normalized
    assert "のあのこと" in normalized

    # お兄 vocative must be replaced with おにい
    assert "おにいにしては" in normalized
    assert "おにいって" in normalized


def test_tanikaze_amane_and_riku_phonetics():
    """Verifies Tanikaze Amane and Tanikaze Riku proper nouns."""
    assert normalize_japanese_for_tts("谷風天音") == "たにかぜあまね"
    assert normalize_japanese_for_tts("天音、おはよう") == "あまね、おはよう"
    assert normalize_japanese_for_tts("谷風李空") == "たにかぜりく"
    assert normalize_japanese_for_tts("李空くん") == "りくくん"


def test_other_yuzusoft_heroines_phonetics():
    """Verifies character name readings across Yuzusoft titles."""
    # Kohibari Kurumi (天使☆騒々)
    assert normalize_japanese_for_tts("小雲雀来海") == "こひばりくるみ"
    assert normalize_japanese_for_tts("来海ちゃん") == "くるみちゃん"

    # Hoshikawa Kaguya (天使☆騒々)
    assert normalize_japanese_for_tts("星河輝耶") == "ほしかわかぐや"
    assert normalize_japanese_for_tts("輝耶様") == "かぐや様"

    # Arihara Nanami (RIDDLE JOKER)
    assert normalize_japanese_for_tts("在原七海") == "ありはらななみ"
    assert normalize_japanese_for_tts("七海、ご飯できたよ") == "ななみ、ご飯できたよ"

    # Nijoin Hazuki (RIDDLE JOKER)
    assert normalize_japanese_for_tts("二条院羽月") == "にじょういんはづき"
    assert normalize_japanese_for_tts("羽月先輩") == "はづき先輩"

    # Murasame (千恋＊万花)
    assert normalize_japanese_for_tts("叢雨、どうしたの？") == "むらさめ、どうしたの？"
    assert normalize_japanese_for_tts("丛雨") == "むらさめ"

    # Mayuzuki Kanna (喫茶ステラと死神の蝶)
    assert normalize_japanese_for_tts("明月栞那") == "めいげつかんな"
    assert normalize_japanese_for_tts("栞那さん") == "かんなさん"

    # Saionji Fuuri (天使☆騒々)
    assert normalize_japanese_for_tts("西園寺風莉") == "さいおんじふうり"
    assert normalize_japanese_for_tts("風莉ちゃん") == "ふうりちゃん"


def test_vocatives_and_kinship():
    """Verifies honorifics and vocatives: お兄様, お兄, お姉様, お姉."""
    # お兄様 -> おにいさま
    assert normalize_japanese_for_tts("お兄様、お帰りなさい") == "おにいさま、お帰りなさい"

    # お兄 alone / vocative -> おにい
    assert normalize_japanese_for_tts("お兄、何してるの？") == "おにい、何してるの？"
    assert normalize_japanese_for_tts("お兄！バカ！") == "おにい！バカ！"
    assert normalize_japanese_for_tts("お兄ってさ…") == "おにいってさ…"

    # お兄ちゃん should preserve ちゃん
    assert normalize_japanese_for_tts("お兄ちゃん、大好き！") == "お兄ちゃん、大好き！"

    # お姉様 -> おねえさま
    assert normalize_japanese_for_tts("お姉様、ごきげんよう") == "おねえさま、ごきげんよう"
    assert normalize_japanese_for_tts("お姉、ちょっと聞いて！") == "おねえ、ちょっと聞いて！"


def test_inline_furigana_resolution():
    """Verifies inline furigana extraction: 漢字（ふりがな） -> ふりがな."""
    assert normalize_japanese_for_tts("乃愛（のあ）") == "のあ"
    assert normalize_japanese_for_tts("李空(りく)くん") == "りくくん"
    assert normalize_japanese_for_tts("天音（あまね）ちゃん") == "あまねちゃん"


def test_stage_directions_with_phonetics():
    """Verifies stage directions are still stripped while names are normalized."""
    assert normalize_japanese_for_tts("（ため息）乃愛、おはよう。") == "のあ、おはよう。"
    assert normalize_japanese_for_tts("【怒り】お兄！どこ見てるの！") == "おにい！どこ見てるの！"
    assert normalize_japanese_for_tts("（クスクス笑い）天音ちゃん可愛いね") == "あまねちゃん可愛いね"


def test_cache_key_computation_uses_normalized_text():
    """Verifies that TtsCacheManager.compute_cache_key uses phonetically normalized text."""
    manager = TtsCacheManager()
    key1, clean1, _ = manager.compute_cache_key("乃愛？")
    key2, clean2, _ = manager.compute_cache_key("のあ？")

    assert clean1 == "のあ？"
    assert clean2 == "のあ？"
    assert key1 == key2, "Cache key for '乃愛？' and 'のあ？' must match for deduplication"


def test_dracu_riot_and_more_titles():
    """Verifies DRACU-RIOT!, Tenjin Ranman, and other titles phonetics."""
    assert normalize_japanese_for_tts("矢来美羽") == "やらいみう"
    assert normalize_japanese_for_tts("美羽、待って！") == "みう、待って！"
    assert normalize_japanese_for_tts("布良梓") == "めらあずさ"
    assert normalize_japanese_for_tts("稲叢莉音") == "いなむらりおん"
    assert normalize_japanese_for_tts("千歳佐奈") == "ちとせさな"
    assert normalize_japanese_for_tts("千恋万花") == "せんれんばんか"
    assert normalize_japanese_for_tts("義妹") == "ぎまい"


def test_expanded_stage_cues():
    """Verifies onomatopoeia stage cues like ふふっ, ドキドキ, えへへ are stripped."""
    assert normalize_japanese_for_tts("（ふふっ）どうしたの？") == "どうしたの？"
    assert normalize_japanese_for_tts("（ドキドキ）緊張するな…") == "緊張するな…"
    assert normalize_japanese_for_tts("（えへへ）褒められちゃった！") == "褒められちゃった！"


class TestJapaneseParenthesesCleaner:
    """Edge cases for Japanese parenthetical stage-cue removal."""

    def test_closing_bracket_only(self):
        text = "こんにちは）マスター"
        assert clean_japanese_parentheses(text) == "こんにちはマスター"

    def test_empty_and_whitespace(self):
        assert clean_japanese_parentheses("") == ""
        assert clean_japanese_parentheses("   ") == ""
        assert clean_japanese_parentheses(None) == ""

    def test_pure_text_unchanged(self):
        pure = "先生、準備は完了しました！いつでも出発できます。"
        assert clean_japanese_parentheses(pure) == pure



def test_tts_cleaner_strips_kaomoji_urls_markdown_laugh_w_and_inner_voice():
    """TTS cleaning removes kaomoji, URLs, Markdown wrappers, laugh w's, asterisk markup, and inner-voice labels."""
    # Kaomoji
    assert normalize_japanese_for_tts("(´・ω・｀)") == ""
    assert normalize_japanese_for_tts("(T_T)") == ""
    assert normalize_japanese_for_tts("(≧ω≦)") == ""
    assert normalize_japanese_for_tts("ごめんね (´・ω・｀)") == "ごめんね"
    assert normalize_japanese_for_tts("やったね！(≧ω≦)") == "やったね！"

    # Markdown links, standalone URLs, bold/italic/code wrappers, laugh w
    assert (
        normalize_japanese_for_tts("詳しくは[公式ガイド](https://example.com/guide)を見てね **めっちゃ簡単** www")
        == "詳しくは公式ガイドを見てね めっちゃ簡単"
    )
    assert normalize_japanese_for_tts("見てね https://example.com/guide だよ") == "見てね だよ"
    assert normalize_japanese_for_tts("https://example.com/guide") == ""
    assert normalize_japanese_for_tts("これは`テスト`と__強調__だよｗｗｗ！") == "これはテストと強調だよ！"
    assert normalize_japanese_for_tts("www") == ""

    # Single-asterisk emphasis vs known stage cues
    assert normalize_japanese_for_tts("*本当に*嬉しい！") == "本当に嬉しい！"
    assert normalize_japanese_for_tts("*ため息* もう知らない！") == "もう知らない！"
    assert normalize_japanese_for_tts("*微笑みながら* もう知らない！") == "もう知らない！"
    assert "*" not in normalize_japanese_for_tts("*頬を膨らませる* もう知らない！")

    # Inner voice label prefix (keep spoken monologue, drop label; bare stage cue still stripped)
    assert (
        normalize_japanese_for_tts("【心の声：本当はすごく嬉しい】ふん、付き合ってあげるわ。")
        == "本当はすごく嬉しいふん、付き合ってあげるわ。"
    )
    assert normalize_japanese_for_tts("（心声：其实我很开心）哼！") == "其实我很开心哼！"
    assert normalize_japanese_for_tts("心の声が聞こえる") == "心の声が聞こえる"


def test_inner_voice_monologue_starting_with_action_word_is_preserved():
    """Inner-voice monologues starting with action words like 顔/落ち着/看 must not be stripped as stage cues."""
    assert normalize_japanese_for_tts("【心の声：顔が熱い……！】ふん。") == "顔が熱い……！ふん。"
    assert normalize_japanese_for_tts("【心の声：落ち着け、私！】大丈夫。") == "落ち着け、私！大丈夫。"
    assert normalize_japanese_for_tts("（心声：看他这么努力，真好）加油。") == "看他这么努力，真好加油。"
    assert normalize_japanese_for_tts("【心の声：顔が熱い……！】") == "顔が熱い……！"
    assert normalize_japanese_for_tts("（心の声）") == ""
    assert normalize_japanese_for_tts("【心の声：……】") == ""


def test_laugh_w_cleaning_runs_after_kaomoji_and_respects_boundaries():
    """Laugh w cleaning handles trailing kaomoji, long vowel/punctuation boundaries, and preserves www domains."""
    assert normalize_japanese_for_tts("やったね！www (≧ω≦)") == "やったね！"
    assert normalize_japanese_for_tts("すげーwww マジで！") == "すげー マジで！"
    assert normalize_japanese_for_tts("えっ！？www ちょっと待って！") == "えっ！？ ちょっと待って！"
    assert normalize_japanese_for_tts("www～") == "～"
    assert normalize_japanese_for_tts("詳しくは www.example.com を見てね") == "詳しくは www.example.com を見てね"
    assert normalize_japanese_for_tts("awww!") == "awww!"
    assert normalize_japanese_for_tts("www123") == "www123"


def test_fullwidth_alphanumeric_and_ideographic_zero_inside_brackets_are_preserved():
    assert normalize_japanese_for_tts("（ＯＫ！）") == "ＯＫ！"
    assert normalize_japanese_for_tts("（１２３！）") == "１２３！"
    assert normalize_japanese_for_tts("（〇）") == "〇"
