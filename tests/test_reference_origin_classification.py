"""Reproduction and formal verification of reference origin classification and resolution.

Defects tested:
1. Defect 1 (Alias rule): Manifest declares 'gentle.wav'. Input is 'refs/gentle.wav' (not on disk).
   Alias rule incorrectly classified it as PACKAGE_OWNED. Must be EXTERNAL_OR_CUSTOM.
2. Defect 2 (Unregistered characters/ subdirectory): Input is 'characters/unregistered/voice.wav'.
   Old logic treated any characters/ subfolder as CROSS_PACKAGE. Must be EXTERNAL_OR_CUSTOM
   when the subfolder is not a discovered valid package.
3. Defect 3 (Same-name file in another package): Package A has external 'refs/engine_only.wav'.
   Package B physically contains 'refs/engine_only.wav'. Must NOT be classified as CROSS_PACKAGE.
4. Defect 4 (Traversal or absolute path in manifest): Traversal or absolute paths in manifest
   cannot be used to claim package ownership.
5. Defect 5 (Resolver EXTERNAL_OR_CUSTOM isolation): VoiceProfileResolver must not borrow
   an external reference from another package that happens to have a same-named file.
6. Defect 6 (Duration pre-check isolation): TTS service duration pre-check must not borrow
   a same-named short (<3s) file from another default package to falsely trigger fallback
   on a valid external reference.
7. Positive Control: A real, explicit local file that exists on disk with duration <3s correctly
   triggers fallback to the default reference audio.
"""

import json
import wave
from pathlib import Path
from types import SimpleNamespace
import aiosqlite
import pytest

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import init_db
from galgame2voice.services.character_manager import CharacterManager, get_character_manager
from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.reference_origin import ReferenceOrigin, classify_reference_origin
from galgame2voice.services.tts_service import TtsService
from galgame2voice.services.voice_resolver import VoiceProfileResolver
from tests.test_character_identity import install


def _create_mock_pkg(folder: Path, emotions: dict[str, str], pkg_id: str = "pkg_a"):
    manifest = SimpleNamespace(
        id=pkg_id,
        name=pkg_id.title(),
        emotions={
            k: SimpleNamespace(audio=v, text="Test text", lang="ja")
            for k, v in emotions.items()
        },
    )
    return SimpleNamespace(
        id=pkg_id,
        name=pkg_id.title(),
        folder=folder,
        folder_path=folder,
        manifest=manifest,
        is_valid=True,
    )


def test_alias_rule_rejected(tmp_path, monkeypatch):
    """Manifest declares 'gentle.wav'. 'refs/gentle.wav' must NOT be classified as PACKAGE_OWNED."""
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()

    pkg_dir = tmp_path / "characters" / "alice"
    pkg_dir.mkdir(parents=True)
    # Physical file is gentle.wav, not refs/gentle.wav
    (pkg_dir / "gentle.wav").write_bytes(b"audio")

    pkg = _create_mock_pkg(pkg_dir, {"gentle": "gentle.wav"}, pkg_id="alice")

    # clean is refs/gentle.wav - not declared in manifest, not on disk inside pkg_dir
    origin = classify_reference_origin("refs/gentle.wav", pkg)
    assert origin == ReferenceOrigin.EXTERNAL_OR_CUSTOM, (
        f"Expected EXTERNAL_OR_CUSTOM without alias rule, got {origin}"
    )


def test_unregistered_characters_subdirectory_not_cross_package(tmp_path, monkeypatch):
    """An unknown/unregistered directory under characters/ is NOT a valid cross-package."""
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()

    pkg_dir = tmp_path / "characters" / "alice"
    pkg_dir.mkdir(parents=True)

    unreg_dir = tmp_path / "characters" / "random_unregistered_dir"
    unreg_dir.mkdir(parents=True)
    unreg_file = unreg_dir / "audio.wav"
    unreg_file.write_bytes(b"audio")

    pkg = _create_mock_pkg(pkg_dir, {"gentle": "gentle.wav"}, pkg_id="alice")

    # Only pass known valid packages (none other than alice)
    origin = classify_reference_origin(str(unreg_file), pkg, known_other_packages=[])
    assert origin == ReferenceOrigin.EXTERNAL_OR_CUSTOM, (
        f"Unregistered subdirectory should be EXTERNAL_OR_CUSTOM, got {origin}"
    )


