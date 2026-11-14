"""Focused integration and security tests for POST /api/characters/import."""

from __future__ import annotations

import io
import json
from pathlib import Path
import tarfile
import wave
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient

from galgame2voice.config import get_settings
from galgame2voice.database.session import init_db
from galgame2voice.main import create_app
from galgame2voice.services import character_package_import as cpi
from galgame2voice.services.character_manager import CharacterManager


def _make_package_bytes(
    fmt: str,
    pkg_id: str,
    name: str,
    *,
    prefix: str = "",
    system_prompt: str | None = None,
    valid: bool = True,
    extra_entries: dict[str, bytes] | None = None,
) -> bytes:
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x01\x00" * 8000 * 4)
    manifest: dict[str, object] = {"id": pkg_id, "name": name, "system_prompt": system_prompt or f"你是{name}"}
    if valid:
        manifest.update(
            {
                "gpt_weights": "gpt.ckpt",
                "sovits_weights": "sovits.pth",
                "emotions": {"gentle": {"audio": "refs/gentle.wav", "text": f"{name}の優しい声。", "lang": "ja"}},
            }
        )
    entries: dict[str, bytes] = {
        f"{prefix}manifest.json": json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
        **(
            {
                f"{prefix}gpt.ckpt": b"x" * 1024,
                f"{prefix}sovits.pth": b"y" * 1024,
                f"{prefix}refs/gentle.wav": wav_buf.getvalue(),
            }
            if valid
            else {}
        ),
        **(extra_entries or {}),
    }
    buf = io.BytesIO()
    if fmt == "zip":
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path, content in entries.items():
                zf.writestr(path, content)
    else:
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
            for path, content in entries.items():
                info = tarfile.TarInfo(name=path)
                info.size, info.mode = len(content), 0o644
                tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _setup_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GALGAME2VOICE_PROJECT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    CharacterManager.reset_instance()
    return tmp_path, create_app()


