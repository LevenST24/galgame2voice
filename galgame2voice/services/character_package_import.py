"""Streaming character package archive (.zip / .tar) importer for /api/characters/import."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import time
from typing import Any, Callable, IO, Iterable
import unicodedata
import zipfile
import zlib

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from galgame2voice.config import get_settings
from galgame2voice.database.crud_modules.voice_profiles import update_voice_profile
from galgame2voice.database.models import VoiceProfileUpdate
from galgame2voice.database.session import get_db
from galgame2voice.schemas.character_manifest import read_manifest_text
from galgame2voice.services.character_identity import AmbiguousProfileName, DuplicateCharacterId, identity_key, select_profile
from galgame2voice.services.character_manager import (
    CharacterManager,
    CharacterPackage,
    format_invalid_package_warning,
    get_character_manager,
)
from galgame2voice.services.character_voice_catalog import get_character_voice_catalog
from galgame2voice.utils.async_tasks import await_protected
from galgame2voice.utils.logger import describe_storage_error, sanitize_error_detail

MAX_IMPORT_BYTES = 8 * 1024 * 1024 * 1024  # 8 GiB
MAX_IMPORT_MEMBERS = 50000
STREAM_WRITE_BUFFER_BYTES = 1024 * 1024  # 1 MiB
EXTRACT_COPY_CHUNK_BYTES = 256 * 1024  # 256 KiB
MAX_PACKAGE_DISCOVERY_DEPTH = 3

NO_PACKAGE_FOUND_DETAIL = "没有找到角色包：需要包含 manifest.json 的文件夹"
SIZE_LIMIT_EXCEEDED_DETAIL = "角色包文件过大（上限 8 GiB），请精简无关文件后重试。"
MEMBER_LIMIT_EXCEEDED_DETAIL = "角色包里的文件太多（超过 50000 个），已停止导入。请确认选对了文件。"

logger = logging.getLogger("galgame2voice.services.character_package_import")


def _package_missing_voice_weights(pkg: CharacterPackage) -> bool:
    """True when GPT or SoVITS does not resolve to a file the catalog accepts."""
    missing = CharacterManager._weight_needs_healing
    return missing(pkg.resolve_weight_path("gpt_weights")) or missing(pkg.resolve_weight_path("sovits_weights"))


def _missing_package_detail(filename: str) -> str:
    name = filename.strip().lower()
    if name.endswith((".zip", ".tar", ".tgz", ".tar.gz")):
        return "这个压缩包里没有 manifest.json"
    return NO_PACKAGE_FOUND_DETAIL


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


# An upload that is still streaming keeps touching its file; a day without a
# change means the server that owned it stopped mid-import.
STALE_IMPORT_TEMP_SECONDS = 24 * 3600


def sweep_import_leftovers(data_dir: Path, *, older_than: float | None = STALE_IMPORT_TEMP_SECONDS) -> int:
    """Delete uploads (up-*.bin), redeem links (dl-*.zip.import), and staging folders (imp-*) a crash left behind.

    Packages can be gigabytes, and nothing else ever removes these. With
    older_than=None (server startup, when no import can be running) every
    leftover goes; otherwise only those untouched for that many seconds.
    """
    temp_root = data_dir / "character_import"
    try:
        entries = list(temp_root.iterdir())
    except OSError:
        return 0
    cutoff = None if older_than is None else time.time() - older_than
    removed = 0
    for entry in entries:
        if not (
            entry.name.startswith("imp-")
            or (entry.name.startswith("up-") and entry.name.endswith(".bin"))
            or (entry.name.startswith("dl-") and entry.name.endswith(".zip.import"))
        ):
            continue
        try:
            if cutoff is not None and entry.lstat().st_mtime >= cutoff:
                continue
            if entry.is_dir() and not entry.is_symlink():
                _rmtree_force(entry)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            logger.warning("Could not remove leftover import file %s: %s", entry.name, exc)
    return removed


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


# Names without the UTF-8 flag are in the zipping computer's code page. Simplified
# Chinese Windows (GBK) is the most common source, then Japanese Windows (cp932).
_LEGACY_ZIP_ENCODINGS = ("utf-8", "gb18030", "cp932")
_MAX_MANIFEST_PROBE_BYTES = 1024 * 1024
_MAX_MANIFEST_PROBE_COUNT = 8


def _raw_zip_member_name(info: zipfile.ZipInfo) -> bytes | None:
    """The stored bytes of a name without the UTF-8 flag, else None.

    orig_filename is used because on Windows zipfile turns every 0x5C byte of
    filename into "/", and 0x5C is the second byte of many GBK and cp932
    characters (黒, 運, ソ, 表 ...).
    """
    if info.flag_bits & 0x800:
        return None
    try:
        return info.orig_filename.encode("cp437").split(b"\0", 1)[0]
    except UnicodeEncodeError:
        return None


def _manifest_strings(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> set[str]:
    """Every string in a manifest.json member, with forward slashes."""
    if info.file_size > _MAX_MANIFEST_PROBE_BYTES:
        return set()
    try:
        with zf.open(info) as fh:
            raw = fh.read(_MAX_MANIFEST_PROBE_BYTES + 1)
        if len(raw) > _MAX_MANIFEST_PROBE_BYTES:
            return set()
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return set()
    found: set[str] = set()
    stack: list[Any] = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            found.add(unicodedata.normalize("NFC", item.replace("\\", "/")).strip("/"))
    return found


def _choose_zip_name_encoding(zf: zipfile.ZipFile) -> str:
    """Pick the one code page for the archive's unflagged names.

    UTF-8 wins when every name is valid UTF-8. Otherwise the candidate under
    which most names match the paths its manifest.json refers to is used,
    falling back to GBK, which decodes almost any byte string.
    """
    raw_names = [(info, raw) for info in zf.infolist() if (raw := _raw_zip_member_name(info)) is not None]
    if not any(any(b >= 0x80 for b in raw) for _, raw in raw_names):
        return "utf-8"
    decoded: dict[str, list[str]] = {}
    for enc in _LEGACY_ZIP_ENCODINGS:
        try:
            decoded[enc] = [raw.decode(enc) for _, raw in raw_names]
        except UnicodeDecodeError:
            continue
    if "utf-8" in decoded:
        return "utf-8"
    references: set[str] = set()
    probed = 0
    for info in zf.infolist():
        if info.filename.replace("\\", "/").rsplit("/", 1)[-1] == "manifest.json":
            if probed >= _MAX_MANIFEST_PROBE_COUNT:
                break
            probed += 1
            references |= _manifest_strings(zf, info)

    def matches(names: list[str]) -> int:
        count = 0
        for name in names:
            parts = unicodedata.normalize("NFC", name.replace("\\", "/")).strip("/").split("/")
            count += any("/".join(parts[i:]) in references for i in range(len(parts)))
        return count

    if "cp932" in decoded and matches(decoded["cp932"]) > matches(decoded.get("gb18030", [])):
        return "cp932"
    return "gb18030"


def _decode_zip_member_name(info: zipfile.ZipInfo, encoding: str = "gb18030") -> str:
    raw = _raw_zip_member_name(info)
    if raw is None:
        return info.filename
    # UTF-8 goes first for GBK archives: GBK decodes almost anything, so a
    # genuinely UTF-8 name in a mixed archive would otherwise be garbled.
    order = ("utf-8", encoding) if encoding == "gb18030" else (encoding, "utf-8")
    for enc in dict.fromkeys((*order, *_LEGACY_ZIP_ENCODINGS)):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def _resolve_safe_extract_path(member_name: str, extract_root: Path) -> Path | None:
    if not member_name:
        return None
    # macOS stores names decomposed (NFD) while manifest.json is typed composed
    # (NFC); Windows and Linux treat the two as different files.
    member_name = unicodedata.normalize("NFC", member_name)
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


# Files the operating system adds when a folder is zipped or browsed. They
# are never part of a package, and macOS's "Icon\r" would otherwise be
# rejected as an illegal name.
_OS_METADATA_NAMES = frozenset({".ds_store", "icon\r", "thumbs.db", "desktop.ini"})


def _is_os_metadata(member_name: str) -> bool:
    parts = [part for part in member_name.replace("\\", "/").split("/") if part]
    if not parts:
        return False
    name = parts[-1]
    return "__MACOSX" in parts or name.startswith("._") or name.lower() in _OS_METADATA_NAMES


def _extract_members(
    members: Iterable[tuple[str, bool, Callable[[], IO[bytes] | None] | None]],
    extract_dir: Path,
    max_bytes: int,
) -> None:
    actual_total = 0
    member_count = 0
    for member_name, is_dir, open_src in members:
        member_count += 1
        if member_count > MAX_IMPORT_MEMBERS:
            raise HTTPException(413, MEMBER_LIMIT_EXCEEDED_DETAIL)
        if _is_os_metadata(member_name):
            continue
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
    try:
        extract_dir.mkdir(parents=True, exist_ok=True)
        if fmt == "zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                if len(zf.infolist()) > MAX_IMPORT_MEMBERS:
                    raise HTTPException(413, MEMBER_LIMIT_EXCEEDED_DETAIL)
                name_encoding = _choose_zip_name_encoding(zf)

                def _zip_members() -> Iterable[tuple[str, bool, Callable[[], IO[bytes] | None] | None]]:
                    for info in zf.infolist():
                        mode = (info.external_attr >> 16) & 0xFFFF
                        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                            raise HTTPException(400, "压缩包内包含符号链接或特殊文件，已拒绝导入。")
                        name = _decode_zip_member_name(info, name_encoding)
                        is_dir = info.is_dir() or name.endswith(("/", "\\"))
                        yield name, is_dir, None if is_dir else (lambda i=info: zf.open(i, "r"))

                _extract_members(_zip_members(), extract_dir, max_bytes)
            return

        with tarfile.open(archive_path, mode="r:", encoding="utf-8") as tf:

            def _tar_members() -> Iterable[tuple[str, bool, Callable[[], IO[bytes] | None] | None]]:
                for m in tf:
                    if not (m.isfile() or m.isdir()):
                        raise HTTPException(400, "归档内包含符号链接或特殊文件，已拒绝导入。")
                    yield m.name, m.isdir(), None if m.isdir() else (lambda member=m: tf.extractfile(member))

            _extract_members(_tar_members(), extract_dir, max_bytes)
    except HTTPException:
        raise
    except (zipfile.BadZipFile, tarfile.TarError, zlib.error, RuntimeError, OSError, EOFError, ValueError) as exc:
        if isinstance(exc, OSError):
            if storage_msg := describe_storage_error(exc):
                raise HTTPException(507, storage_msg) from exc
            if isinstance(exc, PermissionError):
                raise HTTPException(403, "数据目录没有写入权限，角色包未能导入。请把程序文件夹移到有写入权限的位置后重试。") from exc
        if fmt != "zip":
            raise HTTPException(400, "读取文件夹归档失败，请重新选择角色包后重试。") from exc
        msg = (
            "压缩包设有密码，请先解压后再导入角色包文件夹。"
            if "encrypted" in str(exc).lower()
            else "压缩包已损坏或不是有效的 .zip 文件。"
        )
        raise HTTPException(400, msg) from exc


def _resolve_folder_name(raw_name: str, pkg_dir: Path | None = None) -> str:
    base = (raw_name or "").replace("\\", "/").strip("/").rsplit("/", 1)[-1].strip()
    if pkg_dir is not None:
        for ext in (".tar.gz", ".zip", ".tar", ".tgz"):
            if base.lower().endswith(ext):
                base = base[: -len(ext)]
                break
        if not base.strip(". "):
            try:
                raw = json.loads(read_manifest_text(pkg_dir / "manifest.json"))
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
        raw = json.loads(read_manifest_text(manifest_file))
        return isinstance(raw, dict) and ("sprites" in raw or "costumes" in raw) and "emotions" not in raw
    except Exception:
        return False


def discover_extracted_packages(extract_dir: Path, *, filename: str = "") -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []

    def _walk(cur_dir: Path, depth: int) -> None:
        manifest_file = cur_dir / "manifest.json"
        if manifest_file.is_file() and not _is_portrait_manifest(cur_dir, manifest_file, depth):
            try:
                raw = json.loads(read_manifest_text(manifest_file))
            except Exception:
                raw = None
            if isinstance(raw, dict) and isinstance(raw.get("backgrounds"), list) and "emotions" not in raw:
                raise HTTPException(
                    422,
                    "这是背景包，不是角色包；请把背景包文件夹放进程序目录下的 backgrounds 文件夹，无需重启程序，刷新网页即可。",
                )
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
        raise HTTPException(422, _missing_package_detail(filename))
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


def _manifest_id_key(pkg_dir: Path) -> str | None:
    manifest_file = pkg_dir / "manifest.json"
    if not manifest_file.is_file():
        return None
    try:
        raw = json.loads(read_manifest_text(manifest_file))
    except Exception:
        return None
    pkg_id = raw.get("id") if isinstance(raw, dict) else None
    return (identity_key(pkg_id) or None) if isinstance(pkg_id, str) else None


def _target_installed_folders(staged: list[tuple[str, Path]], chars_dir: Path) -> list[tuple[str, Path]]:
    """Send an update of an installed character to that character's folder.

    The folder name comes from the zip's file name, so 「爱丽丝 (1).zip」 for the installed
    「爱丽丝」 would land in a second folder and fail as a duplicate id. Matching by manifest id
    turns it into the usual 「已有同名角色包」 question, and overwrite replaces the right folder.
    """
    installed: dict[str, str | None] = {}
    try:
        for item in chars_dir.iterdir():
            if item.is_dir() and (key := _manifest_id_key(item)):
                installed[key] = None if key in installed else item.name
    except OSError:
        return staged
    if not installed:
        return staged
    taken = {name for name, _ in staged}
    result: list[tuple[str, Path]] = []
    for folder_name, src_dir in staged:
        key = _manifest_id_key(src_dir)
        current = installed.get(key) if key else None
        if current and current != folder_name and current not in taken:
            taken.discard(folder_name)
            taken.add(current)
            folder_name = current
        result.append((folder_name, src_dir))
    return result


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


# Appended to the user's own voice when a character package with the same name is kept beside it.
OWN_VOICE_SUFFIX = "（自建）"


def _staged_identities(staged: list[tuple[str, Path]]) -> list[tuple[str, list[str]]]:
    """Name, id and aliases from each staged manifest; a broken manifest is left to validation."""
    found: list[tuple[str, list[str]]] = []
    for _folder, src_dir in staged:
        try:
            data = json.loads(read_manifest_text(src_dir / "manifest.json"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not data["name"].strip():
            continue
        raw_aliases = data.get("aliases")
        aliases = [a for a in raw_aliases if isinstance(a, str) and a.strip()] if isinstance(raw_aliases, list) else []
        package_id = data.get("id") if isinstance(data.get("id"), str) else ""
        found.append((data["name"].strip(), [x for x in (package_id, *aliases) if x]))
    return found


async def _own_voice_clashes(staged: list[tuple[str, Path]], char_mgr: Any) -> list[dict[str, Any]]:
    """Voices the user made that a new package would take over: same name or alias, no package behind them."""
    identities = await asyncio.to_thread(_staged_identities, staged)
    if not identities:
        return []
    async with get_db() as conn:
        cursor = await conn.execute("SELECT * FROM voice_profiles ORDER BY id;")
        rows = await cursor.fetchall()
    own_rows = [row for row in rows if char_mgr.get_character(row["name"]) is None]
    clashes: list[dict[str, Any]] = []
    for name, aliases in identities:
        if char_mgr.get_character(name) is not None:
            continue  # An update of an installed package keeps its own voice.
        rest = list(own_rows)
        while rest:
            try:
                row = select_profile(rest, name, aliases)
            except AmbiguousProfileName:
                break  # Sync already refuses to touch an ambiguous name.
            if row is None:
                break
            rest = [r for r in rest if r["id"] != row["id"]]
            if all(c["profile_id"] != row["id"] for c in clashes):
                clashes.append(
                    {"package": name, "profile_id": row["id"], "profile_name": row["name"], "snap": dict(row)}
                )
    return clashes


async def _rename_own_voices(clashes: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """Move each clashing voice out of the package's way; returns (id, old name) to undo."""
    renamed: list[tuple[int, str]] = []
    async with get_db() as conn:
        cursor = await conn.execute("SELECT name FROM voice_profiles;")
        taken = {identity_key(row["name"]) for row in await cursor.fetchall()}
        taken.update(identity_key(c["package"]) for c in clashes)
        for clash in clashes:
            base = f"{clash['profile_name']}{OWN_VOICE_SUFFIX}"
            new_name, n = base, 2
            while identity_key(new_name) in taken:
                new_name, n = f"{clash['profile_name']}（自建{n}）", n + 1
            taken.add(identity_key(new_name))
            await conn.execute(
                "UPDATE voice_profiles SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (new_name, clash["profile_id"]),
            )
            renamed.append((clash["profile_id"], clash["profile_name"]))
            clash["renamed_to"] = new_name
        await conn.commit()
    return renamed


