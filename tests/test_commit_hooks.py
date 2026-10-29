"""Notify only after a durable commit, including cancellation and savepoints."""

import asyncio
import sqlite3

import pytest

from galgame2voice.database.commit_hooks import after_commit
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.database import crud
from galgame2voice.database.session import get_db, immediate_transaction, init_db


async def insert(conn, name="Test"):
    return await crud.create_voice_profile(conn, VoiceProfileCreate(name=name, gpt_weights_path="", sovits_weights_path=""))


@pytest.mark.asyncio
async def test_savepoint_rollback_discards_only_its_notifications(tmp_path):
    path = tmp_path / "notifications.db"
    await init_db(path)
    notifications = []
    async with get_db(path) as conn:
        async with immediate_transaction(conn):
            after_commit(conn, lambda: notifications.append("outer"))
            with pytest.raises(RuntimeError):
                async with immediate_transaction(conn):
                    await insert(conn, "Rolled back")
                    after_commit(conn, lambda: notifications.append("inner"))
                    raise RuntimeError("rollback inner")
            async with immediate_transaction(conn):
                await insert(conn, "Committed")
                after_commit(conn, lambda: notifications.append("once"), key="same")
                after_commit(conn, lambda: notifications.append("duplicate"), key="same")
            assert notifications == []
        assert notifications == ["outer", "once"]
        assert [profile.name for profile in await crud.list_voice_profiles(conn)] == ["Committed"]


@pytest.mark.asyncio
async def test_outer_rollback_does_not_leak_callbacks_into_next_transaction(tmp_path):
    path = tmp_path / "notifications.db"
    await init_db(path)
    notifications = []
    async with get_db(path) as conn:
        with pytest.raises(RuntimeError):
            async with immediate_transaction(conn):
                await insert(conn)
                after_commit(conn, lambda: notifications.append("rolled back"))
                raise RuntimeError("outer rollback")
        async with immediate_transaction(conn):
            await insert(conn, "Next")
            after_commit(conn, lambda: notifications.append("next"))
    assert notifications == ["next"]


@pytest.mark.asyncio
@pytest.mark.parametrize("after_write", [False, True])
async def test_cancelled_commit_settles_then_notifies_once(tmp_path, monkeypatch, after_write):
    path = tmp_path / "notifications.db"
    await init_db(path)
    entered, release = asyncio.Event(), asyncio.Event()
    notifications = []
    async with get_db(path) as conn:
        commit = conn.commit

        async def controlled_commit():
            if after_write:
                await commit()
            entered.set()
            await release.wait()
            if not after_write:
                await commit()

        monkeypatch.setattr(conn, "commit", controlled_commit)

        async def write():
            async with immediate_transaction(conn):
                await insert(conn)
                after_commit(conn, lambda: notifications.append("committed"))

        task = asyncio.create_task(write())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert notifications == ["committed"]
    async with get_db(path) as reader:
        assert len(await crud.list_voice_profiles(reader)) == 1


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_without_notification(tmp_path, monkeypatch):
    path = tmp_path / "notifications.db"
    await init_db(path)
    notifications = []
    async with get_db(path) as conn:
        async def failed_commit():
            raise sqlite3.OperationalError("synthetic commit failure")

        monkeypatch.setattr(conn, "commit", failed_commit)
        with pytest.raises(sqlite3.OperationalError, match="synthetic"):
            async with immediate_transaction(conn):
                await insert(conn)
                after_commit(conn, lambda: notifications.append("wrong"))
        assert notifications == []
        assert await crud.list_voice_profiles(conn) == []


@pytest.mark.asyncio
async def test_notification_failure_cannot_undo_commit_or_skip_other_callbacks(tmp_path):
    path = tmp_path / "notifications.db"
    await init_db(path)
    notifications = []

    def broken():
        raise RuntimeError("synthetic notification failure")

    async with get_db(path) as conn:
        async with immediate_transaction(conn):
            await insert(conn)
            after_commit(conn, broken)
            after_commit(conn, lambda: notifications.append("next"))
        assert len(await crud.list_voice_profiles(conn)) == 1
    assert notifications == ["next"]


@pytest.mark.asyncio
async def test_notifications_require_the_transaction_owner(tmp_path):
    path = tmp_path / "notifications.db"
    await init_db(path)
    async with get_db(path) as conn:
        with pytest.raises(RuntimeError, match="current task"):
            after_commit(conn, lambda: None)
        async with immediate_transaction(conn):
            async def other_task():
                after_commit(conn, lambda: None)
            with pytest.raises(RuntimeError, match="current task"):
                await asyncio.create_task(other_task())