def test_same_name_file_in_other_package_not_cross_package(tmp_path, monkeypatch):
    """Package B having 'refs/engine_only.wav' does not make Package A's external reference cross-package."""
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()

    pkg_a_dir = tmp_path / "characters" / "alice"
    pkg_b_dir = tmp_path / "characters" / "bella"
    pkg_a_dir.mkdir(parents=True)
    pkg_b_dir.mkdir(parents=True)

    # Bella has refs/engine_only.wav
    (pkg_b_dir / "refs").mkdir()
    (pkg_b_dir / "refs" / "engine_only.wav").write_bytes(b"audio")

    pkg_a = _create_mock_pkg(pkg_a_dir, {"gentle": "gentle.wav"}, pkg_id="alice")
    pkg_b = _create_mock_pkg(pkg_b_dir, {"gentle": "gentle.wav"}, pkg_id="bella")

    # Alice's profile has external reference "refs/engine_only.wav"
    origin = classify_reference_origin("refs/engine_only.wav", pkg_a, known_other_packages=[pkg_b])
    assert origin == ReferenceOrigin.EXTERNAL_OR_CUSTOM, (
        f"Same-name file in Bella must not make Alice's reference CROSS_PACKAGE, got {origin}"
    )


def test_traversal_in_manifest_not_package_owned(tmp_path, monkeypatch):
    """Traversal path in manifest cannot claim PACKAGE_OWNED."""
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()

    pkg_dir = tmp_path / "characters" / "alice"
    pkg_dir.mkdir(parents=True)

    pkg = _create_mock_pkg(pkg_dir, {"bad": "../../escaped.wav"}, pkg_id="alice")
    origin = classify_reference_origin("../../escaped.wav", pkg)
    assert origin == ReferenceOrigin.EXTERNAL_OR_CUSTOM


def test_resolver_external_or_custom_does_not_borrow_from_other_package(tmp_path, monkeypatch):
    """Resolver for Alice must not resolve 'refs/engine_only.wav' to Bella's file."""
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_CHARACTERS_DIR", str(tmp_path / "characters"))
    get_settings.cache_clear()

    install(tmp_path, "alice", "Alice")
    p_bella = install(tmp_path, "bella", "Bella")
    (p_bella / "refs").mkdir(exist_ok=True)
    bella_engine_wav = p_bella / "refs" / "engine_only.wav"
    bella_engine_wav.write_bytes(b"bella engine audio")

    # Make Bella default so legacy refs/ search finds it
    manifest_b_file = p_bella / "manifest.json"
    m_data = json.loads(manifest_b_file.read_text("utf-8"))
    m_data["is_default"] = True
    manifest_b_file.write_text(json.dumps(m_data), encoding="utf-8")

    mgr = get_character_manager(characters_dir=tmp_path / "characters")
    mgr.discover_characters()
    monkeypatch.setattr(CharacterManager, "_instance", mgr)

    resolver = VoiceProfileResolver()
    raw_profile = {
        "id": 1,
        "name": "Alice",
        "gpt_weights_path": "",
        "sovits_weights_path": "",
        "ref_audio_path": "refs/engine_only.wav",
        "prompt_text": "Alice external prompt",
        "prompt_lang": "zh",
    }

    ctx = resolver._build_context(raw_profile, package=mgr.get_character("alice"))
    # The reference must NOT be hijacked to Bella's file
    assert "bella" not in str(ctx.ref_audio_path).lower(), (
        f"Resolver hijacked Alice's reference to Bella: {ctx.ref_audio_path}"
    )
    assert ctx.ref_audio_path == "refs/engine_only.wav"
    assert ctx.prompt_text == "Alice external prompt"


