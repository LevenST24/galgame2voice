"""Project-relative reference audio resolves against the install root, not the working directory."""

import wave

from galgame2voice.services.gpt_sovits_client import GptSovitsClient
from galgame2voice.services.tts_cache_manager import _normalize_ref_audio
from galgame2voice.utils.audio_spec import project_file, resolve_reference_audio_path, validate_reference_audio


def _install_with_reference(tmp_path, monkeypatch):
    install = tmp_path / "Galgame2Voice"
    reference = install / "characters" / "测试角色" / "refs" / "gentle.wav"
    reference.parent.mkdir(parents=True)
    with wave.open(str(reference), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 16000 * 4)
    elsewhere = tmp_path / "shortcut-start-in"
    elsewhere.mkdir()
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(install))
    monkeypatch.chdir(elsewhere)
    return reference, "characters/测试角色/refs/gentle.wav"


def test_engine_payload_uses_absolute_install_path(tmp_path, monkeypatch):
    reference, stored = _install_with_reference(tmp_path, monkeypatch)
    assert project_file(stored) == reference
    assert GptSovitsClient._resolve_payload_ref_audio(stored) == str(reference.resolve())
    assert resolve_reference_audio_path(stored) == str(reference.resolve())


def test_validation_and_cache_identity_see_the_installed_file(tmp_path, monkeypatch):
    reference, stored = _install_with_reference(tmp_path, monkeypatch)
    assert validate_reference_audio(stored) == (True, "")
    stat = reference.stat()
    # Cache keys include the file revision, so re-recorded references invalidate old audio.
    assert _normalize_ref_audio(stored) == f"gentle.wav:{stat.st_mtime_ns}:{stat.st_size}"


def test_missing_files_keep_the_original_path(tmp_path, monkeypatch):
    _install_with_reference(tmp_path, monkeypatch)
    assert project_file("characters/none/refs/gentle.wav") is None
    assert GptSovitsClient._resolve_payload_ref_audio("characters/none/refs/gentle.wav") == "characters/none/refs/gentle.wav"
    ok, reason = validate_reference_audio("characters/none/refs/gentle.wav")
    assert not ok and "not found" in reason
