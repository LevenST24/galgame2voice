"""Committed voice snapshots must not mix databases, aliases, or transactions."""

import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from galgame2voice.database import crud
from galgame2voice.database.models import SettingsUpdate, VoiceProfileCreate, VoiceProfileUpdate
from galgame2voice.database.session import get_db, immediate_transaction, init_db
from galgame2voice.services import voice_resolver
from galgame2voice.services.voice_resolver import VoiceContextUnavailableError, VoiceProfileResolver, get_voice_resolver


async def seed(path, name):
    await init_db(path)
    async with get_db(path) as conn:
        return await crud.create_voice_profile(conn, VoiceProfileCreate(
            name=name, gpt_weights_path="", sovits_weights_path="", prompt_text=name,
        ))


@pytest.mark.asyncio
async def test_equal_profile_ids_in_different_databases_never_share_context(tmp_path):
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    a, b = await seed(first, "Alpha"), await seed(second, "Bravo")
    assert a.id == b.id
    resolver = get_voice_resolver()
    assert (await resolver.resolve_context(str(first), profile_id=a.id)).name == "Alpha"
    assert (await resolver.resolve_context(str(second), profile_id=b.id)).name == "Bravo"


@pytest.mark.asyncio
async def test_relative_and_absolute_paths_share_one_database_scope(tmp_path, monkeypatch):
    path = tmp_path / "voice.db"
    row = await seed(path, "Alpha")
    monkeypatch.chdir(tmp_path)
    resolver = get_voice_resolver()
    fetch = AsyncMock(wraps=resolver._fetch_raw_profile)
    monkeypatch.setattr(resolver, "_fetch_raw_profile", fetch)
    first = await resolver.resolve_context("voice.db", profile_id=row.id)
    assert await resolver.resolve_context(str(path), profile_id=row.id) is first
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_profile_update_invalidates_every_cached_selector(tmp_path):
    path = tmp_path / "voice.db"
    row = await seed(path, "Alpha")
    resolver = get_voice_resolver()
    await resolver.resolve_context(str(path), character_name="Alpha")
    await resolver.resolve_context(str(path), profile_id=row.id)
    async with get_db(path) as conn:
        await crud.update_voice_profile(conn, row.id, VoiceProfileUpdate(name="Beta", prompt_text="New"))
    updated = await resolver.resolve_context(str(path), profile_id=row.id)
    assert (updated.name, updated.prompt_text) == ("Beta", "New")
    # A stale explicit name must not silently borrow the newly active voice.
    assert await resolver.resolve_context(str(path), character_name="Alpha") is None


@pytest.mark.asyncio
async def test_invalidation_happens_after_outer_commit(tmp_path, monkeypatch):
    path = tmp_path / "voice.db"
    row = await seed(path, "Alpha")
    resolver = get_voice_resolver()
    original = await resolver.resolve_context(str(path), profile_id=row.id)
    notifications = []
    invalidate = resolver.invalidate

    def record(*args, **kwargs):
        notifications.append((args, kwargs))
        invalidate(*args, **kwargs)

    monkeypatch.setattr(resolver, "invalidate", record)
    async with get_db(path) as conn:
        async with immediate_transaction(conn):
            await crud.update_voice_profile(conn, row.id, VoiceProfileUpdate(name="Beta"))
            await crud.update_voice_profile(conn, row.id, VoiceProfileUpdate(prompt_text="Committed"))
            assert notifications == []
            assert await resolver.resolve_context(str(path), profile_id=row.id) is original
    assert len(notifications) == 1
    assert (await resolver.resolve_context(str(path), profile_id=row.id)).prompt_text == "Committed"


@pytest.mark.asyncio
async def test_outer_rollback_keeps_committed_context(tmp_path, monkeypatch):
    path = tmp_path / "voice.db"
    row = await seed(path, "Alpha")
    resolver = get_voice_resolver()
    original = await resolver.resolve_context(str(path), profile_id=row.id)
    notifications = []
    monkeypatch.setattr(resolver, "invalidate", lambda *a, **kw: notifications.append(True))
    async with get_db(path) as conn:
        with pytest.raises(RuntimeError, match="rollback"):
            async with immediate_transaction(conn):
                await crud.update_voice_profile(conn, row.id, VoiceProfileUpdate(name="Beta"))
                raise RuntimeError("rollback")
    assert notifications == []
    assert await resolver.resolve_context(str(path), profile_id=row.id) is original


