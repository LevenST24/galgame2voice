"""
Text sanitization utilities for galgame2voice.
"""

from typing import Any


def sanitize_bot_token(raw: Any) -> str:
    """Sanitizes raw Telegram bot token by removing spaces, carriage returns, line breaks, and trimming whitespace."""
    if not raw:
        return ""
    return str(raw).replace(" ", "").replace("\r", "").replace("\n", "").strip()
