"""Character lookup and reconciliation must never borrow another identity."""

import asyncio
import json
import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path
import wave

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.models import VoiceProfileCreate
from galgame2voice.main import create_app
from galgame2voice.routers import characters
from galgame2voice.services.character_manager import CharacterManager


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    return tmp_path


def install(root, package_id, name, aliases=(), folder=None, seconds=4):
    package = root / "characters" / (folder or package_id)
    package.mkdir(parents=True)
    with wave.open(str(package / "gentle.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x01\x00" * 8000 * seconds)
    (package / "gpt.ckpt").write_bytes(b"x" * 5000)
    (package / "sovits.pth").write_bytes(b"x" * 5000)
    (package / "manifest.json").write_text(json.dumps({
        "id": package_id, "name": name, "aliases": list(aliases),
        "gpt_weights": "gpt.ckpt", "sovits_weights": "sovits.pth",
        "system_prompt": f"Persona for {name}",
        "emotions": {"gentle": {"audio": "gentle.wav", "text": name, "lang": "ja"}},
    }), encoding="utf-8")
    return package


def catalog(root):
    manager = CharacterManager(root / "characters")
    manager.discover_characters()
    return manager


async def profile(conn, name, **overrides):
    return await crud.create_voice_profile(conn, VoiceProfileCreate(
        **{"name": name, "gpt_weights_path": "", "sovits_weights_path": "", **overrides},
    ))


@pytest.mark.parametrize("query", ["Ann", "annabelle-extra", "bel", "A%", "A_"])
def test_lookup_does_not_guess_from_substrings(project, query):
    install(project, "annabelle", "Annabelle")
    assert catalog(project).get_character(query) is None


def test_ambiguous_alias_does_not_depend_on_scan_order(project):
    install(project, "alice", "Alice", ["shared"])
    install(project, "bella", "Bella", ["shared"])
    manager = catalog(project)
    assert manager.get_character("shared") is None
    assert manager.get_character("alice").name == "Alice"


def test_exact_name_beats_another_packages_alias(project):
    install(project, "ann", "Ann")
    install(project, "other", "Other", ["Ann"])
    assert catalog(project).get_character("Ann").id == "ann"


def test_bilingual_and_whitespace_names_keep_compatibility(project):
    install(project, "ann", "安 娜", ["Anna"])
    manager = catalog(project)
    assert manager.get_character("安娜（Anna）").id == "ann"
    assert manager.get_character(" Anna ").id == "ann"


def test_duplicate_manifest_ids_preserve_previous_catalog(project):
    install(project, "ann", "Ann")
    manager = catalog(project)
    original = manager.get_character("ann")
    install(project, "ann", "Impostor", folder="second")
    with pytest.raises(ValueError, match="ann"):
        manager.discover_characters()
    assert manager.get_character("ann") is original


@pytest.mark.asyncio
async def test_sync_exact_profile_beats_longer_name(project, isolate_test_database):
    install(project, "ann", "Ann")
    manager = catalog(project)
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        other = await profile(conn, "Annabelle", system_prompt="User persona")
        exact = await profile(conn, "Ann")
        await manager.sync_with_db(conn)
        assert (await crud.get_voice_profile(conn, other.id)).system_prompt == "User persona"
        assert (await crud.get_voice_profile(conn, exact.id)).system_prompt == "Persona for Ann"
        assert await manager.sync_with_db(conn) == 0


@pytest.mark.asyncio
async def test_sync_alias_cannot_claim_another_package_name(project, isolate_test_database):
    install(project, "ann", "Ann", ["Bella"])
    install(project, "bella", "Bella")
    manager = catalog(project)
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        original = await profile(conn, "Bella")
        await manager.sync_with_db(conn)
        rows = await crud.list_voice_profiles(conn)
        assert {row.name for row in rows} == {"Ann", "Bella"}
        assert (await crud.get_voice_profile(conn, original.id)).system_prompt == "Persona for Bella"


@pytest.mark.asyncio
async def test_ambiguous_profiles_are_preserved_without_duplicate_inserts(project, isolate_test_database):
    install(project, "ann", "Ann", ["Anna"])
    manager = catalog(project)
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        await profile(conn, "Anna", system_prompt="first")
        await profile(conn, "ANNA", system_prompt="second")
        assert await manager.sync_with_db(conn) == 0
        assert await manager.sync_with_db(conn) == 0
        rows = await crud.list_voice_profiles(conn)
        assert [row.system_prompt for row in rows] == ["first", "second"]


@pytest.mark.asyncio
async def test_rescan_does_not_delete_user_profiles(project, isolate_test_database):
    install(project, "ann", "Ann")
    manager = catalog(project)
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        custom = await profile(conn, "kazari", system_prompt="My settings")
        absent = await profile(conn, "Unavailable", ref_audio_path="characters/removed/gentle.wav")
        await manager.sync_with_db(conn)
        assert (await crud.get_voice_profile(conn, custom.id)).system_prompt == "My settings"
        assert await crud.get_voice_profile(conn, absent.id) is not None


def test_existing_e_drive_weights_are_not_replaced(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    assert not CharacterManager._weight_needs_healing("E:/models/custom.ckpt")


@pytest.mark.asyncio
async def test_sync_preserves_shared_reference_and_user_weights(project, isolate_test_database, monkeypatch):
    install(project, "ann", "Ann", folder="different-folder")
    manager = catalog(project)
    shared = project / "audio" / "shared" / "natsume_gentle.wav"
    shared.parent.mkdir(parents=True)
    shared.write_bytes(b"user reference")
    weights = project / "models" / "custom.ckpt"
    weights.parent.mkdir()
    weights.write_bytes(b"user model")
    elsewhere = project / "unrelated-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        original = await profile(conn, "Ann", gpt_weights_path="models/custom.ckpt",
                                 ref_audio_path="audio/shared/natsume_gentle.wav", prompt_text="User text")
        await manager.sync_with_db(conn)
        result = await crud.get_voice_profile(conn, original.id)
        assert result.gpt_weights_path == "models/custom.ckpt"
        assert result.ref_audio_path == "audio/shared/natsume_gentle.wav"
        assert result.prompt_text == "User text"


def test_audio_ownership_uses_package_folder_not_display_name(project):
    folder = install(project, "ann", "Ann", folder="localized-folder")
    manager = catalog(project)
    package = manager.get_character("ann")
    assert not manager._is_cross_character_audio(str(folder / "gentle.wav"), package)
    assert manager._is_cross_character_audio("characters/another/gentle.wav", package)
    assert not manager._is_cross_character_audio("audio/shared/natsume_gentle.wav", package)


@pytest.mark.asyncio
async def test_startup_does_not_replace_unknown_voice_with_default(project, isolate_test_database):
    bundled = project / "audio" / "references" / "natsume" / "gentle.ogg"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"unrelated voice")
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        original = await profile(conn, "Other", ref_audio_path="missing.wav", prompt_text="Other words")
        assert await crud.auto_heal_voice_profiles(conn) == 0
        result = await crud.get_voice_profile(conn, original.id)
        assert result.ref_audio_path == "missing.wav"
        assert result.prompt_text == "Other words"