@pytest.mark.asyncio
async def test_active_settings_changes_refresh_active_selector(tmp_path):
    path = tmp_path / "voice.db"
    a = await seed(path, "Alpha")
    async with get_db(path) as conn:
        b = await crud.create_voice_profile(conn, VoiceProfileCreate(name="Bravo", gpt_weights_path="", sovits_weights_path=""))
        await crud.set_active_voice_profile(conn, a.id)
    resolver = get_voice_resolver()
    assert (await resolver.resolve_context(str(path))).name == "Alpha"
    async with get_db(path) as conn:
        await crud.update_settings(conn, SettingsUpdate(active_voice_profile_id=b.id))
    assert (await resolver.resolve_context(str(path))).name == "Bravo"


@pytest.mark.asyncio
async def test_deleting_profile_cannot_return_cached_deleted_voice(tmp_path):
    path = tmp_path / "voice.db"
    row = await seed(path, "Alpha")
    resolver = get_voice_resolver()
    await resolver.resolve_context(str(path), profile_id=row.id)
    async with get_db(path) as conn:
        await crud.delete_voice_profile(conn, row.id)
    assert await resolver.resolve_context(str(path), profile_id=row.id) is None


@pytest.mark.asyncio
async def test_cold_read_cannot_republish_context_after_invalidation(tmp_path, monkeypatch):
    path = tmp_path / "voice.db"
    row = await seed(path, "Alpha")
    resolver = get_voice_resolver()
    fetch = resolver._fetch_raw_profile
    loaded, release = asyncio.Event(), asyncio.Event()

    async def pause(*args):
        value = await fetch(*args)
        loaded.set()
        await release.wait()
        return value

    monkeypatch.setattr(resolver, "_fetch_raw_profile", pause)
    task = asyncio.create_task(resolver.resolve_context(str(path), profile_id=row.id))
    second = None
    try:
        await asyncio.wait_for(loaded.wait(), 5)
        second = asyncio.create_task(resolver.resolve_context(str(path), profile_id=row.id))
        async with get_db(path) as conn:
            await crud.update_voice_profile(conn, row.id, VoiceProfileUpdate(name="Beta"))
    finally:
        release.set()
    first = await task
    assert first.name == "Beta"
    assert await second is first
    monkeypatch.setattr(resolver, "_fetch_raw_profile", fetch)
    assert (await resolver.resolve_context(str(path), profile_id=row.id)).name == "Beta"


@pytest.mark.asyncio
async def test_deleted_profile_is_not_returned_by_a_pending_read(tmp_path, monkeypatch):
    path = tmp_path / "voice.db"
    row = await seed(path, "Alpha")
    resolver = get_voice_resolver()
    fetch = resolver._fetch_raw_profile
    loaded, release = asyncio.Event(), asyncio.Event()

    async def pause(*args):
        value = await fetch(*args)
        loaded.set()
        await release.wait()
        return value

    monkeypatch.setattr(resolver, "_fetch_raw_profile", pause)
    task = asyncio.create_task(resolver.resolve_context(str(path), profile_id=row.id))
    try:
        await asyncio.wait_for(loaded.wait(), 5)
        async with get_db(path) as conn:
            await crud.delete_voice_profile(conn, row.id)
    finally:
        release.set()
    assert await task is None


@pytest.mark.asyncio
async def test_update_during_reference_preparation_reloads_profile(tmp_path, monkeypatch):
    path = tmp_path / "voice.db"
    row = await seed(path, "Alpha")
    resolver = get_voice_resolver()
    build = resolver._build_context
    started, release = threading.Event(), threading.Event()
    calls = 0

    def slow_build(raw, *args):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            if not release.wait(5):
                raise TimeoutError("test did not release reference preparation")
        return build(raw, *args)

    monkeypatch.setattr(resolver, "_build_context", slow_build)
    task = asyncio.create_task(resolver.resolve_context(str(path), profile_id=row.id))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        async with get_db(path) as conn:
            await crud.update_voice_profile(conn, row.id, VoiceProfileUpdate(prompt_text="Updated reference"))
    finally:
        release.set()
    assert (await task).prompt_text == "Updated reference"
    assert calls == 2


