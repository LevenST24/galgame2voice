"""
HMAC-SHA256 signed, expiring URLs for media assets (e.g. character portraits, backgrounds).

Provides persistent key derivation, hour-bucketed expiry calculation, URL signing,
and constant-time signature verification.
"""

import hashlib
import hmac
import logging
import os
import secrets
import tempfile
import threading
import time
from urllib.parse import quote, unquote

logger = logging.getLogger("galgame2voice.security.media_urls")

_CONTEXT_LABEL = b"galgame2voice media url v1"
_MEDIA_KEY_FILENAME = ".media_url_key"
_warned_missing_key = False
# A manifest signs every sprite, so derive the key once per process.
_cached_key: bytes | None = None
_key_lock = threading.Lock()


def get_media_signing_key() -> bytes | None:
    """Return the media signing key, deriving it at most once per process."""
    global _cached_key
    if _cached_key is not None:
        return _cached_key
    with _key_lock:
        if _cached_key is None:
            _cached_key = _load_media_signing_key()
        return _cached_key


def _load_media_signing_key() -> bytes | None:
    """
    Retrieves or derives the 256-bit symmetric HMAC key for media URL signing.
    Persistent across restarts and identical for every worker process.
    Never logged or returned across any API.

    Priority:
    1. Derived from credential master key via galgame2voice.security.crypto._get_fallback_key()
       using HMAC-SHA256 with context label b"galgame2voice media url v1".
    2. Fallback key in settings.data_dir / ".media_url_key", created atomically with owner-only permissions.
    3. If no key can be obtained, logs a warning once and returns None (caller falls back to unsigned URLs).
    """
    global _warned_missing_key

    # 1. Preferred: derive from existing credential master key
    try:
        from galgame2voice.security.crypto import _get_fallback_key

        master_key = _get_fallback_key(create=True)
        if master_key and len(master_key) >= 16:
            return hmac.new(master_key, _CONTEXT_LABEL, hashlib.sha256).digest()
    except Exception as exc:
        logger.debug("Could not derive media signing key from crypto master key: %s", exc)

    # 2. Fallback: random key in data dir, created atomically with owner-only permissions
    try:
        from galgame2voice.config import get_settings

        key_path = get_settings().data_dir / _MEDIA_KEY_FILENAME
        if key_path.is_file():
            key_bytes = key_path.read_bytes()
            if len(key_bytes) == 32:
                return key_bytes

        key_path.parent.mkdir(parents=True, exist_ok=True)
        new_key = secrets.token_bytes(32)
        fd, temp_file = tempfile.mkstemp(prefix=".media_key.", dir=key_path.parent)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(new_key)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.replace(temp_file, key_path)
            except Exception:
                if key_path.is_file():
                    return key_path.read_bytes()
                raise
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
            return new_key
        finally:
            if os.path.exists(temp_file):
                try:
                    os.unlink(temp_file)
                except OSError:
                    pass
    except Exception as exc:
        logger.debug("Could not obtain fallback media signing key: %s", exc)

    # 3. None obtainable: log warning once and return None
    if not _warned_missing_key:
        logger.warning("Media URL signing key unavailable; falling back to unsigned URLs")
        _warned_missing_key = True
    return None


def compute_bucketed_expiry(now: float | None = None, validity_hours: int = 12) -> int:
    """
    Rounds current time up to the next hour boundary, then adds validity_hours (at least 12).
    Ensures identical URLs for requests within the same hour bucket to support browser caching.
    """
    if now is None:
        now = time.time()
    t = int(now)
    next_hour = ((t // 3600) + 1) * 3600
    validity_seconds = max(12, validity_hours) * 3600
    return next_hour + validity_seconds


def generate_media_signature(
    decoded_path: str, exp: int | str, v: str | None = None, key: bytes | None = None
) -> str | None:
    """
    Generates HMAC-SHA256 signature covering exact decoded path, expiry, and v cache-buster.
    Returns 64-character hex digest, or None if no key is available.
    """
    if key is None:
        key = get_media_signing_key()
    if not key:
        return None

    v_str = str(v) if v is not None else ""
    msg = f"{decoded_path}\n{exp}\n{v_str}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_media_url_signature(
    decoded_path: str,
    exp: int | str,
    v: str | None,
    sig: str,
    now: float | None = None,
    key: bytes | None = None,
) -> bool:
    """
    Verifies that the HMAC-SHA256 signature matches and the URL has not expired.
    Uses hmac.compare_digest for constant-time comparison.
    """
    if not sig or not isinstance(sig, str) or len(sig) != 64:
        return False

    try:
        exp_int = int(exp)
    except (ValueError, TypeError):
        return False

    if now is None:
        now = time.time()
    if now > exp_int:
        return False

    expected_sig = generate_media_signature(decoded_path, exp_int, v, key=key)
    if not expected_sig:
        return False

    return hmac.compare_digest(sig.lower(), expected_sig.lower())


def sign_media_url(
    path: str,
    v: str | None = None,
    now: float | None = None,
    validity_hours: int = 12,
) -> str:
    """
    Signs a URL path with query parameters v, exp, and sig.
    If no key can be obtained, falls back to unsigned URL (path with ?v=... if v exists).
    """
    key = get_media_signing_key()
    v_str = str(v) if v is not None else ""
    if not key:
        return f"{path}?v={v_str}" if v_str else path

    exp = compute_bucketed_expiry(now=now, validity_hours=validity_hours)
    decoded_path = unquote(path)
    sig = generate_media_signature(decoded_path, exp, v=v_str, key=key)
    if not sig:
        return f"{path}?v={v_str}" if v_str else path

    params = []
    if v_str:
        params.append(f"v={quote(v_str, safe='')}")
    params.append(f"exp={exp}")
    params.append(f"sig={sig}")
    return f"{path}?{'&'.join(params)}"
