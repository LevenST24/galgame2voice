"""
Provider CRUD and API key masking module for SQLite persistence in galgame2voice.
"""

import json
import logging
from typing import Any

import aiosqlite

from galgame2voice.database.models import (
    ProviderCreate,
    ProviderInDB,
    ProviderResponse,
    ProviderUpdate,
)
from galgame2voice.database.session import immediate_transaction
from galgame2voice.security.crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger("galgame2voice.database.crud_modules.providers")

__all__ = [
    "mask_api_key",
    "is_masked_key",
    "mask_custom_headers",
    "list_providers",
    "get_provider_raw",
    "get_provider",
    "get_active_provider_raw",
    "get_active_provider",
    "create_provider",
    "update_provider",
    "set_active_provider",
    "delete_provider",
]


def mask_api_key(key: str | None) -> str:
    """
    Mask sensitive keys for safe display in web console or logs.
    Examples:
        None -> ""
        "" -> ""
        "sk-1234567890abcdef" -> "sk-****cdef"
        "1234567890:ABCdefGhI" -> "123****fGhI"
        "short" -> "********"
    """
    if not key:
        return ""
    key = str(key).strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "********"
    if key.startswith("sk-"):
        return f"sk-****{key[-4:]}"
    return f"{key[:3]}****{key[-4:]}"


def is_masked_key(key: str | None) -> bool:
    """Return True if string contains masking pattern."""
    if not key:
        return False
    return "****" in str(key)


def mask_custom_headers(headers: dict[str, Any] | None) -> dict[str, Any]:
    """Mask sensitive authentication headers inside custom_headers dictionary.

    Uses an exact-name list plus a substring heuristic so names like
    X-Auth-Token or X-Signature are also masked instead of leaking verbatim.
    """
    if not headers or not isinstance(headers, dict):
        return {}
    masked = {}
    sensitive_keys = {"authorization", "cookie", "x-api-key", "api-key", "token", "secret", "proxy-authorization"}
    sensitive_substrings = ("key", "token", "secret", "auth", "bearer", "credential", "password", "signature")
    for k, v in headers.items():
        key_lower = str(k).lower()
        is_sensitive = key_lower in sensitive_keys or any(s in key_lower for s in sensitive_substrings)
        masked[k] = mask_api_key(str(v)) if is_sensitive else v
    return masked


def _row_to_provider_dict(row: aiosqlite.Row) -> dict[str, Any]:
    """Converts a SQLite providers row to a dict with decrypted api_key and parsed custom_headers."""
    d = dict(row)
    d["is_active"] = bool(d.get("is_active", 0))
    d["api_key"] = decrypt_secret(d.get("api_key", ""))
    try:
        d["custom_headers"] = json.loads(d.get("custom_headers") or "{}")
    except Exception:
        d["custom_headers"] = {}
    return d


def _build_provider_response(data: dict[str, Any], mask: bool = True) -> ProviderResponse:
    """Builds a ProviderResponse from a dictionary, optionally masking credentials."""
    payload = dict(data)
    if mask:
        payload["api_key"] = mask_api_key(payload.get("api_key"))
        payload["custom_headers"] = mask_custom_headers(payload.get("custom_headers"))
    return ProviderResponse(**payload)


async def list_providers(conn: aiosqlite.Connection, mask: bool = True) -> list[ProviderResponse]:
    """Lists all configured LLM providers with optional API key masking."""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM providers ORDER BY id ASC;")
    rows = await cursor.fetchall()
    return [_build_provider_response(_row_to_provider_dict(r), mask=mask) for r in rows]


async def get_provider_raw(conn: aiosqlite.Connection, provider_id: str) -> ProviderInDB | None:
    """Fetches raw provider entity from database including unmasked decrypted credentials."""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM providers WHERE id = ?;", (provider_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    return ProviderInDB(**_row_to_provider_dict(row))


async def get_provider(conn: aiosqlite.Connection, provider_id: str, mask: bool = True) -> ProviderResponse | None:
    """Fetches provider by ID with optional API key masking."""
    raw = await get_provider_raw(conn, provider_id)
    if not raw:
        return None
    return _build_provider_response(raw.model_dump(), mask=mask)