@pytest.mark.asyncio
async def test_unrelated_database_invalidation_does_not_restart_pending_read(tmp_path, monkeypatch):
    path = tmp_path / "voice.db"
    row = await seed(path, "Alpha")
    resolver = get_voice_resolver()
    fetch = resolver._fetch_raw_profile
    calls = 0

    async def read(*args):
        nonlocal calls
        calls += 1
        value = await fetch(*args)
        resolver.invalidate(db_path=str(tmp_path / "unrelated.db"))
        return value

    monkeypatch.setattr(resolver, "_fetch_raw_profile", read)
    assert (await resolver.resolve_context(str(path), profile_id=row.id)).name == "Alpha"
    assert calls == 1


@pytest.mark.asyncio
async def test_repeated_commits_never_return_a_stale_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "voice.db"
    row = await seed(path, "Alpha")
    resolver = get_voice_resolver()
    fetch = resolver._fetch_raw_profile
    calls = 0

    async def changing(*args):
        nonlocal calls
        calls += 1
        value = await fetch(*args)
        resolver.invalidate(db_path=str(path))
        return value

    monkeypatch.setattr(resolver, "_fetch_raw_profile", changing)
    with pytest.raises(voice_resolver.VoiceContextChangedError, match="音色配置正在更新"):
        await resolver.resolve_context(str(path), profile_id=row.id)
    assert calls == 3
    monkeypatch.setattr(resolver, "_fetch_raw_profile", fetch)
    assert (await resolver.resolve_context(str(path), profile_id=row.id)).name == "Alpha"


@pytest.mark.asyncio
async def test_concurrent_cold_reads_share_one_load(tmp_path, monkeypatch):
    path = tmp_path / "voice.db"
    row = await seed(path, "Alpha")
    resolver = get_voice_resolver()
    fetch = AsyncMock(wraps=resolver._fetch_raw_profile)
    monkeypatch.setattr(resolver, "_fetch_raw_profile", fetch)
    contexts = await asyncio.gather(*(resolver.resolve_context(str(path), profile_id=row.id) for _ in range(20)))
    assert fetch.await_count == 1
    assert all(ctx is contexts[0] for ctx in contexts)


@pytest.mark.asyncio
async def test_read_errors_are_retryable_and_not_cached_as_absence(tmp_path, monkeypatch):
    path = tmp_path / "voice.db"
    row = await seed(path, "Alpha")
    resolver = get_voice_resolver()
    fetch = resolver._fetch_raw_profile
    broken = AsyncMock(side_effect=OSError("temporary read failure"))
    monkeypatch.setattr(resolver, "_fetch_raw_profile", broken)
    with pytest.raises(VoiceContextUnavailableError, match="音色.*暂时无法读取"):
        await resolver.resolve_context(str(path), profile_id=row.id)
    monkeypatch.setattr(resolver, "_fetch_raw_profile", fetch)
    assert (await resolver.resolve_context(str(path), profile_id=row.id)).name == "Alpha"


