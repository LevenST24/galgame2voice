"""
Typed audio error contract.

The audio helpers have always signalled everything with one of two generic
types, so "ffmpeg is not installed" and "this file is corrupt" were
indistinguishable to any caller that did not string-match the message. These
tests pin the typed contract *and* the backward-compatibility promise: existing
``except RuntimeError`` / ``except ValueError`` handlers must keep working.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from galgame2voice.errors import (
    AudioTranscodeFailed,
    ExternalDependencyError,
    FFmpegUnavailable,
    Galgame2VoiceError,
)
from galgame2voice.utils import audio_converter
from galgame2voice.utils.audio_converter import (
    convert_ogg_to_wav,
    convert_wav_to_ogg,
    run_ffmpeg_command,
)

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


async def test_missing_ffmpeg_on_wav_to_ogg_raises_typed_dependency_error(monkeypatch):
    """convert_wav_to_ogg must raise FFmpegUnavailable, not an empty successful payload."""
    monkeypatch.setattr(audio_converter, "find_ffmpeg", lambda *args, **kwargs: None)

    sample_wav = (
        b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
        b"\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x08\x00\x00"
    )
    with pytest.raises(FFmpegUnavailable) as excinfo:
        await audio_converter.convert_wav_to_ogg(sample_wav)

    assert excinfo.value.code == "FFMPEG_UNAVAILABLE"
    assert "ffmpeg executable not found" in str(excinfo.value)


@pytest.mark.asyncio
class TestAudioConverterHardening:
    async def test_convert_ogg_to_wav_empty_and_too_short(self):
        with pytest.raises(ValueError, match="Audio payload is empty or too short"):
            await convert_ogg_to_wav(b"")

        with pytest.raises(ValueError, match="Audio payload is empty or too short"):
            await convert_ogg_to_wav(b"OggS123")

    async def test_convert_wav_to_ogg_empty_and_too_short(self):
        with pytest.raises(ValueError, match="WAV bytes cannot be empty"):
            await convert_wav_to_ogg(b"")

        with pytest.raises(ValueError, match="Audio payload is empty or too short"):
            await convert_wav_to_ogg(b"RIFFshort")

    async def test_convert_wav_to_ogg_corrupt_wrapped_as_value_error(self):
        # Fake discovery too: the transcode step resolves the executable through
        # find_ffmpeg, so patching only the executor left this test depending on a
        # real ffmpeg install being present.
        with patch("galgame2voice.utils.audio_converter.find_ffmpeg", return_value="/fake/ffmpeg"):
            with patch("galgame2voice.utils.audio_converter.run_ffmpeg_command", side_effect=RuntimeError("Invalid data")):
                fake_wav = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00" + b"\x00" * 40
                with pytest.raises(ValueError, match="Audio conversion failed"):
                    await convert_wav_to_ogg(fake_wav)

    async def test_run_ffmpeg_command_injects_nostdin(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0
        with patch("galgame2voice.utils.audio_converter.find_ffmpeg", return_value="/fake/ffmpeg"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
                await run_ffmpeg_command("ffmpeg", "-y", "-i", "input.ogg", "output.wav")
                args, _ = mock_exec.call_args
                assert "-nostdin" in args

    async def test_run_ffmpeg_command_timeout_kills_process_and_no_zombies(self):
        mock_proc = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        async def _hang():
            await asyncio.sleep(10.0)
            return b"", b""

        mock_proc.communicate.side_effect = _hang

        with patch("galgame2voice.utils.audio_converter.find_ffmpeg", return_value="/fake/ffmpeg"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                with pytest.raises(TimeoutError, match="timed out after 0.05 seconds"):
                    await run_ffmpeg_command("ffmpeg", "-i", "fake.wav", timeout=0.05)

                mock_proc.kill.assert_called_once()
                mock_proc.wait.assert_awaited_once()

    async def test_run_ffmpeg_command_cancellation_kills_process(self):
        mock_proc = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        async def _hang():
            await asyncio.sleep(10.0)
            return b"", b""

        mock_proc.communicate.side_effect = _hang

        with patch("galgame2voice.utils.audio_converter.find_ffmpeg", return_value="/fake/ffmpeg"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                task = asyncio.create_task(run_ffmpeg_command("ffmpeg", "-i", "fake.wav", timeout=30.0))
                await asyncio.sleep(0.02)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

            mock_proc.kill.assert_called_once()
            mock_proc.wait.assert_awaited_once()

    async def test_early_rejection_non_audio_and_passthrough(self):
        # 1. Non-audio formats rejected early without calling ffmpeg
        with patch("galgame2voice.utils.audio_converter.run_ffmpeg_command") as mock_run:
            for non_audio in (
                b"\x89PNG\r\n\x1a\n" + b"\x00" * 30,
                b"<!DOCTYPE html><html>" + b"\x00" * 30,
                b'{"json_error": "bad"}' + b"\x00" * 30,
                b"\x7fELF" + b"\x00" * 30,
                b"%PDF-1.4" + b"\x00" * 30,
            ):
                with pytest.raises(ValueError, match="Corrupted or unsupported audio format"):
                    await convert_ogg_to_wav(non_audio)
                with pytest.raises(ValueError, match="Corrupted or unsupported audio format"):
                    await convert_wav_to_ogg(non_audio)
            mock_run.assert_not_called()

        # 2. Passthrough when WAV input is already OGG Opus
        fake_ogg = b"OggS\x00\x02" + b"\x00" * 50
        with patch("galgame2voice.utils.audio_converter.run_ffmpeg_command") as mock_run:
            out = await convert_wav_to_ogg(fake_ogg)
            assert out == fake_ogg
            mock_run.assert_not_called()

