"""Redeem-code client against a real g2v_dist server and the import API."""

from __future__ import annotations

import hashlib
import io
import json
import logging
from pathlib import Path
import secrets
import sqlite3
import threading
import wave
import zipfile

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.database.session import init_db
from galgame2voice.main import create_app
from galgame2voice.services.character_manager import CharacterManager
from galgame2voice.services.package_redeem import RedeemCodeError, parse_redeem_code
from scripts.g2v_dist.g2v_dist import DistRequestHandler, create_server, encode_code, parse_code


def _package(path: Path, folder: str, name: str) -> None:
    wav = io.BytesIO()
    with wave.open(wav, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x01\x00" * 8000 * 4)
    manifest = {
        "id": folder,
        "name": name,
        "system_prompt": f"你是{name}",
        "gpt_weights": "gpt.ckpt",
        "sovits_weights": "sovits.pth",
        "emotions": {"gentle": {"audio": "refs/gentle.wav", "text": f"{name}の優しい声。", "lang": "ja"}},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{folder}/manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr(f"{folder}/gpt.ckpt", b"x" * 1024)
        zf.writestr(f"{folder}/sovits.pth", b"y" * 1024)
        zf.writestr(f"{folder}/refs/gentle.wav", wav.getvalue())
    path.write_bytes(buf.getvalue())


@pytest.fixture()
def dist_env(tmp_path: Path):
    state = tmp_path / "state"
    httpd = create_server(state, host="127.0.0.1", port=0, prefix="/_g2v")
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield {"store": httpd.store, "state": state, "base": f"http://{host}:{port}/_g2v", "tmp": tmp_path}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


@pytest.fixture()
async def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path / "proj"))
    get_settings.cache_clear()
    CharacterManager.reset_instance()
    app = create_app()
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        yield client
    CharacterManager.reset_instance()
    get_settings.cache_clear()


def _publish(env: dict, folder: str, name: str, pkg_id: str, *, downloads: int = 3) -> str:
    path = env["tmp"] / f"{pkg_id}.zip"
    _package(path, folder, name)
    env["store"].add_package(path, package_id=pkg_id)
    code, _cid = env["store"].create_code(server_url=env["base"], packages=[pkg_id], max_downloads=downloads)
    return code


def _no_parts() -> None:
    root = get_settings().data_dir / "character_import"
    assert not root.exists() or list(root.glob("*.part")) == []


def test_parse_matches_dist_and_rejects_public_http() -> None:
    secret = secrets.token_bytes(32)
    for url in (
        "https://example.com/_g2v/",
        "http://127.0.0.1:9/_g2v",
        "http://localhost:9/_g2v",
        "http://[::1]:9/_g2v",
    ):
        code = encode_code(url, secret)
        assert parse_redeem_code(f" \n{code}\r ") == parse_code(code)
    with pytest.raises(RedeemCodeError, match="仅本机回环地址允许 http://"):
        parse_redeem_code(encode_code("http://example.com/_g2v", secret))


