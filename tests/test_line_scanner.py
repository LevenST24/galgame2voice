"""
Unit and property tests for LineScanner and validate_line.
Verifies chunking invariance, early emission, and all required fixtures.
"""

from __future__ import annotations

import json
import random

from galgame2voice.services.chat_pipelines.line_scanner import (
    EMOTIONS,
    FX,
    MOTIONS,
    LineScanner,
    ScannedLine,
    repair_line_quotes,
    validate_line,
)

def _line(zh: str, ja: str, emotion: str | None = None, act: dict | None = None) -> dict:
    item: dict = {"zh": zh, "ja": ja}
    if emotion is not None:
        item["emotion"] = emotion
    if act is not None:
        item["act"] = act
    return item


def _lines_fixture(
    *lines,
    tts: dict | None = None,
    tts_after: bool = False,
    ensure_ascii: bool = True,
    prefix: str = "",
    suffix: str = "",
) -> str:
    payload: dict = {}
    if tts is not None and not tts_after:
        payload["tts"] = tts
    payload["lines"] = list(lines)
    if tts is not None and tts_after:
        payload["tts"] = tts
    return prefix + json.dumps(payload, ensure_ascii=ensure_ascii) + suffix


def _scan_fixture(name: str):
    scanner = LineScanner()
    lines = scanner.feed(FIXTURES[name])
    return lines, scanner.finish()


FIXTURES: dict[str, str] = {
    "normal": _lines_fixture(
        _line("你好呀，指挥官！", "こんにちは、指揮官！", "gentle", {"face": "smile", "pose": "b", "motion": "nod", "blush": False, "fx": None}),
        _line("今天天气真好呢。", "今日はいい天気ですね。", "happy", {"face": "happy", "pose": "a", "motion": "bounce", "blush": True, "fx": "sparkle"}),
        tts={"speed": 1.05, "temp": 0.95, "emotion": "gentle"},
    ),
    "fenced": _lines_fixture(
        _line("测试围栏", "フェンスのテスト", "happy", {"face": "smile"}),
        tts={"speed": 1.0, "temp": 1.0, "emotion": "happy"},
        prefix="```json\n",
        suffix="\n```",
    ),
    "preamble": _lines_fixture(
        _line("有前导文字的回复", "前置きのある返信", "cool", {"face": "neutral"}),
        tts={"speed": 0.9, "temp": 1.0, "emotion": "cool"},
        prefix="好的，这是为你准备的回复内容：\n```json\n",
        suffix="\n```",
    ),
    "tts_after_lines": _lines_fixture(
        _line("第一行", "一行目", "gentle", {"face": "smile"}),
        _line("第二行", "二行目", "happy", {"face": "smile"}),
        tts={"speed": 1.1, "temp": 0.9, "emotion": "happy"},
        tts_after=True,
    ),
    "escaped_quotes_and_backslashes": (
        '{"lines": [{"zh": "她说：\\"你好\\\\世界\\"！", "ja": "彼女は「こんにちは\\\\世界」と言った！", "emotion": "gentle", "act": {"face": "smile"}}]}'
    ),
    "unicode_and_surrogate_pairs": (
        '{"lines": [{"zh": "\\u4f60\\u597d \\uD83D\\uDE0A", "ja": "\\u3053\\u3093\\u306b\\u3061\\u306f \\uD83D\\uDE0A", "emotion": "happy", "act": {"face": "smile"}}]}'
    ),
    "raw_emoji": _lines_fixture(
        _line("指挥官，加油！🎉✨😊", "指揮官、頑張って！🎉✨😊", "happy", {"face": "wink", "fx": "sparkle"}),
        ensure_ascii=False,
    ),
    "braces_and_brackets_in_strings": _lines_fixture(
        _line("代码是 function() { return [1, 2]; } 哦", "コードは function() { return [1, 2]; } ですよ", "gentle", {"face": "smile"}),
    ),
    "nested_act": _lines_fixture(
        _line("嵌套动作测试", "ネストされたアクションのテスト", "shy", {"face": "blush", "pose": "pose_shy", "motion": "fidget", "blush": True, "fx": "heart"}),
    ),
    "invalid_act_values": _lines_fixture(
        _line("部分非法 act 字段", "一部不正な act フィールド", "gentle", {"face": "invalid/face/with/slashes", "pose": "valid_pose", "motion": "flying_high", "blush": "yes_very_blushing", "fx": 999}),
    ),
    "outfit_and_extra_act_keys": _lines_fixture(
        _line("非法 keys 测试", "不正なキーのテスト", "gentle", {"face": "smile", "outfit": "maid_uniform", "extra_param": 42}),
    ),
    "missing_or_invalid_emotion": _lines_fixture(
        _line("没有有效情绪", "有効な感情なし", "super_excited", {"face": "smile"}),
        _line("缺失情绪字段", "感情フィールドなし", act={"face": "smile"}),
    ),
    "empty_zh_and_ja": _lines_fixture(
        _line("   ", "", "gentle"),
        _line("有效行", "有効な行", "gentle"),
    ),
    "non_object_element": _lines_fixture(
        "invalid non-object element string",
        _line("第二行", "二行目", "gentle"),
    ),
    "truncated_inside_element": '{"lines": [{"zh": "行1", "ja": "行1", "emotion": "gentle"}, {"zh": "截断行", "ja": "中',
    "truncated_inside_string": '{"lines": [{"zh": "截断字符串',
    "truncated_between_elements": '{"lines": [{"zh": "行1", "ja": "行1", "emotion": "gentle"}, ',
    "legacy_format": json.dumps({
        "tts": {"speed": 1.0, "temp": 0.95, "emotion": "gentle"},
        "chinese": "旧格式中文内容",
        "japanese": "旧フォーマット日本語内容",
    }),
    "trailing_chatter": _lines_fixture(
        _line("回答结束", "回答終了", "gentle"),
        suffix="\n希望这个回答对您有帮助！如果有其他问题请随时问我。",
    ),
    "empty_lines_array": _lines_fixture(),
    "top_level_array": json.dumps(
        [
            _line("早上好。", "おはよう。", "happy", {"face": "smile"}),
            _line("今天天气真好。", "今日はいい天気だね。", "gentle", {"motion": "nod"}),
        ],
        ensure_ascii=False,
    ),
    "flat_object": json.dumps(
        _line("早上好。", "おはよう。", "happy", {"face": "smile"}),
        ensure_ascii=False,
    ),
    "unescaped_quotes": (
        '{"lines": [{"zh": "他说"早上好"就走了。", "ja": "「おはよう」と言って行った。", "emotion": "happy"}]}'
    ),
}


