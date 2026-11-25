"""Packages zipped on macOS store names decomposed (NFD).

manifest.json is typed composed (NFC), and Windows and Linux treat the two
spellings as different files, so the import used to report the reference
audio as missing even though it was in the archive.
"""

from __future__ import annotations

import io
import json
import tarfile
import unicodedata
import wave
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.database.session import init_db
from galgame2voice.main import create_app
from galgame2voice.services.character_manager import CharacterManager

AUDIO = "refs/がっかり.wav"


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x01\x00" * 8000 * 4)
    return buf.getvalue()


def _mac_package(fmt: str) -> bytes:
    manifest = {
        "id": "yuzu", "name": "柚子", "system_prompt": "你是柚子",
        "gpt_weights": "gpt.ckpt", "sovits_weights": "sovits.pth",
        "emotions": {"gentle": {"audio": AUDIO, "text": "優しい声。", "lang": "ja"}},
    }
    entries = {
        "yuzu/manifest.json": json.dumps(manifest, ensure_ascii=False).encode(),
        unicodedata.normalize("NFD", f"yuzu/{AUDIO}"): _wav(),
        "yuzu/gpt.ckpt": b"x" * 1024,
        "yuzu/sovits.pth": b"y" * 1024,
    }
    buf = io.BytesIO()
    if fmt == "zip":
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
    else:
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
            for name, data in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["zip", "tar"])
async def test_package_with_decomposed_names_imports(tmp_path, monkeypatch, fmt):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    CharacterManager.reset_instance()
    app = create_app()
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        response = await client.post(
            "/api/characters/import", params={"filename": f"yuzu.{fmt}"}, content=_mac_package(fmt),
        )
    assert response.status_code == 200, response.text
    assert (tmp_path / "characters" / "yuzu" / AUDIO).is_file()
