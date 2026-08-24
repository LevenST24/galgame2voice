"""
Telegram Bot Integration Package for galgame2voice.
Exports TelegramBotManager, handlers, and proxy helpers.
"""

from galgame2voice.telegram_bot.bot import (
    TelegramBotManager,
    get_telegram_bot_manager,
    validate_bot_token,
)
from galgame2voice.telegram_bot.handlers import TelegramBotHandlers
from galgame2voice.telegram_bot.proxy import (
    get_proxy_url,
    get_telegram_request_kwargs,
    test_proxy_connectivity,
)

__all__ = [
    "TelegramBotManager",
    "get_telegram_bot_manager",
    "validate_bot_token",
    "TelegramBotHandlers",
    "get_proxy_url",
    "get_telegram_request_kwargs",
    "test_proxy_connectivity",
]
