import wave
from pathlib import Path

from galgame2voice.config import get_settings
from galgame2voice.utils.audio_spec import (
    resolve_reference_audio_path,
    validate_reference_audio,
)


def _write_wav(path: Path, duration_seconds: int, sample_rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x01\x00" * (sample_rate * duration_seconds))


def test_reference_audio_prefers_project_root_over_cwd(tmp_path, monkeypatch):
    dir_a = tmp_path / "project_a"
    dir_b = tmp_path / "workdir_b"

    wav_a = dir_a / "refs" / "x.wav"
    wav_b = dir_b / "refs" / "x.wav"

    _write_wav(wav_a, duration_seconds=5)
    _write_wav(wav_b, duration_seconds=1)

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(dir_a))
    get_settings.cache_clear()
    monkeypatch.chdir(dir_b)

    # Relative path should prefer project root (A) over current working directory (B)
    resolved = resolve_reference_audio_path("refs/x.wav")
    assert resolved == str(wav_a.resolve())

    ok, reason = validate_reference_audio("refs/x.wav")
    assert ok is True
    assert reason == ""

    # Legacy fallback: when only B (workdir) has the file, it should still be found
    wav_only_in_b = dir_b / "refs" / "legacy.wav"
    _write_wav(wav_only_in_b, duration_seconds=5)
    resolved_legacy = resolve_reference_audio_path("refs/legacy.wav")
    assert resolved_legacy == str(wav_only_in_b.resolve())
    ok_legacy, reason_legacy = validate_reference_audio("refs/legacy.wav")
    assert ok_legacy is True
    assert reason_legacy == ""

    # Absolute path should be returned as-is
    abs_path_a = str(wav_a.resolve())
    assert resolve_reference_audio_path(abs_path_a) == abs_path_a

    # Non-existent absolute path should also be returned unchanged
    abs_nonexistent = str((dir_a / "nonexistent.wav").resolve())
    assert resolve_reference_audio_path(abs_nonexistent) == abs_nonexistent