@pytest.mark.asyncio
async def test_sync_rolls_back_all_changes_when_one_package_fails(project, isolate_test_database, monkeypatch):
    install(project, "ann", "Ann")
    install(project, "bella", "Bella")
    manager = catalog(project)
    insert = manager._insert_new_profile

    async def fail_second(conn, name, *args):
        if name == "Bella":
            raise RuntimeError("synthetic sync failure")
        await insert(conn, name, *args)

    monkeypatch.setattr(manager, "_insert_new_profile", fail_second)
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        with pytest.raises(RuntimeError, match="synthetic sync"):
            await manager.sync_with_db(conn)
        assert await crud.list_voice_profiles(conn) == []


@pytest.mark.asyncio
async def test_switch_lookup_does_not_select_similar_profile(project, isolate_test_database, monkeypatch):
    from galgame2voice.routers import characters

    manager = catalog(project)
    monkeypatch.setattr(characters, "get_character_manager", lambda: manager)
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        await profile(conn, "Annabelle")
        assert await characters._resolve_profile_by_name(conn, "Ann") is None


@pytest.mark.asyncio
async def test_switch_alias_syncs_own_profile_instead_of_borrowing(project, isolate_test_database, monkeypatch):
    from galgame2voice.routers import characters

    install(project, "ann", "Ann", ["Bella"])
    install(project, "bella", "Bella")
    manager = catalog(project)
    monkeypatch.setattr(characters, "get_character_manager", lambda: manager)
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        other = await profile(conn, "Bella")
        selected = await characters._resolve_profile_by_name(conn, "ann")
        assert selected.id != other.id
        assert selected.name == "Ann"


@pytest.mark.asyncio
async def test_switch_ambiguous_normalized_names_reports_conflict(project, isolate_test_database, monkeypatch):
    from fastapi import HTTPException
    from galgame2voice.routers import characters

    manager = catalog(project)
    monkeypatch.setattr(characters, "get_character_manager", lambda: manager)
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        await profile(conn, "Anna")
        await profile(conn, "ANNA")
        with pytest.raises(HTTPException) as error:
            await characters._resolve_profile_by_name(conn, "anna")
        assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_sync_does_not_borrow_a_bare_reference_from_another_package(project, isolate_test_database, monkeypatch):
    from galgame2voice.services import character_manager

    install(project, "ann", "Ann")
    other = install(project, "bella", "Bella")
    refs = other / "refs"
    refs.mkdir()
    (refs / "gentle.wav").write_bytes((other / "gentle.wav").read_bytes())
    manager = catalog(project)
    monkeypatch.setattr(character_manager, "get_character_manager", lambda: manager)
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        original = await profile(conn, "Ann", ref_audio_path="refs/gentle.wav", prompt_text="Old reference")
        await manager.sync_with_db(conn)
        updated = await crud.get_voice_profile(conn, original.id)
        assert updated.ref_audio_path == "characters/ann/gentle.wav"
        assert updated.prompt_text == "Ann"


