"""A transient metadata failure must never turn reference audio into garbage."""

from contextlib import asynccontextmanager
import os
import sqlite3
import time
import threading

import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database.session import get_db, init_db
from galgame2voice.services import audio_cleaner


@pytest.mark.parametrize("failure", ["open", "reference", "cache"])
async def test_cleanup_preserves_audio_until_metadata_recovers(tmp_path, monkeypatch, failure):
    db = tmp_path / "cleanup.db"
    await init_db(db)
    audio = tmp_path / "audio"
    cache = audio / "cache"
    cache.mkdir(parents=True)
    reference = audio / "custom-reference.wav"
    ephemeral = audio / "chunk_expired.wav"
    hot = cache / "hot.wav"
    orphan = cache / "orphan.wav"
    for path in (reference, ephemeral, hot, orphan):
        path.write_bytes(path.name.encode())
        old = time.time() - 10 * 86400
        os.utime(path, (old, old))
    async with get_db(db) as conn:
        await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="Reference owner", gpt_weights_path="model.ckpt", sovits_weights_path="model.pth",
            ref_audio_path=str(reference),
        ))
        await conn.execute(
            "INSERT INTO tts_cache_entries (cache_key, text, clean_text, params_hash, file_path, file_size) "
            "VALUES ('hot', 'hello', 'hello', 'params', ?, ?)", (str(hot), hot.stat().st_size),
        )
        await conn.commit()

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        async def execute(self, sql, *args):
            if (failure == "reference" and "SELECT ref_audio_path" in sql
                    or failure == "cache" and "SELECT cache_key" in sql):
                raise sqlite3.OperationalError("database temporarily unavailable")
            return await self.connection.execute(sql, *args)

    @asynccontextmanager
    async def unavailable_db():
        if failure == "open":
            raise sqlite3.OperationalError("database temporarily unavailable")
        async with get_db(db) as conn:
            yield FailingConnection(conn)

    monkeypatch.setattr(audio_cleaner, "_resolve_get_db", lambda: unavailable_db)
    await audio_cleaner._run_audio_cleanup_cycle(audio)
    assert all(path.exists() for path in (reference, ephemeral, hot, orphan))

    monkeypatch.setattr(audio_cleaner, "_resolve_get_db", lambda: lambda: get_db(db))
    await audio_cleaner._run_audio_cleanup_cycle(audio)
    assert reference.read_bytes() == b"custom-reference.wav"
    assert hot.read_bytes() == b"hot.wav"
    assert not ephemeral.exists()
    assert not orphan.exists()


@pytest.mark.parametrize("failure", ["permission", "outside"])
async def test_cache_eviction_retains_files_and_metadata_when_unlink_is_unsafe(tmp_path, monkeypatch, failure):
    from pathlib import Path
    db = tmp_path / "eviction.db"
    await init_db(db)
    cache = tmp_path / "cache"
    cache.mkdir()
    path = (cache if failure == "permission" else tmp_path) / "expired.wav"
    path.write_bytes(b"preserve until safely removed")
    async with get_db(db) as conn:
        await conn.execute(
            "INSERT INTO tts_cache_entries (cache_key, text, clean_text, params_hash, file_path, file_size, last_accessed_at) "
            "VALUES ('expired', 'text', 'text', 'hash', ?, 29, '2000-01-01 00:00:00')", (str(path),),
        )
        await conn.commit()
        original_unlink = Path.unlink
        event_loop_thread = threading.get_ident()

        def denied_unlink(file, *args, **kwargs):
            if file == path:
                assert threading.get_ident() != event_loop_thread
                raise PermissionError("audio file is in use")
            return original_unlink(file, *args, **kwargs)

        if failure == "permission":
            monkeypatch.setattr(Path, "unlink", denied_unlink)
        count, keys = await audio_cleaner._clean_lru_cache_from_db(conn, cache, time.time() - 86400)
        assert path.read_bytes() == b"preserve until safely removed"
        remaining = await (await conn.execute("SELECT cache_key FROM tts_cache_entries")).fetchall()
        if failure == "permission":
            assert (count, keys) == (0, [])
            assert remaining
            monkeypatch.setattr(Path, "unlink", original_unlink)
            assert await audio_cleaner._clean_lru_cache_from_db(conn, cache, time.time() - 86400) == (1, ["expired"])
            assert not path.exists()
            assert await (await conn.execute("SELECT cache_key FROM tts_cache_entries")).fetchall() == []
        else:
            # The file outside the cache stays. The expired index does not: playback
            # uses cache/{key}.wav, which is already absent.
            assert (count, keys) == (1, ["expired"])
            assert remaining == []


