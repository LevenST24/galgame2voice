"""
Console authentication dependency for FastAPI routes.

Single-tenant access control: every protected route requires a valid console
token supplied as `Authorization: Bearer <token>`. The token source priority is:

1. Environment override `GALGAME2VOICE_CONSOLE_TOKEN`
2. `settings.console_token` column in SQLite (auto-seeded with a random value
   on first startup; the generated value is printed to the log)

The kill-switch env `GALGAME2VOICE_AUTH_DISABLED=1` bypasses auth entirely and
exists only for the automated test suite and local development.
"""

import logging
import os
from typing import Optional

from fastapi import HTTPException, Request, status

from galgame2voice.database.session import get_db
from galgame2voice.database import crud

logger = logging.getLogger("galgame2voice.security.auth")

ENV_TOKEN_VAR = "GALGAME2VOICE_CONSOLE_TOKEN"
AUTH_DISABLED_VAR = "GALGAME2VOICE_AUTH_DISABLED"


def get_env_console_token() -> Optional[str]:
    env_value = os.getenv(ENV_TOKEN_VAR, "").strip()
    if env_value:
        return env_value
    try:
        from galgame2voice.config import get_settings
        settings_token = (get_settings().console_token or "").strip()
        return settings_token or None
    except Exception:
        return None


def is_auth_disabled() -> bool:
    env_flag = os.getenv(AUTH_DISABLED_VAR, "").strip().lower() in ("1", "true", "yes")
    if env_flag:
        return True
    try:
        from galgame2voice.config import get_settings
        return bool(get_settings().auth_disabled)
    except Exception:
        return False


async def verify_console_token(token: str) -> bool:
    """Validate a candidate token against env override or the DB settings row."""
    if not token:
        return False
    env_token = get_env_console_token()
    if env_token:
        import hmac
        return hmac.compare_digest(token, env_token)
    async with get_db() as conn:
        return await crud.verify_console_token(conn, token)


async def require_auth(request: Request) -> None:
    """FastAPI dependency enforcing console token auth on protected routes."""
    if is_auth_disabled():
        return

    auth_header = request.headers.get("authorization", "")
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    if not token:
        token = request.headers.get("x-console-token", "").strip()

    if not await verify_console_token(token):
        logger.warning("Rejected unauthorized request: %s %s from %s",
                       request.method, request.url.path,
                       request.client.host if request.client else "unknown")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing console token",
            headers={"WWW-Authenticate": "Bearer"},
        )


__all__ = ["require_auth", "verify_console_token", "is_auth_disabled", "get_env_console_token"]