def _assert_temp_empty() -> None:
    temp_dir = get_settings().data_dir / "character_import"
    assert not temp_dir.exists() or list(temp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_import_zip_root_nested_and_ustar_tar(tmp_path, monkeypatch):
    root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        r1 = await client.post(
            "/api/characters/import",
            params={"filename": "爱丽丝.zip"},
            content=_make_package_bytes("zip", "alice", "爱丽丝"),
        )
        r2 = await client.post(
            "/api/characters/import",
            params={"filename": "outer.zip"},
            content=_make_package_bytes("zip", "natsume", "四季夏目", prefix="natsume_pkg/"),
        )
        r3 = await client.post(
            "/api/characters/import",
            params={"filename": "yoshino"},
            content=_make_package_bytes("tar", "yoshino", "朝武芳乃", prefix="yoshino/"),
        )
        # Loose files dropped without their folder arrive with no name; the manifest id names the folder.
        r4 = await client.post("/api/characters/import", content=_make_package_bytes("tar", "loose", "散装角色"))
    for res, folder in ((r1, "爱丽丝"), (r2, "natsume_pkg"), (r3, "yoshino"), (r4, "loose")):
        assert res.status_code == 200, res.text
        item = res.json()["imported"][0]
        assert isinstance(item["profile_id"], int) and item["profile_id"] >= 1
        assert (root / "characters" / folder / "manifest.json").is_file()
    _assert_temp_empty()


@pytest.mark.asyncio
async def test_import_rejects_traversal_absolute_and_symlinks(tmp_path, monkeypatch):
    root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()

    sym_zip = io.BytesIO()
    with zipfile.ZipFile(sym_zip, "w") as zf:
        zi = zipfile.ZipInfo("evil_pkg/link")
        zi.create_system = 3
        zi.external_attr = 0o120777 << 16
        zf.writestr(zi, "../outside")

    sym_tar = io.BytesIO()
    with tarfile.open(fileobj=sym_tar, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        ti = tarfile.TarInfo("evil_pkg/link")
        ti.type, ti.linkname = tarfile.SYMTYPE, "../outside"
        tf.addfile(ti)

    payloads = [
        _make_package_bytes("zip", "evil", "坏包", extra_entries={"../escaped.txt": b"boom"}),
        _make_package_bytes("zip", "evil", "坏包", extra_entries={"/etc/passwd": b"boom"}),
        sym_zip.getvalue(),
        sym_tar.getvalue(),
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        for payload in payloads:
            res = await client.post("/api/characters/import", params={"filename": "evil.zip"}, content=payload)
            assert res.status_code == 400, res.text
    assert not (root / "characters").exists() or list((root / "characters").iterdir()) == []
    assert not (root / "escaped.txt").exists()
    _assert_temp_empty()


@pytest.mark.asyncio
async def test_invalid_package_rolls_back_leaving_existing_intact(tmp_path, monkeypatch):
    root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        ok_res = await client.post(
            "/api/characters/import",
            params={"filename": "alice.zip"},
            content=_make_package_bytes("zip", "alice", "爱丽丝", prefix="alice/"),
        )
        assert ok_res.status_code == 200
        bad_res = await client.post(
            "/api/characters/import",
            params={"filename": "broken.zip"},
            content=_make_package_bytes("zip", "broken", "坏包", prefix="broken/", valid=False),
        )
        assert bad_res.status_code == 422
        profiles = (await client.get("/api/voice/profiles")).json()["profiles"]
    assert not (root / "characters" / "broken").exists()
    assert (root / "characters" / "alice" / "manifest.json").is_file()
    assert [p["name"] for p in profiles] == ["爱丽丝"]
    _assert_temp_empty()


@pytest.mark.asyncio
async def test_conflict_409_overwrite_backup_and_failed_overwrite_restore(tmp_path, monkeypatch):
    root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()
    v1 = _make_package_bytes("zip", "natsume", "夏目", prefix="natsume/", system_prompt="第一版人设")
    v2 = _make_package_bytes("zip", "natsume", "夏目", prefix="natsume/", system_prompt="第二版人设")
    bad_v3 = _make_package_bytes("zip", "natsume", "夏目", prefix="natsume/", valid=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        assert (
            await client.post("/api/characters/import", params={"filename": "n.zip"}, content=v1)
        ).status_code == 200
        r409 = await client.post("/api/characters/import", params={"filename": "n.zip"}, content=v2)
        assert r409.status_code == 409
        assert r409.json()["code"] == "package_exists" and r409.json()["names"] == ["natsume"]

        r_ok = await client.post("/api/characters/import", params={"filename": "n.zip", "overwrite": "1"}, content=v2)
        assert r_ok.status_code == 200
        assert (
            json.loads((root / "characters" / "natsume" / "manifest.json").read_text("utf-8"))["system_prompt"]
            == "第二版人设"
        )
        backups = list((get_settings().data_dir / "replaced_characters").glob("natsume-*"))
        assert len(backups) == 1
        assert json.loads((backups[0] / "manifest.json").read_text("utf-8"))["system_prompt"] == "第一版人设"

        r_bad = await client.post(
            "/api/characters/import", params={"filename": "n.zip", "overwrite": "1"}, content=bad_v3
        )
        assert r_bad.status_code == 422
        assert (
            json.loads((root / "characters" / "natsume" / "manifest.json").read_text("utf-8"))["system_prompt"]
            == "第二版人设"
        )

        # Only the latest backup is kept, so repeated overwrites do not pile up copies of the package.
        v3 = _make_package_bytes("zip", "natsume", "夏目", prefix="natsume/", system_prompt="第三版人设")
        r_v3 = await client.post("/api/characters/import", params={"filename": "n.zip", "overwrite": "1"}, content=v3)
        assert r_v3.status_code == 200
        backups = list((get_settings().data_dir / "replaced_characters").glob("natsume-*"))
        assert len(backups) == 1
        assert json.loads((backups[0] / "manifest.json").read_text("utf-8"))["system_prompt"] == "第二版人设"
    _assert_temp_empty()


@pytest.mark.asyncio
async def test_missing_manifest_names_zip_and_folder(tmp_path, monkeypatch):
    _root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("readme.txt", b"no manifest")
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        info = tarfile.TarInfo("角色文件夹/readme.txt")
        info.size = 2
        tf.addfile(info, io.BytesIO(b"no"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        zipped = await client.post("/api/characters/import", params={"filename": "bad.zip"}, content=zip_buf.getvalue())
        folder = await client.post("/api/characters/import", params={"filename": "角色文件夹"}, content=tar_buf.getvalue())
    assert zipped.status_code == 422 and zipped.json()["detail"] == "这个压缩包里没有 manifest.json"
    folder_detail = folder.json()["detail"]
    assert folder.status_code == 422 and "文件夹" in folder_detail and "压缩包" not in folder_detail


@pytest.mark.asyncio
async def test_import_exceeding_size_limit_returns_413(tmp_path, monkeypatch):
    root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()
    monkeypatch.setattr(cpi, "MAX_IMPORT_BYTES", 512)
    payload = _make_package_bytes("zip", "alice", "爱丽丝")
    assert len(payload) > 512
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        res = await client.post("/api/characters/import", params={"filename": "alice.zip"}, content=payload)
    assert res.status_code == 413
    _assert_temp_empty()