# =====================================================================
# Unit Tests for validate_line
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


# =====================================================================
# Fixture Behavior Tests
# =====================================================================

def test_fixture_normal() -> None:
    lines, result = _scan_fixture("normal")
    assert result.format == "lines"
    assert len(result.lines) == 2
    assert len(lines) == 2
    assert result.tts == {"speed": 1.05, "temp": 0.95, "emotion": "gentle"}
    assert result.truncated is False
    assert result.trailing_text == ""


def test_fixture_preamble() -> None:
    _, result = _scan_fixture("preamble")
    assert result.format == "lines"
    assert len(result.lines) == 1
    assert result.truncated is False


def test_fixture_tts_after_lines() -> None:
    _, result = _scan_fixture("tts_after_lines")
    assert result.format == "lines"
    assert len(result.lines) == 2
    assert result.tts == {"speed": 1.1, "temp": 0.9, "emotion": "happy"}
    assert result.truncated is False


def test_fixture_braces_and_brackets_in_strings() -> None:
    _, result = _scan_fixture("braces_and_brackets_in_strings")
    assert len(result.lines) == 1
    assert "{ return [1, 2]; }" in result.lines[0].zh


def test_fixture_nested_act() -> None:
    _, result = _scan_fixture("nested_act")
    assert len(result.lines) == 1
    assert result.lines[0].act["face"] == "blush"
    assert result.lines[0].act["blush"] is True


def test_fixture_invalid_act_values() -> None:
    _, result = _scan_fixture("invalid_act_values")
    assert len(result.lines) == 1
    assert result.lines[0].act == {"pose": "valid_pose"}


def test_fixture_missing_or_invalid_emotion() -> None:
    _, result = _scan_fixture("missing_or_invalid_emotion")
    assert len(result.lines) == 2
    assert result.lines[0].emotion is None
    assert result.lines[1].emotion is None


def test_fixture_empty_zh_and_ja() -> None:
    _, result = _scan_fixture("empty_zh_and_ja")
    assert len(result.lines) == 1
    assert result.lines[0].ordinal == 1
    assert result.lines[0].zh == "有效行"


def test_fixture_non_object_element() -> None:
    _, result = _scan_fixture("non_object_element")
    assert len(result.lines) == 1
    assert result.lines[0].ordinal == 1
    assert result.lines[0].zh == "第二行"


def test_fixture_legacy_format() -> None:
    lines, result = _scan_fixture("legacy_format")
    assert result.format == "unknown"
    assert lines == []
    assert result.lines == []
    assert result.tts == {"speed": 1.0, "temp": 0.95, "emotion": "gentle"}
    assert result.truncated is False


