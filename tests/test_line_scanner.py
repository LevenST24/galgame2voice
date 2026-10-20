"""
Unit and property tests for LineScanner and validate_line.
Verifies chunking invariance, early emission, performance, and all required fixtures.
"""

from __future__ import annotations

import json
import random
import time

from galgame2voice.services.chat_pipelines.line_scanner import (
    EMOTIONS,
    FX,
    MOTIONS,
    LineScanner,
    ScannedLine,
    aggregate,
    validate_line,
)

FIXTURES: dict[str, str] = {
    "normal": json.dumps({
        "tts": {"speed": 1.05, "temp": 0.95, "emotion": "gentle"},
        "lines": [
            {
                "zh": "你好呀，指挥官！",
                "ja": "こんにちは、指揮官！",
                "emotion": "gentle",
                "act": {"face": "smile", "pose": "b", "motion": "nod", "blush": False, "fx": None},
            },
            {
                "zh": "今天天气真好呢。",
                "ja": "今日はいい天気ですね。",
                "emotion": "happy",
                "act": {"face": "happy", "pose": "a", "motion": "bounce", "blush": True, "fx": "sparkle"},
            },
        ],
    }),
    "fenced": (
        "```json\n"
        + json.dumps({
            "tts": {"speed": 1.0, "temp": 1.0, "emotion": "happy"},
            "lines": [
                {"zh": "测试围栏", "ja": "フェンスのテスト", "emotion": "happy", "act": {"face": "smile"}},
            ],
        })
        + "\n```"
    ),
    "preamble": (
        "好的，这是为你准备的回复内容：\n```json\n"
        + json.dumps({
            "tts": {"speed": 0.9, "temp": 1.0, "emotion": "cool"},
            "lines": [
                {"zh": "有前导文字的回复", "ja": "前置きのある返信", "emotion": "cool", "act": {"face": "neutral"}},
            ],
        })
        + "\n```"
    ),
    "tts_after_lines": json.dumps({
        "lines": [
            {"zh": "第一行", "ja": "一行目", "emotion": "gentle", "act": {"face": "smile"}},
            {"zh": "第二行", "ja": "二行目", "emotion": "happy", "act": {"face": "smile"}},
        ],
        "tts": {"speed": 1.1, "temp": 0.9, "emotion": "happy"},
    }),
    "escaped_quotes_and_backslashes": (
        '{"lines": [{"zh": "她说：\\"你好\\\\世界\\"！", "ja": "彼女は「こんにちは\\\\世界」と言った！", "emotion": "gentle", "act": {"face": "smile"}}]}'
    ),
    "unicode_and_surrogate_pairs": (
        '{"lines": [{"zh": "\\u4f60\\u597d \\uD83D\\uDE0A", "ja": "\\u3053\\u3093\\u306b\\u3061\\u306f \\uD83D\\uDE0A", "emotion": "happy", "act": {"face": "smile"}}]}'
    ),
    "raw_emoji": json.dumps({
        "lines": [
            {
                "zh": "指挥官，加油！🎉✨😊",
                "ja": "指揮官、頑張って！🎉✨😊",
                "emotion": "happy",
                "act": {"face": "wink", "fx": "sparkle"},
            }
        ]
    }, ensure_ascii=False),
    "braces_and_brackets_in_strings": json.dumps({
        "lines": [
            {
                "zh": "代码是 function() { return [1, 2]; } 哦",
                "ja": "コードは function() { return [1, 2]; } ですよ",
                "emotion": "gentle",
                "act": {"face": "smile"},
            }
        ]
    }),
    "nested_act": json.dumps({
        "lines": [
            {
                "zh": "嵌套动作测试",
                "ja": "ネストされたアクションのテスト",
                "emotion": "shy",
                "act": {
                    "face": "blush",
                    "pose": "pose_shy",
                    "motion": "fidget",
                    "blush": True,
                    "fx": "heart",
                },
            }
        ]
    }),
    "invalid_act_values": json.dumps({
        "lines": [
            {
                "zh": "部分非法 act 字段",
                "ja": "一部不正な act フィールド",
                "emotion": "gentle",
                "act": {
                    "face": "invalid/face/with/slashes",
                    "pose": "valid_pose",
                    "motion": "flying_high",
                    "blush": "yes_very_blushing",
                    "fx": 999,
                },
            }
        ]
    }),
    "outfit_and_extra_act_keys": json.dumps({
        "lines": [
            {
                "zh": "非法 keys 测试",
                "ja": "不正なキーのテスト",
                "emotion": "gentle",
                "act": {
                    "face": "smile",
                    "outfit": "maid_uniform",
                    "extra_param": 42,
                },
            }
        ]
    }),
    "missing_or_invalid_emotion": json.dumps({
        "lines": [
            {
                "zh": "没有有效情绪",
                "ja": "有効な感情なし",
                "emotion": "super_excited",
                "act": {"face": "smile"},
            },
            {
                "zh": "缺失情绪字段",
                "ja": "感情フィールドなし",
                "act": {"face": "smile"},
            },
        ]
    }),
    "empty_zh_and_ja": json.dumps({
        "lines": [
            {"zh": "   ", "ja": "", "emotion": "gentle"},
            {"zh": "有效行", "ja": "有効な行", "emotion": "gentle"},
        ]
    }),
    "non_object_element": json.dumps({
        "lines": [
            "invalid non-object element string",
            {"zh": "第二行", "ja": "二行目", "emotion": "gentle"},
        ]
    }),
    "truncated_inside_element": '{"lines": [{"zh": "行1", "ja": "行1", "emotion": "gentle"}, {"zh": "截断行", "ja": "中',
    "truncated_inside_string": '{"lines": [{"zh": "截断字符串',
    "truncated_between_elements": '{"lines": [{"zh": "行1", "ja": "行1", "emotion": "gentle"}, ',
    "legacy_format": json.dumps({
        "tts": {"speed": 1.0, "temp": 0.95, "emotion": "gentle"},
        "chinese": "旧格式中文内容",
        "japanese": "旧フォーマット日本語内容",
    }),
    "trailing_chatter": (
        json.dumps({
            "lines": [
                {"zh": "回答结束", "ja": "回答終了", "emotion": "gentle"}
            ]
        })
        + "\n希望这个回答对您有帮助！如果有其他问题请随时问我。"
    ),
    "empty_lines_array": json.dumps({"lines": []}),
}


