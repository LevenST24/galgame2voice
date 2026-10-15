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
        assert (count, keys) == (0, [])
        assert path.read_bytes() == b"preserve until safely removed"
        assert await (await conn.execute("SELECT cache_key FROM tts_cache_entries")).fetchall()
        if failure == "permission":
            monkeypatch.setattr(Path, "unlink", original_unlink)
            assert await audio_cleaner._clean_lru_cache_from_db(conn, cache, time.time() - 86400) == (1, ["expired"])
            assert not path.exists()
            assert await (await conn.execute("SELECT cache_key FROM tts_cache_entries")).fetchall() == []


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
