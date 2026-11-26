"""Focused integration and security tests for POST /api/characters/import."""

from __future__ import annotations

import errno
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
    include_weights: bool = True,
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
    asset_entries: dict[str, bytes] = {}
    if valid:
        asset_entries[f"{prefix}refs/gentle.wav"] = wav_buf.getvalue()
        if include_weights:
            asset_entries[f"{prefix}gpt.ckpt"] = b"x" * 1024
            asset_entries[f"{prefix}sovits.pth"] = b"y" * 1024
    entries: dict[str, bytes] = {
        f"{prefix}manifest.json": json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
        **asset_entries,
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
async def test_update_under_another_file_name_asks_to_overwrite_the_installed_character(tmp_path, monkeypatch):
    """A browser saves a second download as 「爱丽丝 (1).zip」; that is still an update of 爱丽丝."""
    root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()
    v1 = _make_package_bytes("zip", "alice", "爱丽丝", system_prompt="第一版人设")
    v2 = _make_package_bytes("zip", "alice", "爱丽丝", system_prompt="第二版人设")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        first = await client.post("/api/characters/import", params={"filename": "爱丽丝.zip"}, content=v1)
        assert first.status_code == 200, first.text
        asked = await client.post("/api/characters/import", params={"filename": "爱丽丝 (1).zip"}, content=v2)
        assert asked.status_code == 409, asked.text
        assert asked.json()["code"] == "package_exists" and asked.json()["names"] == ["爱丽丝"]
        replaced = await client.post(
            "/api/characters/import", params={"filename": "爱丽丝 (1).zip", "overwrite": "1"}, content=v2
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["imported"][0]["profile_id"] == first.json()["imported"][0]["profile_id"]
    assert sorted(p.name for p in (root / "characters").iterdir()) == ["爱丽丝"]
    manifest = json.loads((root / "characters" / "爱丽丝" / "manifest.json").read_text("utf-8"))
    assert manifest["system_prompt"] == "第二版人设"
    _assert_temp_empty()


@pytest.mark.asyncio
async def test_update_under_another_file_name_replaces_invalid_package(tmp_path, monkeypatch):
    root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()
    broken_dir = root / "characters" / "爱丽丝"
    broken_dir.mkdir(parents=True)
    (broken_dir / "manifest.json").write_text(json.dumps({"id": "alice", "name": "爱丽丝"}), encoding="utf-8")
    CharacterManager.get_instance().discover_characters()
    fixed = _make_package_bytes("zip", "alice", "爱丽丝", system_prompt="修复版人设")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        asked = await client.post("/api/characters/import", params={"filename": "爱丽丝 (1).zip"}, content=fixed)
        assert asked.status_code == 409 and asked.json()["names"] == ["爱丽丝"]
        replaced = await client.post(
            "/api/characters/import", params={"filename": "爱丽丝 (1).zip", "overwrite": "1"}, content=fixed
        )
        assert replaced.status_code == 200, replaced.text
    assert sorted(p.name for p in (root / "characters").iterdir()) == ["爱丽丝"]
    assert json.loads((broken_dir / "manifest.json").read_text("utf-8"))["system_prompt"] == "修复版人设"


@pytest.mark.asyncio
async def test_update_follows_disk_when_folder_changed_without_rescan(tmp_path, monkeypatch):
    root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()
    v1 = _make_package_bytes("zip", "alice", "爱丽丝")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        assert (await client.post("/api/characters/import", params={"filename": "爱丽丝.zip"}, content=v1)).status_code == 200
        manifest_path = root / "characters" / "爱丽丝" / "manifest.json"
        data = json.loads(manifest_path.read_text("utf-8"))
        manifest_path.write_text(json.dumps({**data, "id": "bob", "name": "鲍勃"}), encoding="utf-8")
        res = await client.post("/api/characters/import", params={"filename": "alice_v2.zip"}, content=v1)
        assert res.status_code == 200, res.text
    assert sorted(p.name for p in (root / "characters").iterdir()) == ["alice_v2", "爱丽丝"]
    assert json.loads(manifest_path.read_text("utf-8"))["id"] == "bob"


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
async def test_import_without_voice_weights_succeeds_and_marks_them_missing(tmp_path, monkeypatch):
    """A package whose manifest names weights, but does not contain the files, still imports.

    The import response must say those weights are not usable. A package that
    actually contains both files, and one that contains only the GPT file, use
    the same flag.
    """
    root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()
    weightless = _make_package_bytes(
        "zip", "walk_noweight", "缺权重小灯", prefix="walk_noweight/", include_weights=False,
    )
    ready = _make_package_bytes("zip", "walk_alice", "星野爱丽丝", prefix="walk_alice/")
    gpt_only = _make_package_bytes(
        "zip", "walk_gpt_only", "只有一半", prefix="walk_gpt_only/", include_weights=False,
        extra_entries={"walk_gpt_only/gpt.ckpt": b"x" * 1024},
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        missing_res = await client.post(
            "/api/characters/import", params={"filename": "walk_noweight.zip"}, content=weightless,
        )
        ready_res = await client.post(
            "/api/characters/import", params={"filename": "walk_alice.zip"}, content=ready,
        )
        half_res = await client.post(
            "/api/characters/import", params={"filename": "walk_gpt_only.zip"}, content=gpt_only,
        )
        profiles = (await client.get("/api/voice/profiles")).json()["profiles"]
    assert missing_res.status_code == 200, missing_res.text
    assert ready_res.status_code == 200, ready_res.text
    assert half_res.status_code == 200, half_res.text
    missing_item = missing_res.json()["imported"][0]
    assert missing_item["name"] == "缺权重小灯"
    assert missing_item["missing_voice_weights"] is True
    pkg_dir = root / "characters" / "walk_noweight"
    assert (pkg_dir / "manifest.json").is_file()
    assert (pkg_dir / "refs" / "gentle.wav").is_file()
    assert not (pkg_dir / "gpt.ckpt").exists()
    assert not (pkg_dir / "sovits.pth").exists()
    assert ready_res.json()["imported"][0]["missing_voice_weights"] is False
    assert half_res.json()["imported"][0]["missing_voice_weights"] is True
    assert [p["name"] for p in profiles] == ["缺权重小灯", "星野爱丽丝", "只有一半"]
    _assert_temp_empty()


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


@pytest.mark.asyncio
async def test_import_corrupt_deflate_zip_returns_400_chinese(tmp_path, monkeypatch):
    """A .zip with a valid central directory but corrupted DEFLATE stream must return 400 in Chinese."""
    _root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", b"{\"id\": \"alice\", \"name\": \"Alice\"}" * 20)
    corrupted = bytearray(buf.getvalue())
    # Corrupt compressed payload right after the 30-byte local header + filename
    corrupted[43:53] = b"\xff" * 10

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        res = await client.post("/api/characters/import", params={"filename": "corrupt.zip"}, content=bytes(corrupted))

    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert "压缩包已损坏" in detail
    assert "decompressing" not in detail.lower()
    _assert_temp_empty()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "entry", "manifest_text", "expected"),
    [
        ("bad_schema.zip", "bad_schema/manifest.json", '{"emotions": ["bad"]}', ("缺少 id、name", "emotions 的格式不对")),
        ("bad_json.zip", "bad_json/manifest.json", '{\n  "id": "x",\n  invalid\n}', ("manifest.json 不是有效的 JSON（第 3 行附近）",)),
        (
            "cafe_bg.zip",
            "cafe/manifest.json",
            '{"version": 1, "work": "cafe", "backgrounds": [{"id": "room", "name": "房间", "variants": {}}]}',
            ("这是背景包，不是角色包", "backgrounds"),
        ),
    ],
    ids=["schema_error", "json_syntax_error", "background_pack"],
)
async def test_import_reports_bad_manifest_and_background_pack_in_chinese(
    tmp_path, monkeypatch, filename, entry, manifest_text, expected
):
    root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(entry, manifest_text.encode("utf-8"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        res = await client.post("/api/characters/import", params={"filename": filename}, content=buf.getvalue())
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    for phrase in expected:
        assert phrase in detail
    for leak in ("validation error", "Field required", "Expecting", "JSONDecodeError", "http://", "https://"):
        assert leak.lower() not in detail.lower()
    assert not (root / "characters").exists() or list((root / "characters").iterdir()) == []
    _assert_temp_empty()


@pytest.mark.asyncio
@pytest.mark.parametrize(("exc", "code", "msg"), [
    (OSError(errno.ENOSPC, "No space left on device"), 507, "磁盘空间不足"),
    (PermissionError(errno.EACCES, "Permission denied"), 403, "数据目录没有写入权限，角色包未能导入。请把程序文件夹移到有写入权限的位置后重试。"),
])
async def test_import_extract_disk_and_permission_errors(tmp_path, monkeypatch, exc, code, msg):
    _root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()
    real_open = Path.open
    def fake_open(self, *a, **kw):
        if "extracted" in self.parts:
            raise exc
        return real_open(self, *a, **kw)
    monkeypatch.setattr(Path, "open", fake_open)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        res = await client.post("/api/characters/import", params={"filename": "a.zip"}, content=_make_package_bytes("zip", "a", "甲"))
    assert res.status_code == code and msg in res.json()["detail"]
    _assert_temp_empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["zip", "tar"])
async def test_import_skips_files_macos_and_windows_add(tmp_path, monkeypatch, fmt):
    """Finder's __MACOSX folder, .DS_Store and "Icon\\r" are not package files.

    "Icon\\r" (added when a folder has a custom icon) used to fail the whole
    import as an illegal path, and the rest were copied into the package.
    """
    root, app = _setup_app(tmp_path, monkeypatch)
    await init_db()
    junk = {
        "__MACOSX/._manifest.json": b"\x00\x05\x16\x07",
        "__MACOSX/refs/._gentle.wav": b"\x00\x05\x16\x07",
        ".DS_Store": b"Bud1",
        "refs/.DS_Store": b"Bud1",
        "Icon\r": b"",
        "Thumbs.db": b"\xd0\xcf",
    }
    content = _make_package_bytes(fmt, "mika", "美嘉", extra_entries=junk)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        res = await client.post("/api/characters/import", params={"filename": f"mika.{fmt}"}, content=content)
    assert res.status_code == 200, res.text
    installed = root / "characters" / "mika"
    names = sorted(p.relative_to(installed).as_posix() for p in installed.rglob("*"))
    assert names == ["gpt.ckpt", "manifest.json", "refs", "refs/gentle.wav", "sovits.pth"]
    _assert_temp_empty()