def test_fixture_trailing_chatter() -> None:
    _, result = _scan_fixture("trailing_chatter")
    assert len(result.lines) == 1
    assert "希望这个回答对您有帮助" in result.trailing_text
    assert result.truncated is False


def test_fixture_empty_lines_array() -> None:
    _, result = _scan_fixture("empty_lines_array")
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


def test_validate_line_normalizes_emotion_case_and_spacing() -> None:
    line, issues = validate_line({"zh": "嗯。", "ja": "うん。", "emotion": " Happy "})
    assert line is not None
    assert line.emotion == "happy"
    assert not issues


def test_unescaped_quotes_in_zh_whole_and_chunked() -> None:
    text = FIXTURES["unescaped_quotes"]

    whole_scanner = LineScanner()
    whole_emitted = whole_scanner.feed(text)
    whole_result = whole_scanner.finish()

    assert len(whole_emitted) == 1
    assert len(whole_result.lines) == 1
    assert whole_result.lines[0].zh == '他说"早上好"就走了。'
    assert whole_result.lines[0].ja == "「おはよう」と言って行った。"
    assert whole_result.lines[0].emotion == "happy"
    assert whole_result.issues == []
    assert whole_result.truncated is False

    for step in (1, 7):
        chunk_scanner = LineScanner()
        chunk_emitted: list[ScannedLine] = []
        for idx in range(0, len(text), step):
            chunk_emitted.extend(chunk_scanner.feed(text[idx : idx + step]))
        chunk_result = chunk_scanner.finish()
        assert chunk_emitted == whole_emitted
        assert chunk_result == whole_result

    comma_scanner = LineScanner()
    comma_scanner.feed(
        '{"lines": [{"zh": "他说"早上好", 然后走了。", "ja": "「おはよう」と言って行った。", "emotion": "happy"}]}'
    )
    comma_result = comma_scanner.finish()
    assert len(comma_result.lines) == 1
    assert comma_result.lines[0].zh == '他说"早上好", 然后走了。'


def test_unescaped_quotes_in_ja_preserves_act() -> None:
    raw = (
        '{"lines": [{"zh": "他说早上好。", '
        '"ja": "彼は"おはよう"と言って行った。", '
        '"emotion": "gentle", "act": {"face": "smile"}}]}'
    )
    scanner = LineScanner()
    emitted = scanner.feed(raw)
    result = scanner.finish()

    assert len(emitted) == 1
    assert len(result.lines) == 1
    assert result.lines[0].zh == "他说早上好。"
    assert result.lines[0].ja == '彼は"おはよう"と言って行った。'
    assert result.lines[0].emotion == "gentle"
    assert result.lines[0].act == {"face": "smile"}
    assert result.issues == []


def test_escaped_quotes_and_broken_json_unchanged() -> None:
    emitted, escaped_res = _scan_fixture("escaped_quotes_and_backslashes")
    assert len(emitted) == 1
    assert len(escaped_res.lines) == 1
    assert escaped_res.lines[0].zh == '她说："你好\\世界"！'
    assert escaped_res.lines[0].act == {"face": "smile"}
    assert escaped_res.issues == []

    for broken in (
        '{"lines": [{"zh" "他说\\"早上好\\"就走了。", "ja": "「おはよう」", "emotion": "happy"}]}',
        '{"lines": [{"zh": "他说"早上好"就走了。", "ja" "「おはよう」", "emotion": "happy"}]}',
    ):
        scanner = LineScanner()
        assert scanner.feed(broken) == []
        res = scanner.finish()
        assert res.format == "lines"
        assert res.lines == []
        assert any("JSON decode error in line 0" in issue for issue in res.issues)


def test_repair_line_quotes_helper() -> None:
    raw_zh = '{"zh": "他说"早上好"就走了。", "ja": "「おはよう」と言って行った。", "emotion": "happy"}'
    repaired_zh = repair_line_quotes(raw_zh)
    assert json.loads(repaired_zh) == {
        "zh": '他说"早上好"就走了。',
        "ja": "「おはよう」と言って行った。",
        "emotion": "happy",
    }

    raw_ja_act = '{"zh": "早安。", "ja": "彼は"おはよう"と言った。", "act": {"face": "smile"}}'
    repaired_ja_act = repair_line_quotes(raw_ja_act)
    assert '"act": {"face": "smile"}' in repaired_ja_act
    assert json.loads(repaired_ja_act) == {
        "zh": "早安。",
        "ja": '彼は"おはよう"と言った。',
        "act": {"face": "smile"},
    }

    raw_emo = '{"zh": "早安。", "ja": "おはよう。", "emotion": ""happy""}'
    assert json.loads(repair_line_quotes(raw_emo))["emotion"] == '"happy"'

    valid = '{"zh": "她说：\\"你好\\"！", "ja": "こんにちは！", "emotion": "gentle"}'
    assert repair_line_quotes(valid) == valid

    broken = '{"zh": "他说"早上好"就走了。", "ja" "「おはよう」"}'
    assert repair_line_quotes(broken) == broken