@pytest.mark.asyncio
async def test_empty_database_does_not_borrow_global_voice_manager(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    await init_db(path)
    foreign = SimpleNamespace(id=99, name="Foreign voice", ref_audio_path="", prompt_text="")
    monkeypatch.setattr("galgame2voice.services.voice_manager.get_voice_manager", lambda: SimpleNamespace(active_profile=foreign))
    assert await get_voice_resolver().resolve_context(str(path)) is None


@pytest.mark.asyncio
async def test_context_filesystem_work_runs_off_the_event_loop(tmp_path, monkeypatch):
    path = tmp_path / "voice.db"
    row = await seed(path, "Alpha")
    resolver = get_voice_resolver()
    build = resolver._build_context
    threads = []

    def capture(raw, *args):
        threads.append(threading.get_ident())
        return build(raw, *args)

    monkeypatch.setattr(resolver, "_build_context", capture)
    loop_thread = threading.get_ident()
    await resolver.resolve_context(str(path), profile_id=row.id)
    assert threads and all(thread != loop_thread for thread in threads)


@pytest.mark.asyncio
async def test_confirmed_absence_is_cached_but_new_profile_becomes_visible(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    await init_db(path)
    resolver = get_voice_resolver()
    fetch = AsyncMock(wraps=resolver._fetch_raw_profile)
    monkeypatch.setattr(resolver, "_fetch_raw_profile", fetch)
    assert await resolver.resolve_context(str(path)) is None
    assert await resolver.resolve_context(str(path)) is None
    assert fetch.await_count == 1
    async with get_db(path) as conn:
        await crud.create_voice_profile(conn, VoiceProfileCreate(name="New", gpt_weights_path="", sovits_weights_path=""))
    assert (await resolver.resolve_context(str(path))).name == "New"


@pytest.mark.asyncio
async def test_tts_uses_explicit_cache_database(tmp_path):
    from galgame2voice.services.tts_service import TtsService

    path = tmp_path / "cache.db"
    cache = SimpleNamespace(db_path=str(path))
    service = TtsService(client=SimpleNamespace(), cache_manager=cache, audio_dir=tmp_path / "audio")
    assert Path(service.db_path) == path


@pytest.mark.asyncio
async def test_ambiguous_explicit_voice_never_reaches_synthesis(tmp_path, monkeypatch):
    from test_character_identity import install, catalog
    from galgame2voice.config import get_settings
    from galgame2voice.services import character_manager
    from galgame2voice.services.character_identity import AmbiguousProfileName
    from galgame2voice.services.tts_service import TtsService

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    for package_id in ("alpha", "bravo"):
        install(tmp_path, package_id, package_id.title(), ["shared"])
    manager = catalog(tmp_path)
    monkeypatch.setattr(character_manager, "get_character_manager", lambda: manager)
    path = tmp_path / "voices.db"
    await seed(path, "Alpha")
    await seed(path, "Bravo")
    client = SimpleNamespace(synthesize=AsyncMock(return_value=b"wrong voice"))
    service = TtsService(client=client, cache_manager=SimpleNamespace(db_path=str(path)),
                         audio_dir=tmp_path / "audio", db_path=str(path))
    with pytest.raises(AmbiguousProfileName):
        await service.synthesize_to_file("hello", options={"character_name": "shared"}, use_cache=False)
    client.synthesize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{}, {"voice_profile_id": 1}])
async def test_unstable_voice_snapshot_never_falls_back_to_engine_voice(tmp_path, monkeypatch, options):
    from galgame2voice.services.tts_service import TtsService

    resolver = get_voice_resolver()
    monkeypatch.setattr(resolver, "resolve_context", AsyncMock(
        side_effect=voice_resolver.VoiceContextChangedError("音色配置正在更新，请稍后重试。")
    ))
    client = SimpleNamespace(synthesize=AsyncMock(return_value=b"wrong voice"))
    service = TtsService(client=client, cache_manager=SimpleNamespace(db_path=str(tmp_path / "voices.db")),
                         audio_dir=tmp_path / "audio", db_path=str(tmp_path / "voices.db"))
    with pytest.raises(voice_resolver.VoiceContextChangedError, match="音色配置正在更新"):
        await service.synthesize_to_file("hello", options=options, use_cache=False)
    client.synthesize.assert_not_awaited()


@pytest.mark.asyncio
async def test_updating_one_database_keeps_other_database_cache_warm(tmp_path):
    a_path, b_path = tmp_path / "a.db", tmp_path / "b.db"
    a = await seed(a_path, "Alpha")
    b = await seed(b_path, "Bravo")
    resolver = get_voice_resolver()
    await resolver.resolve_context(str(a_path), profile_id=a.id)
    b_context = await resolver.resolve_context(str(b_path), profile_id=b.id)
    async with get_db(a_path) as conn:
        await crud.update_voice_profile(conn, a.id, VoiceProfileUpdate(name="Updated"))
    assert await resolver.resolve_context(str(b_path), profile_id=b.id) is b_context