@pytest.mark.asyncio
async def test_startup_preserves_valid_external_audio(project, isolate_test_database, monkeypatch):
    root = project / "app"
    root.mkdir()
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(root))
    get_settings.cache_clear()
    external = project / "external.wav"
    external.write_bytes(b"user audio outside the install")
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        original = await profile(conn, "Custom", ref_audio_path=str(external))
        assert await crud.auto_heal_voice_profiles(conn) == 0
        assert (await crud.get_voice_profile(conn, original.id)).ref_audio_path == str(external)


def test_runtime_audio_search_refuses_ambiguous_bare_paths(project, monkeypatch):
    from galgame2voice.services import character_manager
    from galgame2voice.utils.path_guard import resolve_existing_audio_path

    for package_id in ("ann", "bella"):
        folder = install(project, package_id, package_id.title())
        refs = folder / "refs"
        refs.mkdir()
        (refs / "gentle.wav").write_bytes((folder / "gentle.wav").read_bytes())
    manager = catalog(project)
    monkeypatch.setattr(character_manager, "get_character_manager", lambda: manager)
    assert resolve_existing_audio_path("refs/gentle.wav") is None
    assert resolve_existing_audio_path("characters/ann/refs/gentle.wav") == project / "characters/ann/refs/gentle.wav"


@pytest.mark.asyncio
async def test_sync_qualifies_own_package_relative_reference(project, isolate_test_database):
    folder = install(project, "ann", "Ann")
    refs = folder / "refs"
    refs.mkdir()
    (refs / "custom.wav").write_bytes((folder / "gentle.wav").read_bytes())
    manager = catalog(project)
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        original = await profile(conn, "Ann", ref_audio_path="refs/custom.wav", prompt_text="Custom transcript")
        await manager.sync_with_db(conn)
        updated = await crud.get_voice_profile(conn, original.id)
        assert updated.ref_audio_path == "characters/ann/refs/custom.wav"
        assert updated.prompt_text == "Custom transcript"


def test_invalid_package_does_not_poison_valid_package_identity(project):
    install(project, "ann", "Ann", ["Heroine"], folder="mmm_valid")
    for folder_name, pkg_id in (("aaa_broken", "broken_a"), ("zzz_broken", "broken_z")):
        broken = install(project, pkg_id, "Ann", ["Heroine"], folder=folder_name)
        (broken / "gentle.wav").unlink()
    portrait_only = install(project, "portrait_only", "PortraitOnly")
    (portrait_only / "gentle.wav").unlink()

    manager = catalog(project)
    assert manager.get_character("Ann").id == "ann"
    assert manager.get_character("Heroine").id == "ann"
    assert manager.get_character("PortraitOnly").id == "portrait_only"


def test_runtime_audio_search_prioritizes_package_id_over_alias(project, monkeypatch):
    from galgame2voice.services import character_manager
    from galgame2voice.utils.path_guard import resolve_existing_audio_path

    ann = install(project, "ann", "Ann", ["Bella"])
    bella = install(project, "bella", "Bella")
    for folder in (ann, bella):
        refs = folder / "refs"
        refs.mkdir()
        (refs / "gentle.wav").write_bytes((folder / "gentle.wav").read_bytes())
    (bella / "refs" / "only_bella.wav").write_bytes((bella / "gentle.wav").read_bytes())

    manager = catalog(project)
    monkeypatch.setattr(character_manager, "get_character_manager", lambda: manager)
    assert resolve_existing_audio_path("audio/references/bella/gentle.wav") == bella / "refs" / "gentle.wav"
    assert resolve_existing_audio_path("refs/only_bella.wav") is None