_OWN_VOICE_COLS = (
    "name", "description", "gpt_weights_path", "sovits_weights_path",
    "ref_audio_path", "prompt_text", "prompt_lang", "text_lang", "system_prompt",
)


async def _restore_own_voices(snapshots: list[dict[str, Any]]) -> None:
    if not snapshots:
        return
    async with get_db() as conn:
        for snap in snapshots:
            try:
                await update_voice_profile(
                    conn, snap["id"], VoiceProfileUpdate(**{k: snap[k] for k in _OWN_VOICE_COLS})
                )
            except sqlite3.IntegrityError:
                logger.warning("导入失败后无法恢复音色原名：%s", snap["name"])


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
    request: Request, archive_path: Path, *, filename: str = "", overwrite: bool = False, same_name: str = ""
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
            staged = await asyncio.to_thread(_target_installed_folders, staged, chars_dir)
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

            # A voice the user made under the same name would silently get the package's persona and files.
            clashes = await _own_voice_clashes(staged, char_mgr)
            if clashes and same_name not in ("replace", "keep_both"):
                names = [c["profile_name"] for c in clashes]
                return JSONResponse(
                    status_code=status.HTTP_409_CONFLICT,
                    content={
                        "code": "own_voice_same_name",
                        "names": names,
                        "suffix": OWN_VOICE_SUFFIX,
                        "detail": f"你自己建的音色和角色包同名：{'、'.join(names)}",
                    },
                )

            placed: list[Path] = []
            backups: list[tuple[Path, Path]] = []
            snapshots: list[dict[str, Any]] = []

            async def _undo() -> None:
                await _restore_own_voices(snapshots)
                await _rollback_and_rescan(placed, backups, catalog, char_mgr)

            try:
                if clashes and same_name in ("keep_both", "replace"):
                    snapshots.extend(c["snap"] for c in clashes)
                    if same_name == "keep_both":
                        await _rename_own_voices(clashes)
                await asyncio.to_thread(
                    _place_packages, staged, chars_dir, settings.data_dir / "replaced_characters", placed, backups
                )
            except BaseException as exc:
                await await_protected(_undo())
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
                        if same_name == "replace" and any(c["profile_id"] == prof.id for c in clashes):
                            d = await asyncio.to_thread(CharacterManager._resolve_package_profile_defaults, pkg)
                            await update_voice_profile(
                                conn, prof.id, VoiceProfileUpdate(
                                    name=pkg.name, description=pkg.manifest.description,
                                    gpt_weights_path=d["gpt_weights"], sovits_weights_path=d["sovits_weights"],
                                    ref_audio_path=d["ref_audio_str"], prompt_text=d["prompt_text"],
                                    prompt_lang=d["prompt_lang"], text_lang=d["prompt_lang"],
                                    system_prompt=d["system_prompt"],
                                ),
                            )
                        imported_items.append(
                            {
                                "name": pkg.name,
                                "folder": pkg.folder.name,
                                "character_id": pkg.id,
                                "profile_id": prof.id,
                                "missing_voice_weights": await asyncio.to_thread(_package_missing_voice_weights, pkg),
                            }
                        )
            except (_ImportValidationError, DuplicateCharacterId, HTTPException) as exc:
                await await_protected(_undo())
                raise HTTPException(422, str(getattr(exc, "detail", exc))) from exc
            except BaseException:
                await await_protected(_undo())
                raise

            return {
                "status": "ok",
                "imported": imported_items,
                "active_profile_id": rescan_res["active_profile_id"],
                "total_profiles": rescan_res["total_profiles"],
                "catalog_warning": rescan_res["catalog_warning"],
                "kept_own_voices": [
                    {"name": c["profile_name"], "renamed_to": c["renamed_to"]} for c in clashes if "renamed_to" in c
                ],
            }
    finally:
        await asyncio.to_thread(_rmtree_force, staging_dir)


async def import_character_archive(
    request: Request, *, filename: str = "", overwrite: bool = False, same_name: str = ""
) -> dict[str, Any] | JSONResponse:
    """Streams, safely unpacks, validates, and installs character package(s)."""
    temp_root = get_settings().data_dir / "character_import"
    await asyncio.to_thread(temp_root.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(sweep_import_leftovers, temp_root.parent)
    fd, raw_upload = await asyncio.to_thread(tempfile.mkstemp, prefix="up-", suffix=".bin", dir=str(temp_root))
    os.close(fd)
    upload_path = Path(raw_upload)
    try:
        await _stream_request_to_file(request, upload_path, MAX_IMPORT_BYTES)
        return await import_character_archive_file(
            request, upload_path, filename=filename, overwrite=overwrite, same_name=same_name
        )
    finally:
        await asyncio.to_thread(upload_path.unlink, missing_ok=True)
