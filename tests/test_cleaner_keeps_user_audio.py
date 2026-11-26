"""The audio cleaner must only delete audio the app synthesized.

使用说明 tells users to put reference clips in audio/, and the voice scan offers any
non-synthesized clip there as a reference. Copying a file keeps its old timestamp,
so the cleaner used to delete such a clip within minutes unless a voice already
pointed at it.
"""
import os
import time

import pytest

from galgame2voice.routers.voice import _SYNTH_AUDIO_FILENAME_PATTERN
from galgame2voice.services.audio_cleaner import _scan_and_clean


def _age(path, days):
    path.write_bytes(b"RIFF")
    old = time.time() - days * 86400
    os.utime(path, (old, old))


@pytest.mark.parametrize("name", ["柚子_参考.wav", "my voice.MP3", "sample.opus", "chunk_final.wav"])
def test_user_clip_in_audio_dir_survives_cleanup(tmp_path, name):
    audio = tmp_path / "audio"
    audio.mkdir()
    user_clip = audio / name
    _age(user_clip, 30)
    assert not _SYNTH_AUDIO_FILENAME_PATTERN.match(name)  # the scan offers it as a reference

    _scan_and_clean(audio, set(), time.time() - 30 * 60, time.time() - 7 * 86400, set())

    assert user_clip.exists()


def test_synthesized_audio_is_still_cleaned(tmp_path):
    audio = tmp_path / "audio"
    audio.mkdir()
    expired = [audio / n for n in (
        "chunk_3_0123456789ab.wav", "full_0123456789ab.wav", "voice_0123456789ab.WAV",
    )]
    repair = audio / "repair_0123456789ab.wav"
    for p in expired:
        _age(p, 1)
    _age(repair, 30)
    fresh = audio / "chunk_0_aaaaaaaaaaaa.wav"
    fresh.write_bytes(b"RIFF")

    cleaned, _ = _scan_and_clean(audio, set(), time.time() - 30 * 60, time.time() - 7 * 86400, set())

    assert cleaned == 4
    assert not any(p.exists() for p in expired + [repair])
    assert fresh.exists()
