"""Timing assertions are skipped only when coverage instrumentation is enabled."""

from types import SimpleNamespace

import pytest

import conftest


@pytest.mark.parametrize(("sources", "disabled", "skip_timing"), [
    (None, False, False),
    ([], False, False),
    (["galgame2voice"], False, True),
    ([""], False, True),
    (["galgame2voice"], True, False),
])
def test_timing_marker_requires_enabled_coverage(sources, disabled, skip_timing):
    options = {"cov_source": sources, "no_cov": disabled}
    config = SimpleNamespace(getoption=lambda name, default=None: options.get(name, default))
    timing_marks, normal_marks = [], []
    timing = SimpleNamespace(keywords={"wall_clock": True}, add_marker=timing_marks.append)
    normal = SimpleNamespace(keywords={}, add_marker=normal_marks.append)
    conftest.pytest_collection_modifyitems(config, [timing, normal])
    assert bool(timing_marks) == skip_timing
    assert normal_marks == []
