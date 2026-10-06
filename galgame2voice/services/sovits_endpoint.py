"""
Unified GPT-SoVITS endpoint resolution for galgame2voice.

Single source of truth for "which GPT-SoVITS engine address do we use".
The launcher (scripts/run_server.py), backend startup (main lifespan),
health probes, and the /api/system/restart_sovits endpoint must all agree.

Priority (highest first):
  1. "env"     - explicit process environment variables
                 GALGAME2VOICE_GPT_SOVITS_BASE_URL / GPT_SOVITS_BASE_URL.
                 Read from os.environ DIRECTLY: Settings merges OS env and
                 .env, so it cannot distinguish the two.
  2. "db"      - SQLite gpt_sovits_url (the persisted Web-console value).
  3. "dotenv"  - .env / Settings.gpt_sovits_base_url. After ruling out an
                 explicit process env var, the Settings value represents the
                 .env-or-default layer.
  4. "default" - built-in default http://127.0.0.1:9880.

Only loopback ("local") endpoints may be managed (spawned/restarted) by this
host. Remote endpoints are used as-is; the launcher and the restart endpoint
must never fall back to a local 127.0.0.1:9880 process for them.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger("galgame2voice.services.sovits_endpoint")

DEFAULT_SOVITS_BASE_URL = "http://127.0.0.1:9880"
DEFAULT_SOVITS_PORT = 9880

# Explicit process-env keys, highest priority. Checked against os.environ
# directly so a real exported variable wins over .env / Settings merging.
_ENV_KEYS = ("GALGAME2VOICE_GPT_SOVITS_BASE_URL", "GPT_SOVITS_BASE_URL")


@dataclass(frozen=True)
class SovitsEndpoint:
    """Resolved GPT-SoVITS engine address plus where it came from."""

    base_url: str
    host: str
    port: int
    scheme: str
    source: str  # "env" | "db" | "dotenv" | "default"
    path: str = ""

    @property
    def is_loopback(self) -> bool:
        """True when the host is loopback (localhost / 127.0.0.1 / ::1)."""
        host = (self.host or "").strip().lower()
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            # Unparsable hostnames are treated as remote
            return False

    @property
    def is_local(self) -> bool:
        """True when the engine is expected to be launched and managed locally.
        Requires loopback host, plain http scheme, and root path (no TLS or subpath).
        Remote or TLS/subpath loopback endpoints are treated as unmanaged (client-only)."""
        return self.is_loopback and self.scheme == "http" and self.path in ("", "/")


def parse_sovits_endpoint(value: str, *, source: str) -> SovitsEndpoint:
    """Parses and validates a GPT-SoVITS base URL.

    Raises ValueError when the value is empty, has a non-http(s) scheme,
    has no hostname, or embeds credentials.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError(f"empty GPT-SoVITS URL (source: {source})")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"invalid GPT-SoVITS URL scheme {parsed.scheme!r} (source: {source}): "
            "must be http or https"
        )
    if not parsed.hostname:
        raise ValueError(f"invalid GPT-SoVITS URL {raw!r} (source: {source}): missing hostname")
    if parsed.username or parsed.password:
        raise ValueError(
            f"invalid GPT-SoVITS URL (source: {source}): embedded credentials are not allowed"
        )
    host = parsed.hostname
    port = parsed.port
    if port is None:
        # Preserves the historical launcher behavior of `parsed.port or 9880`.
        port = 443 if parsed.scheme == "https" else DEFAULT_SOVITS_PORT
    netloc_host = f"[{host}]" if ":" in host else host
    path = parsed.path.rstrip("/")
    base_url = f"{parsed.scheme}://{netloc_host}:{port}{path}"
    return SovitsEndpoint(
        base_url=base_url,
        host=host,
        port=port,
        scheme=parsed.scheme,
        source=source,
        path=path,
    )


