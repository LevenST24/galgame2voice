"""
Shared Telegram authorization constants for the automated test suite.

``TelegramBotHandlers`` fails CLOSED when no admin IDs are configured: a bot with
an empty whitelist must never be usable by strangers (it would otherwise burn the
owner's LLM quota and GPU time). Consequently, tests that drive the handlers
directly must declare the synthetic user IDs emitted by their mocks instead of
relying on the old fail-open default.

The IDs below are the values used by the MagicMock-based updates across the
suite; ``0`` is what a mock produces when ``effective_user`` / ``callback_query``
is left unset.
"""

TELEGRAM_TEST_ADMINS = {0, 1, 12345}

__all__ = ["TELEGRAM_TEST_ADMINS"]