# =====================================================================
# Unit Tests for validate_line & aggregate
# =====================================================================

def test_constants() -> None:
    assert "gentle" in EMOTIONS
    assert len(EMOTIONS) == 7
    assert "nod" in MOTIONS
    assert len(MOTIONS) == 8
    assert "sparkle" in FX
    assert len(FX) == 6


def test_validate_line_success() -> None:
    obj = {
        "zh": "  你好  ",
        "ja": " こんにちは ",
        "emotion": "gentle",
        "act": {
            "face": "smile",
            "pose": "pose_a",
            "motion": "nod",
            "blush": False,
            "fx": None,
        },
    }
    line, issues = validate_line(obj, ordinal=2)
    assert line is not None
    assert line.ordinal == 2
    assert line.zh == "你好"
    assert line.ja == "こんにちは"
    assert line.emotion == "gentle"
    assert line.act == {
        "face": "smile",
        "pose": "pose_a",
        "motion": "nod",
        "blush": False,
        "fx": None,
    }
    assert issues == []


def test_validate_line_dropped_empty() -> None:
    line, issues = validate_line({"zh": "   ", "ja": ""})
    assert line is None
    assert any("empty" in issue for issue in issues)


def test_validate_line_capping() -> None:
    long_zh = "中" * 2500
    line, issues = validate_line({"zh": long_zh, "ja": "テスト"})
    assert line is not None
    assert len(line.zh) == 2000
    assert any("exceeds 2000" in issue for issue in issues)


def test_validate_line_invalid_emotion() -> None:
    line, issues = validate_line({"zh": "测试", "emotion": "unknown_emo"})
    assert line is not None
    assert line.emotion is None
    assert any("invalid or missing emotion" in issue for issue in issues)


def test_validate_line_act_field_isolation() -> None:
    obj = {
        "zh": "测试",
        "emotion": "happy",
        "act": {
            "face": "invalid/face",
            "pose": "valid_pose",
            "motion": "dance",  # invalid
            "blush": "not_bool",  # invalid
            "fx": "laser",  # invalid
            "outfit": "swimsuit",  # rejected
            "custom": 123,  # rejected
        },
    }
    line, issues = validate_line(obj)
    assert line is not None
    # Only valid pose is kept
    assert line.act == {"pose": "valid_pose"}
    assert len(issues) == 6


def test_validate_line_non_object() -> None:
    line, issues = validate_line("not a dict")
    assert line is None
    assert any("non-object" in issue for issue in issues)


def test_aggregate() -> None:
    lines = [
        ScannedLine(ordinal=0, zh="你好", ja="こんにちは", emotion="gentle", act={}),
        ScannedLine(ordinal=1, zh="指挥官", ja="指揮官", emotion="happy", act={}),
    ]
    zh, ja = aggregate(lines)
    assert zh == "你好指挥官"
    assert ja == "こんにちは指揮官"
    assert aggregate([]) == ("", "")


# =====================================================================
# Fixture Behavior Tests
# =====================================================================

