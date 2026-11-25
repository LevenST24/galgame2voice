"""A custom voice made from scanned files keeps working after the install folder moves.

The scan returns absolute paths. The reference audio was already stored project-relative,
but the GPT and SoVITS weights were stored as absolute paths into the old folder.
"""
import shutil

from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.database import crud
from galgame2voice.database.session import get_db
from galgame2voice.main import create_app
from galgame2voice.utils.path_guard import resolve_existing_audio_path, resolve_weight_file_path


async def test_weights_still_resolve_after_install_folder_moves(tmp_path, monkeypatch):
    old_root = tmp_path / "old" / "galgame2voice"
    new_root = tmp_path / "new" / "galgame2voice"
    weights = old_root / "GPT-SoVITS" / "GPT_weights_v2"
    weights.mkdir(parents=True)
    (weights / "my.ckpt").write_bytes(b"ckpt")
    sovits = old_root / "GPT-SoVITS" / "SoVITS_weights_v2"
    sovits.mkdir(parents=True)
    (sovits / "my.pth").write_bytes(b"pth")
    refs = old_root / "audio"
    refs.mkdir(parents=True)
    (refs / "ref.wav").write_bytes(b"RIFF")

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(old_root))
    get_settings.cache_clear()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        # Same shape as the settings UI: absolute paths picked from /api/voice/scan-models
        resp = await client.post("/api/voice/profiles", json={
            "name": "自定义",
            "gpt_weights_path": str(weights / "my.ckpt"),
            "sovits_weights_path": str(sovits / "my.pth"),
            "ref_audio_path": str(refs / "ref.wav"),
            "prompt_text": "こんにちは",
            "prompt_lang": "ja",
            "text_lang": "ja",
        })
        assert resp.status_code == 201, resp.text
        async with get_db() as conn:
            profile = await crud.get_voice_profile(conn, resp.json()["id"])

    # The user moves the whole folder (e.g. from the Desktop to D:\Games).
    shutil.move(str(old_root), str(new_root))
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(new_root))
    get_settings.cache_clear()

    assert resolve_existing_audio_path(profile.ref_audio_path) is not None  # reference survives
    gpt = resolve_weight_file_path(profile.gpt_weights_path)
    sov = resolve_weight_file_path(profile.sovits_weights_path)
    assert (new_root / "GPT-SoVITS" / "GPT_weights_v2" / "my.ckpt").samefile(gpt), f"stored {profile.gpt_weights_path!r}"
    assert (new_root / "GPT-SoVITS" / "SoVITS_weights_v2" / "my.pth").samefile(sov), f"stored {profile.sovits_weights_path!r}"


async def test_updated_weights_are_stored_project_relative(tmp_path, monkeypatch):
    root = tmp_path / "galgame2voice"
    weights = root / "GPT-SoVITS" / "GPT_weights_v2"
    weights.mkdir(parents=True)
    (weights / "a.ckpt").write_bytes(b"ckpt")
    (weights / "b.ckpt").write_bytes(b"ckpt")
    sovits = root / "GPT-SoVITS" / "SoVITS_weights_v2"
    sovits.mkdir(parents=True)
    (sovits / "a.pth").write_bytes(b"pth")
    (sovits / "b.pth").write_bytes(b"pth")
    (root / "audio").mkdir()
    (root / "audio" / "ref.wav").write_bytes(b"RIFF")

    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(root))
    get_settings.cache_clear()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        resp = await client.post("/api/voice/profiles", json={
            "name": "自定义",
            "gpt_weights_path": str(weights / "a.ckpt"),
            "sovits_weights_path": str(sovits / "a.pth"),
            "ref_audio_path": str(root / "audio" / "ref.wav"),
            "prompt_text": "こんにちは",
            "prompt_lang": "ja",
        })
        assert resp.status_code == 201, resp.text
        profile_id = resp.json()["id"]
        resp = await client.put(f"/api/voice/profiles/{profile_id}", json={
            "gpt_weights_path": str(weights / "b.ckpt"),
            "sovits_weights_path": str(sovits / "b.pth"),
        })
        assert resp.status_code == 200, resp.text
        async with get_db() as conn:
            profile = await crud.get_voice_profile(conn, profile_id)

    assert profile.gpt_weights_path == "GPT-SoVITS/GPT_weights_v2/b.ckpt"
    assert profile.sovits_weights_path == "GPT-SoVITS/SoVITS_weights_v2/b.pth"
