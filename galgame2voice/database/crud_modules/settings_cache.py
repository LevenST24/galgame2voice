"""
Settings, TTS Cache metadata, and telemetry metrics CRUD operations for SQLite in galgame2voice.
"""

import hmac
import logging
import sqlite3
from typing import Any, Dict, List, Optional

import aiosqlite

from galgame2voice.database.crud_modules.providers import is_masked_key, mask_api_key
from galgame2voice.database.migrations import init_schema_and_seeds
from galgame2voice.database.models import (
    SettingsInDB,
    SettingsResponse,
    SettingsUpdate,
    TtsCacheEntry,
)
from galgame2voice.database.session import immediate_transaction
from galgame2voice.security.crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger("galgame2voice.database.crud_modules.settings_cache")

__all__ = [
    "get_settings",
    "get_settings_raw",
    "update_settings",
    "verify_console_token",
    "get_tts_cache_entry",
    "touch_tts_cache_entry",
    "batch_touch_tts_cache_entries",
    "upsert_tts_cache_entry",
    "delete_tts_cache_entry",
    "get_oldest_tts_cache_entries",
    "get_tts_cache_stats",
    "clear_all_tts_cache_entries",
    "record_tts_cache_hit",
    "record_tts_cache_miss",
    "clean_tts_cache_lru",
    "insert_token_metric",
    "get_metrics_overview",
    "get_provider_metrics_breakdown",
    "get_recent_latency_trends",
]

USD_TO_CNY_RATE: float = 7.20


async def get_settings_raw(conn: aiosqlite.Connection) -> SettingsInDB:
    """Reads raw decrypted settings entity from database."""
    conn.row_factory = aiosqlite.Row
    try:
        cursor = await conn.execute("SELECT * FROM settings WHERE id = 1;")
        row = await cursor.fetchone()
        if not row:
            await init_schema_and_seeds(conn)
            cursor = await conn.execute("SELECT * FROM settings WHERE id = 1;")
            row = await cursor.fetchone()
        if row:
            data = dict(row)
            data["telegram_proxy_enabled"] = bool(data.get("telegram_proxy_enabled", 0))
            data["telegram_enabled"] = bool(data.get("telegram_enabled", 0))
            data["allow_private_llm_endpoints"] = bool(data.get("allow_private_llm_endpoints", 0))
            data["telegram_bot_token"] = decrypt_secret(data.get("telegram_bot_token", ""))
            data["console_token"] = decrypt_secret(data.get("console_token", ""))
            return SettingsInDB(**data)
    except (sqlite3.OperationalError, aiosqlite.OperationalError):
        pass

    # Fallback if settings table is key-value schema (e.g. in test fixture)
    try:
        cursor = await conn.execute("SELECT key, value FROM settings;")
        rows = await cursor.fetchall()
        kv = {r["key"]: r["value"] for r in rows}
        return SettingsInDB(
            id=1,
            active_provider_id=kv.get("active_provider_id", "deepseek"),
            active_voice_profile_id=int(kv.get("active_voice_profile_id", 1)) if kv.get("active_voice_profile_id") else 1,
            max_history_messages=int(kv.get("max_history_messages", 10)) if kv.get("max_history_messages") else 10,
            telegram_enabled=bool(int(kv.get("telegram_enabled", 0))) if kv.get("telegram_enabled") else False,
            telegram_bot_token=decrypt_secret(kv.get("telegram_bot_token", "")),
            console_token=decrypt_secret(kv.get("console_token", "")),
        )
    except Exception:
        return SettingsInDB(id=1)


async def get_settings(conn: aiosqlite.Connection, mask: bool = True) -> SettingsResponse:
    """Reads settings from database with optional API key masking."""
    raw = await get_settings_raw(conn)
    resp_data = raw.model_dump()
    if mask:
        resp_data["telegram_bot_token"] = mask_api_key(raw.telegram_bot_token)
        resp_data["console_token"] = mask_api_key(raw.console_token)
    if not resp_data.get("telegram_chat_id"):
        resp_data["telegram_chat_id"] = resp_data.get("telegram_admin_ids", "")
    return SettingsResponse(**resp_data)