def test_fixture_normal() -> None:
    scanner = LineScanner()
    lines = scanner.feed(FIXTURES["normal"])
    result = scanner.finish()
    assert result.format == "lines"
    assert len(result.lines) == 2
    assert len(lines) == 2
    assert result.tts == {"speed": 1.05, "temp": 0.95, "emotion": "gentle"}
    assert result.truncated is False
    assert result.trailing_text == ""


def test_fixture_fenced() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["fenced"])
    result = scanner.finish()
    assert result.format == "lines"
    assert len(result.lines) == 1
    assert result.truncated is False


def test_fixture_preamble() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["preamble"])
    result = scanner.finish()
    assert result.format == "lines"
    assert len(result.lines) == 1
    assert result.truncated is False


def test_fixture_tts_after_lines() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["tts_after_lines"])
    result = scanner.finish()
    assert result.format == "lines"
    assert len(result.lines) == 2
    assert result.tts == {"speed": 1.1, "temp": 0.9, "emotion": "happy"}
    assert result.truncated is False


def test_fixture_escaped_quotes_and_backslashes() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["escaped_quotes_and_backslashes"])
    result = scanner.finish()
    assert len(result.lines) == 1
    assert result.lines[0].zh == '她说："你好\\世界"！'
    assert result.lines[0].ja == '彼女は「こんにちは\\世界」と言った！'


def test_fixture_unicode_and_surrogate_pairs() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["unicode_and_surrogate_pairs"])
    result = scanner.finish()
    assert len(result.lines) == 1
    assert result.lines[0].zh == "你好 😊"
    assert result.lines[0].ja == "こんにちは 😊"


def test_fixture_raw_emoji() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["raw_emoji"])
    result = scanner.finish()
    assert len(result.lines) == 1
    assert "😊" in result.lines[0].zh


def test_fixture_braces_and_brackets_in_strings() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["braces_and_brackets_in_strings"])
    result = scanner.finish()
    assert len(result.lines) == 1
    assert "{ return [1, 2]; }" in result.lines[0].zh


def test_fixture_nested_act() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["nested_act"])
    result = scanner.finish()
    assert len(result.lines) == 1
    assert result.lines[0].act["face"] == "blush"
    assert result.lines[0].act["blush"] is True


def test_fixture_invalid_act_values() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["invalid_act_values"])
    result = scanner.finish()
    assert len(result.lines) == 1
    assert result.lines[0].act == {"pose": "valid_pose"}


def test_fixture_outfit_and_extra_act_keys() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["outfit_and_extra_act_keys"])
    result = scanner.finish()
    assert len(result.lines) == 1
    assert result.lines[0].act == {"face": "smile"}


def test_fixture_missing_or_invalid_emotion() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["missing_or_invalid_emotion"])
    result = scanner.finish()
    assert len(result.lines) == 2
    assert result.lines[0].emotion is None
    assert result.lines[1].emotion is None


def test_fixture_empty_zh_and_ja() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["empty_zh_and_ja"])
    result = scanner.finish()
    assert len(result.lines) == 1
    assert result.lines[0].ordinal == 1
    assert result.lines[0].zh == "有效行"


def test_fixture_non_object_element() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["non_object_element"])
    result = scanner.finish()
    assert len(result.lines) == 1
    assert result.lines[0].ordinal == 1
    assert result.lines[0].zh == "第二行"


def test_fixture_truncated_inside_element() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["truncated_inside_element"])
    result = scanner.finish()
    assert result.truncated is True
    assert len(result.lines) == 1
    assert result.lines[0].zh == "行1"


def test_fixture_truncated_inside_string() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["truncated_inside_string"])
    result = scanner.finish()
    assert result.truncated is True
    assert len(result.lines) == 0


def test_fixture_truncated_between_elements() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["truncated_between_elements"])
    result = scanner.finish()
    assert result.truncated is True
    assert len(result.lines) == 1


def test_fixture_legacy_format() -> None:
    scanner = LineScanner()
    lines = scanner.feed(FIXTURES["legacy_format"])
    result = scanner.finish()
    assert result.format == "legacy"
    assert lines == []
    assert result.lines == []
    assert result.tts == {"speed": 1.0, "temp": 0.95, "emotion": "gentle"}
    assert result.truncated is False


def test_fixture_trailing_chatter() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["trailing_chatter"])
    result = scanner.finish()
    assert len(result.lines) == 1
    assert "希望这个回答对您有帮助" in result.trailing_text
    assert result.truncated is False


def test_fixture_empty_lines_array() -> None:
    scanner = LineScanner()
    scanner.feed(FIXTURES["empty_lines_array"])
    result = scanner.finish()
    assert result.format == "lines"
    assert result.lines == []
    assert result.truncated is False