@pytest.mark.parametrize("indexed", [True, False])
async def test_audio_used_as_a_reference_is_protected_inside_the_cache(tmp_path, monkeypatch, indexed):
    db = tmp_path / "cache-reference.db"
    await init_db(db)
    audio = tmp_path / "audio"
    cache = audio / "cache"
    cache.mkdir(parents=True)
    reference = cache / "selected-sample.wav"
    reference.write_bytes(b"voice sample")
    old = time.time() - 10 * 86400
    os.utime(reference, (old, old))
    async with get_db(db) as conn:
        await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="Cached reference", gpt_weights_path="voice.ckpt", sovits_weights_path="voice.pth",
            ref_audio_path=str(reference),
        ))
        if indexed:
            await conn.execute(
                "INSERT INTO tts_cache_entries (cache_key, text, clean_text, params_hash, file_path, file_size, last_accessed_at) "
                "VALUES ('selected-sample', 'text', 'text', 'hash', ?, 12, '2000-01-01 00:00:00')", (str(reference),),
            )
            await conn.commit()
    monkeypatch.setattr(audio_cleaner, "_resolve_get_db", lambda: lambda: get_db(db))
    await audio_cleaner._run_audio_cleanup_cycle(audio)
    assert reference.read_bytes() == b"voice sample"
    if indexed:
        async with get_db(db) as conn:
            assert await (await conn.execute("SELECT cache_key FROM tts_cache_entries")).fetchall()


async def test_moved_cache_directory_expires_live_files_and_keeps_outside_audio(tmp_path, monkeypatch):
    """A moved project keeps absolute paths from the old disk. Seven-day cleanup must remove the live cache file and its index, and must leave files outside the new cache directory alone."""
    from pathlib import Path

    old_cache = tmp_path / "old_location" / "audio" / "cache"
    new_audio = tmp_path / "new_location" / "audio"
    new_cache = new_audio / "cache"
    old_cache.mkdir(parents=True)
    new_cache.mkdir(parents=True)
    db = tmp_path / "new_location" / "galgame2voice.db"
    await init_db(db)

    moved_key = "abc12345deadbeef"
    current_key = "currentexpiredkey01"
    fresh_key = "freshcachekey000001"
    protected_key = "protected-sample"
    locked_key = "lockedaftermove001"
    payload = {
        moved_key: b"live-copy",
        current_key: b"current-expired",
        fresh_key: b"still-fresh",
        protected_key: b"reference-audio",
        locked_key: b"busy-audio",
    }
    old_payload = {
        moved_key: b"old-copy",
        protected_key: b"old-reference",
        locked_key: b"old-busy",
    }
    new_files = {key: new_cache / f"{key}.wav" for key in payload}
    old_files = {key: old_cache / f"{key}.wav" for key in old_payload}
    for key, data in payload.items():
        new_files[key].write_bytes(data)
    for key, data in old_payload.items():
        old_files[key].write_bytes(data)
    expired_mtime = time.time() - (8 * 86400)
    for path in (*new_files.values(), *old_files.values()):
        if path != new_files[fresh_key]:
            os.utime(path, (expired_mtime, expired_mtime))

    async with get_db(db) as conn:
        await crud.create_voice_profile(conn, VoiceProfileCreate(
            name="Kept reference", gpt_weights_path="model.ckpt", sovits_weights_path="model.pth",
            ref_audio_path=str(new_files[protected_key]),
        ))
        rows = (
            (moved_key, "hello", str(old_files[moved_key])),
            (current_key, "there", str(new_files[current_key])),
            (fresh_key, "now", str(new_files[fresh_key])),
            (protected_key, "ref", str(old_files[protected_key])),
            (locked_key, "busy", str(old_files[locked_key])),
        )
        for key, text, file_path in rows:
            await crud.upsert_tts_cache_entry(
                conn, key, text, text, 1, f"hash-{key}", file_path, 16, 500,
            )
        await conn.execute(
            "UPDATE tts_cache_entries SET last_accessed_at = datetime('now', '-8 days') WHERE cache_key != ?",
            (fresh_key,),
        )
        await conn.commit()

    monkeypatch.setattr(audio_cleaner, "_resolve_get_db", lambda: lambda: get_db(db))
    original_unlink = Path.unlink
    locked_file = new_files[locked_key].resolve()

    def denied_unlink(file, *args, **kwargs):
        if Path(file).resolve() == locked_file:
            raise PermissionError("audio file is in use")
        return original_unlink(file, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied_unlink)
    await audio_cleaner._run_audio_cleanup_cycle(new_audio)

    async with get_db(db) as conn:
        remaining = {
            row[0] for row in await (await conn.execute("SELECT cache_key FROM tts_cache_entries")).fetchall()
        }
    assert moved_key not in remaining
    assert not new_files[moved_key].exists()
    assert old_files[moved_key].read_bytes() == old_payload[moved_key]
    assert current_key not in remaining
    assert not new_files[current_key].exists()
    assert fresh_key in remaining
    assert new_files[fresh_key].read_bytes() == payload[fresh_key]
    assert protected_key in remaining
    assert new_files[protected_key].read_bytes() == payload[protected_key]
    assert old_files[protected_key].read_bytes() == old_payload[protected_key]
    assert locked_key in remaining
    assert new_files[locked_key].read_bytes() == payload[locked_key]
    assert old_files[locked_key].read_bytes() == old_payload[locked_key]