@pytest.mark.asyncio
async def test_duration_precheck_does_not_borrow_short_audio_from_other_package(tmp_path, monkeypatch):
    """Duration pre-check in TtsService must not borrow a same-named short file from another default package."""
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_CHARACTERS_DIR", str(tmp_path / "characters"))
    monkeypatch.setenv("GALGAME2VOICE_AUDIO_DIR", str(tmp_path / "audio"))
    get_settings.cache_clear()

    install(tmp_path, "alice", "Alice", seconds=4)
    p_bella = install(tmp_path, "bella", "Bella", seconds=4)
    (p_bella / "refs").mkdir(exist_ok=True)

    # Bella's refs/gentle.wav is deliberately 1.0s (too short)
    with wave.open(str(p_bella / "refs" / "gentle.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x01\x00" * 8000 * 1)

    # Make Bella default so legacy refs/ search finds Bella
    mb = json.loads((p_bella / "manifest.json").read_text("utf-8"))
    mb["is_default"] = True
    (p_bella / "manifest.json").write_text(json.dumps(mb), encoding="utf-8")

    mgr = get_character_manager(characters_dir=tmp_path / "characters")
    mgr.discover_characters()
    monkeypatch.setattr(CharacterManager, "_instance", mgr)

    db_path = tmp_path / "test.db"
    await init_db(db_path)

    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await mgr.sync_with_db(conn)
        profiles = await crud.list_voice_profiles(conn)
        alice_prof = [p for p in profiles if p.name == "Alice"][0]

    service = TtsService(client=GptSovitsClient(server=None), audio_dir=tmp_path / "audio", db_path=str(db_path))
    opts = {
        "voice_profile_id": alice_prof.id,
        "ref_audio_path": "refs/remote_sub/gentle.wav",
        "prompt_text": "",
        "prompt_lang": "zh",
    }
    res = await service._populate_voice_profile_opts(dict(opts))

    # Must preserve the external reference and empty prompt, NOT replaced by Bella's short file or fallback
    assert res["ref_audio_path"] == "refs/remote_sub/gentle.wav"
    assert res["prompt_text"] == ""
    assert res["prompt_lang"] == "zh"


@pytest.mark.asyncio
async def test_duration_precheck_positive_control_triggers_fallback_for_actual_short_file(tmp_path, monkeypatch):
    """Positive control: A real local file with invalid duration (<3s) DOES trigger fallback."""
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_CHARACTERS_DIR", str(tmp_path / "characters"))
    monkeypatch.setenv("GALGAME2VOICE_AUDIO_DIR", str(tmp_path / "audio"))
    get_settings.cache_clear()

    install(tmp_path, "alice", "Alice", seconds=4)
    mgr = get_character_manager(characters_dir=tmp_path / "characters")
    mgr.discover_characters()
    monkeypatch.setattr(CharacterManager, "_instance", mgr)

    db_path = tmp_path / "test.db"
    await init_db(db_path)

    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await mgr.sync_with_db(conn)
        profiles = await crud.list_voice_profiles(conn)
        alice_prof = [p for p in profiles if p.name == "Alice"][0]

    # Create real explicit local file with 1s duration
    short_file = tmp_path / "audio" / "actual_short.wav"
    short_file.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(short_file), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x01\x00" * 8000 * 1)

    service = TtsService(client=GptSovitsClient(server=None), audio_dir=tmp_path / "audio", db_path=str(db_path))
    opts = {
        "voice_profile_id": alice_prof.id,
        "ref_audio_path": str(short_file),
        "prompt_text": "短音频文本",
        "prompt_lang": "zh",
    }
    res = await service._populate_voice_profile_opts(dict(opts))

    # Because actual file exists locally and duration is 1.0s (< 3.0s), fallback MUST be triggered!
    assert "gentle.wav" in res["ref_audio_path"]
    assert res["prompt_text"] == "Alice"


@pytest.mark.asyncio
async def test_unregistered_dir_custom_audio_relative_and_absolute_consistency(tmp_path, monkeypatch):
    """Unregistered characters subfolder: 1s relative and absolute both fallback; 4s both preserved."""
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_CHARACTERS_DIR", str(tmp_path / "characters"))
    monkeypatch.setenv("GALGAME2VOICE_AUDIO_DIR", str(tmp_path / "audio"))
    get_settings.cache_clear()

    install(tmp_path, "alice", "Alice", seconds=4)
    mgr = get_character_manager(characters_dir=tmp_path / "characters")
    mgr.discover_characters()
    monkeypatch.setattr(CharacterManager, "_instance", mgr)

    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await mgr.sync_with_db(conn)
        profiles = await crud.list_voice_profiles(conn)
        alice_prof = [p for p in profiles if p.name == "Alice"][0]

    # Create unregistered directory under characters/
    unreg_dir = tmp_path / "characters" / "unregistered"
    unreg_dir.mkdir(parents=True, exist_ok=True)
    short_wav = unreg_dir / "short.wav"
    with wave.open(str(short_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x01\x00" * 8000 * 1)

    service = TtsService(client=GptSovitsClient(server=None), audio_dir=tmp_path / "audio", db_path=str(db_path))

    # 1. Relative path to 1s file must measure duration and fallback
    opts_rel = {
        "voice_profile_id": alice_prof.id,
        "ref_audio_path": "characters/unregistered/short.wav",
        "prompt_text": "短文本",
        "prompt_lang": "zh",
    }
    res_rel = await service._populate_voice_profile_opts(dict(opts_rel))
    assert "gentle.wav" in res_rel["ref_audio_path"], f"Expected fallback for 1s relative path, got {res_rel['ref_audio_path']}"
    assert res_rel["prompt_text"] == "Alice"

    # 2. Absolute path to 1s file must measure duration and fallback
    opts_abs = {
        "voice_profile_id": alice_prof.id,
        "ref_audio_path": str(short_wav.resolve()),
        "prompt_text": "短文本",
        "prompt_lang": "zh",
    }
    res_abs = await service._populate_voice_profile_opts(dict(opts_abs))
    assert "gentle.wav" in res_abs["ref_audio_path"], f"Expected fallback for 1s absolute path, got {res_abs['ref_audio_path']}"
    assert res_abs["prompt_text"] == "Alice"


@pytest.mark.asyncio
async def test_cross_package_fallback_when_gentle_deleted_uses_alternative_emotion(tmp_path, monkeypatch):
    """When gentle.wav is deleted on disk, cross-package reference falls back to another available emotion."""
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_CHARACTERS_DIR", str(tmp_path / "characters"))
    get_settings.cache_clear()

    # Install Alice with gentle and joy
    p_alice = install(tmp_path, "alice", "Alice", seconds=4)
    with wave.open(str(p_alice / "joy.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x01\x00" * 8000 * 5)

    m_alice = json.loads((p_alice / "manifest.json").read_text("utf-8"))
    m_alice["emotions"]["joy"] = {"audio": "joy.wav", "text": "Alice joy text", "lang": "ja"}
    (p_alice / "manifest.json").write_text(json.dumps(m_alice), encoding="utf-8")

    # Install Bella
    p_bella = install(tmp_path, "bella", "Bella", seconds=4)

    mgr = get_character_manager(characters_dir=tmp_path / "characters")
    mgr.discover_characters()
    monkeypatch.setattr(CharacterManager, "_instance", mgr)

    # Delete Alice's gentle.wav on disk after discovery
    (p_alice / "gentle.wav").unlink()

    resolver = VoiceProfileResolver()
    raw_profile = {
        "id": 1,
        "name": "Alice",
        "gpt_weights_path": "",
        "sovits_weights_path": "",
        "ref_audio_path": str(p_bella / "gentle.wav"),  # Explicit cross-package reference to Bella
        "prompt_text": "Bella text",
        "prompt_lang": "ja",
    }

    ctx = resolver._build_context(raw_profile, package=mgr.get_character("alice"))
    # Must NOT leak Bella's audio/text; must fall back to Alice's joy.wav
    assert "bella" not in str(ctx.ref_audio_path).lower(), f"Leaked Bella reference: {ctx.ref_audio_path}"
    assert "joy.wav" in str(ctx.ref_audio_path)
    assert ctx.prompt_text == "Alice joy text"


@pytest.mark.asyncio
async def test_cross_package_fallback_when_all_emotions_deleted_clears_reference(tmp_path, monkeypatch):
    """When all emotion audios of Alice are deleted, cross-package reference clears rather than leaking."""
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_CHARACTERS_DIR", str(tmp_path / "characters"))
    get_settings.cache_clear()

    p_alice = install(tmp_path, "alice", "Alice", seconds=4)
    p_bella = install(tmp_path, "bella", "Bella", seconds=4)

    mgr = get_character_manager(characters_dir=tmp_path / "characters")
    mgr.discover_characters()
    monkeypatch.setattr(CharacterManager, "_instance", mgr)

    # Delete Alice's only emotion audio
    (p_alice / "gentle.wav").unlink()

    resolver = VoiceProfileResolver()
    raw_profile = {
        "id": 1,
        "name": "Alice",
        "gpt_weights_path": "",
        "sovits_weights_path": "",
        "ref_audio_path": str(p_bella / "gentle.wav"),  # Explicit cross-package reference to Bella
        "prompt_text": "Bella text",
        "prompt_lang": "ja",
    }

    ctx = resolver._build_context(raw_profile, package=mgr.get_character("alice"))
    # Must NOT leak Bella's audio/text; must clear reference
    assert "bella" not in str(ctx.ref_audio_path).lower()
    assert ctx.ref_audio_path == ""
    assert ctx.prompt_text == ""


@pytest.mark.asyncio
async def test_populate_voice_profile_opts_canonical_empty_prompt_text_takes_precedence_over_alias(tmp_path, monkeypatch):
    """When canonical prompt_text is explicitly present as empty string, it must NOT be overwritten by refer_text."""
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_CHARACTERS_DIR", str(tmp_path / "characters"))
    monkeypatch.setenv("GALGAME2VOICE_AUDIO_DIR", str(tmp_path / "audio"))
    get_settings.cache_clear()

    install(tmp_path, "alice", "Alice", seconds=4)
    mgr = get_character_manager(characters_dir=tmp_path / "characters")
    mgr.discover_characters()
    monkeypatch.setattr(CharacterManager, "_instance", mgr)

    db_path = tmp_path / "test.db"
    await init_db(db_path)

    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await mgr.sync_with_db(conn)
        profiles = await crud.list_voice_profiles(conn)
        alice_prof = [p for p in profiles if p.name == "Alice"][0]

    client = GptSovitsClient("http://127.0.0.1:9880")
    service = TtsService(client=client, audio_dir=tmp_path / "audio", db_path=str(db_path))

    opts = {
        "voice_profile_id": alice_prof.id,
        "ref_audio_path": "refs/unknown_ref.wav",
        "prompt_text": "",
        "refer_text": "stale alias text",
        "prompt_lang": "zh",
    }
    res = await service._populate_voice_profile_opts(dict(opts))

    # Canonical empty prompt_text MUST be preserved, not overwritten by refer_text
    assert res["prompt_text"] == "", f"Expected empty string for prompt_text, got {res['prompt_text']!r}"
    assert res["ref_audio_path"] == "refs/unknown_ref.wav"


@pytest.mark.asyncio
async def test_populate_voice_profile_opts_missing_canonical_preserves_alias_compatibility(tmp_path, monkeypatch):
    """When canonical prompt_text is omitted or None, refer_text is merged for backward compatibility."""
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_CHARACTERS_DIR", str(tmp_path / "characters"))
    monkeypatch.setenv("GALGAME2VOICE_AUDIO_DIR", str(tmp_path / "audio"))
    get_settings.cache_clear()

    install(tmp_path, "alice", "Alice", seconds=4)
    mgr = get_character_manager(characters_dir=tmp_path / "characters")
    mgr.discover_characters()
    monkeypatch.setattr(CharacterManager, "_instance", mgr)

    db_path = tmp_path / "test.db"
    await init_db(db_path)

    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await mgr.sync_with_db(conn)
        profiles = await crud.list_voice_profiles(conn)
        alice_prof = [p for p in profiles if p.name == "Alice"][0]

    client = GptSovitsClient("http://127.0.0.1:9880")
    service = TtsService(client=client, audio_dir=tmp_path / "audio", db_path=str(db_path))

    # 1. prompt_text is missing completely from opts
    opts_missing = {
        "voice_profile_id": alice_prof.id,
        "ref_audio_path": "refs/unknown_ref.wav",
        "refer_text": "valid alias text",
        "prompt_lang": "zh",
    }
    res_missing = await service._populate_voice_profile_opts(dict(opts_missing))
    assert res_missing["prompt_text"] == "valid alias text"

    # 2. prompt_text is explicitly None
    opts_none = {
        "voice_profile_id": alice_prof.id,
        "ref_audio_path": "refs/unknown_ref.wav",
        "prompt_text": None,
        "refer_text": "valid alias text 2",
        "prompt_lang": "zh",
    }
    res_none = await service._populate_voice_profile_opts(dict(opts_none))
    assert res_none["prompt_text"] == "valid alias text 2"


@pytest.fixture
def alice_undeclared_custom_setup(tmp_path, monkeypatch):
    """Synchronous fixture precomputing undeclared custom audio paths to avoid async Path.resolve."""
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GALGAME2VOICE_CHARACTERS_DIR", str(tmp_path / "characters"))
    monkeypatch.setenv("GALGAME2VOICE_AUDIO_DIR", str(tmp_path / "audio"))
    get_settings.cache_clear()

    # Install Alice with 4s gentle.wav (in manifest)
    p_alice = install(tmp_path, "alice", "Alice", seconds=4)

    # Put an undeclared 4s custom audio inside Alice's refs/ directory
    custom_wav = p_alice / "refs" / "custom.wav"
    custom_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(custom_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x01\x00" * 8000 * 4)

    expected_path = str(custom_wav.resolve())
    return SimpleNamespace(
        tmp_path=tmp_path,
        expected_path=expected_path,
    )


@pytest.mark.asyncio
async def test_resolved_valid_package_custom_ref_writes_back_absolute_path_and_captures_identity(
    alice_undeclared_custom_setup, monkeypatch
):
    """When a 4s custom ref exists inside package folder but not in manifest, _populate_voice_profile_opts

    must write back the resolved absolute path so AudioIdentity captures a valid revision.
    """
    tmp_path = alice_undeclared_custom_setup.tmp_path
    expected_path = alice_undeclared_custom_setup.expected_path

    mgr = get_character_manager(characters_dir=tmp_path / "characters")
    mgr.discover_characters()
    monkeypatch.setattr(CharacterManager, "_instance", mgr)

    db_path = tmp_path / "test.db"
    await init_db(db_path)

    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await mgr.sync_with_db(conn)
        profiles = await crud.list_voice_profiles(conn)
        alice_prof = [p for p in profiles if p.name == "Alice"][0]

    client = GptSovitsClient("http://127.0.0.1:9880")
    service = TtsService(client=client, audio_dir=tmp_path / "audio", db_path=str(db_path))

    opts = {
        "voice_profile_id": alice_prof.id,
        "ref_audio_path": "refs/custom.wav",  # Relative to Alice pkg folder
        "prompt_text": "",                    # Explicit canonical empty prompt_text
        "prompt_lang": "zh",
    }
    res = await service._populate_voice_profile_opts(dict(opts))

    # 1. ref_audio_path must be written back as the resolved absolute path
    assert res["ref_audio_path"] == expected_path, (
        f"Expected resolved absolute path {expected_path}, got {res['ref_audio_path']}"
    )

    # 2. AudioIdentity must capture a valid revision (not None) and exact canonical path
    identity = res.get("_audio_identity")
    assert identity is not None, "Missing _audio_identity"
    assert identity.reference.revision is not None, (
        f"Expected AudioIdentity.reference.revision to be captured, got None (path={identity.reference.path})"
    )
    assert identity.reference.path.lower() == expected_path.lower()

    # 3. Canonical prompt_text empty string and prompt_lang are preserved
    assert res["prompt_text"] == ""
    assert res["prompt_lang"] == "zh"

    # 4. Client payload resolves to the exact absolute reference audio path
    payload = client._build_tts_payload("测试", res)
    assert payload["ref_audio_path"] == expected_path, (
        f"Expected client payload ref_audio_path to resolve to {expected_path}, got {payload['ref_audio_path']}"
    )
    assert payload["prompt_text"] == ""
    assert payload["prompt_lang"] == "zh"