def _feed_by_step(text: str, step: int | None):
    scanner = LineScanner()
    if step is None:
        emitted = scanner.feed(text)
    else:
        emitted = []
        for idx in range(0, len(text), step):
            emitted.extend(scanner.feed(text[idx : idx + step]))
    return emitted, scanner.finish()


def test_top_level_array_whole_and_chunked_1_and_7() -> None:
    payload = [
        {"zh": "早上好。", "ja": "おはよう。", "emotion": "happy", "act": {"face": "smile"}},
        {"zh": "今天天气真好。", "ja": "今日はいい天気だね。", "emotion": "gentle", "act": {"motion": "nod"}},
    ]
    variants = (
        json.dumps(payload, ensure_ascii=False),
        "[\n  " + ",\n  ".join(json.dumps(item, ensure_ascii=False) for item in payload) + "\n]",
    )
    for raw in variants:
        for step in (None, 1, 7):
            emitted, result = _feed_by_step(raw, step)
            assert result.format == "lines"
            assert result.truncated is False
            assert emitted == result.lines
            assert [(line.ordinal, line.zh, line.ja, line.emotion, line.act) for line in result.lines] == [
                (0, "早上好。", "おはよう。", "happy", {"face": "smile"}),
                (1, "今天天气真好。", "今日はいい天気だね。", "gentle", {"motion": "nod"}),
            ]

    # First element emits immediately before the second element or closing ']' arrives
    compact = variants[0]
    first_close = compact.index("}}", compact.index('"face"')) + 2
    early_scanner = LineScanner()
    first_batch = early_scanner.feed(compact[:first_close])
    assert len(first_batch) == 1 and first_batch[0].zh == "早上好。"
    second_batch = early_scanner.feed(compact[first_close:])
    assert len(second_batch) == 1 and second_batch[0].zh == "今天天气真好。"
    assert early_scanner.finish().truncated is False


def test_flat_root_object_emits_single_line() -> None:
    raw = json.dumps(
        {"zh": "早上好。", "ja": "おはよう。", "emotion": "happy", "act": {"face": "smile"}},
        ensure_ascii=False,
    )
    for step in (None, 1, 7):
        emitted, result = _feed_by_step(raw, step)
        assert result.format == "lines"
        assert result.truncated is False
        assert emitted == result.lines
        assert len(result.lines) == 1
        assert (
            result.lines[0].ordinal,
            result.lines[0].zh,
            result.lines[0].ja,
            result.lines[0].emotion,
            result.lines[0].act,
        ) == (0, "早上好。", "おはよう。", "happy", {"face": "smile"})

    _, ja_only = _feed_by_step('{"ja": "おはよう。", "emotion": "gentle"}', 7)
    assert ja_only.format == "lines"
    assert len(ja_only.lines) == 1 and ja_only.lines[0].ja == "おはよう。" and ja_only.lines[0].zh == ""

    _, non_str = _feed_by_step('{"zh": 123}', 1)
    assert non_str.format == "unknown"
    assert non_str.lines == []


def test_bracketed_emotion_plain_text_and_legacy_format_unchanged() -> None:
    from galgame2voice.services.streaming_parser import StreamingBilingualParser

    plain = "[happy] 今天真开心！"
    for step in (None, 1, 7):
        emitted, result = _feed_by_step(plain, step)
        assert emitted == []
        assert result.format == "unknown"
        assert result.lines == []
        assert result.truncated is False

    parser = StreamingBilingualParser()
    for idx in range(0, len(plain), 7):
        parser.feed_chunk(plain[idx : idx + 7])
    zh, ja, _ = parser.finalize()
    assert parser.line_scanner.format == "unknown"
    assert zh == "今天真开心！"
    assert "今天真开心！" in ja
    assert parser.get_emotion() == "happy"

    for step in (None, 1, 7):
        legacy_emitted, legacy_res = _feed_by_step(FIXTURES["legacy_format"], step)
        assert legacy_emitted == []
        assert legacy_res.format == "unknown"
        assert legacy_res.lines == []
        assert legacy_res.tts == {"speed": 1.0, "temp": 0.95, "emotion": "gentle"}
        assert legacy_res.truncated is False