def test_voice_profile_resolver_scopes_audio_to_owning_package(project, monkeypatch):
    from galgame2voice.services import character_manager
    from galgame2voice.services.voice_resolver import VoiceProfileResolver

    ann = install(project, "ann", "Ann")
    bella = install(project, "bella", "Bella")
    for folder in (ann, bella):
        refs = folder / "refs"
        refs.mkdir()
        (refs / "custom.wav").write_bytes((folder / "gentle.wav").read_bytes())

    manager = catalog(project)
    monkeypatch.setattr(character_manager, "get_character_manager", lambda: manager)
    resolver = VoiceProfileResolver()

    own_ctx = resolver._build_context({
        "id": 1,
        "name": "Ann",
        "ref_audio_path": "refs/custom.wav",
        "prompt_text": "Ann custom",
    })
    assert Path(own_ctx.ref_audio_path) == (ann / "refs" / "custom.wav").resolve()
    assert own_ctx.prompt_text == "Ann custom"

    healed_ctx = resolver._build_context({
        "id": 2,
        "name": "Ann",
        "ref_audio_path": "characters/bella/gentle.wav",
        "prompt_text": "Bella prompt",
    })
    assert Path(healed_ctx.ref_audio_path) == (ann / "gentle.wav").resolve()
    assert healed_ctx.prompt_text == "Ann"

    conflicting_ctx = resolver._build_context({
        "id": 3,
        "name": "Ann (Bella)",
        "ref_audio_path": "missing.wav",
        "prompt_text": "",
    })
    assert conflicting_ctx.ref_audio_path == "missing.wav"
    assert conflicting_ctx.emotions == {}


@pytest.mark.asyncio
async def test_telegram_character_lookup_does_not_match_substrings(project, isolate_test_database, monkeypatch):
    from galgame2voice.services import character_manager
    from galgame2voice.telegram_bot.handlers import TelegramBotHandlers

    install(project, "natsume", "四季夏目", ["Natsume"])
    manager = catalog(project)
    monkeypatch.setattr(character_manager, "get_character_manager", lambda: manager)
    bot = TelegramBotHandlers.__new__(TelegramBotHandlers)

    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        annabelle = await profile(conn, "Annabelle")
        natsume = await profile(conn, "四季夏目")
        profiles = [annabelle, natsume]
        assert await bot._lookup_character_profile_by_query(conn, "Ann", profiles) is None
        matched = await bot._lookup_character_profile_by_query(conn, "natsume", profiles)
        assert matched is not None and matched.id == natsume.id


def test_character_manager_is_conflicting(project):
    install(project, "alice", "Alice", ["shared"])
    install(project, "bella", "Bella", ["shared"])
    manager = catalog(project)
    assert manager.is_conflicting("shared") is True
    assert manager.is_conflicting("Alice") is False
    assert manager.is_conflicting("") is False
    assert manager.is_conflicting("nonexistent") is False


@pytest.mark.asyncio
async def test_switch_conflicting_package_identity_returns_409(project, isolate_test_database, monkeypatch):
    from fastapi import HTTPException
    from galgame2voice.routers import characters

    install(project, "alice", "Alice", ["shared"])
    install(project, "bella", "Bella", ["shared"])
    manager = catalog(project)
    monkeypatch.setattr(characters, "get_character_manager", lambda: manager)
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        await profile(conn, "Alice")
        await profile(conn, "Bella")
        with pytest.raises(HTTPException) as exc_info:
            await characters._resolve_profile_by_name(conn, "shared")
        assert exc_info.value.status_code == 409
        assert "多个角色包" in exc_info.value.detail


@pytest.mark.asyncio
async def test_resolve_profile_for_query_edge_cases(project, isolate_test_database, monkeypatch):
    from galgame2voice.services import character_manager
    from galgame2voice.services.character_identity import (
        AmbiguousProfileName,
        resolve_profile_for_query,
    )

    install(project, "ann", "Ann", ["Annie"])
    install(project, "cathy", "Cathy", ["shared_alias"])
    install(project, "dora", "Dora", ["shared_alias"])
    manager = catalog(project)
    monkeypatch.setattr(character_manager, "get_character_manager", lambda: manager)

    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        p_ann = await profile(conn, "Ann")
        p_cathy = await profile(conn, "Cathy")
        p_custom = await profile(conn, "CustomStandalone")
        profiles = [p_ann, p_cathy, p_custom]

        # 1. Empty query
        assert resolve_profile_for_query(profiles, "", manager=manager) == (None, None, False)
        assert resolve_profile_for_query(profiles, "   ", manager=manager) == (None, None, False)

        # 2. Conflicting query across packages
        matched, pkg, conflicting = resolve_profile_for_query(profiles, "shared_alias", manager=manager)
        assert matched is None
        assert pkg is None
        assert conflicting is True

        # 3. Exact package name
        matched, pkg, conflicting = resolve_profile_for_query(profiles, "Ann", manager=manager)
        assert matched is not None and matched.id == p_ann.id
        assert pkg is not None and pkg.id == "ann"
        assert conflicting is False

        # 4. Package alias
        matched, pkg, conflicting = resolve_profile_for_query(profiles, "annie", manager=manager)
        assert matched is not None and matched.id == p_ann.id
        assert pkg is not None and pkg.id == "ann"
        assert conflicting is False

        # 5. Non-package custom profile
        matched, pkg, conflicting = resolve_profile_for_query(profiles, "CustomStandalone", manager=manager)
        assert matched is not None and matched.id == p_custom.id
        assert pkg is None
        assert conflicting is False

        # 6. Ambiguous custom profiles
        p_dup = await profile(conn, "CUSTOMSTANDALONE")
        profiles_with_dup = [*profiles, p_dup]
        with pytest.raises(AmbiguousProfileName):
            resolve_profile_for_query(profiles_with_dup, "customstandalone", manager=manager)


