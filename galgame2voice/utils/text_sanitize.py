"""
Text sanitization utilities for galgame2voice.
"""

from typing import Any, List, Optional


def sanitize_bot_token(raw: Any) -> str:
    """Sanitizes raw Telegram bot token by removing spaces, carriage returns, line breaks, and trimming whitespace."""
    if not raw:
        return ""
    return str(raw).replace(" ", "").replace("\r", "").replace("\n", "").strip()


def parse_admin_ids(raw: Optional[Any]) -> List[int]:
    """Parses a comma-separated list of Telegram user IDs (supports both English and fullwidth commas)."""
    if not raw:
        return []
    ids: List[int] = []
    for part in str(raw).replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


__all__ = [
    "sanitize_bot_token",
    "parse_admin_ids",
]
