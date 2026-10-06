"""
Typed audio error contract.

The audio helpers have always signalled everything with one of two generic
types, so "ffmpeg is not installed" and "this file is corrupt" were
indistinguishable to any caller that did not string-match the message. These
tests pin the typed contract *and* the backward-compatibility promise: existing
``except RuntimeError`` / ``except ValueError`` handlers must keep working.
"""

import pytest

from galgame2voice.errors import (
    AudioTranscodeFailed,
    ExternalDependencyError,
    FFmpegUnavailable,
    Galgame2VoiceError,
)
from galgame2voice.utils import audio_converter

# Passes the payload sanity checks and is not already target-format PCM, so the
# converter proceeds to the ffmpeg step.
VALID_LOOKING_OGG = b"OggS" + b"\x00" * 64


class _FakeProcess:
    """Minimal stand-in for an asyncio subprocess with a failing exit code."""

    returncode = 1

    async def communicate(self):
        return b"", b"Invalid data found when processing input"

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def test_missing_ffmpeg_is_a_dependency_error_not_a_value_error():
    """The whole point: 'install ffmpeg' and 'bad input' must be different types."""
    assert issubclass(FFmpegUnavailable, ExternalDependencyError)
    assert issubclass(FFmpegUnavailable, RuntimeError)
    assert not issubclass(FFmpegUnavailable, ValueError)


def test_transcode_failure_keeps_value_error_compatibility():
    """Callers (and older tests) catch ValueError for conversion failures."""
    assert issubclass(AudioTranscodeFailed, Galgame2VoiceError)
    assert issubclass(AudioTranscodeFailed, RuntimeError)
    assert issubclass(AudioTranscodeFailed, ValueError)


def test_error_codes_are_stable_and_distinct():
    codes = {
        Galgame2VoiceError.code,
        ExternalDependencyError.code,
        FFmpegUnavailable.code,
        AudioTranscodeFailed.code,
    }

    assert FFmpegUnavailable.code == "FFMPEG_UNAVAILABLE"
    assert AudioTranscodeFailed.code == "AUDIO_TRANSCODE_FAILED"
    assert ExternalDependencyError.code == "EXTERNAL_DEPENDENCY_UNAVAILABLE"
    assert len(codes) == 4, "consumers must be able to tell these apart"


async def test_missing_ffmpeg_raises_typed_dependency_error(monkeypatch):
    monkeypatch.setattr(audio_converter, "find_ffmpeg", lambda *args, **kwargs: None)

    with pytest.raises(FFmpegUnavailable) as excinfo:
        await audio_converter.convert_ogg_to_wav(VALID_LOOKING_OGG)

    assert excinfo.value.code == "FFMPEG_UNAVAILABLE"
    assert "ffmpeg executable not found" in str(excinfo.value)


async def test_missing_ffmpeg_is_not_reported_as_a_transcode_failure(monkeypatch):
    monkeypatch.setattr(audio_converter, "find_ffmpeg", lambda *args, **kwargs: None)

    with pytest.raises(Galgame2VoiceError) as excinfo:
        await audio_converter.convert_ogg_to_wav(VALID_LOOKING_OGG)

    assert not isinstance(excinfo.value, AudioTranscodeFailed)
    assert not isinstance(excinfo.value, ValueError)


async def test_nonzero_ffmpeg_exit_raises_transcode_error(monkeypatch):
    async def _fake_exec(*args, **kwargs):
        return _FakeProcess()

    # Discovery must be faked as well as the executor: run_ffmpeg_command resolves
    # the binary through find_ffmpeg before spawning anything.
    monkeypatch.setattr(audio_converter, "find_ffmpeg", lambda *args, **kwargs: "/fake/ffmpeg")
    monkeypatch.setattr(audio_converter.asyncio, "create_subprocess_exec", _fake_exec)

    with pytest.raises(AudioTranscodeFailed) as excinfo:
        await audio_converter.run_ffmpeg_command("ffmpeg", "-i", "in.wav", "out.ogg")

    assert excinfo.value.code == "AUDIO_TRANSCODE_FAILED"
    assert "Invalid data found" in str(excinfo.value)


async def test_wrapped_transcode_failure_is_still_catchable_as_value_error(monkeypatch):
    """The wrapped path must keep the historical ValueError contract."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("ffmpeg conversion failed (code 1): Invalid data")

    monkeypatch.setattr(audio_converter, "find_ffmpeg", lambda *args, **kwargs: "/usr/bin/ffmpeg")
    monkeypatch.setattr(audio_converter, "run_ffmpeg_command", _boom)

    with pytest.raises(ValueError, match="Audio conversion failed") as excinfo:
        await audio_converter.convert_ogg_to_wav(VALID_LOOKING_OGG)

    assert isinstance(excinfo.value, AudioTranscodeFailed)
    assert excinfo.value.code == "AUDIO_TRANSCODE_FAILED"


async def test_corrupt_payload_is_still_a_plain_value_error(monkeypatch):
    """Input validation is a caller mistake, not an external failure."""
    monkeypatch.setattr(audio_converter, "find_ffmpeg", lambda *args, **kwargs: "/usr/bin/ffmpeg")

    with pytest.raises(ValueError):
        await audio_converter.convert_ogg_to_wav(b"")
