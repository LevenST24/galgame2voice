"""Translate local bind addresses into usable browser targets."""


def browser_hosts(bind_host: str) -> tuple[str, str]:
    """Return the socket host and its URL-safe representation."""
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(bind_host, bind_host)
    return host, f"[{host}]" if ":" in host else host