@pytest.mark.asyncio
async def test_voice_resolver_finds_profile_by_character_query(project, isolate_test_database, monkeypatch):
    from galgame2voice.services import character_manager
    from galgame2voice.services.voice_resolver import VoiceProfileResolver

    install(project, "ann", "Ann", ["Annie"])
    install(project, "bella", "Bella", ["shared"])
    install(project, "clara", "Clara", ["shared"])
    manager = catalog(project)
    monkeypatch.setattr(character_manager, "get_character_manager", lambda: manager)

    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        p_ann = await profile(conn, "Ann")
        p_bella = await profile(conn, "Bella")

    resolver = VoiceProfileResolver()

    # Query by package alias "Annie" matches p_ann
    ctx_ann = await resolver.resolve_context(
        db_path=isolate_test_database,
        profile_id=None,
        character_name="Annie",
    )
    assert ctx_ann.name == "Ann"
    assert ctx_ann.profile_id == p_ann.id

    # A conflicting explicit query must not speak in the unrelated active voice.
    from galgame2voice.services.character_identity import AmbiguousProfileName

    with pytest.raises(AmbiguousProfileName):
        await resolver.resolve_context(
            db_path=isolate_test_database, profile_id=None, character_name="shared",
        )

@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_id", ["ann", "ＡＮＮ"])
async def test_duplicate_id_cold_start_keeps_profiles_and_reports_conflict(
    project, isolate_test_database, monkeypatch, duplicate_id,
):
    from galgame2voice.routers import characters, voice
    from galgame2voice.services import character_manager

    install(project, "ann", "Ann", folder="first")
    second = install(project, duplicate_id, "Ann Copy", folder="second")
    manager = CharacterManager(project / "characters")
    monkeypatch.setattr(character_manager, "get_character_manager", lambda: manager)
    monkeypatch.setattr(characters, "get_character_manager", lambda: manager)
    monkeypatch.setattr(voice, "get_character_manager", lambda: manager)
    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        await profile(conn, "Saved")
        await conn.commit()

    with pytest.raises(ValueError, match="角色包 ID 重复"):
        manager.discover_characters()
    result = await characters.list_characters(user_id="default_user")
    voices = await voice.list_voice_profiles()
    assert any(p["name"] == "Saved" for p in result["characters"])
    assert any(p["name"] == "Saved" for p in voices["profiles"])
    assert "角色包 ID 重复" in result["catalog_warning"]
    assert voices["catalog_warning"] == result["catalog_warning"]
    assert manager.get_character("ann") is None

    (second / "manifest.json").unlink()
    manager.discover_characters()
    assert manager.get_character("ann").name == "Ann"
    assert (await characters.list_characters(user_id="default_user"))["catalog_warning"] is None
    assert (await voice.list_voice_profiles())["catalog_warning"] is None


@pytest.mark.asyncio
async def test_missing_reference_under_default_directory_does_not_borrow_default_package(
    project, isolate_test_database, monkeypatch
):
    from galgame2voice.services import character_manager
    from galgame2voice.services.voice_resolver import VoiceProfileResolver
    from galgame2voice.services.tts_service import TtsService, VoiceReferenceMissingError
    from galgame2voice.utils.path_guard import resolve_existing_audio_path

    # Install a valid default character package containing gentle.wav
    pkg_dir = install(project, "natsume", "四季夏目")
    manager = catalog(project)
    monkeypatch.setattr(character_manager, "get_character_manager", lambda: manager)
    CharacterManager._instance = manager

    async with aiosqlite.connect(isolate_test_database) as conn:
        conn.row_factory = aiosqlite.Row
        await manager.sync_with_db(conn)
        bob = await profile(
            conn,
            "Bob",
            gpt_weights_path=str(pkg_dir / "gpt.ckpt"),
            sovits_weights_path=str(pkg_dir / "sovits.pth"),
            ref_audio_path="audio/default/gentle.wav",
            prompt_text="Bob custom transcript",
        )
        alice = await profile(
            conn,
            "Alice",
            gpt_weights_path=str(pkg_dir / "gpt.ckpt"),
            sovits_weights_path=str(pkg_dir / "sovits.pth"),
            ref_audio_path="",
            prompt_text="Alice custom transcript",
        )
        await conn.commit()

    # 1. resolve_existing_audio_path must return None for missing audio with default directory segment
    rel_resolved = resolve_existing_audio_path("audio/default/gentle.wav")
    win_resolved = resolve_existing_audio_path(r"C:\Users\Default\voices\gentle.wav")
    chars_default_resolved = resolve_existing_audio_path("characters/default/gentle.wav")
    assert rel_resolved is None
    assert win_resolved is None
    assert chars_default_resolved is None

    # 2. Voice resolution flow: Bob's context must not borrow Natsume's gentle.wav
    resolver = VoiceProfileResolver()
    ctx = await resolver.resolve_context(isolate_test_database, profile_id=bob.id)
    assert ctx is not None
    assert ctx.ref_audio_path == "audio/default/gentle.wav"
    assert "natsume" not in ctx.ref_audio_path

    # 3. Real voice resolution & TTS options flow: custom voice referencing missing audio gets VoiceReferenceMissingError
    class RealEngineClient:
        base_url = "http://127.0.0.1:9880"

    service = TtsService(client=RealEngineClient(), audio_dir=project / "audio", db_path=isolate_test_database)
    with pytest.raises(VoiceReferenceMissingError, match="缺少参考音频"):
        await service._populate_voice_profile_opts({"voice_profile_id": alice.id, "ref_audio_path": "audio/default/gentle.wav"})

    # 4. Normal character package path segment elsewhere can still be found by fallback search
    normal_resolved = resolve_existing_audio_path("external/relocated/characters/natsume/gentle.wav")
    assert normal_resolved is not None
    assert normal_resolved == pkg_dir / "gentle.wav"

    # 5. Legacy refs/ paths still resolve to the only package (refs/ is a separate rule)
    legacy_ref_resolved = resolve_existing_audio_path("refs/gentle.wav")
    assert legacy_ref_resolved is not None
    assert legacy_ref_resolved == pkg_dir / "gentle.wav"