@pytest.mark.asyncio
async def test_missing_id_with_different_name_fallbacks_does_not_share_context(tmp_path):
    path = tmp_path / "voices.db"
    await seed(path, "Alpha")
    await seed(path, "Bravo")
    resolver = get_voice_resolver()
    assert (await resolver.resolve_context(str(path), profile_id=999, character_name="Alpha")).name == "Alpha"
    assert (await resolver.resolve_context(str(path), profile_id=999, character_name="Bravo")).name == "Bravo"


@pytest.mark.asyncio
async def test_cancelled_cold_reader_does_not_cancel_another_reader(tmp_path, monkeypatch):
    path = tmp_path / "voices.db"
    row = await seed(path, "Alpha")
    resolver = get_voice_resolver()
    fetch = resolver._fetch_raw_profile
    entered = asyncio.Event()
    attempts = 0

    async def slow_first(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await asyncio.Event().wait()
        return await fetch(*args)

    monkeypatch.setattr(resolver, "_fetch_raw_profile", slow_first)
    first = asyncio.create_task(resolver.resolve_context(str(path), profile_id=row.id))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(resolver.resolve_context(str(path), profile_id=row.id))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (await asyncio.wait_for(second, 5)).name == "Alpha"
        assert attempts == 2
    finally:
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_lru_capacity_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / "voices.db"
    profiles = [await seed(path, name) for name in ("Alpha", "Bravo", "Charlie")]
    resolver = VoiceProfileResolver(max_entries=2)
    fetch = AsyncMock(wraps=resolver._fetch_raw_profile)
    monkeypatch.setattr(resolver, "_fetch_raw_profile", fetch)
    for row in profiles:
        await resolver.resolve_context(str(path), profile_id=row.id)
    assert len(resolver._cache) == 2
    await resolver.resolve_context(str(path), profile_id=profiles[0].id)
    assert fetch.await_count == 4


@pytest.mark.asyncio
async def test_expired_context_is_reloaded(tmp_path, monkeypatch):
    path = tmp_path / "voices.db"
    row = await seed(path, "Alpha")
    resolver = VoiceProfileResolver(ttl_seconds=0)
    fetch = AsyncMock(wraps=resolver._fetch_raw_profile)
    monkeypatch.setattr(resolver, "_fetch_raw_profile", fetch)
    first = await resolver.resolve_context(str(path), profile_id=row.id)
    assert await resolver.resolve_context(str(path), profile_id=row.id) is not first
    assert fetch.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_package", [False, True])
async def test_catalog_rescan_refreshes_context_even_without_profile_changes(tmp_path, monkeypatch, remove_package):
    import json
    from test_character_identity import install, catalog
    from galgame2voice.config import get_settings

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    folder = install(tmp_path, "ann", "Ann")
    manager = catalog(tmp_path)
    monkeypatch.setattr("galgame2voice.services.character_manager.get_character_manager", lambda: manager)
    path = tmp_path / "voices.db"
    row = await seed(path, "Ann")
    async with get_db(path) as conn:
        await manager.sync_with_db(conn)
    resolver = get_voice_resolver()
    original = await resolver.resolve_context(str(path), profile_id=row.id)
    assert original.emotions["gentle"]["prompt_text"] == "Ann"
    manifest = folder / "manifest.json"
    if remove_package:
        manifest.unlink()
    else:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["emotions"]["gentle"]["text"] = "New reference transcript"
        manifest.write_text(json.dumps(data), encoding="utf-8")
    manager.discover_characters()
    async with get_db(path) as conn:
        assert await manager.sync_with_db(conn) == 0
    refreshed = await resolver.resolve_context(str(path), profile_id=row.id)
    assert refreshed is not original
    if remove_package:
        assert refreshed.emotions == {}
    else:
        assert refreshed.emotions["gentle"]["prompt_text"] == "New reference transcript"
