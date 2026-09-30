"""
Server-Sent Events (SSE) utility formatting for galgame2voice.
"""

import json
from typing import Any


def format_sse_frame(event: dict[str, Any] | str) -> str:
    """Formats an event dictionary or string into a standard Server-Sent Events text frame."""
    if isinstance(event, str):
        return event
    if isinstance(event, dict) and (event.get("event") == ":keep-alive" or "comment" in event):
        return str(event.get("comment", ": keep-alive\n\n"))
    event_name = event.get("event", "message") if isinstance(event, dict) else "message"
    data_payload = event.get("data", {}) if isinstance(event, dict) else event
    event_data = json.dumps(data_payload, ensure_ascii=False)
    return f"event: {event_name}\ndata: {event_data}\n\n"