def _profile_named(payload: dict, name: str) -> dict:
    matches = [row for row in payload["profiles"] if row["name"] == name]
    assert len(matches) == 1, payload
    return matches[0]


@pytest.mark.asyncio
async def test_failed_persona_save_keeps_the_old_manifest_and_reports_the_duplicate(
    project, isolate_test_database,
):
    """A duplicate package id must not keep the persona the failed save just wrote."""
    CharacterManager.reset_instance()
    try:
        folder = install(project, "ann", "Ann")
        original_manifest = (folder / "manifest.json").read_text(encoding="utf-8")
        edited = "NEW PERSONA THAT THE FAILURE MUST NOT KEEP"
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
            scanned = await client.post("/api/characters/rescan")
            assert scanned.status_code == 200, scanned.text
            ann = _profile_named((await client.get("/api/voice/profiles")).json(), "Ann")
            original_prompt = ann["system_prompt"]

            second = install(project, "ann", "Ann Copy", folder="second")
            saved = await client.put(
                f"/api/characters/{ann['id']}/system-prompt",
                json={"system_prompt": edited},
            )
            body = saved.json()
            detail = body.get("detail") if isinstance(body, dict) else ""
            if not isinstance(detail, str):
                detail = saved.text
            on_disk = (folder / "manifest.json").read_text(encoding="utf-8")
            profiles = (await client.get("/api/voice/profiles")).json()
            ann_after = _profile_named(profiles, "Ann")
            warning = profiles.get("catalog_warning")

            shutil.rmtree(second)
            recovered_scan = await client.post("/api/characters/rescan")
            assert recovered_scan.status_code == 200, recovered_scan.text
            recovered = _profile_named((await client.get("/api/voice/profiles")).json(), "Ann")

        problems = []
        if not 400 <= saved.status_code < 500:
            problems.append(f"status {saved.status_code} body {saved.text}")
        if "角色包 ID 重复" not in detail:
            problems.append(f"user-visible error is {detail!r}")
        if on_disk != original_manifest:
            problems.append(f"failed save rewrote manifest to {on_disk!r} (db still {ann_after['system_prompt']!r})")
        if ann_after["system_prompt"] != original_prompt:
            problems.append(f"database changed to {ann_after['system_prompt']!r}")
        if not warning:
            problems.append(f"voice list catalog_warning stayed empty after the failed save ({warning!r})")
        elif "角色包 ID 重复" not in warning:
            problems.append(f"catalog_warning does not name the duplicate ({warning!r})")
        if recovered["system_prompt"] != original_prompt:
            problems.append(
                f"after the duplicate folder is removed, sync stores the failed text {recovered['system_prompt']!r}"
            )
        assert not problems, "\n".join(problems)
    finally:
        CharacterManager.reset_instance()


