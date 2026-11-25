"""A package written on Windows may use "refs\\gentle.wav" in manifest.json.

The importer already turns backslashes in archive entry names into folders,
but the manifest's own paths were joined as-is, so on Linux and Docker the
package was rejected with its reference audio reported missing.
"""

from __future__ import annotations

import io
import json
import wave
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.database.session import init_db
from galgame2voice.main import create_app
from galgame2voice.services.character_manager import CharacterManager


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x01\x00" * 8000 * 4)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_windows_style_paths_in_manifest_import_and_resolve(tmp_path, monkeypatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    CharacterManager.reset_instance()
    app = create_app()
    await init_db()
    manifest = {
        "id": "yuzu", "name": "柚子", "system_prompt": "你是柚子",
        "gpt_weights": "weights\\gpt.ckpt", "sovits_weights": "weights\\sovits.pth",
        "emotions": {"gentle": {"audio": "refs\\gentle.wav", "text": "優しい声。", "lang": "ja"}},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("yuzu/manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("yuzu/refs/gentle.wav", _wav())
        zf.writestr("yuzu/weights/gpt.ckpt", b"x" * 1024)
        zf.writestr("yuzu/weights/sovits.pth", b"y" * 1024)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        response = await client.post(
            "/api/characters/import", params={"filename": "yuzu.zip"}, content=buf.getvalue(),
        )
    assert response.status_code == 200, response.text

    package = CharacterManager.get_instance().get_character("yuzu")
    assert package.resolve_audio_path(package.manifest.emotions["gentle"].audio).is_file()
    for field in ("gpt_weights", "sovits_weights"):
        assert (tmp_path / package.resolve_weight_path(field)).is_file()