async def update_settings(conn: aiosqlite.Connection, updates: SettingsUpdate) -> SettingsResponse:
    """Updates runtime settings with encryption for secrets."""
    # Ensure the settings row exists before updating: on a fresh DB this seeds
    # schema + row, otherwise the UPDATE below (WHERE id = 1) is a silent no-op.
    await get_settings_raw(conn)
    fields = []
    values: List[Any] = []

    update_dict = updates.model_dump(exclude_unset=True)
    for k, v in update_dict.items():
        if k == "telegram_bot_token":
            if v is not None and not is_masked_key(str(v)):
                cleaned = str(v).replace(" ", "").replace("\r", "").replace("\n", "").strip()
                fields.append(f"{k} = ?")
                values.append(encrypt_secret(cleaned) if cleaned else "")
        elif k == "console_token":
            if v is not None and not is_masked_key(str(v)):
                cleaned = str(v).strip()
                fields.append(f"{k} = ?")
                values.append(encrypt_secret(cleaned) if cleaned else "")
        elif k in ("telegram_enabled", "telegram_proxy_enabled", "allow_private_llm_endpoints"):
            fields.append(f"{k} = ?")
            values.append(1 if v else 0)
        elif k in ("telegram_admin_ids", "telegram_chat_id"):
            ids = ",".join(
                part.strip() for part in str(v or "").replace("，", ",").split(",")
                if part.strip().isdigit()
            )
            if not any(f.startswith("telegram_admin_ids") for f in fields):
                fields.append("telegram_admin_ids = ?")
                values.append(ids)
        elif k == "stt_engine":
            fields.append("stt_engine = ?")
            values.append(str(v or "browser").strip())
        else:
            fields.append(f"{k} = ?")
            values.append(v)

    if fields or ("active_provider_id" in update_dict and update_dict["active_provider_id"]):
        async with immediate_transaction(conn):
            if fields:
                fields.append("updated_at = CURRENT_TIMESTAMP")
                query = f"UPDATE settings SET {', '.join(fields)} WHERE id = 1;"
                await conn.execute(query, tuple(values))

            if "active_provider_id" in update_dict and update_dict["active_provider_id"]:
                prov_id = str(update_dict["active_provider_id"]).strip()
                await conn.execute("UPDATE providers SET is_active = 0;")
                await conn.execute("UPDATE providers SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (prov_id,))

    return await get_settings(conn, mask=True)


async def verify_console_token(conn: aiosqlite.Connection, token: str) -> bool:
    """Verifies if the provided token matches the configured console token."""
    if not token:
        return False
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT console_token FROM settings WHERE id = 1;")
    row = await cursor.fetchone()
    if not row:
        return False
    stored = str(row["console_token"] or "")
    if not stored:
        return False
    decrypted = decrypt_secret(stored)
    return hmac.compare_digest(decrypted, token)


async def get_tts_cache_entry(conn: aiosqlite.Connection, cache_key: str) -> Optional[TtsCacheEntry]:
    """Fetches a TTS cache entry record by cache key."""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM tts_cache_entries WHERE cache_key = ?;", (cache_key,))
    row = await cursor.fetchone()
    if not row:
        return None
    return TtsCacheEntry(**dict(row))


async def touch_tts_cache_entry(conn: aiosqlite.Connection, cache_key: str) -> None:
    """Updates last_accessed_at and increments hit count for a cache key."""
    async with immediate_transaction(conn):
        await conn.execute("""
            UPDATE tts_cache_entries
            SET hit_count = hit_count + 1, last_accessed_at = CURRENT_TIMESTAMP
            WHERE cache_key = ?;
        """, (cache_key,))


async def batch_touch_tts_cache_entries(conn: aiosqlite.Connection, touches: Dict[str, int]) -> None:
    """Uses an immediate_transaction to batch-update hit counts and last_accessed_at for multiple cache keys in a single transaction."""
    if not touches:
        return
    params = [(count, key) for key, count in touches.items() if count > 0]
    if not params:
        return
    async with immediate_transaction(conn):
        await conn.executemany("""
            UPDATE tts_cache_entries
            SET hit_count = hit_count + ?, last_accessed_at = CURRENT_TIMESTAMP
            WHERE cache_key = ?;
        """, params)


async def record_tts_cache_hit(conn: aiosqlite.Connection, cache_key: str) -> None:
    """Record a TTS cache hit in the database by touching the entry timestamp and incrementing hit count."""
    await touch_tts_cache_entry(conn, cache_key)


async def record_tts_cache_miss(conn: aiosqlite.Connection, cache_key: Optional[str] = None) -> None:
    """Record a TTS cache miss (placeholder hook for SQLite telemetry if needed)."""
    # SQLite cache table does not store misses; this is a safe telemetry hook.
    pass


async def upsert_tts_cache_entry(
    conn: aiosqlite.Connection,
    cache_key: str,
    text: str,
    clean_text: str,
    voice_profile_id: Optional[int],
    params_hash: str,
    file_path: str,
    file_size: int,
    duration_ms: int = 0
) -> TtsCacheEntry:
    """Inserts or updates TTS cache entry metadata."""
    conn.row_factory = aiosqlite.Row
    async with immediate_transaction(conn):
        await conn.execute("""
            INSERT INTO tts_cache_entries (
                cache_key, text, clean_text, voice_profile_id, params_hash,
                file_path, file_size, duration_ms, hit_count, created_at, last_accessed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(cache_key) DO UPDATE SET
                file_path = excluded.file_path,
                file_size = excluded.file_size,
                duration_ms = excluded.duration_ms,
                last_accessed_at = CURRENT_TIMESTAMP;
        """, (cache_key, text, clean_text, voice_profile_id or 1, params_hash, file_path, file_size, duration_ms))
        cursor = await conn.execute("SELECT * FROM tts_cache_entries WHERE cache_key = ?;", (cache_key,))
        row = await cursor.fetchone()
    return TtsCacheEntry(**dict(row))


async def delete_tts_cache_entry(conn: aiosqlite.Connection, cache_key: str) -> bool:
    """Deletes a TTS cache entry record by cache key."""
    async with immediate_transaction(conn):
        cursor = await conn.execute("DELETE FROM tts_cache_entries WHERE cache_key = ?;", (cache_key,))
    return cursor.rowcount > 0


async def get_oldest_tts_cache_entries(conn: aiosqlite.Connection, limit: int = 100) -> List[TtsCacheEntry]:
    """Retrieves oldest TTS cache entries ordered by last_accessed_at."""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("""
        SELECT * FROM tts_cache_entries
        ORDER BY last_accessed_at ASC
        LIMIT ?;
    """, (limit,))
    rows = await cursor.fetchall()
    return [TtsCacheEntry(**dict(r)) for r in rows]


async def clean_tts_cache_lru(
    conn: aiosqlite.Connection,
    max_entries: int = 5000,
    max_mb: Optional[int] = None,
) -> int:
    """
    Prunes the oldest entries in SQLite TTS cache when total entries or size exceed limits.
    Returns the number of deleted database entries.
    """
    stats = await get_tts_cache_stats(conn)
    total_files = stats.get("total_files", 0)
    total_mb = stats.get("total_size_mb", 0.0)

    entries_exceeded = total_files > max_entries
    mb_exceeded = max_mb is not None and total_mb > max_mb

    if not entries_exceeded and not mb_exceeded:
        return 0

    deleted_count = 0
    while True:
        excess = total_files - max_entries if entries_exceeded else 50
        batch_limit = min(max(excess, 10), 200)
        oldest = await get_oldest_tts_cache_entries(conn, limit=batch_limit)
        if not oldest:
            break

        for entry in oldest:
            if await delete_tts_cache_entry(conn, entry.cache_key):
                deleted_count += 1
                total_files -= 1

        if total_files <= max_entries:
            if max_mb is None:
                break
            new_stats = await get_tts_cache_stats(conn)
            if new_stats.get("total_size_mb", 0.0) <= max_mb:
                break

    return deleted_count


async def get_tts_cache_stats(conn: aiosqlite.Connection) -> Dict[str, Any]:
    """Returns summary statistics of TTS cache entries."""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("""
        SELECT
            COUNT(*) as total_files,
            COALESCE(SUM(file_size), 0) as total_size_bytes,
            COALESCE(SUM(hit_count), 0) as total_hits
        FROM tts_cache_entries;
    """)
    row = await cursor.fetchone()
    if not row:
        return {"total_files": 0, "total_size_bytes": 0, "total_size_mb": 0.0, "total_hits": 0}

    total_files = row["total_files"] or 0
    total_size_bytes = row["total_size_bytes"] or 0
    total_hits = row["total_hits"] or 0
    total_size_mb = round(total_size_bytes / (1024 * 1024), 2)
    return {
        "total_files": total_files,
        "total_size_bytes": total_size_bytes,
        "total_size_mb": total_size_mb,
        "total_hits": total_hits,
    }


async def clear_all_tts_cache_entries(conn: aiosqlite.Connection) -> int:
    """Deletes all TTS cache entries from database."""
    async with immediate_transaction(conn):
        cursor = await conn.execute("DELETE FROM tts_cache_entries;")
    return cursor.rowcount


async def insert_token_metric(
    conn: aiosqlite.Connection,
    session_id: str,
    channel: str,
    provider_id: str,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    estimated_cost: float,
    ttft_ms: float,
    tts_first_chunk_ms: float,
    total_latency_ms: float,
    tts_cached_chunks: int = 0,
    tts_generated_chunks: int = 0
) -> int:
    """Records token usage and latency metrics for a generation turn."""
    total_tokens = prompt_tokens + completion_tokens
    async with immediate_transaction(conn):
        cursor = await conn.execute("""
            INSERT INTO token_usage_metrics (
                session_id, channel, provider_id, model_name, prompt_tokens,
                completion_tokens, total_tokens, estimated_cost, ttft_ms,
                tts_first_chunk_ms, total_latency_ms, tts_cached_chunks, tts_generated_chunks,
                timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
        """, (
            session_id, channel, provider_id, model_name, prompt_tokens,
            completion_tokens, total_tokens, estimated_cost, ttft_ms,
            tts_first_chunk_ms, total_latency_ms, tts_cached_chunks, tts_generated_chunks
        ))
        new_id = cursor.lastrowid
    return new_id


async def get_metrics_overview(conn: aiosqlite.Connection) -> Dict[str, Any]:
    """Computes aggregated token usage, cost, and latency metrics."""
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("""
        SELECT
            COUNT(*) as total_requests,
            COALESCE(SUM(prompt_tokens), 0) as total_prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) as total_completion_tokens,
            COALESCE(SUM(total_tokens), 0) as total_tokens,
            COALESCE(SUM(estimated_cost), 0.0) as estimated_cost_usd,
            COALESCE(AVG(NULLIF(ttft_ms, 0)), 0.0) as avg_ttft_ms,
            COALESCE(AVG(NULLIF(tts_first_chunk_ms, 0)), 0.0) as avg_tts_first_chunk_ms,
            COALESCE(AVG(NULLIF(total_latency_ms, 0)), 0.0) as avg_total_latency_ms
        FROM token_usage_metrics;
    """)
    row = await cursor.fetchone()
    if not row:
        return {
            "total_requests": 0, "total_prompt_tokens": 0, "total_completion_tokens": 0,
            "total_tokens": 0, "estimated_cost_usd": 0.0, "estimated_cost_cny": 0.0,
            "avg_ttft_ms": 0.0, "avg_tts_first_chunk_ms": 0.0, "avg_total_latency_ms": 0.0,
        }
    cost_usd = float(row["estimated_cost_usd"] or 0.0)
    cost_cny = round(cost_usd * USD_TO_CNY_RATE, 4)
    return {
        "total_requests": row["total_requests"] or 0,
        "total_prompt_tokens": row["total_prompt_tokens"] or 0,
        "total_completion_tokens": row["total_completion_tokens"] or 0,
        "total_tokens": row["total_tokens"] or 0,
        "estimated_cost_usd": round(cost_usd, 6),
        "estimated_cost_cny": cost_cny,
        "avg_ttft_ms": round(float(row["avg_ttft_ms"] or 0.0), 1),
        "avg_tts_first_chunk_ms": round(float(row["avg_tts_first_chunk_ms"] or 0.0), 1),
        "avg_total_latency_ms": round(float(row["avg_total_latency_ms"] or 0.0), 1),
    }


async def get_provider_metrics_breakdown(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    """Calculates token usage and cost breakdown grouped by provider."""
    conn.row_factory = aiosqlite.Row
    cur_tot = await conn.execute("SELECT COALESCE(SUM(total_tokens), 0) as grand_total FROM token_usage_metrics;")
    row_tot = await cur_tot.fetchone()
    grand_total = row_tot["grand_total"] if row_tot and row_tot["grand_total"] > 0 else 1

    cursor = await conn.execute("""
        SELECT
            provider_id,
            COUNT(*) as request_count,
            COALESCE(SUM(total_tokens), 0) as total_tokens,
            COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) as completion_tokens,
            COALESCE(SUM(estimated_cost), 0.0) as estimated_cost_usd
        FROM token_usage_metrics
        GROUP BY provider_id
        ORDER BY total_tokens DESC;
    """)
    rows = await cursor.fetchall()
    results = []

    provider_names = {
        "deepseek": "DeepSeek",
        "openai": "OpenAI",
        "gemini": "Google Gemini",
        "anthropic": "Anthropic Claude",
        "qwen": "通义千问 (Qwen)",
        "glm": "智谱 GLM",
        "xai": "xAI (Grok)",
        "siliconflow": "SiliconFlow",
        "custom": "自定义模型"
    }

    for r in rows:
        pid = r["provider_id"]
        t_tokens = r["total_tokens"] or 0
        pct = round((t_tokens / grand_total) * 100.0, 1)
        results.append({
            "provider_id": pid,
            "name": provider_names.get(pid, pid.capitalize()),
            "request_count": r["request_count"] or 0,
            "total_tokens": t_tokens,
            "prompt_tokens": r["prompt_tokens"] or 0,
            "completion_tokens": r["completion_tokens"] or 0,
            "estimated_cost_usd": round(float(r["estimated_cost_usd"] or 0.0), 6),
            "percentage": pct
        })
    return results


async def get_recent_latency_trends(conn: aiosqlite.Connection, limit: int = 30) -> List[Dict[str, Any]]:
    """Retrieves recent chronological latency measurements."""
    conn.row_factory = aiosqlite.Row
    try:
        safe_limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        safe_limit = 30
    cursor = await conn.execute("""
        SELECT * FROM (
            SELECT
                timestamp,
                ttft_ms,
                tts_first_chunk_ms,
                total_latency_ms,
                model_name,
                provider_id
            FROM token_usage_metrics
            ORDER BY id DESC
            LIMIT ?
        ) ORDER BY timestamp ASC;
    """, (safe_limit,))
    rows = await cursor.fetchall()
    return [
        {
            "timestamp": r["timestamp"],
            "ttft_ms": round(float(r["ttft_ms"] or 0.0), 1),
            "tts_first_chunk_ms": round(float(r["tts_first_chunk_ms"] or 0.0), 1),
            "total_latency_ms": round(float(r["total_latency_ms"] or 0.0), 1),
            "model_name": r["model_name"] or "",
            "provider_id": r["provider_id"] or "",
        }
        for r in rows
    ]