@pytest.mark.asyncio
async def test_overlapping_persona_saves_keep_manifest_and_database_together(
    project, isolate_test_database, monkeypatch,
):
    """Two saves must not leave the manifest on one text and the database on the other."""
    CharacterManager.reset_instance()
    first = second = None
    release = asyncio.Event()
    try:
        folder = install(project, "ann", "Ann")
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
            scanned = await client.post("/api/characters/rescan")
            assert scanned.status_code == 200, scanned.text
            ann = _profile_named((await client.get("/api/voice/profiles")).json(), "Ann")

            # 第一次保存停在写数据库的那次 get_db（本请求里的第二次进入）。
            # 锁会让第二次保存一直等到第一次整段结束，所以第二次用超时等待，避免死锁。
            started = asyncio.Event()
            calls = {"n": 0}
            real_get_db = characters.get_db

            @asynccontextmanager
            async def gating_get_db(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 2:
                    started.set()
                    await release.wait()
                async with real_get_db(*args, **kwargs) as conn:
                    yield conn

            monkeypatch.setattr(characters, "get_db", gating_get_db)
            first = asyncio.create_task(client.put(
                f"/api/characters/{ann['id']}/system-prompt",
                json={"system_prompt": "PROMPT-A"},
            ))
            await asyncio.wait_for(started.wait(), timeout=10)
            second = asyncio.create_task(client.put(
                f"/api/characters/{ann['id']}/system-prompt",
                json={"system_prompt": "PROMPT-B"},
            ))
            await asyncio.wait({second}, timeout=3)
            release.set()
            first_response, second_response = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)

            on_disk = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["system_prompt"]
            in_db = _profile_named((await client.get("/api/voice/profiles")).json(), "Ann")["system_prompt"]
            synced = await client.post("/api/characters/rescan")
            assert synced.status_code == 200, synced.text
            after_sync = _profile_named((await client.get("/api/voice/profiles")).json(), "Ann")["system_prompt"]

        assert first_response.status_code == 200, first_response.text
        assert second_response.status_code == 200, second_response.text
        assert on_disk == in_db == after_sync == "PROMPT-B", (
            f"both saves returned success A={first_response.json().get('system_prompt')!r} "
            f"B={second_response.json().get('system_prompt')!r}, but manifest={on_disk!r} "
            f"db={in_db!r}; a later sync makes the chat persona {after_sync!r}"
        )
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        CharacterManager.reset_instance()


