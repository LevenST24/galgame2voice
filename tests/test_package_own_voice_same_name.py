"""A character package named like a voice the user made asks before taking that voice over."""

import io
import wave
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.database.session import init_db
from test_character_package_import import _make_package_bytes, _setup_app


def _own_voice_files(root: Path) -> dict[str, str]:
    own = root / "my"
    own.mkdir()
    (own / "g.ckpt").write_bytes(b"g" * 1024)
    (own / "s.pth").write_bytes(b"s" * 1024)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x02\x00" * 8000 * 5)
    (own / "ref.wav").write_bytes(buf.getvalue())
    return {
        "name": "爱丽丝",
        "gpt_weights_path": str(own / "g.ckpt"),
        "sovits_weights_path": str(own / "s.pth"),
        "ref_audio_path": str(own / "ref.wav"),
        "prompt_text": "自作の声です。",
        "system_prompt": "我自己写的人设",
    }


async def _create_own_voice(client, root) -> int:
    created = await client.post("/api/voice/profiles", json=_own_voice_files(root))
    assert created.status_code in (200, 201), created.text
    return created.json()["id"]


def _client(tmp_path, monkeypatch):
    _root, app = _setup_app(tmp_path, monkeypatch)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080")


async def _profiles(client) -> dict[int, dict]:
    return {p["id"]: p for p in (await client.get("/api/voice/profiles")).json()["profiles"]}


def _import(client, **params):
    return client.post(
        "/api/characters/import",
        params={"filename": "a.zip", **params},
        content=_make_package_bytes("zip", "alice", "爱丽丝", system_prompt="角色包的人设"),
    )


@pytest.mark.asyncio
async def test_import_asks_before_taking_over_an_own_voice_of_the_same_name(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        await init_db()
        own_id = await _create_own_voice(client, tmp_path)
        res = await _import(client)
        assert res.status_code == 409
        assert res.json()["code"] == "own_voice_same_name" and res.json()["names"] == ["爱丽丝"]
        assert not (tmp_path / "characters" / "alice").exists()
        own = (await _profiles(client))[own_id]
        assert own["name"] == "爱丽丝" and own["system_prompt"] == "我自己写的人设"


@pytest.mark.asyncio
async def test_keep_both_renames_the_own_voice_and_leaves_its_persona(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        await init_db()
        own_id = await _create_own_voice(client, tmp_path)
        res = await _import(client, same_name="keep_both")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["kept_own_voices"] == [{"name": "爱丽丝", "renamed_to": "爱丽丝（自建）"}]
        profiles = await _profiles(client)
        own = profiles[own_id]
        assert own["name"] == "爱丽丝（自建）"
        assert own["system_prompt"] == "我自己写的人设" and own["prompt_text"] == "自作の声です。"
        package_id = body["imported"][0]["profile_id"]
        assert package_id != own_id
        assert profiles[package_id]["name"] == "爱丽丝" and profiles[package_id]["system_prompt"] == "角色包的人设"


@pytest.mark.asyncio
async def test_replace_lets_the_package_take_over_the_own_voice(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        await init_db()
        own_id = await _create_own_voice(client, tmp_path)
        res = await _import(client, same_name="replace")
        assert res.status_code == 200, res.text
        assert res.json()["imported"][0]["profile_id"] == own_id
        assert (await _profiles(client))[own_id]["system_prompt"] == "角色包的人设"


@pytest.mark.asyncio
async def test_reimporting_an_installed_package_does_not_ask(tmp_path, monkeypatch):
    root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        assert (await _import(client)).status_code == 200
        res = await _import(client, overwrite="1")
        assert res.status_code == 200, res.text
