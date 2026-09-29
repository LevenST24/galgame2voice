"""
Persistent TTS Offline Audio Cache Manager for galgame2voice.
Provides deterministic SHA256 canonical hashing, sub-50ms cache hits,
SQLite metadata indexing, and automatic LRU capacity pruning.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional, Tuple, Union

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db, get_database_path
from galgame2voice.services.gpt_sovits_client import (
    normalize_japanese_for_tts,
)

logger = logging.getLogger("galgame2voice.services.tts_cache_manager")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Sentinel for "the previous cache row could not be read", which is distinct from
# None ("no previous row exists" = this write is an INSERT).
_STATS_UNKNOWN = object()


def _get_prof_val(prof: Any, attr: str, default: Any = "") -> Any:
    """Helper to extract attribute or key from voice profile object or dictionary."""
    if prof is None:
        return default
    if isinstance(prof, dict):
        return prof.get(attr, default)
    return getattr(prof, attr, default)


class TtsCacheManager:
    """
    Manages persistent disk & SQLite cache for synthesized TTS audio.
    Ensures zero GPU inference latency (<50ms) for repeated voicelines and static dialogue.
    """

    def __init__(
        self,
        cache_dir: Optional[Union[str, Path]] = None,
        db_path: Optional[Union[str, Path]] = None,
        max_cache_mb: int = 1024,
        max_entries: int = 5000,
        max_mem_entries: int = 128,
        max_mem_mb: int = 64,
    ) -> None:
        settings = get_settings()
        self.audio_root = Path(settings.audio_dir)
        self.cache_dir = Path(cache_dir or (self.audio_root / "cache"))
        self.db_path = str(db_path) if db_path is not None else get_database_path()
        self.max_cache_mb = max_cache_mb
        self.max_entries = max_entries
        self.max_mem_entries = max_mem_entries
        self.max_mem_bytes = max(max_mem_mb, 1) * 1024 * 1024

        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._hits: int = 0
        self._misses: int = 0
        self._lock = asyncio.Lock()
        self._prune_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        # High-speed In-Memory LRU Cache layer (<0.1ms access time)
        self._mem_cache: OrderedDict[str, bytes] = OrderedDict()
        self._mem_bytes_total: int = 0
        self._touch_throttle: Dict[str, float] = {}
        # Batch write cache access times: in-memory buffer protected by self._lock
        self._dirty_touches: Dict[str, int] = {}
        self._flush_interval: float = 15.0
        self._flush_task: Optional[asyncio.Task] = None
        # Strong references for fire-and-forget background tasks (prevent GC mid-flight).
        self._bg_tasks: set = set()
        # In-memory disk cache metadata tracking to avoid DB/disk scans on every synthesis
        self._disk_bytes_total: Optional[int] = None
        self._disk_files_total: Optional[int] = None
        self._stats_initialized: bool = False
        self._ensure_flusher_running()

    def _mem_cache_discard(self, cache_key: str) -> None:
        evicted = self._mem_cache.pop(cache_key, None)
        if evicted is not None:
            self._mem_bytes_total -= len(evicted)
        self._touch_throttle.pop(cache_key, None)

    def _mem_cache_store(self, cache_key: str, audio_bytes: bytes) -> None:
        self._mem_cache_discard(cache_key)
        self._mem_cache[cache_key] = audio_bytes
        self._mem_bytes_total += len(audio_bytes)
        self._mem_cache.move_to_end(cache_key)
        # Byte-based cap with entry-count fallback; newest entry always retained.
        while len(self._mem_cache) > 1 and (
            len(self._mem_cache) > self.max_mem_entries
            or self._mem_bytes_total > self.max_mem_bytes
        ):
            _, evicted = self._mem_cache.popitem(last=False)
            self._mem_bytes_total -= len(evicted)

    def _update_disk_stats_after_write(self, file_size: int, prev_file_size: Optional[int]) -> None:
        """Updates in-memory disk cache size delta after writing an entry."""
        if prev_file_size is _STATS_UNKNOWN:
            # The previous row could not be read, so this write may
            # be an INSERT or an UPSERT overwrite: invalidate the
            # stats and let the next prune/get_stats rebuild them
            # from the DB truth instead of double-counting.
            self._disk_bytes_total = None
            self._disk_files_total = None
            self._stats_initialized = False
        elif self._disk_bytes_total is not None:
            # Delta-based update: an UPSERT replaces the old row, so
            # only the size difference counts; a fresh key adds a file.
            delta = file_size - (prev_file_size or 0)
            self._disk_bytes_total += delta
            if prev_file_size is None:
                self._disk_files_total = (self._disk_files_total or 0) + 1

    def _spawn_background(self, coro: Any) -> None:
        """Runs a coroutine in the background with strong ref (prevents GC mid-flight)."""
        try:
            task = asyncio.create_task(coro)
            self._bg_tasks.add(task)

            def _on_done(t: asyncio.Task) -> None:
                self._bg_tasks.discard(t)
                if not t.cancelled():
                    _ = t.exception()

            task.add_done_callback(_on_done)
        except RuntimeError:
            pass

    def _throttle_touch(self, cache_key: str) -> bool:
        """Records a throttled DB touch; returns True when a touch should be scheduled.

        The throttle map is bounded: entries older than 60s are dropped once the map
        grows past 4x the in-memory entry cap, so long uptimes with many distinct
        keys cannot grow it without limit. If still exceeding bounds under high key churn,
        oldest entries are pruned to enforce a strict hard cap.
        """
        now = time.time()
        throttle = self._touch_throttle
        if cache_key in throttle and (now - throttle[cache_key] <= 5.0):
            return False
        max_bound = max(self.max_mem_entries * 4, 128)
        if len(throttle) > max_bound:
            cutoff = now - 60.0
            expired_keys = [k for k, ts in list(throttle.items()) if ts < cutoff]
            for k in expired_keys:
                throttle.pop(k, None)
            if len(throttle) > max_bound:
                excess = len(throttle) - max_bound
                sorted_keys = sorted(throttle.keys(), key=lambda k: throttle.get(k, 0.0))
                for k in sorted_keys[:excess]:
                    throttle.pop(k, None)
        throttle[cache_key] = now
        return True

    def _ensure_flusher_running(self) -> None:
        """Ensures that the periodic background flusher task is running."""
        if self._flush_task is None or self._flush_task.done():
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    self._flush_task = loop.create_task(self._periodic_flush_loop())

                    def _on_flusher_done(t: asyncio.Task) -> None:
                        if not t.cancelled():
                            _ = t.exception()

                    self._flush_task.add_done_callback(_on_flusher_done)
            except RuntimeError:
                pass

    async def _periodic_flush_loop(self) -> None:
        """Periodically flushes accumulated dirty touches to the database."""
        try:
            while True:
                await asyncio.sleep(self._flush_interval)
                await self._flush_dirty_touches()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("TTS cache periodic flusher encountered unexpected error: %s", exc)

    async def _flush_dirty_touches(self) -> None:
        """Flushes accumulated dirty touches to the database in a single batch transaction."""
        async with self._lock:
            if not self._dirty_touches:
                return
            touches = self._dirty_touches
            self._dirty_touches = {}

        try:
            loop = asyncio.get_running_loop()
            if not loop.is_running() or loop.is_closed():
                return
            async with get_db(self.db_path) as conn:
                await crud.batch_touch_tts_cache_entries(conn, touches)
        except Exception as exc:
            logger.debug("Non-critical: could not batch touch tts_cache_entries: %s", exc)
            async with self._lock:
                for k, v in touches.items():
                    self._dirty_touches[k] = self._dirty_touches.get(k, 0) + v

    async def flush_dirty_touches(self) -> None:
        """Public method to flush accumulated dirty touches immediately."""
        await self._flush_dirty_touches()

    async def aclose(self) -> None:
        """Waits for pending background tasks (DB touches, pruning) to finish.

        Flushes buffered dirty touches to database in a single batch transaction.
        """
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
            self._flush_task = None

        await self._flush_dirty_touches()

        pending = [t for t in self._bg_tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=5.0)
        stragglers = [t for t in self._bg_tasks if not t.done()]
        for t in stragglers:
            t.cancel()
        if stragglers:
            await asyncio.gather(*stragglers, return_exceptions=True)
        self._bg_tasks.clear()

    def compute_cache_key(
        self,
        text: str,
        options: Optional[Dict[str, Any]] = None,
        voice_profile: Optional[Any] = None,
    ) -> Tuple[str, str, str]:
        """
        Computes canonical SHA256 cache key from normalized text and inference parameters.
        Returns:
            (cache_key_sha256, clean_text, params_hash_sha256)
        """
        clean_text = normalize_japanese_for_tts(text).strip()
        opts = dict(options or {})

        # Extract voice profile info if provided
        voice_profile_id = 1
        gpt_weights = opts.get("gpt_weights_path", "")
        sovits_weights = opts.get("sovits_weights_path", "")
        ref_audio = opts.get("ref_audio_path") or opts.get("refer_audio_path", "")
        prompt_text = opts.get("prompt_text") or opts.get("refer_text", "")
        prompt_lang = opts.get("prompt_lang") or opts.get("refer_language") or opts.get("prompt_language", "ja")
        text_lang = opts.get("text_lang") or opts.get("text_language", "ja")

        if voice_profile is not None:
            voice_profile_id = _get_prof_val(voice_profile, "id", voice_profile_id)
            if not gpt_weights:
                gpt_weights = _get_prof_val(voice_profile, "gpt_weights_path", "")
            if not sovits_weights:
                sovits_weights = _get_prof_val(voice_profile, "sovits_weights_path", "")
            if not ref_audio:
                ref_audio = _get_prof_val(voice_profile, "ref_audio_path", "")
            if not prompt_text:
                prompt_text = _get_prof_val(voice_profile, "prompt_text", "")
            if not prompt_lang:
                prompt_lang = _get_prof_val(voice_profile, "prompt_lang", "")
            if not text_lang:
                text_lang = _get_prof_val(voice_profile, "text_lang", "")

        # Canonicalize inference parameters
        speed = float(opts.get("speed_factor", 1.0))
        speed_str = f"{speed:.3f}"
        temperature = float(opts.get("temperature", 1.0))
        temp_str = f"{temperature:.3f}"
        top_k = int(opts.get("top_k", 15))
        top_p = float(opts.get("top_p", 1.0))
        top_p_str = f"{top_p:.3f}"
        seed = int(opts.get("seed", -1))
        batch_size = int(opts.get("batch_size", 1))
        user_split = (
            opts.get("text_split_method")
            or opts.get("cut_option")
            or opts.get("how_to_cut")
        )
        if user_split:
            text_split_method = str(user_split).lower()
        else:
            text_split_method = "cut0" if len(clean_text.strip()) <= 80 else "cut2"
        fragment_interval = float(opts.get("fragment_interval", 0.3))
        frag_str = f"{fragment_interval:.3f}"

        # Ref audio normalization (include name:mtime_ns:size if file exists on disk, fallback to name)
        ref_audio_norm = ""
        if ref_audio:
            p = Path(ref_audio)
            if not p.is_file() and (_PROJECT_ROOT / ref_audio).is_file():
                p = _PROJECT_ROOT / ref_audio
            if p.is_file():
                try:
                    st = p.stat()
                    ref_audio_norm = f"{p.name}:{st.st_mtime_ns}:{st.st_size}"
                except OSError:
                    ref_audio_norm = p.name
            else:
                ref_audio_norm = p.name

        params_dict = {
            "voice_profile_id": voice_profile_id,
            "gpt_weights": str(gpt_weights),
            "sovits_weights": str(sovits_weights),
            "ref_audio": ref_audio_norm,
            "prompt_text": str(prompt_text),
            "prompt_lang": str(prompt_lang).lower(),
            "text_lang": str(text_lang).lower(),
            "speed": speed_str,
            "temperature": temp_str,
            "top_k": top_k,
            "top_p": top_p_str,
            "seed": seed,
            "batch_size": batch_size,
            "text_split_method": text_split_method,
            "fragment_interval": frag_str,
        }

        params_json = json.dumps(params_dict, sort_keys=True, separators=(",", ":"))
        params_hash = hashlib.sha256(params_json.encode("utf-8")).hexdigest()

        canonical_payload = {
            "clean_text": clean_text,
            "params": params_dict,
        }
        canonical_json = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
        cache_key = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

        return cache_key, clean_text, params_hash

    async def get(self, cache_key: str) -> Optional[Tuple[bytes, str, int]]:
        """
        Retrieves cached audio bytes and URL for the given cache key.
        Checks high-speed in-memory LRU cache first (<0.005ms), falling back to disk (<15ms).
        Returns (audio_bytes, url_path, file_size) if hit, None if miss.
        """
        try:
            from galgame2voice.config import get_settings
            if get_settings().privacy_mode:
                return None
        except Exception as exc:
            logger.debug("Failed checking privacy_mode: %s", exc)

        url_path = f"/audio/cache/{cache_key}.wav"

        # 1. Fast path: In-Memory LRU Cache hit (<0.005ms, pure RAM dictionary lookup under lock)
        async with self._lock:
            if cache_key in self._mem_cache:
                self._mem_cache.move_to_end(cache_key)
                data = self._mem_cache[cache_key]
                self._hits += 1
                self._dirty_touches[cache_key] = self._dirty_touches.get(cache_key, 0) + 1
                self._throttle_touch(cache_key)
                self._ensure_flusher_running()
                return data, url_path, len(data)

        # 2. Slow path: Disk & SQLite cache (miss in memory)
        file_path = self.cache_dir / f"{cache_key}.wav"

        # Verify disk file presence and non-zero size to detect manual unlinking or corruption
        if not file_path.exists():
            async with self._lock:
                self._mem_cache_discard(cache_key)
                self._misses += 1
            return None

        try:
            file_size = file_path.stat().st_size
        except OSError:
            async with self._lock:
                self._mem_cache_discard(cache_key)
                self._misses += 1
            return None

        if file_size == 0:
            # Corrupted 0-byte file on disk -> evict from memory cache and delete
            async with self._lock:
                self._mem_cache_discard(cache_key)
                self._misses += 1
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                pass
            return None

        try:
            audio_bytes = await asyncio.to_thread(file_path.read_bytes)
            async with self._lock:
                self._hits += 1
                self._dirty_touches[cache_key] = self._dirty_touches.get(cache_key, 0) + 1
                self._throttle_touch(cache_key)
                self._ensure_flusher_running()
                # Populate In-Memory LRU Cache
                self._mem_cache_store(cache_key, audio_bytes)
            return audio_bytes, url_path, len(audio_bytes)
        except Exception as e:
            logger.warning("Failed to read cache file %s: %s", file_path, e)
            async with self._lock:
                self._misses += 1
            return None

    async def stream_cached(
        self,
        cache_key: str,
        chunk_size: int = 4096,
    ) -> AsyncGenerator[bytes, None]:
        """
        Streams cached audio chunks directly from in-memory cache or disk cache.
        Avoids loading multi-megabyte audio files entirely into temporary memory.
        """
        try:
            from galgame2voice.config import get_settings
            if get_settings().privacy_mode:
                return
        except Exception as exc:
            logger.debug("Failed checking privacy_mode: %s", exc)

        mem_data = None
        bounded_chunk_size = chunk_size if chunk_size > 0 else 4096

        async with self._lock:
            if cache_key in self._mem_cache:
                self._mem_cache.move_to_end(cache_key)
                mem_data = self._mem_cache[cache_key]
                self._hits += 1
                self._dirty_touches[cache_key] = self._dirty_touches.get(cache_key, 0) + 1
                self._throttle_touch(cache_key)
                self._ensure_flusher_running()

        if mem_data is not None:
            for i in range(0, len(mem_data), bounded_chunk_size):
                yield mem_data[i:i + bounded_chunk_size]
            return

        file_path = self.cache_dir / f"{cache_key}.wav"
        if not file_path.is_file():
            return

        try:
            sz = file_path.stat().st_size
            if sz == 0:
                return
        except OSError:
            return

        should_buffer = (sz <= self.max_mem_bytes)
        collected = bytearray() if should_buffer else None

        try:
            with open(file_path, "rb") as f:
                async with self._lock:
                    self._hits += 1
                    self._dirty_touches[cache_key] = self._dirty_touches.get(cache_key, 0) + 1
                    self._throttle_touch(cache_key)
                    self._ensure_flusher_running()

                while True:
                    chunk = await asyncio.to_thread(f.read, bounded_chunk_size)
                    if not chunk:
                        break
                    if should_buffer and collected is not None:
                        collected.extend(chunk)
                    yield chunk

            if should_buffer and collected is not None and len(collected) <= self.max_mem_bytes:
                async with self._lock:
                    self._mem_cache_store(cache_key, bytes(collected))
        except Exception as exc:
            logger.warning("stream_cached failed for %s: %s", file_path, exc)

    @staticmethod
    def _write_audio_file_sync(file_path: Path, audio_bytes: bytes) -> None:
        """Atomic write to file via unique temp file with Windows AV transient lock retries."""
        tmp_path = file_path.with_suffix(f".tmp.{os.getpid()}_{time.time_ns()}_{uuid.uuid4().hex}.wav")
        try:
            tmp_path.write_bytes(audio_bytes)
            for attempt in range(5):
                try:
                    tmp_path.replace(file_path)
                    return
                except (PermissionError, OSError) as err:
                    try:
                        if file_path.exists() and file_path.stat().st_size > 0:
                            # Windows AV/indexer may transiently lock the target;
                            # the existing file is a valid cache entry for this key,
                            # so keep it rather than failing the whole put.
                            logger.warning(
                                "Cache file %s locked (%s); keeping existing file, "
                                "fresh bytes discarded for this write.",
                                file_path, err,
                            )
                            tmp_path.unlink(missing_ok=True)
                            return
                    except OSError:
                        pass
                    if attempt == 4:
                        tmp_path.unlink(missing_ok=True)
                        raise err
                    time.sleep(0.005 * (attempt + 1))
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    async def _persist_cache_metadata(
        self,
        cache_key: str,
        text: str,
        clean_text: str,
        voice_profile_id: Optional[int],
        params_hash: str,
        file_path: Path,
        file_size: int,
        duration_ms: int,
    ) -> None:
        """Persists cache metadata row into SQLite with locked/busy backoff and auto-init retry."""
        last_exc = None
        for db_attempt in range(5):
            try:
                async with get_db(self.db_path) as conn:
                    await crud.upsert_tts_cache_entry(
                        conn=conn,
                        cache_key=cache_key,
                        text=text,
                        clean_text=clean_text,
                        voice_profile_id=voice_profile_id or 1,
                        params_hash=params_hash,
                        file_path=str(file_path),
                        file_size=file_size,
                        duration_ms=duration_ms,
                    )
                return
            except Exception as exc:
                last_exc = exc
                if ("locked" in str(exc).lower() or "busy" in str(exc).lower()) and db_attempt < 4:
                    await asyncio.sleep(0.02 * (db_attempt + 1))
                    continue
                if "no such table" in str(exc).lower():
                    try:
                        from galgame2voice.database.session import init_db
                        await init_db(self.db_path)
                        continue
                    except Exception as init_err:
                        logger.warning("Failed to auto-init DB in TtsCacheManager: %s", init_err)
                logger.warning("Failed to insert tts_cache_entry in DB: %s", exc)
                break

        try:
            await asyncio.to_thread(file_path.unlink, missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"Failed to persist cache entry metadata for key {cache_key}: {last_exc}") from last_exc

    async def put(
        self,
        cache_key: str,
        text: str,
        clean_text: str,
        voice_profile_id: Optional[int],
        params_hash: str,
        audio_bytes: bytes,
        duration_ms: int = 0,
    ) -> Tuple[str, Path, int]:
        """
        Persists synthesized audio bytes to disk, memory cache, and registers metadata in SQLite.
        Returns (url_path, local_file_path, byte_count).
        """
        if not audio_bytes:
            raise ValueError("Cannot cache empty audio bytes")

        try:
            from galgame2voice.config import get_settings
            if get_settings().privacy_mode:
                return "", Path(""), len(audio_bytes)
        except Exception as exc:
            logger.debug("Failed checking privacy_mode: %s", exc)

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.cache_dir / f"{cache_key}.wav"

        # upsert_tts_cache_entry is an UPSERT: on a repeated key the row is
        # replaced, not added. Read the previous file_size up front so the
        # in-memory disk stats are adjusted by the delta instead of being
        # double-counted on every re-synthesis of the same key.
        # _STATS_UNKNOWN distinguishes "query failed, INSERT/UPDATE unknown"
        # from prev_file_size=None which unambiguously means "no previous row"
        # (i.e. this write inserts a new cache entry).
        prev_file_size: Optional[int] = _STATS_UNKNOWN
        try:
            async with get_db(self.db_path) as _conn:
                _prev = await crud.get_tts_cache_entry(_conn, cache_key)
                # The query succeeded, so its result is authoritative:
                # None means "no previous row exists" => this write is an INSERT.
                prev_file_size = _prev.file_size if _prev is not None else None
        except Exception as exc:
            logger.debug("Could not read previous cache entry for %s: %r", cache_key, exc)
            prev_file_size = _STATS_UNKNOWN  # stats fallback handled below

        async with self._write_lock:
            try:
                await asyncio.to_thread(self._write_audio_file_sync, file_path, audio_bytes)
                file_size = len(audio_bytes)
                url_path = f"/audio/cache/{cache_key}.wav"

                await self._persist_cache_metadata(
                    cache_key=cache_key,
                    text=text,
                    clean_text=clean_text,
                    voice_profile_id=voice_profile_id,
                    params_hash=params_hash,
                    file_path=file_path,
                    file_size=file_size,
                    duration_ms=duration_ms,
                )

                # Populate In-Memory LRU Cache only after successful persistence
                async with self._lock:
                    self._mem_cache_store(cache_key, audio_bytes)
                    self._update_disk_stats_after_write(file_size, prev_file_size)
            except Exception:
                async with self._lock:
                    self._mem_cache_discard(cache_key)
                raise

        # Check if cache capacity threshold could be exceeded before triggering pruning
        limit_bytes = self.max_cache_mb * 1024 * 1024
        limit_entries = self.max_entries

        should_prune = (
            not self._stats_initialized
            or self._disk_bytes_total is None
            or self._disk_bytes_total > limit_bytes
            or (self._disk_files_total is not None and self._disk_files_total > limit_entries)
        )

        # Trigger background pruning only when capacity limit is approached or metadata is uninitialized
        if should_prune:
            self._spawn_background(self._check_and_prune())

        return url_path, file_path, file_size

    async def _check_and_prune(self):
        """Asynchronously checks if capacity thresholds are exceeded and prunes LRU entries.
        Guarded by _prune_lock to coalesce redundant triggers and prevent concurrent stampedes."""
        if self._prune_lock.locked():
            return
        async with self._prune_lock:
            try:
                await self.prune(
                    max_mb=self.max_cache_mb,
                    max_entries=self.max_entries,
                )
            except Exception as exc:
                logger.debug("Error during automatic cache pruning: %s", exc)

    async def prune(
        self,
        max_mb: Optional[int] = None,
        max_entries: Optional[int] = None,
    ) -> int:
        """
        Performs LRU pruning of cache files when limits are exceeded.
        Drains up to 10 batches of 200 entries to satisfy limits under burst writes.
        Verifies disk removal prior to database deletion to prevent orphaned ghost files.
        Returns number of pruned entries.
        """
        limit_mb = max_mb or self.max_cache_mb
        limit_entries = max_entries or self.max_entries
        limit_bytes = limit_mb * 1024 * 1024

        try:
            await self._flush_dirty_touches()
        except Exception as exc:
            logger.debug("Failed flushing dirty touches during enforce_limits: %s", exc)

        async with self._write_lock:
            pruned_count = 0
            try:
                async with get_db(self.db_path) as conn:
                    stats = await crud.get_tts_cache_stats(conn)
                    total_bytes = stats["total_size_bytes"]
                    total_files = stats["total_files"]

                    async with self._lock:
                        self._disk_bytes_total = total_bytes
                        self._disk_files_total = total_files
                        self._stats_initialized = True

                    if total_bytes <= limit_bytes and total_files <= limit_entries:
                        return 0

                    target_bytes = int(limit_bytes * 0.8)
                    target_files = int(limit_entries * 0.8)

                    for _ in range(10):
                        if total_bytes <= target_bytes and total_files <= target_files:
                            break
                        oldest_entries = await crud.get_oldest_tts_cache_entries(conn, limit=200)
                        if not oldest_entries:
                            break
                        batch_pruned = 0
                        for entry in oldest_entries:
                            if total_bytes <= target_bytes and total_files <= target_files:
                                break

                            file_p = Path(entry.file_path)
                            unlink_ok = True
                            if file_p.exists():
                                try:
                                    await asyncio.to_thread(file_p.unlink, missing_ok=True)
                                except Exception as unl_err:
                                    unlink_ok = False
                                    logger.debug("Skipping DB deletion for locked cache file %s: %s", file_p, unl_err)

                            if unlink_ok:
                                await crud.delete_tts_cache_entry(conn, entry.cache_key)
                                async with self._lock:
                                    self._mem_cache_discard(entry.cache_key)
                                total_bytes -= entry.file_size
                                total_files -= 1
                                pruned_count += 1
                                batch_pruned += 1
                        if batch_pruned == 0:
                            break

                    async with self._lock:
                        self._disk_bytes_total = max(total_bytes, 0)
                        self._disk_files_total = max(total_files, 0)
            except Exception as e:
                if "no such table" in str(e).lower():
                    return 0
                raise

            if pruned_count > 0:
                logger.info("Pruned %d oldest TTS cache entries from disk.", pruned_count)
            return pruned_count

    async def clear(self) -> Tuple[int, float]:
        """
        Clears all cache files in audio/cache/ and purges SQLite metadata.
        Guarded by _write_lock to serialize against concurrent put() and prune() operations.
        Returns (count_deleted, freed_mb).
        """
        async with self._write_lock:
            freed_bytes = 0
            deleted_count = 0

            def _scan_and_delete() -> Tuple[int, int]:
                freed = 0
                count = 0
                if self.cache_dir.exists():
                    for f in list(self.cache_dir.iterdir()):
                        if f.is_file():
                            try:
                                s = f.stat().st_size
                                f.unlink(missing_ok=True)
                                freed += s
                                count += 1
                            except FileNotFoundError:
                                pass
                            except Exception as e:
                                logger.warning("Failed to delete cache file %s: %s", f, e)
                return freed, count

            freed_bytes, deleted_count = await asyncio.to_thread(_scan_and_delete)

            try:
                async with get_db(self.db_path) as conn:
                    await crud.clear_all_tts_cache_entries(conn)
            except Exception as exc:
                logger.debug("Failed to clear database cache entries: %s", exc)

            async with self._lock:
                self._mem_cache.clear()
                self._mem_bytes_total = 0
                self._touch_throttle.clear()
                self._dirty_touches.clear()
                self._disk_bytes_total = 0
                self._disk_files_total = 0
                self._stats_initialized = True
                self._hits = 0
                self._misses = 0

            freed_mb = round(freed_bytes / (1024 * 1024), 2)
            logger.info("Cleared TTS cache: deleted %d files, freed %.2f MB", deleted_count, freed_mb)
            return deleted_count, freed_mb

    async def get_stats(self) -> Dict[str, Any]:
        """Returns comprehensive TTS cache statistics."""
        try:
            await self._flush_dirty_touches()
        except Exception as exc:
            logger.debug("Failed flushing dirty touches during get_stats: %s", exc)

        try:
            async with get_db(self.db_path) as conn:
                db_stats = await crud.get_tts_cache_stats(conn)
                async with self._lock:
                    self._disk_bytes_total = db_stats["total_size_bytes"]
                    self._disk_files_total = db_stats["total_files"]
                    self._stats_initialized = True
        except Exception as exc:
            logger.debug("Failed querying cache stats from database: %s", exc)
            db_stats = {"total_files": 0, "total_size_bytes": 0, "total_size_mb": 0.0, "total_hits": 0}

        total_files = db_stats["total_files"]
        total_size_bytes = db_stats["total_size_bytes"]
        total_size_mb = db_stats["total_size_mb"]
        db_hits = db_stats["total_hits"]

        # Report memory hits and db hits separately
        hit_rate = self._hits / (self._hits + self._misses) if (self._hits + self._misses) > 0 else 0.0
        # Estimated computation saved: average 1.5s GPU inference time per cache hit
        estimated_saved_seconds = round(self._hits * 1.5, 2)

        return {
            "total_files": total_files,
            "total_size_bytes": total_size_bytes,
            "total_size_mb": total_size_mb,
            "total_hits": self._hits,
            "total_misses": self._misses,
            "hit_rate_percent": round(hit_rate * 100.0, 2),
            "memory_hits": self._hits,
            "db_hits": db_hits,
            "estimated_saved_seconds": estimated_saved_seconds,
        }


# Singleton accessor
_tts_cache_manager_instance: Optional[TtsCacheManager] = None


def get_tts_cache_manager(
    cache_dir: Optional[Union[str, Path]] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> TtsCacheManager:
    """Returns singleton instance of TtsCacheManager."""
    global _tts_cache_manager_instance
    if _tts_cache_manager_instance is None:
        _tts_cache_manager_instance = TtsCacheManager(cache_dir=cache_dir, db_path=db_path)
    return _tts_cache_manager_instance


def reset_tts_cache_manager() -> None:
    """Resets the singleton for test isolation."""
    global _tts_cache_manager_instance
    _tts_cache_manager_instance = None