# =====================================================================
# Property: Chunking Invariance
# =====================================================================

def test_chunking_invariance_all_fixtures() -> None:
    rng = random.Random(20261019)

    for name, text in FIXTURES.items():
        # 1. Whole feed (baseline)
        base_scanner = LineScanner()
        base_emitted = base_scanner.feed(text)
        base_res = base_scanner.finish()

        # 2. Char by char feed
        char_scanner = LineScanner()
        char_emitted: list[ScannedLine] = []
        for c in text:
            char_emitted.extend(char_scanner.feed(c))
        char_res = char_scanner.finish()

        assert char_res == base_res, f"Char-by-char mismatch in fixture '{name}'"
        assert char_emitted == base_emitted, f"Char-by-char emitted lines mismatch in fixture '{name}'"

        # 3. Every 2-way split
        for split_pos in range(len(text) + 1):
            s_scanner = LineScanner()
            s_emitted = s_scanner.feed(text[:split_pos])
            s_emitted.extend(s_scanner.feed(text[split_pos:]))
            s_res = s_scanner.finish()
            assert s_res == base_res, f"2-way split at {split_pos} failed in fixture '{name}'"
            assert s_emitted == base_emitted, f"2-way split emitted mismatch at {split_pos} in fixture '{name}'"

        # 4. 50 random partitions (seeded)
        for _ in range(50):
            n_cuts = rng.randint(1, min(10, max(1, len(text))))
            cut_points = sorted(rng.sample(range(len(text) + 1), min(n_cuts, len(text) + 1)))
            r_scanner = LineScanner()
            r_emitted: list[ScannedLine] = []
            prev = 0
            for pt in cut_points:
                r_emitted.extend(r_scanner.feed(text[prev:pt]))
                prev = pt
            r_emitted.extend(r_scanner.feed(text[prev:]))
            r_res = r_scanner.finish()
            assert r_res == base_res, f"Random partition failed in fixture '{name}'"
            assert r_emitted == base_emitted, f"Random partition emitted mismatch in fixture '{name}'"


# =====================================================================
# Property: Early Emission
# =====================================================================

def test_early_emission() -> None:
    text = FIXTURES["normal"]
    # Find the position right after the first line element's closing '}'
    first_zh_idx = text.find('"zh":')
    assert first_zh_idx > 0
    second_zh_idx = text.find('"zh":', first_zh_idx + 1)
    assert second_zh_idx > first_zh_idx
    line_close_idx = text.rfind("}", first_zh_idx, second_zh_idx) + 1
    assert line_close_idx > first_zh_idx

    scanner = LineScanner()
    # Feed chunk 1: from start up to and including the first line's closing '}'
    first_chunk = text[:line_close_idx]
    emitted = scanner.feed(first_chunk)

    # First line MUST be emitted now, BEFORE array closes or full reply exists
    assert len(emitted) == 1
    assert emitted[0].ordinal == 0
    assert emitted[0].zh == "你好呀，指挥官！"

    # Now feed the rest
    remainder = text[line_close_idx:]
    emitted_rest = scanner.feed(remainder)
    assert len(emitted_rest) == 1
    assert emitted_rest[0].ordinal == 1
    assert emitted_rest[0].zh == "今天天气真好呢。"

    result = scanner.finish()
    assert len(result.lines) == 2
    assert result.truncated is False


# =====================================================================
# Property: Performance
# =====================================================================

def test_performance_200kb_char_by_char() -> None:
    # Build approx 200 KB valid reply
    single_line = {
        "zh": "这是一段用于测试长文本性能的中文对话内容，包含一些动作和表情。",
        "ja": "これは長文性能テスト用の日本語会話内容であり、アクションと表情を含みます。",
        "emotion": "gentle",
        "act": {"face": "smile", "pose": "pose_a", "motion": "nod", "blush": False, "fx": None},
    }
    # ~250 bytes per line * 800 lines = ~200 KB
    lines_array = [dict(single_line) for _ in range(800)]
    big_payload = json.dumps({"tts": {"speed": 1.0, "temp": 1.0, "emotion": "gentle"}, "lines": lines_array})
    assert len(big_payload) >= 200_000

    scanner = LineScanner()
    t0 = time.perf_counter()
    for c in big_payload:
        scanner.feed(c)
    result = scanner.finish()
    elapsed = time.perf_counter() - t0

    assert len(result.lines) == 800
    assert result.truncated is False
    assert elapsed < 2.0, f"Expected < 2.0s, got {elapsed:.3f}s"


def test_validate_line_normalizes_emotion_case_and_spacing() -> None:
    line, issues = validate_line({"zh": "嗯。", "ja": "うん。", "emotion": " Happy "})
    assert line is not None
    assert line.emotion == "happy"
    assert not issues
