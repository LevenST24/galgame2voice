"""Streaming character package archive (.zip / .tar) importer for /api/characters/import."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from typing import Any, Callable, IO, Iterable
import zipfile

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from galgame2voice.config import get_settings
from galgame2voice.database.session import get_db
from galgame2voice.services.character_identity import DuplicateCharacterId
from galgame2voice.services.character_manager import (
    CharacterPackage,
    format_invalid_package_warning,
    get_character_manager,
)
from galgame2voice.services.character_voice_catalog import get_character_voice_catalog
from galgame2voice.utils.async_tasks import await_protected
from galgame2voice.utils.logger import sanitize_error_detail

MAX_IMPORT_BYTES = 8 * 1024 * 1024 * 1024  # 8 GiB
STREAM_WRITE_BUFFER_BYTES = 1024 * 1024  # 1 MiB
EXTRACT_COPY_CHUNK_BYTES = 256 * 1024  # 256 KiB
MAX_PACKAGE_DISCOVERY_DEPTH = 3

NO_PACKAGE_FOUND_DETAIL = "没有找到角色包：需要包含 manifest.json 的文件夹"
SIZE_LIMIT_EXCEEDED_DETAIL = "角色包文件过大（上限 8 GiB），请精简无关文件后重试。"

_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
_ILLEGAL_FOLDER_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


class _ImportValidationError(Exception):
    pass


def _rmtree_force(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return

    def _clear_ro(func: Any, target: str, _exc: Any) -> None:
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            func(target)
        except OSError:
            pass

    kw = {"onexc": _clear_ro} if sys.version_info >= (3, 12) else {"onerror": _clear_ro}
    shutil.rmtree(path, **kw)


def detect_archive_format(archive_path: Path) -> str:
    """Detects 'zip' or 'tar' strictly from leading magic bytes."""
    with archive_path.open("rb") as fh:
        header = fh.read(512)
    if header.startswith(b"PK"):
        return "zip"
    if header.startswith(b"7z\xbc\xaf\x27\x1c"):
        raise HTTPException(400, "暂不支持 .7z 压缩包，请先解压后直接选择或拖入文件夹，或改用 .zip 格式。")
    if header.startswith(b"Rar!\x1a\x07"):
        raise HTTPException(400, "暂不支持 .rar 压缩包，请先解压后直接选择或拖入文件夹，或改用 .zip 格式。")
    if len(header) >= 262 and header[257:262] == b"ustar":
        return "tar"
    raise HTTPException(400, "不支持的文件格式：请选择角色包 .zip 压缩包或直接选择角色包文件夹。")


def _decode_zip_member_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def _resolve_safe_extract_path(member_name: str, extract_root: Path) -> Path | None:
    if not member_name:
        return None
    if (
        "\x00" in member_name
        or any(ord(c) < 32 for c in member_name)
        or member_name.startswith(("/", "\\"))
        or ":" in member_name
    ):
        raise HTTPException(400, "压缩包内包含非法或绝对路径，已拒绝导入。")
    parts: list[str] = []
    for part in member_name.replace("\\", "/").split("/"):
        if not part or part == ".":
            continue
        if part == ".." or part.endswith((" ", ".")) or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise HTTPException(400, f"压缩包内包含不安全路径「{part}」，已拒绝导入。")
        parts.append(part)
    if not parts:
        return None
    root = extract_root.resolve()
    dest = (root / Path(*parts)).resolve()
    if dest == root or not dest.is_relative_to(root):
        raise HTTPException(400, "压缩包内包含越界路径，已拒绝导入。")
    return dest


def _extract_members(
    members: Iterable[tuple[str, bool, Callable[[], IO[bytes] | None] | None]],
    extract_dir: Path,
    max_bytes: int,
) -> None:
    actual_total = 0
    for member_name, is_dir, open_src in members:
        dest = _resolve_safe_extract_path(member_name, extract_dir)
        if dest is None:
            continue
        if is_dir:
            dest.mkdir(parents=True, exist_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = open_src() if open_src else None
        if src is None:
            dest.write_bytes(b"")
            continue
        with src, dest.open("wb") as out:
            while chunk := src.read(EXTRACT_COPY_CHUNK_BYTES):
                actual_total += len(chunk)
                if actual_total > max_bytes:
                    raise HTTPException(413, SIZE_LIMIT_EXCEEDED_DETAIL)
                out.write(chunk)


def extract_archive_safely(
    archive_path: Path, fmt: str, extract_dir: Path, *, max_bytes: int = MAX_IMPORT_BYTES
) -> None:
    extract_dir.mkdir(parents=True, exist_ok=True)
    if fmt == "zip":
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:

                def _zip_members() -> Iterable[tuple[str, bool, Callable[[], IO[bytes] | None] | None]]:
                    for info in zf.infolist():
                        mode = (info.external_attr >> 16) & 0xFFFF
                        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                            raise HTTPException(400, "压缩包内包含符号链接或特殊文件，已拒绝导入。")
                        name = _decode_zip_member_name(info)
                        is_dir = info.is_dir() or name.endswith(("/", "\\"))
                        yield name, is_dir, None if is_dir else (lambda i=info: zf.open(i, "r"))

                _extract_members(_zip_members(), extract_dir, max_bytes)
        except HTTPException:
            raise
        except (zipfile.BadZipFile, RuntimeError, OSError, EOFError) as exc:
            msg = (
                "压缩包设有密码，请先解压后再导入角色包文件夹。"
                if "encrypted" in str(exc).lower()
                else "压缩包已损坏或不是有效的 .zip 文件。"
            )
            raise HTTPException(400, msg) from exc
        return

    try:
        with tarfile.open(archive_path, mode="r:", encoding="utf-8") as tf:

            def _tar_members() -> Iterable[tuple[str, bool, Callable[[], IO[bytes] | None] | None]]:
                for m in tf:
                    if not (m.isfile() or m.isdir()):
                        raise HTTPException(400, "归档内包含符号链接或特殊文件，已拒绝导入。")
                    yield m.name, m.isdir(), None if m.isdir() else (lambda member=m: tf.extractfile(member))

            _extract_members(_tar_members(), extract_dir, max_bytes)
    except HTTPException:
        raise
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        raise HTTPException(400, "读取文件夹归档失败，请重新选择角色包后重试。") from exc


def _resolve_folder_name(raw_name: str, pkg_dir: Path | None = None) -> str:
    base = (raw_name or "").replace("\\", "/").strip("/").rsplit("/", 1)[-1].strip()
    if pkg_dir is not None:
        for ext in (".tar.gz", ".zip", ".tar", ".tgz"):
            if base.lower().endswith(ext):
                base = base[: -len(ext)]
                break
        if not base.strip(". "):
            try:
                raw = json.loads((pkg_dir / "manifest.json").read_text(encoding="utf-8-sig"))
                base = next(
                    (
                        raw[k]
                        for k in ("id", "name")
                        if isinstance(raw, dict) and isinstance(raw.get(k), str) and raw[k].strip()
                    ),
                    "",
                )
            except Exception:
                base = ""
    cleaned = _ILLEGAL_FOLDER_CHARS_RE.sub("_", base.strip()).strip(". ")[:100].rstrip(". ")
    if not cleaned or cleaned in {".", ".."}:
        return "character_package"
    return f"_{cleaned}" if cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES else cleaned


def _is_portrait_manifest(cur_dir: Path, manifest_file: Path, depth: int) -> bool:
    if depth == 0 or cur_dir.name.lower() != "portrait":
        return False
    try:
        raw = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
        return isinstance(raw, dict) and ("sprites" in raw or "costumes" in raw) and "emotions" not in raw
    except Exception:
        return False


def discover_extracted_packages(extract_dir: Path, *, filename: str = "") -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []

    def _walk(cur_dir: Path, depth: int) -> None:
        manifest_file = cur_dir / "manifest.json"
        if manifest_file.is_file() and not _is_portrait_manifest(cur_dir, manifest_file, depth):
            found.append(
                (_resolve_folder_name(filename, cur_dir) if depth == 0 else _resolve_folder_name(cur_dir.name), cur_dir)
            )
            return
        if depth >= MAX_PACKAGE_DISCOVERY_DEPTH:
            return
        for child in sorted(cur_dir.iterdir(), key=lambda p: p.name):
            if child.is_dir() and not child.is_symlink() and child.name != "__MACOSX":
                _walk(child, depth + 1)

    _walk(extract_dir, 0)
    if not found:
        raise HTTPException(422, NO_PACKAGE_FOUND_DETAIL)
    seen: set[str] = set()
    for folder_name, _ in found:
        if folder_name in seen:
            raise HTTPException(422, f"导入内容中包含多个同名角色包文件夹「{folder_name}」，请重命名后重试。")
        seen.add(folder_name)
    return found


async def _stream_request_to_file(request: Request, archive_path: Path, max_bytes: int = MAX_IMPORT_BYTES) -> None:
    raw_cl = (request.headers.get("content-length") or "").strip()
    if raw_cl.isdigit() and int(raw_cl) > max_bytes:
        raise HTTPException(413, SIZE_LIMIT_EXCEEDED_DETAIL)
    total, buf = 0, bytearray()
    fh = await asyncio.to_thread(archive_path.open, "wb")
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(413, SIZE_LIMIT_EXCEEDED_DETAIL)
            buf.extend(chunk)
            if len(buf) >= STREAM_WRITE_BUFFER_BYTES:
                await asyncio.to_thread(fh.write, bytes(buf))
                buf.clear()
        if buf:
            await asyncio.to_thread(fh.write, bytes(buf))
    finally:
        await asyncio.to_thread(fh.close)
    if total == 0:
        raise HTTPException(400, "未收到角色包数据，请选择 .zip 压缩包或角色包文件夹。")


def _place_packages(
    staged: list[tuple[str, Path]],
    chars_dir: Path,
    backup_root: Path,
    placed: list[Path],
    backups: list[tuple[Path, Path]],
) -> None:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    for folder_name, src_dir in staged:
        target = chars_dir / folder_name
        if target.exists():
            backup_root.mkdir(parents=True, exist_ok=True)
            # One backup per package: repeated overwrites of multi-GB packages would otherwise fill the disk unseen.
            older = re.compile(rf"{re.escape(folder_name)}-\d{{8}}-\d{{6}}")
            for old in backup_root.iterdir():
                if old.is_dir() and older.fullmatch(old.name):
                    _rmtree_force(old)
            backup = backup_root / f"{folder_name}-{ts}"
            shutil.move(str(target), str(backup))
            backups.append((target, backup))
        shutil.move(str(src_dir), str(target))
        placed.append(target)


async def _rollback_and_rescan(
    placed: list[Path], backups: list[tuple[Path, Path]], catalog: Any, char_mgr: Any
) -> None:
    def _undo_fs() -> None:
        for target in placed:
            _rmtree_force(target)
        for orig_target, backup in reversed(backups):
            if backup.exists():
                _rmtree_force(orig_target)
                shutil.move(str(backup), str(orig_target))

    await asyncio.to_thread(_undo_fs)
    try:
        await catalog.rescan_packages_unlocked(character_manager=char_mgr, db_factory=get_db)
    except DuplicateCharacterId:
        pass


def _validate_placed_packages(mgr: Any, placed: list[Path], out_pkgs: list[CharacterPackage]) -> None:
    with mgr._catalog_lock:
        by_folder = {pkg.folder.resolve(): pkg for pkg in mgr._packages.values()}
    resolved, invalid = [], []
    for target in placed:
        pkg = by_folder.get(target.resolve())
        if pkg is None:
            raise _ImportValidationError(f"角色包「{target.name}」缺少有效的 manifest.json。")
        resolved.append(pkg)
        if not pkg.is_valid:
            invalid.append(pkg)
    if invalid:
        raise _ImportValidationError(format_invalid_package_warning(invalid) or "导入的角色包不合格。")
    for pkg in resolved:
        if mgr.get_character(pkg.name) is not pkg:
            raise _ImportValidationError(
                f"角色包「{pkg.folder.name}」的角色名「{pkg.name}」与已有角色冲突，请修改 manifest.json 中的 name 或覆盖原角色包。"
            )
    out_pkgs.clear()
    out_pkgs.extend(resolved)


async def import_character_archive_file(
    request: Request, archive_path: Path, *, filename: str = "", overwrite: bool = False
) -> dict[str, Any] | JSONResponse:
    """Safely unpacks, validates, and installs character package(s) from a local archive file."""
    settings = get_settings()
    temp_root = settings.data_dir / "character_import"
    await asyncio.to_thread(temp_root.mkdir, parents=True, exist_ok=True)
    staging_dir = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="imp-", dir=str(temp_root)))

    try:
        def _prepare() -> list[tuple[str, Path]]:
            fmt = detect_archive_format(archive_path)
            extract_archive_safely(archive_path, fmt, staging_dir / "extracted", max_bytes=MAX_IMPORT_BYTES)
            archive_path.unlink(missing_ok=True)
            return discover_extracted_packages(staging_dir / "extracted", filename=filename)

        staged = await asyncio.to_thread(_prepare)
        lock = getattr(request.app.state, "character_rescan_lock", None)
        if lock is None:
            lock = request.app.state.character_rescan_lock = asyncio.Lock()

        char_mgr, catalog = get_character_manager(), get_character_voice_catalog()
        async with lock:
            chars_dir = char_mgr.characters_dir
            await asyncio.to_thread(chars_dir.mkdir, parents=True, exist_ok=True)
            existing = [name for name, _ in staged if (chars_dir / name).exists()]
            if existing and not overwrite:
                return JSONResponse(
                    status_code=status.HTTP_409_CONFLICT,
                    content={
                        "code": "package_exists",
                        "names": existing,
                        "detail": f"已有同名角色包：{'、'.join(existing)}",
                    },
                )

            placed: list[Path] = []
            backups: list[tuple[Path, Path]] = []
            try:
                await asyncio.to_thread(
                    _place_packages, staged, chars_dir, settings.data_dir / "replaced_characters", placed, backups
                )
            except BaseException as exc:
                await await_protected(_rollback_and_rescan(placed, backups, catalog, char_mgr))
                if not isinstance(exc, Exception):
                    raise
                raise HTTPException(500, f"移动角色包失败：{sanitize_error_detail(exc)}") from exc

            imported_pkgs: list[CharacterPackage] = []
            try:
                rescan_res = await catalog.rescan_packages_unlocked(
                    character_manager=char_mgr,
                    db_factory=get_db,
                    validate_before_sync=lambda mgr, _: _validate_placed_packages(mgr, placed, imported_pkgs),
                )
                imported_items: list[dict[str, Any]] = []
                async with get_db() as conn:
                    for pkg in imported_pkgs:
                        prof = await catalog.resolve_profile_by_name(conn, pkg.name, character_manager=char_mgr)
                        if prof is None:
                            raise _ImportValidationError(
                                f"角色包「{pkg.name}」未能同步为可用音色，请检查是否存在同名音色冲突。"
                            )
                        imported_items.append(
                            {"name": pkg.name, "folder": pkg.folder.name, "character_id": pkg.id, "profile_id": prof.id}
                        )
            except (_ImportValidationError, DuplicateCharacterId, HTTPException) as exc:
                await await_protected(_rollback_and_rescan(placed, backups, catalog, char_mgr))
                raise HTTPException(422, str(getattr(exc, "detail", exc))) from exc
            except BaseException:
                await await_protected(_rollback_and_rescan(placed, backups, catalog, char_mgr))
                raise

            return {
                "status": "ok",
                "imported": imported_items,
                "active_profile_id": rescan_res["active_profile_id"],
                "total_profiles": rescan_res["total_profiles"],
                "catalog_warning": rescan_res["catalog_warning"],
            }
    finally:
        await asyncio.to_thread(_rmtree_force, staging_dir)


async def import_character_archive(
    request: Request, *, filename: str = "", overwrite: bool = False
) -> dict[str, Any] | JSONResponse:
    """Streams, safely unpacks, validates, and installs character package(s)."""
    temp_root = get_settings().data_dir / "character_import"
    await asyncio.to_thread(temp_root.mkdir, parents=True, exist_ok=True)
    fd, raw_upload = await asyncio.to_thread(tempfile.mkstemp, prefix="up-", suffix=".bin", dir=str(temp_root))
    os.close(fd)
    upload_path = Path(raw_upload)
    try:
        await _stream_request_to_file(request, upload_path, MAX_IMPORT_BYTES)
        return await import_character_archive_file(request, upload_path, filename=filename, overwrite=overwrite)
    finally:
        await asyncio.to_thread(upload_path.unlink, missing_ok=True)
