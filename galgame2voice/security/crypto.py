"""
Security-hardened at-rest encryption and decryption for sensitive credentials.
Supports native Windows DPAPI (tied to OS user account), cross-platform AES-GCM,
and read-only decoding of legacy credentials for migration.
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets
import sys
import tempfile
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("galgame2voice.security.crypto")

_DPAPI_PREFIX = "dpapi:"
_ENC_PREFIX = "enc:"


class MasterKeyError(RuntimeError):
    """The persistent credential key could not be loaded or safely stored."""


def _get_fallback_key(*, create: bool = True) -> bytes:
    """
    Derives or loads a 256-bit symmetric encryption key for non-DPAPI environments.
    Priority:
    1. GALGAME2VOICE_SECRET_KEY / GALGAME2VOICE_MASTER_KEY environment variable
    2. Persistent local keyfile in data/.master_key with 0600 permissions
    """
    env_key = os.getenv("GALGAME2VOICE_SECRET_KEY") or os.getenv("GALGAME2VOICE_MASTER_KEY")
    if env_key and env_key.strip():
        return hashlib.sha256(env_key.strip().encode("utf-8")).digest()

    from galgame2voice.config import get_settings
    key_path = get_settings().data_dir / ".master_key"
    try:
        key_bytes = key_path.read_bytes()
    except FileNotFoundError:
        if not create:
            raise MasterKeyError(f"Master key is missing at {key_path}; restore it before decrypting credentials") from None
        new_key = secrets.token_bytes(32)
        try:
            key_path.parent.mkdir(parents=True, exist_ok=True)
            # Publish only a complete key without replacing a concurrent one.
            fd, temporary_key = tempfile.mkstemp(prefix=".master_key.", dir=key_path.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(new_key)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary_key, key_path)
                except FileExistsError:
                    return _get_fallback_key(create=False)
                return new_key
            finally:
                os.unlink(temporary_key)
        except OSError as exc:
            raise MasterKeyError(f"Cannot persist master key at {key_path}; encryption aborted") from exc
    except OSError as exc:
        raise MasterKeyError(f"Cannot read master key at {key_path}") from exc
    if len(key_bytes) != 32:
        raise MasterKeyError(f"Invalid master key at {key_path}: expected 32 bytes; restore the original key")
    return key_bytes


# ---------------------------------------------------------------------------
# Windows DPAPI Implementation via ctypes
# ---------------------------------------------------------------------------
_HAS_DPAPI = False
if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes

        class _DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte)),
            ]

        _crypt32 = ctypes.windll.crypt32
        _kernel32 = ctypes.windll.kernel32
        _HAS_DPAPI = True
    except Exception as exc:
        logger.debug("Windows DPAPI initialization failed: %s", exc)
        _HAS_DPAPI = False


def _dpapi_protect(plaintext_bytes: bytes) -> bytes:
    if not _HAS_DPAPI:
        raise RuntimeError("Windows DPAPI unavailable")
    blob_in = _DATA_BLOB(
        len(plaintext_bytes),
        ctypes.cast(ctypes.create_string_buffer(plaintext_bytes), ctypes.POINTER(ctypes.c_byte)),
    )
    blob_out = _DATA_BLOB()
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in), "galgame2voice_secret", None, None, None, 0x1, ctypes.byref(blob_out)
    )
    if not ok:
        raise RuntimeError("CryptProtectData failed")
    ciphertext = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    _kernel32.LocalFree(blob_out.pbData)
    return ciphertext


def _dpapi_unprotect(ciphertext_bytes: bytes) -> bytes:
    if not _HAS_DPAPI:
        raise RuntimeError("Windows DPAPI unavailable")
    blob_in = _DATA_BLOB(
        len(ciphertext_bytes),
        ctypes.cast(ctypes.create_string_buffer(ciphertext_bytes), ctypes.POINTER(ctypes.c_byte)),
    )
    blob_out = _DATA_BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)
    )
    if not ok:
        raise RuntimeError("CryptUnprotectData failed")
    plaintext = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    _kernel32.LocalFree(blob_out.pbData)
    return plaintext


# ---------------------------------------------------------------------------
# Cross-Platform AES-GCM (legacy v2 decoding is only for migration)
# ---------------------------------------------------------------------------
def _fallback_encrypt(plaintext_bytes: bytes) -> bytes:
    """Always creates AES-GCM ciphertext; a missing dependency fails at import."""
    key = _get_fallback_key()
    nonce = secrets.token_bytes(12)
    return b"\x01" + nonce + AESGCM(key).encrypt(nonce, plaintext_bytes, None)


def _fallback_decrypt(payload: bytes) -> bytes:
    """Authenticated decryption matching _fallback_encrypt."""
    if not payload:
        return b""
    version = payload[0]
    key = _get_fallback_key(create=False)
    if version == 1:
        nonce = payload[1:13]
        ct = payload[13:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ct, None)
    elif version == 2:
        # Read-only compatibility: never emit this format for new writes.
        if len(payload) < 49:
            raise ValueError("Truncated legacy encrypted credential")
        nonce = payload[1:17]
        tag = payload[17:49]
        ct = payload[49:]
        expected_tag = hmac.new(key, b"auth" + nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected_tag):
            raise ValueError("Integrity check failed on decrypted secret")
        keystream = bytearray()
        block_idx = 0
        while len(keystream) < len(ct):
            block_idx += 1
            counter = block_idx.to_bytes(4, "big")
            block = hmac.new(key, nonce + counter, hashlib.sha256).digest()
            keystream.extend(block)
        return bytes(c ^ k for c, k in zip(ct, keystream[:len(ct)], strict=True))
    raise ValueError(f"Unknown encrypted payload version {version}")


def upgrade_secret_encryption(value: str) -> str:
    """Migrates plaintext/v2 to the current cipher, preserving other formats.

    Decode directly so an integrity/key failure cannot overwrite a stored secret
    with the public decrypt helper's empty-string error result.
    """
    if value.startswith(_ENC_PREFIX):
        payload = base64.b64decode(value[len(_ENC_PREFIX):], validate=True)
        if payload and payload[0] == 2:
            return encrypt_secret(_fallback_decrypt(payload).decode("utf-8"))
        return value
    return value if is_encrypted_secret(value) else encrypt_secret(value)


# ---------------------------------------------------------------------------
# Public Encryption / Decryption Interface
# ---------------------------------------------------------------------------
def encrypt_secret(value: Optional[str]) -> Optional[str]:
    """
    Encrypts a plaintext secret string.
    Returns ciphertext formatted with prefix 'dpapi:' or 'enc:'.
    If already encrypted, returns unchanged. If empty, returns empty.
    """
    if not value or not str(value).strip():
        return value
    text = str(value).strip()
    if text.startswith(_DPAPI_PREFIX) or text.startswith(_ENC_PREFIX):
        return text

    raw_bytes = text.encode("utf-8")
    if _HAS_DPAPI:
        try:
            cipher = _dpapi_protect(raw_bytes)
            return _DPAPI_PREFIX + base64.b64encode(cipher).decode("ascii")
        except Exception as exc:
            logger.debug("DPAPI encryption failed, falling back: %s", exc)

    cipher = _fallback_encrypt(raw_bytes)
    return _ENC_PREFIX + base64.b64encode(cipher).decode("ascii")


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    """
    Decrypts a secret string.
    If value is plaintext (legacy record without prefix), returns as-is.
    If value has 'dpapi:' or 'enc:' prefix, decrypts and returns plaintext.
    """
    if not value or not str(value).strip():
        return value
    text = str(value).strip()

    if text.startswith(_DPAPI_PREFIX):
        try:
            cipher = base64.b64decode(text[len(_DPAPI_PREFIX):])
            return _dpapi_unprotect(cipher).decode("utf-8")
        except Exception as exc:
            logger.error("Failed to decrypt DPAPI secret: %s", exc)
            return ""

    if text.startswith(_ENC_PREFIX):
        try:
            cipher = base64.b64decode(text[len(_ENC_PREFIX):])
            return _fallback_decrypt(cipher).decode("utf-8")
        except MasterKeyError:
            raise
        except Exception as exc:
            logger.error("Failed to decrypt encrypted secret: %s", exc)
            return ""

    # Legacy plaintext
    return text


def is_encrypted_secret(value: Optional[str]) -> bool:
    """Checks whether a value is stored in encrypted form."""
    if not value:
        return False
    val = str(value).strip()
    return val.startswith(_DPAPI_PREFIX) or val.startswith(_ENC_PREFIX)