async def test_list_download_import_and_revoked_code(
    api: AsyncClient, dist_env: dict, caplog: pytest.LogCaptureFixture
) -> None:
    code = _publish(dist_env, "alice", "爱丽丝", "alice")
    caplog.set_level(logging.INFO, logger="galgame2voice.services.package_redeem")
    listed = await api.post("/api/characters/redeem/list", json={"code": code})
    assert listed.status_code == 200, listed.text
    pkg = listed.json()["packages"][0]
    assert pkg["id"] == "alice" and pkg["name"] == "爱丽丝" and pkg["remaining"] == 3
    assert pkg["sha256"] == hashlib.sha256((dist_env["state"] / "packages" / "alice.zip").read_bytes()).hexdigest()
    saved = await api.post(
        "/api/characters/redeem/download", json={"code": code, "package_id": "alice", "overwrite": False}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["imported"][0]["name"] == "爱丽丝"
    assert (get_settings().characters_dir / "alice" / "manifest.json").is_file()
    progress = await api.get("/api/characters/redeem/progress")
    assert progress.json() == {"phase": "idle", "done": 0, "total": 0, "name": ""}
    _no_parts()
    assert code not in caplog.text and parse_code(code).code_secret not in caplog.text
    revoked, cid = dist_env["store"].create_code(server_url=dist_env["base"], packages=["alice"], max_downloads=1)
    dist_env["store"].revoke_code(cid)
    denied = await api.post("/api/characters/redeem/list", json={"code": revoked})
    assert denied.status_code == 400 and denied.json()["detail"] == "兑换码无效或已过期"


async def test_sha256_mismatch_deletes_partial_and_imports_nothing(api: AsyncClient, dist_env: dict) -> None:
    code = _publish(dist_env, "badpack", "坏包", "bad")
    stored = dist_env["state"] / "packages" / "bad.zip"
    stored.write_bytes(stored.read_bytes() + b"x")
    denied = await api.post(
        "/api/characters/redeem/download", json={"code": code, "package_id": "bad", "overwrite": False}
    )
    assert denied.status_code == 502 and denied.json()["detail"] == "下载的文件校验失败，请重试"
    assert not (get_settings().characters_dir / "badpack").exists()
    _no_parts()


async def test_download_resumes_once_with_range(
    api: AsyncClient, dist_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = _publish(dist_env, "resume", "续传", "resume", downloads=1)
    orig, seen = httpx.Response.aiter_bytes, {"n": 0, "ranges": []}

    async def aiter_bytes(self, chunk_size=None):
        if "/v1/packages/" not in str(self.request.url):
            async for chunk in orig(self, chunk_size):
                yield chunk
            return
        seen["n"] += 1
        seen["ranges"].append(self.request.headers.get("range"))
        first = seen["n"] == 1
        async for chunk in orig(self, chunk_size):
            if first:
                yield chunk[:64]
                raise httpx.RemoteProtocolError("drop")
            yield chunk

    monkeypatch.setattr(httpx.Response, "aiter_bytes", aiter_bytes)
    saved = await api.post(
        "/api/characters/redeem/download", json={"code": code, "package_id": "resume", "overwrite": False}
    )
    assert saved.status_code == 200, saved.text
    assert (get_settings().characters_dir / "resume" / "manifest.json").is_file()
    assert seen["ranges"][0] in (None, "") and seen["ranges"][1] == "bytes=64-"
    _no_parts()


async def test_overwrite_reuses_verified_download(
    api: AsyncClient, dist_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = _publish(dist_env, "alice", "爱丽丝", "once", downloads=1)
    payload = (dist_env["state"] / "packages" / "once.zip").read_bytes()
    installed = await api.post("/api/characters/import", params={"filename": "once.zip"}, content=payload)
    assert installed.status_code == 200, installed.text
    gets = {"n": 0}
    original = DistRequestHandler.do_GET

    def counting_get(self):
        if "/v1/packages/" in (self.path or ""):
            gets["n"] += 1
        return original(self)

    monkeypatch.setattr(DistRequestHandler, "do_GET", counting_get)
    body = {"code": code, "package_id": "once", "overwrite": False}
    first = await api.post("/api/characters/redeem/download", json=body)
    assert first.status_code == 409, first.text
    assert first.json()["code"] == "package_exists"
    second = await api.post("/api/characters/redeem/download", json={**body, "overwrite": True})
    assert second.status_code == 200, second.text
    assert second.json()["imported"][0]["name"] == "爱丽丝"
    assert gets["n"] == 1
    with sqlite3.connect(dist_env["state"] / "dist.db") as db:
        count = db.execute("SELECT SUM(download_count) FROM downloads").fetchone()[0]
    assert count == 1
    assert (get_settings().characters_dir / "alice" / "manifest.json").is_file()
    _no_parts()
