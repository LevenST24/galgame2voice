"""Saving a custom voice with reference audio outside GPT-SoVITS's 3~10 s range is refused up front."""

import wave

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.main import create_app


def _write_wav(name: str, seconds: float) -> str:
    folder = get_settings().data_dir / "ref_length_test"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    rate = 16000
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\x01\x00" * int(rate * seconds))
    return str(path)


def _profile(ref_audio_path: str) -> dict:
    return {
        "name": "测试音色",
        "gpt_weights_path": "weights/test.ckpt",
        "sovits_weights_path": "weights/test.pth",
        "ref_audio_path": ref_audio_path,
        "prompt_text": "こんにちは",
    }


@pytest.mark.parametrize(("seconds", "shown"), [(12.0, "12.0"), (1.5, "1.5")])
async def test_out_of_range_reference_audio_is_refused_with_its_length(seconds, shown):
    long_ref = _write_wav(f"ref_{seconds}.wav", seconds)
    good_ref = _write_wav("ok.wav", 5.0)
    expected = f"参考音频长 {shown} 秒，GPT-SoVITS 只接受 3～10 秒的参考音频，请换一段。"
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        created = await client.post("/api/voice/profiles", json=_profile(long_ref))
        assert created.status_code == 422, created.text
        assert created.json()["detail"] == expected

        created = await client.post("/api/voice/profiles", json=_profile(good_ref))
        assert created.status_code == 201, created.text
        profile_id = created.json()["id"]

        updated = await client.put(f"/api/voice/profiles/{profile_id}", json={"ref_audio_path": long_ref})
        assert updated.status_code == 422, updated.text
        assert updated.json()["detail"] == expected
    async with get_db() as conn:
        stored = await crud.get_voice_profile(conn, profile_id)
    assert stored is not None and stored.ref_audio_path.endswith("ok.wav")


async def test_reference_audio_that_cannot_be_measured_is_left_to_the_engine():
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        created = await client.post("/api/voice/profiles", json=_profile("audio/not_there_yet.wav"))
        assert created.status_code == 201, created.text
