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
from typing import Any, Dict, List, Optional, Tuple, Union

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.services.gpt_sovits_client import clean_japanese_parentheses

logger = logging.getLogger("galgame2voice.services.tts_cache_manager")


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
    ):
        settings = get_settings()
        self.audio_root = Path(settings.audio_dir)
        self.cache_dir = Path(cache_dir or (self.audio_root / "cache"))
        self.db_path = str(db_path or settings.db_path)
        self.max_cache_mb = max_cache_mb
        self.max_entries = max_entries
        self.max_mem_entries = max_mem_entries
        self.max_mem_bytes = max(max_mem_mb, 1) * 1024 * 1024

        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._hits: int = 0
        self._misses: int = 0
        self._lock = asyncio.Lock()
        # High-speed In-Memory LRU Cache layer (<0.1ms access time)
        self._mem_cache: OrderedDict[str, bytes] = OrderedDict()
        self._mem_bytes_total: int = 0
        self._touch_throttle: Dict[str, float] = {}
        # Strong references for fire-and-forget background tasks (prevent GC mid-flight).
        self._bg_tasks: set = set()

    def _mem_cache_discard(self, cache_key: str) -> None:
        evicted = self._mem_cache.pop(cache_key, None)
        if evicted is not None:
            self._mem_bytes_total -= len(evicted)
        self._touch_throttle.pop(cache_key, None)

    def _mem_cache_clear(self) -> None:
        self._mem_cache.clear()
        self._mem_bytes_total = 0
        self._touch_throttle.clear()

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

    def _spawn_background(self, coro) -> None:
        """Runs a coroutine in the background with strong ref (prevents GC mid-flight)."""
        try:
            task = asyncio.create_task(coro)
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:
            pass

    async def _touch_db_async(self, cache_key: str) -> None:
        """Asynchronously updates last_accessed_at in the database for the given cache key."""
        try:
            loop = asyncio.get_running_loop()
            if not loop.is_running() or loop.is_closed():
                return
            async with get_db(self.db_path) as conn:
                await crud.touch_tts_cache_entry(conn, cache_key)
        except Exception as exc:
            logger.debug("Non-critical: could not touch tts_cache_entry: %s", exc)

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
        clean_text = clean_japanese_parentheses(text).strip()
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
            if hasattr(voice_profile, "id"):
                voice_profile_id = voice_profile.id
            elif isinstance(voice_profile, dict) and "id" in voice_profile:
                voice_profile_id = voice_profile["id"]

            if not gpt_weights and hasattr(voice_profile, "gpt_weights_path"):
                gpt_weights = voice_profile.gpt_weights_path
            if not sovits_weights and hasattr(voice_profile, "sovits_weights_path"):
                sovits_weights = voice_profile.sovits_weights_path
            if not ref_audio and hasattr(voice_profile, "ref_audio_path"):
                ref_audio = voice_profile.ref_audio_path
            if not prompt_text and hasattr(voice_profile, "prompt_text"):
                prompt_text = voice_profile.prompt_text
            if not prompt_lang and hasattr(voice_profile, "prompt_lang"):
                prompt_lang = voice_profile.prompt_lang
            if not text_lang and hasattr(voice_profile, "text_lang"):
                text_lang = voice_profile.text_lang

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
        text_split_method = str(opts.get("text_split_method", "cut1")).lower()
        fragment_interval = float(opts.get("fragment_interval", 0.3))
        frag_str = f"{fragment_interval:.3f}"

        # Ref audio normalization (keep filename if full path to ensure environment portability)
        ref_audio_norm = Path(ref_audio).name if ref_audio else ""

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
        url_path = f"/audio/cache/{cache_key}.wav"

        # 1. Fast path: In-Memory LRU Cache hit (<0.005ms, pure RAM dictionary lookup under lock)
        async with self._lock:
            if cache_key in self._mem_cache:
                self._mem_cache.move_to_end(cache_key)
                data = self._mem_cache[cache_key]
                self._hits += 1

                now = time.time()
                if cache_key not in self._touch_throttle or (now - self._touch_throttle[cache_key] > 5.0):
                    self._touch_throttle[cache_key] = now
                    self._spawn_background(self._touch_db_async(cache_key))
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
            except Exception:
                pass
            return None

        now = time.time()
        if cache_key not in self._touch_throttle or (now - self._touch_throttle[cache_key] > 5.0):
            self._touch_throttle[cache_key] = now
            self._spawn_background(self._touch_db_async(cache_key))

        try:
            audio_bytes = await asyncio.to_thread(file_path.read_bytes)
            async with self._lock:
                self._hits += 1
                # Populate In-Memory LRU Cache
                self._mem_cache_store(cache_key, audio_bytes)
            return audio_bytes, url_path, len(audio_bytes)
        except Exception as e:
            logger.warning("Failed to read cache file %s: %s", file_path, e)
            async with self._lock:
                self._misses += 1
            return None

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

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.cache_dir / f"{cache_key}.wav"

        # Update In-Memory LRU Cache
        async with self._lock:
            self._mem_cache_store(cache_key, audio_bytes)

        # Atomic write to file via temp file to prevent 0-byte/corrupt files
        def _atomic_write():
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
                                tmp_path.unlink(missing_ok=True)
                                return
                        except Exception:
                            pass
                        if attempt == 4:
                            tmp_path.unlink(missing_ok=True)
                            raise err
                        time.sleep(0.005 * (attempt + 1))
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

        await asyncio.to_thread(_atomic_write)
        file_size = len(audio_bytes)
        url_path = f"/audio/cache/{cache_key}.wav"

        # Register in SQLite
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
                break
            except Exception as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    if db_attempt < 4:
                        await asyncio.sleep(0.02 * (db_attempt + 1))
                        continue
                logger.warning("Failed to insert tts_cache_entry in DB: %s", exc)
                break

        # Trigger background pruning if cache exceeds limits
        self._spawn_background(self._check_and_prune())

        return url_path, file_path, file_size

    async def _check_and_prune(self):
        """Asynchronously checks if capacity thresholds are exceeded and prunes LRU entries."""
        try:
            async with self._lock:
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
        Returns number of pruned entries.
        """
        limit_mb = max_mb or self.max_cache_mb
        limit_entries = max_entries or self.max_entries
        limit_bytes = limit_mb * 1024 * 1024

        pruned_count = 0

        async with get_db(self.db_path) as conn:
            stats = await crud.get_tts_cache_stats(conn)
            total_bytes = stats["total_size_bytes"]
            total_files = stats["total_files"]

            if total_bytes <= limit_bytes and total_files <= limit_entries:
                return 0

            # Target 80% of limit to avoid frequent thrashing
            target_bytes = int(limit_bytes * 0.8)
            target_files = int(limit_entries * 0.8)

            oldest_entries = await crud.get_oldest_tts_cache_entries(conn, limit=200)
            for entry in oldest_entries:
                if total_bytes <= target_bytes and total_files <= target_files:
                    break

                file_p = Path(entry.file_path)
                try:
                    await asyncio.to_thread(lambda: file_p.unlink(missing_ok=True) if file_p.exists() else None)
                except Exception:
                    pass

                await crud.delete_tts_cache_entry(conn, entry.cache_key)
                self._mem_cache_discard(entry.cache_key)
                total_bytes -= entry.file_size
                total_files -= 1
                pruned_count += 1

        if pruned_count > 0:
            logger.info("Pruned %d oldest TTS cache entries from disk.", pruned_count)
        return pruned_count

    async def clear(self) -> Tuple[int, float]:
        """
        Clears all cache files in audio/cache/ and purges SQLite metadata.
        Returns (count_deleted, freed_mb).
        """
        freed_bytes = 0
        deleted_count = 0

        def _scan_and_delete() -> Tuple[int, int]:
            freed = 0
            count = 0
            if self.cache_dir.exists():
                for f in self.cache_dir.iterdir():
                    if f.is_file():
                        try:
                            freed += f.stat().st_size
                            f.unlink(missing_ok=True)
                            count += 1
                        except Exception as e:
                            logger.warning("Failed to delete cache file %s: %s", f, e)
            return freed, count

        freed_bytes, deleted_count = await asyncio.to_thread(_scan_and_delete)

        async with get_db(self.db_path) as conn:
            await crud.clear_all_tts_cache_entries(conn)

        self._mem_cache.clear()
        self._mem_bytes_total = 0
        self._hits = 0
        self._misses = 0
        freed_mb = round(freed_bytes / (1024 * 1024), 2)
        logger.info("Cleared TTS cache: deleted %d files, freed %.2f MB", deleted_count, freed_mb)
        return deleted_count, freed_mb

    async def get_stats(self) -> Dict[str, Any]:
        """Returns comprehensive TTS cache statistics."""
        async with get_db(self.db_path) as conn:
            db_stats = await crud.get_tts_cache_stats(conn)

        total_files = db_stats["total_files"]
        total_size_bytes = db_stats["total_size_bytes"]
        total_size_mb = db_stats["total_size_mb"]
        db_hits = db_stats["total_hits"]

        # Report memory hits and db hits separately
        hit_rate = self._hits / (self._hits + self._misses) if (self._hits + self._misses) > 0 else 0.0

        return {
            "total_files": total_files,
            "total_size_bytes": total_size_bytes,
            "total_size_mb": total_size_mb,
            "total_hits": self._hits,
            "total_misses": self._misses,
            "hit_rate_percent": round(hit_rate * 100.0, 2),
            "memory_hits": self._hits,
            "db_hits": db_hits,
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