async def _rescan_and_list(project, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from galgame2voice.main import create_app
    from galgame2voice.routers import characters, voice
    from galgame2voice.services import character_manager

    manager = CharacterManager(project / "characters")
    for module in (character_manager, characters, voice):
        monkeypatch.setattr(module, "get_character_manager", lambda: manager)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        scan = await client.post("/api/characters/rescan")
        listed = await client.get("/api/voice/profiles")
    assert scan.status_code == 200 and listed.status_code == 200
    return scan.json(), listed.json()


@pytest.mark.asyncio
async def test_rescan_names_a_rejected_package_and_why(project, isolate_test_database, monkeypatch):
    install(project, "valid_char", "有效角色")
    install(project, "broken_char", "坏掉的角色", folder="broken_folder", seconds=1)

    scan, listed = await _rescan_and_list(project, monkeypatch)

    assert scan["characters"] == ["有效角色"]
    warning = scan["catalog_warning"]
    assert warning.startswith("角色包「broken_folder（坏掉的角色）」不合格：")
    assert "1.00 秒，不在 3 到 10 秒范围内" in warning
    assert listed["catalog_warning"] == warning


@pytest.mark.asyncio
async def test_rescan_lists_three_rejected_packages_and_the_total(project, isolate_test_database, monkeypatch):
    for index in range(4):
        install(project, f"short_{index}", f"短音频{index}", seconds=1)

    scan, _listed = await _rescan_and_list(project, monkeypatch)

    warning = scan["catalog_warning"]
    assert [f"short_{index}（" in warning for index in range(4)] == [True, True, True, False]
    assert warning.endswith("另有 1 个角色包不合格（共 4 个）。")


@pytest.mark.asyncio
async def test_rescan_and_validation_accept_utf8_bom_manifest_and_system_prompt(
    project, isolate_test_database, monkeypatch,
):
    from galgame2voice.schemas.character_manifest import validate_character_package
    from scripts.tools.character_packager import cmd_install, cmd_pack, cmd_validate

    folder = install(project, "bom_char", "天音")
    manifest_path = folder / "manifest.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_data["system_prompt"] = ""
    manifest_path.write_text(
        json.dumps(manifest_data, ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )
    prompt_txt_path = folder / "system_prompt.txt"
    prompt_txt_path.write_text("记事本保存的初始人设", encoding="utf-8-sig")

    assert manifest_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert prompt_txt_path.read_bytes().startswith(b"\xef\xbb\xbf")

    scan, listed = await _rescan_and_list(project, monkeypatch)
    assert scan["characters"] == ["天音"]
    assert scan["catalog_warning"] is None
    assert listed["catalog_warning"] is None
    assert _profile_named(listed, "天音")["system_prompt"] == "记事本保存的初始人设"

    is_valid, errors = validate_character_package(folder)
    assert is_valid is True, errors
    assert errors == []

    out_zip = project / "bom_char.zip"
    install_target = project / "installed_chars"
    assert cmd_validate(folder) == 0
    assert cmd_pack(folder, out_zip) == 0
    assert cmd_install(out_zip, install_target) == 0
    assert (install_target / "天音" / "manifest.json").is_file()


@pytest.mark.asyncio
async def test_saving_persona_on_utf8_bom_manifest_succeeds_and_writes_utf8_without_bom(
    project, isolate_test_database,
):
    CharacterManager.reset_instance()
    try:
        folder = install(project, "ann", "Ann")
        manifest_path = folder / "manifest.json"
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
            scanned = await client.post("/api/characters/rescan")
            assert scanned.status_code == 200, scanned.text
            ann = _profile_named((await client.get("/api/voice/profiles")).json(), "Ann")

            original_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_path.write_text(
                json.dumps(original_data, ensure_ascii=False, indent=2),
                encoding="utf-8-sig",
            )
            assert manifest_path.read_bytes().startswith(b"\xef\xbb\xbf")

            updated_prompt = "记事本保存后通过界面更新的人设"
            saved = await client.put(
                f"/api/characters/{ann['id']}/system-prompt",
                json={"system_prompt": updated_prompt},
            )
            assert saved.status_code == 200, saved.text

            raw_bytes = manifest_path.read_bytes()
            assert not raw_bytes.startswith(b"\xef\xbb\xbf")
            written_data = json.loads(raw_bytes.decode("utf-8"))
            assert written_data == {**original_data, "system_prompt": updated_prompt}

            profiles_after = (await client.get("/api/voice/profiles")).json()
            assert _profile_named(profiles_after, "Ann")["system_prompt"] == updated_prompt

            rescanned = await client.post("/api/characters/rescan")
            assert rescanned.status_code == 200, rescanned.text
            assert rescanned.json()["catalog_warning"] is None
            assert rescanned.json()["characters"] == ["Ann"]
    finally:
        CharacterManager.reset_instance()


@pytest.mark.asyncio
async def test_persona_save_character_scan_does_not_block_other_requests(
    project, isolate_test_database, monkeypatch,
):
    """A lightweight request completes while a persona save is scanning character packages."""
    from galgame2voice.services.tts_service import TtsService

    CharacterManager.reset_instance()
    save_task = None
    scan_entered = threading.Event()
    release_scan = threading.Event()
    try:
        ann_folder = install(project, "ann", "Ann")
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
            scanned = await client.post("/api/characters/rescan")
            assert scanned.status_code == 200, scanned.text
            ann = _profile_named((await client.get("/api/voice/profiles")).json(), "Ann")

            # Drop an uncached character package so the save's rescan probes its audio.
            install(project, "bella", "Bella")

            real_get_audio_duration = TtsService.get_audio_duration
            events: list[str] = []
            released_while_scanning: list[bool] = []

            def slow_get_audio_duration(path):
                events.append("scan-start")
                scan_entered.set()
                released_while_scanning.append(release_scan.wait(timeout=1.5))
                duration = real_get_audio_duration(path)
                events.append("scan-end")
                return duration

            monkeypatch.setattr(TtsService, "get_audio_duration", staticmethod(slow_get_audio_duration))

            updated_prompt = "Updated persona while other requests stay responsive"
            save_task = asyncio.create_task(
                client.put(
                    f"/api/characters/{ann['id']}/system-prompt",
                    json={"system_prompt": updated_prompt},
                )
            )
            assert await asyncio.to_thread(scan_entered.wait, 5.0), "persona save did not enter character scan"

            listed = await client.get("/api/characters")
            assert listed.status_code == 200, listed.text
            events.append("light-request-done")
            release_scan.set()

            save_response = await asyncio.wait_for(save_task, timeout=10.0)
            detail = await client.get(f"/api/characters/{ann['id']}")
            assert detail.status_code == 200, detail.text

        assert events == ["scan-start", "light-request-done", "scan-end"]
        assert released_while_scanning == [True]
        assert [c["name"] for c in listed.json()["characters"]] == ["Ann"]

        assert save_response.status_code == 200, save_response.text
        assert save_response.json() == {
            "character_id": ann["id"],
            "name": "Ann",
            "system_prompt": updated_prompt,
            "length": len(updated_prompt),
            "manifest_path": str(ann_folder / "manifest.json"),
        }
        on_disk = json.loads((ann_folder / "manifest.json").read_text(encoding="utf-8"))["system_prompt"]
        assert on_disk == updated_prompt
        assert detail.json()["system_prompt"] == updated_prompt
        mgr_pkg = characters.get_character_manager().get_character("Ann")
        assert mgr_pkg is not None and mgr_pkg.system_prompt == updated_prompt
    finally:
        release_scan.set()
        if save_task is not None and not save_task.done():
            save_task.cancel()
        CharacterManager.reset_instance()
