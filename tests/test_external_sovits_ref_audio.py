"""Voice profile creation allows reference audio under an external GPT_SOVITS_DIR
while rejecting paths outside authorized roots.
"""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.database.session import init_db
from galgame2voice.main import create_app


def _profile_payload(engine: Path, ref_audio: Path, name: str = "外部引擎参考音") -> dict:
    return {
        "name": name,
        "gpt_weights_path": str(engine / "dummy.ckpt"),
        "sovits_weights_path": str(engine / "dummy.pth"),
        "refer_audio_path": str(ref_audio),
        "refer_text": "こんにちは",
        "refer_language": "ja",
    }


@pytest.mark.asyncio
async def test_profile_accepts_ref_audio_under_external_gpt_sovits_dir(tmp_path, monkeypatch):
    """Create a profile from an external engine sample and reject stray paths outside authorized roots."""
    # The engine and the stray folder sit beside the project root, outside it, wherever tmp_path is.
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path / "project"))
    get_settings.cache_clear()
    engine = tmp_path / "engine"
    stray = tmp_path / "stray"
    engine.mkdir()
    stray.mkdir()
    ref_audio = engine / "sample.wav"
    ref_audio.write_bytes(b"RIFFdummywav")
    (engine / "dummy.ckpt").write_bytes(b"ckpt")
    (engine / "dummy.pth").write_bytes(b"pth")
    stray_audio = stray / "nope.wav"
    stray_audio.write_bytes(b"RIFFdummywav")
    monkeypatch.setenv("GPT_SOVITS_DIR", str(engine))

    await init_db()
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8080") as client:
        created = await client.post("/api/voice/profiles", json=_profile_payload(engine, ref_audio))
        assert created.status_code == 201, created.text
        stored = created.json()["profile"]["ref_audio_path"]
        assert Path(stored).resolve() == ref_audio.resolve()

        rejected = await client.post(
            "/api/voice/profiles",
            json=_profile_payload(engine, stray_audio, name="越界参考音"),
        )
        assert rejected.status_code == 400, rejected.text
        assert "Invalid file path" in rejected.text
