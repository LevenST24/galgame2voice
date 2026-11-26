"""GALGAME2VOICE_LOG_LEVEL: tolerant parsing and a visible warning for unknown values."""

import logging

import pytest

from galgame2voice.utils.logger import resolve_log_level, setup_logger


@pytest.fixture
def restore_root_logger():
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, root.handlers[:]
    yield root
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        if handler not in saved_handlers:
            handler.close()
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("debug", logging.DEBUG), (" Debug ", logging.DEBUG), ("WARN", logging.WARNING), ("10", logging.DEBUG),
     ("error\n", logging.ERROR), ("critical", logging.CRITICAL)],
)
def test_level_names_are_read_leniently(value, expected):
    assert resolve_log_level(value) == (expected, logging.getLevelName(expected))


@pytest.mark.parametrize("value", ["verbose", "", "basic_format", "15", None])
def test_unknown_values_fall_back_to_info(value):
    assert resolve_log_level(value) == (logging.INFO, None)


def test_setup_logger_applies_padded_level(restore_root_logger):
    setup_logger(log_level=" debug ", log_to_file=False)
    assert restore_root_logger.level == logging.DEBUG


def test_setup_logger_warns_about_unknown_level(restore_root_logger, capsys):
    setup_logger(log_level="verbose", log_to_file=False)
    assert restore_root_logger.level == logging.INFO
    out = capsys.readouterr().out
    assert "不是可用的日志级别" in out and "'verbose'" in out
    assert "at level INFO" in out and "VERBOSE" not in out
