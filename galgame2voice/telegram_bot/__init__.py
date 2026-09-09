"""
Telegram Bot Integration Package for galgame2voice.
Exports TelegramBotManager, handlers, and proxy helpers.
"""

from typing import Any

__all__ = [
    "TelegramBotManager",
    "get_telegram_bot_manager",
    "validate_bot_token",
    "TelegramBotHandlers",
    "get_proxy_url",
    "get_telegram_request_kwargs",
    "test_proxy_connectivity",
]


def __getattr__(name: str) -> Any:
    if name in ("TelegramBotManager", "get_telegram_bot_manager", "validate_bot_token"):
        from galgame2voice.telegram_bot import bot

        val = getattr(bot, name)
        globals()[name] = val
        return val
    if name == "TelegramBotHandlers":
        from galgame2voice.telegram_bot.handlers import TelegramBotHandlers

        globals()["TelegramBotHandlers"] = TelegramBotHandlers
        return TelegramBotHandlers
    if name in ("get_proxy_url", "get_telegram_request_kwargs", "test_proxy_connectivity"):
        from galgame2voice.telegram_bot import proxy

        val = getattr(proxy, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

