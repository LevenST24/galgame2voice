"""Standalone synthesis must allow the same reference-audio roots as profile creation.

Creating a voice whose reference audio lives under an external GPT_SOVITS_DIR
succeeds, because profile validation uses get_authorized_roots(include_sovits=True).
POST /api/voice/synthesize used to resolve that path against only the project
audio directory and reject it with HTTP 400.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.database.session import init_db
from galgame2voice.main import create_app


def _profile_payload(engine: Path, ref_audio: Path) -> dict:
    return {
        "name": "外部引擎参考音",
        "gpt_weights_path": str(engine / "dummy.ckpt"),
        "sovits_weights_path": str(engine / "dummy.pth"),
        "refer_audio_path": str(ref_audio),
        "refer_text": "こんにちは",
        "refer_language": "ja",
    }


@pytest.mark.asyncio
async def test_synthesize_accepts_ref_audio_under_external_gpt_sovits_dir(tmp_path, monkeypatch):
    """Create a profile from an external engine sample, then synthesize with that path."""
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
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/voice/profiles", json=_profile_payload(engine, ref_audio))
        assert created.status_code == 201, created.text
        stored = created.json()["profile"]["ref_audio_path"]
        assert Path(stored).resolve() == ref_audio.resolve()

        with patch(
            "galgame2voice.services.voice_manager.VoiceManager.synthesize",
            new_callable=AsyncMock,
        ) as mock_synth:
            mock_synth.return_value = b"RIFFmockwav"
            allowed = await client.post(
                "/api/voice/synthesize",
                json={"text": "こんにちは", "options": {"ref_audio_path": stored}},
            )
            assert allowed.status_code == 200, allowed.text
            assert allowed.content == b"RIFFmockwav"
            assert mock_synth.await_count == 1

            rejected = await client.post(
                "/api/voice/synthesize",
                json={"text": "こんにちは", "options": {"ref_audio_path": str(stray_audio)}},
            )
            assert rejected.status_code == 400, rejected.text
            assert "Invalid reference audio path" in rejected.text
            assert mock_synth.await_count == 1