async def test_prune_removes_live_cache_files_when_index_points_outside(tmp_path):
    """Capacity prune must not treat a missing or external absolute path as the file it deleted."""
    from galgame2voice.services.tts_cache_manager import TtsCacheManager

    old_cache = tmp_path / "old_location" / "audio" / "cache"
    new_cache = tmp_path / "new_location" / "audio" / "cache"
    old_cache.mkdir(parents=True)
    new_cache.mkdir(parents=True)
    db = tmp_path / "new_location" / "galgame2voice.db"
    await init_db(db)

    outside_key = "outsidefilekey0001"
    missing_old_key = "missingoldpath0001"
    outside_file = old_cache / f"{outside_key}.wav"
    missing_old_file = old_cache / f"{missing_old_key}.wav"
    live_outside = new_cache / f"{outside_key}.wav"
    live_missing_old = new_cache / f"{missing_old_key}.wav"
    outside_file.write_bytes(b"do-not-delete")
    live_outside.write_bytes(b"live-outside")
    live_missing_old.write_bytes(b"live-without-old")

    async with get_db(db) as conn:
        await crud.upsert_tts_cache_entry(
            conn, outside_key, "old", "old", 1, "hash-outside", str(outside_file), 64, 500,
        )
        await crud.upsert_tts_cache_entry(
            conn, missing_old_key, "gone", "gone", 1, "hash-missing", str(missing_old_file), 64, 500,
        )
        await conn.execute(
            "UPDATE tts_cache_entries SET last_accessed_at = datetime('now', '-8 days')"
        )
        await conn.commit()

    manager = TtsCacheManager(cache_dir=new_cache, db_path=db, max_cache_mb=1024, max_entries=5000)
    try:
        pruned = await manager.prune(max_entries=1, max_mb=1024)
    finally:
        await manager.aclose()

    assert pruned == 2
    assert outside_file.read_bytes() == b"do-not-delete"
    assert not live_outside.exists()
    assert not live_missing_old.exists()
    assert not missing_old_file.exists()
    async with get_db(db) as conn:
        remaining = await (await conn.execute("SELECT cache_key FROM tts_cache_entries")).fetchall()
    assert remaining == []