async def get_active_provider_raw(conn: aiosqlite.Connection) -> ProviderInDB | None:
    """Fetches the raw active provider model from database without masking."""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM providers WHERE is_active = 1 LIMIT 1;")
    row = await cursor.fetchone()
    if not row:
        cursor = await conn.execute("SELECT active_provider_id FROM settings WHERE id = 1;")
        s_row = await cursor.fetchone()
        if s_row and s_row["active_provider_id"]:
            return await get_provider_raw(conn, s_row["active_provider_id"])
        return None
    return ProviderInDB(**_row_to_provider_dict(row))


async def get_active_provider(conn: aiosqlite.Connection, mask: bool = True) -> ProviderResponse | None:
    """Fetches the currently active provider with optional masking."""
    raw = await get_active_provider_raw(conn)
    if not raw:
        return None
    return _build_provider_response(raw.model_dump(), mask=mask)


async def create_provider(conn: aiosqlite.Connection, provider: ProviderCreate) -> ProviderResponse:
    """Creates and persists a new LLM provider record."""
    headers_str = json.dumps(provider.custom_headers)
    enc_key = encrypt_secret(provider.api_key) if provider.api_key else ""
    async with immediate_transaction(conn):
        await conn.execute("""
            INSERT OR IGNORE INTO providers (id, name, api_base_url, api_key, chat_model, stt_model, is_active, custom_headers)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            provider.id, provider.name, provider.api_base_url,
            enc_key, provider.chat_model, provider.stt_model,
            1 if provider.is_active else 0, headers_str
        ))
        if provider.is_active:
            await set_active_provider(conn, provider.id)
    res = await get_provider(conn, provider.id, mask=True)
    if res is None:
        raise RuntimeError(f"Failed to retrieve created provider {provider.id}")
    return res


async def update_provider(conn: aiosqlite.Connection, provider_id: str, updates: ProviderUpdate) -> ProviderResponse | None:
    """Updates fields of an existing provider with secret encryption and header merging."""
    current = await get_provider_raw(conn, provider_id)
    if not current:
        return None

    fields = []
    values: list[Any] = []
    up_dict = updates.model_dump(exclude_unset=True)

    for k, v in up_dict.items():
        if v is None:
            continue
        if k == "api_key":
            if str(v).strip() != "" and not is_masked_key(str(v)):
                fields.append("api_key = ?")
                values.append(encrypt_secret(str(v).strip()))
        elif k == "custom_headers":
            # Merge per-entry: masked values (****) coming back from the UI
            # must never overwrite the stored secrets they were masked from.
            merged = dict(current.custom_headers or {})
            for hk, hv in (v or {}).items():
                if isinstance(hv, str) and "****" in hv:
                    continue
                merged[hk] = hv
            fields.append("custom_headers = ?")
            values.append(json.dumps(merged))
        elif k == "is_active":
            fields.append("is_active = ?")
            values.append(1 if v else 0)
        else:
            fields.append(f"{k} = ?")
            values.append(v)

    async with immediate_transaction(conn):
        if fields:
            fields.append("updated_at = CURRENT_TIMESTAMP")
            values.append(provider_id)
            query = f"UPDATE providers SET {', '.join(fields)} WHERE id = ?;"
            await conn.execute(query, tuple(values))

        if updates.is_active:
            await set_active_provider(conn, provider_id)

    return await get_provider(conn, provider_id, mask=True)


async def set_active_provider(conn: aiosqlite.Connection, provider_id: str) -> bool:
    """Marks the specified provider as active and updates global settings."""
    provider = await get_provider_raw(conn, provider_id)
    if not provider:
        return False
    async with immediate_transaction(conn):
        await conn.execute("UPDATE providers SET is_active = 0;")
        await conn.execute("UPDATE providers SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (provider_id,))
        cur = await conn.execute("UPDATE settings SET active_provider_id = ?, updated_at = CURRENT_TIMESTAMP;", (provider_id,))
        if cur.rowcount == 0:
            await conn.execute("INSERT OR IGNORE INTO settings (id, active_provider_id) VALUES (1, ?);", (provider_id,))
    return True


async def delete_provider(conn: aiosqlite.Connection, provider_id: str) -> bool:
    """Deletes a provider record by ID."""
    async with immediate_transaction(conn):
        cursor = await conn.execute("DELETE FROM providers WHERE id = ?;", (provider_id,))
    return cursor.rowcount > 0