def get_explicit_process_env_url() -> str | None:
    """Returns the first non-empty explicit process env URL, else None."""
    for key in _ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def resolve_sovits_url(*, db_url: str | None, settings_url: str | None) -> SovitsEndpoint:
    """Resolves the effective endpoint from the priority chain.

    Priority:
      1. Explicit process environment variables (GALGAME2VOICE_GPT_SOVITS_BASE_URL / GPT_SOVITS_BASE_URL).
      2. Explicit user-customized DB setting (custom non-default value in SQLite).
      3. Explicit .env / Settings configuration (custom non-default value in .env).
      4. Database default / .env default / built-in default (http://127.0.0.1:9880).
    """
    env_url = get_explicit_process_env_url()
    if env_url:
        return parse_sovits_endpoint(env_url, source="env")

    db_clean = (db_url or "").strip()
    settings_clean = (settings_url or "").strip()

    is_custom_db = bool(db_clean and db_clean.rstrip("/") != DEFAULT_SOVITS_BASE_URL)
    is_custom_dotenv = bool(settings_clean and settings_clean.rstrip("/") != DEFAULT_SOVITS_BASE_URL)

    # 1. Custom DB value explicitly configured by user via UI console
    if is_custom_db:
        return parse_sovits_endpoint(db_clean, source="db")

    # 2. Custom value explicitly configured in .env / Settings
    if is_custom_dotenv:
        return parse_sovits_endpoint(settings_clean, source="dotenv")

    # 3. Default seeded values
    if db_clean:
        return parse_sovits_endpoint(db_clean, source="default")
    if settings_clean:
        return parse_sovits_endpoint(settings_clean, source="default")

    return parse_sovits_endpoint(DEFAULT_SOVITS_BASE_URL, source="default")


async def resolve_effective_sovits_endpoint() -> SovitsEndpoint:
    """Async resolver for the running backend: reads SQLite (best-effort)
    plus Settings, then applies the priority chain."""
    db_url: str | None = None
    try:
        from galgame2voice.database import crud
        from galgame2voice.database.session import get_db

        async with get_db() as conn:
            db_settings = await crud.get_settings_raw(conn)
            db_url = getattr(db_settings, "gpt_sovits_url", None)
    except Exception as exc:
        logger.debug("Could not read gpt_sovits_url from DB: %s", exc)
        db_url = None
    settings_url: str | None = None
    try:
        from galgame2voice.config import get_settings

        settings_url = get_settings().gpt_sovits_base_url
    except Exception as exc:
        logger.debug("Could not load settings for GPT-SoVITS URL: %s", exc)
    return resolve_sovits_url(db_url=db_url, settings_url=settings_url)


def _read_db_sovits_url_sync(db_path: str | None) -> str | None:
    """Best-effort read of the persisted gpt_sovits_url via stdlib sqlite3.

    Never raises: any DB problem degrades to None.
    """
    if not db_path:
        return None
    try:
        if not os.path.isfile(db_path):
            return None
        # Read-only open: never creates or mutates the database file.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT gpt_sovits_url FROM settings WHERE id = 1"
                ).fetchone()
                if row and row["gpt_sovits_url"]:
                    return str(row["gpt_sovits_url"])
            except sqlite3.Error:
                pass
            try:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = 'gpt_sovits_url' LIMIT 1"
                ).fetchone()
                if row and row["value"]:
                    return str(row["value"])
            except sqlite3.Error:
                pass
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Could not read gpt_sovits_url from DB (sync): %s", exc)
    return None


def _resolve_db_path_sync() -> str | None:
    """Resolves the SQLite path the same way the backend does, without raising."""
    try:
        env_path = os.environ.get("GALGAME2VOICE_DB_PATH") or os.environ.get("GALGAME_DB_PATH")
        if env_path:
            return env_path
        from galgame2voice.config import get_settings

        return str(get_settings().db_path)
    except Exception:
        return None


def resolve_effective_sovits_endpoint_sync() -> SovitsEndpoint:
    """Sync resolver for scripts/run_server.py (launcher, no event loop).

    Never raises on DB problems: they degrade gracefully to the next
    priority level. A malformed explicitly-configured URL raises ValueError
    with a clear message so the misconfiguration is visible.
    """
    db_url = _read_db_sovits_url_sync(_resolve_db_path_sync())
    settings_url: str | None = None
    try:
        from galgame2voice.config import get_settings

        settings_url = get_settings().gpt_sovits_base_url
    except Exception as exc:
        logger.debug("Could not load settings for GPT-SoVITS URL (sync): %s", exc)
    return resolve_sovits_url(db_url=db_url, settings_url=settings_url)


__all__ = [
    "DEFAULT_SOVITS_BASE_URL",
    "DEFAULT_SOVITS_PORT",
    "SovitsEndpoint",
    "get_explicit_process_env_url",
    "parse_sovits_endpoint",
    "resolve_sovits_url",
    "resolve_effective_sovits_endpoint",
    "resolve_effective_sovits_endpoint_sync",
]
