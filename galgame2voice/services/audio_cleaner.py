"""
Audio Cleaner Service for galgame2voice.
Periodically scans and cleans expired ephemeral synthesized audio files
and LRU cached TTS audio fragments while strictly protecting reference audio files.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import List, Optional, Set, Tuple

import sys
import aiosqlite

from galgame2voice.database.session import get_db, immediate_transaction
import galgame2voice.database.crud as crud

logger = logging.getLogger("galgame2voice.services.audio_cleaner")

# TTS cache retention in days: balances compute savings against disk storage
CACHE_RETENTION_DAYS = 7


def _resolve_get_db():
    """Resolves get_db dynamically to maintain 100% compatibility with test suites monkeypatching main.get_db."""
    main_mod = sys.modules.get("galgame2voice.main")
    if main_mod and hasattr(main_mod, "get_db"):
        return main_mod.get_db
    return get_db


from datetime import datetime, timezone


def _cache_scan_and_clean(
    cache_dir: Path,
    cutoff: float,
    active_cache_keys: Optional[Set[str]] = None,
) -> Tuple[int, List[str]]:
    """
    Removes cached audio files.
    When active_cache_keys is provided:
        The database is the single source of truth for LRU retention.
        Disk scanning is ONLY used to purge orphan audio files (files not present in active_cache_keys)
        that were modified more than 1 hour ago (to prevent race conditions with in-progress writes).
    When active_cache_keys is None:
        Fallback legacy mode: unlinks files where mtime < cutoff.
    """
    cleaned = 0
    unlinked_keys: List[str] = []
    if not cache_dir.is_dir():
        return 0, []

    orphan_threshold = time.time() - 3600  # 1 hour safe buffer

    for f in cache_dir.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() in (".wav", ".ogg", ".mp3", ".opus"):
            try:
                should_unlink = False
                if active_cache_keys is not None:
                    # Database is single source of truth:
                    # Only unlink if NOT in active_cache_keys (orphan) and modified before orphan threshold
                    if f.stem not in active_cache_keys and f.stat().st_mtime < orphan_threshold:
                        should_unlink = True
                else:
                    # Legacy fallback
                    if f.stat().st_mtime < cutoff:
                        should_unlink = True

                if should_unlink:
                    f.unlink(missing_ok=True)
                    cleaned += 1
                    unlinked_keys.append(f.stem)
            except Exception as e:
                logger.debug("Failed to remove cached audio %s: %s", f, e)
    return cleaned, unlinked_keys


def _scan_and_clean(
    audio_dir: Path,
    protected_audio_names: Set[str],
    cutoff: float,
    cache_cutoff: float,
    active_cache_keys: Optional[Set[str]] = None,
) -> Tuple[int, List[str]]:
    """Scans and unlinks expired ephemeral audio files and invokes cache cleanup."""
    cleaned = 0
    if audio_dir.exists():
        for f in audio_dir.iterdir():
            # Strictly protect non-file entries and subdirectories (cache, references)
            if f.is_dir() or f.name.lower() in ("cache", "references"):
                continue
            # Strictly protect reference audio files (*.ogg) and registered voice profile references
            if f.suffix.lower() == ".ogg" or f.name.lower() in protected_audio_names:
                continue
            # Target ephemeral synthesized audio files (e.g. chunk_*.wav, full_*.wav)
            if f.is_file() and f.suffix.lower() in (".wav", ".mp3", ".opus"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink(missing_ok=True)
                        cleaned += 1
                except Exception as e:
                    logger.debug("Failed to remove audio file %s: %s", f, e)
    # TTS 分句缓存按天级保留期清理，防止磁盘无限增长并同步清理数据库记录
    cache_cleaned, unlinked_keys = _cache_scan_and_clean(
        audio_dir / "cache", cache_cutoff, active_cache_keys=active_cache_keys
    )
    cleaned += cache_cleaned
    return cleaned, unlinked_keys


async def _clean_lru_cache_from_db(
    conn: aiosqlite.Connection, cache_dir: Path, cache_cutoff: float
) -> Tuple[int, List[str]]:
    """
    Evicts cached TTS entries based on SQLite last_accessed_at (single source of truth for LRU).
    Deletes the underlying audio files and drops the DB records in batches.
    """
    cutoff_iso = datetime.fromtimestamp(cache_cutoff, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = await conn.execute(
        """
        SELECT cache_key, file_path FROM tts_cache_entries
        WHERE datetime(last_accessed_at) < datetime(?)
           OR last_accessed_at < ?;
        """,
        (cutoff_iso, cutoff_iso),
    )
    rows = await cur.fetchall()
    unlinked_keys: List[str] = []
    for r in rows:
        key = r[0]
        raw_path = r[1]
        f = Path(raw_path) if raw_path else (cache_dir / f"{key}.wav")
        if not f.is_absolute():
            f = cache_dir / f.name
        try:
            if f.exists():
                f.unlink(missing_ok=True)
        except Exception as e:
            logger.debug("Failed to unlink LRU expired file %s: %s", f, e)
        unlinked_keys.append(key)

    if unlinked_keys:
        async with immediate_transaction(conn):
            for i in range(0, len(unlinked_keys), 100):
                batch = unlinked_keys[i:i + 100]
                placeholders = ",".join(["?"] * len(batch))
                await conn.execute(
                    f"DELETE FROM tts_cache_entries WHERE cache_key IN ({placeholders});",
                    batch,
                )
    return len(unlinked_keys), unlinked_keys


async def _audio_cleanup_loop(audio_dir: Path, interval_seconds: int) -> None:
    """Periodically removes ephemeral audio files exceeding retention duration while protecting persistent cache.

    The retention duration is re-read from DB settings each cycle so console
    edits to audio_retention_minutes take effect without a restart."""
    logger.info("Started background audio cleanup worker (interval=%d sec)", interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            db_getter = _resolve_get_db()
            protected_audio_names: Set[str] = set()
            active_cache_keys: Optional[Set[str]] = None
            db_evicted_count = 0

            try:
                async with db_getter() as conn:
                    db_settings = await crud.get_settings_raw(conn)
                    if conn is not None:
                        try:
                            cur = await conn.execute("SELECT ref_audio_path FROM voice_profiles;")
                            rows = await cur.fetchall()
                            for r in rows:
                                if r and r[0]:
                                    protected_audio_names.add(Path(r[0]).name.lower())
                        except Exception:
                            pass

                    # LRU Eviction: SQLite last_accessed_at is the sole authority for 7-day retention
                    now = time.time()
                    cache_cutoff = now - (CACHE_RETENTION_DAYS * 86400)
                    try:
                        db_evicted_count, _ = await _clean_lru_cache_from_db(
                            conn, audio_dir / "cache", cache_cutoff
                        )
                        # Fetch active keys to protect valid cache files from orphan cleanup
                        cur = await conn.execute("SELECT cache_key FROM tts_cache_entries;")
                        rows = await cur.fetchall()
                        active_cache_keys = {r[0] for r in rows if r and r[0]}
                    except Exception as lru_err:
                        logger.debug("Database LRU cache eviction failed/skipped: %s", lru_err)

                retention_minutes = int(getattr(db_settings, "audio_retention_minutes", 30) or 30)
            except Exception as exc:
                logger.debug("Falling back to default audio retention: %s", exc)
                retention_minutes = 30

            now = time.time()
            cutoff = now - (retention_minutes * 60)
            cache_cutoff = now - (CACHE_RETENTION_DAYS * 86400)

            cleaned_count, unlinked_keys = await asyncio.to_thread(
                _scan_and_clean, audio_dir, protected_audio_names, cutoff, cache_cutoff, active_cache_keys
            )
            total_cleaned = cleaned_count + db_evicted_count

            if unlinked_keys:
                try:
                    async with db_getter() as conn:
                        async with immediate_transaction(conn):
                            for batch_idx in range(0, len(unlinked_keys), 100):
                                batch = unlinked_keys[batch_idx:batch_idx + 100]
                                filenames = [f"{k}.wav" for k in batch]
                                all_params = batch + filenames + batch
                                p_batch = ",".join(["?"] * len(batch))
                                p_files = ",".join(["?"] * len(filenames))
                                await conn.execute(
                                    f"DELETE FROM tts_cache_entries WHERE cache_key IN ({p_batch}) OR audio_filename IN ({p_files}) OR audio_filename IN ({p_batch});",
                                    all_params,
                                )
                except Exception as db_clean_err:
                    logger.debug("Failed to purge tts_cache_entries for unlinked keys: %s", db_clean_err)

            if total_cleaned > 0 or unlinked_keys:
                logger.info("Audio cleanup removed %d expired/orphan audio files.", total_cleaned)
                try:
                    from galgame2voice.utils.hardware import release_system_memory
                    release_system_memory()
                except Exception:
                    pass
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Error in audio cleanup loop: %s", exc)


class AudioCleanerService:
    """Manages the background audio cleanup loop lifecycle."""

    def __init__(self, audio_dir: Path, interval_seconds: int = 600):
        self.audio_dir = audio_dir
        self.interval_seconds = interval_seconds
        self._task: Optional[asyncio.Task] = None

    def start(self) -> asyncio.Task:
        """Starts the background audio cleanup task if not already running."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                _audio_cleanup_loop(self.audio_dir, self.interval_seconds)
            )
        return self._task

    async def stop(self) -> None:
        """Cancels and waits for the background cleanup task to terminate."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
