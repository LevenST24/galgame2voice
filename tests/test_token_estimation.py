"""Keep token budgeting identical while speeding up long mixed-script input."""

import random
import statistics
import time
import tracemalloc

import pytest

from galgame2voice.services.metrics_collector import MetricsCollector
from galgame2voice.services.session_manager import SessionManager


def previous_estimate(text):
    if not text or not isinstance(text, str):
        return 0
    cjk = non_cjk = 0
    for character in text:
        code = ord(character)
        if (0x4E00 <= code <= 0x9FFF or 0x3040 <= code <= 0x309F
                or 0x30A0 <= code <= 0x30FF or 0x3400 <= code <= 0x4DBF):
            cjk += 1
        elif not character.isspace():
            non_cjk += 1
    return max(1, int(cjk * 1.1 + non_cjk / 3.5 + 1))


@pytest.mark.parametrize('value', [None, False, 0, 17, [], {}, '', ' ', '\t\n\r\v\f',
                                  '\x1c\x1d\x1e\x1f', '\u0085\u00a0\u1680\u2003\u2028\u2029\u202f\u205f\u3000',
                                  '\u200b\ufeff', 'Hello 世界！ひらがなカタカナ 12345! 🌸',
                                  '\u303f\u3040\u309f\u30a0\u30ff\u3100',
                                  '\u33ff\u3400\u4dbf\u4dc0\u4dff\u4e00\u9fff\ua000',
                                  '\U00020000\U0002a6df\ud800\udfff'])
def test_budget_and_telemetry_preserve_estimation_for_inputs(value):
    expected = previous_estimate(value)
    assert MetricsCollector.estimate_tokens(value) == expected
    assert SessionManager(':memory:').estimate_tokens(value) == expected


def test_all_unicode_codepoints_in_contiguous_blocks_preserve_estimates():
    for start in range(0, 0x110000, 4096):
        block = ''.join(chr(point) for point in range(start, min(start + 4096, 0x110000)))
        assert MetricsCollector.estimate_tokens(block) == previous_estimate(block), hex(start)


def test_mixed_boundaries_and_whitespace_preserve_rounding():
    rng = random.Random(4207)
    alphabet = 'a中漢あア🌸\t\n\u2003\u200b\x1c\ud800\U00020000'
    for _ in range(200):
        text = ''.join(rng.choice(alphabet) for _ in range(rng.randrange(1, 300)))
        assert MetricsCollector.estimate_tokens(text) == previous_estimate(text)


@pytest.mark.wall_clock
@pytest.mark.parametrize(('fragment', 'repeats'), [
    pytest.param('中a ', 120000, id='mixed-alternating'),
    pytest.param('中 ', 180000, id='cjk-and-space'),
    pytest.param('', 0, id='diverse-unicode'),
])
def test_long_alternating_text_does_not_severely_regress(fragment, repeats):
    text = (fragment * repeats if fragment else
            ''.join(chr(point) for point in range(0x30000, 0x50000)) * 4)
    expected = previous_estimate(text)
    samples = {'previous': [], 'current': []}
    algorithms = [('previous', previous_estimate), ('current', MetricsCollector.estimate_tokens)]
    for index in range(5):
        for name, algorithm in algorithms if index % 2 == 0 else reversed(algorithms):
            started = time.perf_counter()
            assert algorithm(text) == expected
            samples[name].append(time.perf_counter() - started)
    previous = statistics.median(samples['previous'])
    current = statistics.median(samples['current'])
    assert current <= previous * 1.5, (samples, current / previous)


def test_diverse_unicode_uses_bounded_temporary_memory():
    text = ''.join(chr(point) for point in range(0x30000, 0x40000)) * 4
    expected = previous_estimate(text)
    tracemalloc.start()
    try:
        actual = MetricsCollector.estimate_tokens(text)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert actual == expected
    assert peak < 2 * 1024 * 1024
