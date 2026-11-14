"""Redeem a G2V1 code: list packages, Range-resume a download, then import it."""

from __future__ import annotations

import asyncio
import base64
from collections import namedtuple
from contextlib import suppress
import hashlib
import ipaddress
import json
import logging
from pathlib import Path
import re
import shutil
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
import httpx

from galgame2voice.config import get_settings
from galgame2voice.services.character_package_import import (
    MAX_IMPORT_BYTES,
    SIZE_LIMIT_EXCEEDED_DETAIL,
    import_character_archive_file,
)
from galgame2voice.utils.http_client import create_async_client
from galgame2voice.utils.logger import sanitize_error_detail

logger = logging.getLogger("galgame2voice.services.package_redeem")
_B64_RE, _PKG_RE = re.compile(r"^[A-Za-z0-9_-]+$"), re.compile(r"^[a-z0-9_-]{1,64}$")
_SHA_RE, _RANGE_RE = re.compile(r"^[0-9a-fA-F]{64}$"), re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
ParsedCode = namedtuple("ParsedCode", ("server_url", "code_secret"))
_PROGRESS: dict[str, Any] = {"phase": "idle", "done": 0, "total": 0, "name": ""}
_KEPT: dict[str, str] = {}
_LOCK: asyncio.Lock | None = None
_LOCK_LOOP: asyncio.AbstractEventLoop | None = None


class RedeemCodeError(ValueError):
    pass


def _loopback(host: str) -> bool:
    text = (host or "").strip().lower()
    if text == "localhost":
        return True
    with suppress(ValueError):
        addr = ipaddress.ip_address(text)
        mapped = addr.ipv4_mapped if isinstance(addr, ipaddress.IPv6Address) else None
        return bool((mapped or addr).is_loopback)
    return False


def _b64d(seg: str, err: str) -> bytes:
    if not seg or len(seg) % 4 == 1 or not _B64_RE.fullmatch(seg):
        raise RedeemCodeError(err)
    raw = base64.urlsafe_b64decode((seg + "=" * ((4 - len(seg) % 4) % 4)).encode("ascii"))
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != seg:
        raise RedeemCodeError(err)
    return raw


def _norm_url(url: str) -> str:
    s = url.strip() if isinstance(url, str) else ""
    with suppress(ValueError):
        parsed = urlsplit(s)
        _ = parsed.port
        spaced = any(ch.isspace() for ch in s)
        scheme_ok = parsed.scheme in ("http", "https") and bool(parsed.hostname)
        if s and not spaced and scheme_ok and not parsed.query and not parsed.fragment:
            return s.rstrip("/")
    raise RedeemCodeError("服务器地址必须为合法的 http(s) 根地址")


def parse_redeem_code(text: str) -> ParsedCode:
    s = text.strip() if isinstance(text, str) else ""
    parts = s.split(".") if s and not any(ch.isspace() for ch in s) else []
    if len(parts) != 3 or parts[0] != "G2V1":
        raise RedeemCodeError("兑换码格式无效，应为 G2V1.<地址>.<密钥>")
    try:
        url = _norm_url(_b64d(parts[1], "兑换码地址段编码无效").decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise RedeemCodeError("兑换码地址段不是有效的 UTF-8 文本") from exc
    parsed_url = urlsplit(url)
    if parsed_url.scheme != "https" and not _loopback(parsed_url.hostname or ""):
        raise RedeemCodeError("分发服务器必须使用 https://（仅本机回环地址允许 http://）")
    if len(_b64d(parts[2], "兑换码密钥段编码无效")) != 32:
        raise RedeemCodeError("兑换码密钥长度必须为 32 字节")
    return ParsedCode(url, parts[2])


def get_redeem_progress() -> dict[str, Any]:
    return dict(_PROGRESS)


def _lock() -> asyncio.Lock:
    global _LOCK, _LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _LOCK is None or _LOCK_LOOP is not loop:
        _LOCK, _LOCK_LOOP = asyncio.Lock(), loop
    return _LOCK


def _client(host: str, timeout: httpx.Timeout) -> httpx.AsyncClient:
    return create_async_client(timeout=timeout, follow_redirects=False, trust_env=not _loopback(host))


def _dist_error(status: int, raw: bytes) -> HTTPException:
    message = ""
    with suppress(Exception):
        data = json.loads(raw.decode("utf-8"))
        message = data["error"].strip() if isinstance(data, dict) and isinstance(data.get("error"), str) else ""
    if status in (401, 403, 429):
        return HTTPException(400, message or "兑换码无效或已过期")
    if status == 413:
        return HTTPException(413, message or SIZE_LIMIT_EXCEEDED_DETAIL)
    return HTTPException(502, message or f"分发服务器返回异常（HTTP {status}）")


def _net_error(exc: BaseException, parsed: ParsedCode, host: str) -> HTTPException:
    text = "连接超时" if isinstance(exc, httpx.TimeoutException) else (sanitize_error_detail(exc) or type(exc).__name__)
    safe = text.replace(parsed.code_secret, "***").replace(parsed.server_url, host)
    return HTTPException(502, f"连接分发服务器失败：{safe}")


async def _packages(parsed: ParsedCode) -> list[dict[str, Any]]:
    host = urlsplit(parsed.server_url).hostname or ""
    try:
        async with _client(host, httpx.Timeout(30.0, connect=10.0)) as client:
            resp = await client.post(f"{parsed.server_url}/v1/redeem", json={"code_secret": parsed.code_secret})
    except httpx.HTTPError as exc:
        raise _net_error(exc, parsed, host) from exc
    if resp.status_code != 200:
        raise _dist_error(resp.status_code, resp.content)
    try:
        raw = resp.json()["packages"]
        if not isinstance(raw, list):
            raise ValueError
        found = []
        for item in raw:
            pkg_id, digest = item["id"], item["sha256"]
            id_ok = isinstance(pkg_id, str) and bool(_PKG_RE.fullmatch(pkg_id))
            sha_ok = isinstance(digest, str) and bool(_SHA_RE.fullmatch(digest))
            if not (id_ok and sha_ok):
                raise ValueError
            name = str(item.get("name") or pkg_id)
            size = max(0, int(item.get("size") or 0))
            rem = max(0, int(item.get("remaining") or 0))
            found.append({"id": pkg_id, "name": name, "size": size, "sha256": digest.lower(), "remaining": rem})
    except Exception as exc:
        raise HTTPException(502, "分发服务器返回的数据格式无效") from exc
    logger.info("Redeem list host=%s packages=%d", host, len(found))
    return found


async def redeem_list(code: str) -> dict[str, Any]:
    try:
        return {"packages": await _packages(parse_redeem_code(code))}
    except RedeemCodeError as exc:
        raise HTTPException(400, str(exc)) from exc


def _resume_total(header: str, got: int, expect: int) -> int:
    match = _RANGE_RE.fullmatch((header or "").strip())
    start, end = (int(match[1]), int(match[2])) if match else (-1, -1)
    total = (int(match[3]) if match[3] != "*" else expect) if match else 0
    if not match or start != got or end < got or (total and (end != total - 1 or (expect and total != expect))):
        raise HTTPException(502, "断点续传校验失败，请重试")
    return total or expect


async def _save_package(parsed: ParsedCode, host: str, pkg: dict[str, Any], part: Path) -> None:
    url, expect = f"{parsed.server_url}/v1/packages/{pkg['id']}", int(pkg["size"])
    digest, got, resumes = hashlib.sha256(), 0, 0
    handle = await asyncio.to_thread(part.open, "wb")
    try:
        async with _client(host, httpx.Timeout(15.0, read=60.0, write=30.0, pool=15.0)) as client:
            while True:
                headers = {"Authorization": f"Bearer {parsed.code_secret}"}
                if got:
                    headers["Range"] = f"bytes={got}-"
                try:
                    async with client.stream("GET", url, headers=headers) as resp:
                        if (got == 0 and resp.status_code != 200) or (got and resp.status_code in (401, 403, 429)):
                            raise _dist_error(resp.status_code, await resp.aread())
                        if got and resp.status_code != 206:
                            raise HTTPException(502, "断点续传校验失败，请重试")
                        length = (resp.headers.get("content-length") or "").strip()
                        total = (
                            _resume_total(resp.headers.get("content-range") or "", got, expect)
                            if got
                            else (expect or (int(length) if length.isdigit() else 0))
                        )
                        async for chunk in resp.aiter_bytes(65536):
                            if got + len(chunk) > MAX_IMPORT_BYTES:
                                raise HTTPException(413, SIZE_LIMIT_EXCEEDED_DETAIL)
                            if chunk:
                                await asyncio.to_thread(handle.write, chunk)
                                digest.update(chunk)
                                got += len(chunk)
                                _PROGRESS.update(phase="download", done=got, total=total, name=str(pkg["name"]))
                        if expect and got < expect:
                            raise httpx.RemoteProtocolError("short")
                        break
                except httpx.HTTPError as exc:
                    if not (got and resumes < 3):
                        raise _net_error(exc, parsed, host) from exc
                    resumes += 1
                    logger.warning("Redeem resume=%d host=%s package_id=%s", resumes, host, pkg["id"])
    finally:
        await asyncio.to_thread(handle.close)
    if digest.hexdigest() != pkg["sha256"]:
        raise HTTPException(502, "下载的文件校验失败，请重试")


def _prepare_local(root: Path, part: Path, package_id: str, listed: dict[str, Any] | None) -> tuple:
    digest = ""
    if part.is_file():
        # file_digest 要 3.11，程序仍支持 3.10。
        hasher = hashlib.sha256()
        with part.open("rb") as handle:
            while chunk := handle.read(65536):
                hasher.update(chunk)
        digest = hasher.hexdigest()
    # 次数用完后分发列表不再包含该包，只能对照第一次校验通过时记下的 sha256。
    expected = listed["sha256"] if listed is not None else _KEPT.get(package_id)
    if digest and expected and digest == expected:
        if listed is not None:
            return "reuse", listed
        meta = {"id": package_id, "name": package_id, "size": part.stat().st_size, "sha256": digest, "remaining": 0}
        return "reuse", meta
    if listed is None:
        return "missing", None
    if root.is_dir():
        for stale in root.glob("dl-*.zip.part"):
            _KEPT.pop(stale.name.removeprefix("dl-").removesuffix(".zip.part"), None)
            stale.unlink(missing_ok=True)
    return "download", listed


async def redeem_download(request: Request, *, code: str, package_id: str, overwrite: bool = False) -> Any:
    try:
        parsed = parse_redeem_code(code)
    except RedeemCodeError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not _PKG_RE.fullmatch(package_id or ""):
        raise HTTPException(400, "角色包 ID 格式无效")
    lock = _lock()
    if lock.locked():
        raise HTTPException(409, "已有角色包正在下载")
    async with lock:
        host = urlsplit(parsed.server_url).hostname or ""
        root = get_settings().data_dir / "character_import"
        part = root / f"dl-{package_id}.zip.part"
        keep = False
        try:
            listed = next((item for item in await _packages(parsed) if item["id"] == package_id), None)
            await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
            action, pkg = await asyncio.to_thread(_prepare_local, root, part, package_id, listed)
            if pkg is None:
                raise HTTPException(400, "该角色包不存在、无权下载或下载次数已用完")
            if int(pkg["size"]) > MAX_IMPORT_BYTES:
                raise HTTPException(413, SIZE_LIMIT_EXCEEDED_DETAIL)
            if action == "download":
                _PROGRESS.update(phase="download", done=0, total=int(pkg["size"]), name=str(pkg["name"]))
                await _save_package(parsed, host, pkg, part)
            logger.info("Redeem %s host=%s package_id=%s", action, host, package_id)
            _PROGRESS.update(phase="import", done=int(pkg["size"]), total=int(pkg["size"]), name=str(pkg["name"]))
            # 导入会删掉传入的路径。硬链接（不支持时复制）被删后，已校验的压缩包还在，确认覆盖不用再下载。
            link = root / f"dl-{package_id}.zip.import"

            def _link_download() -> None:
                link.unlink(missing_ok=True)
                try:
                    link.hardlink_to(part)
                except OSError:
                    shutil.copyfile(part, link)

            try:
                await asyncio.to_thread(_link_download)
                result = await import_character_archive_file(
                    request, link, filename=f"{package_id}.zip", overwrite=overwrite
                )
            finally:
                await asyncio.to_thread(link.unlink, missing_ok=True)
            if getattr(result, "status_code", None) == 409:
                with suppress(Exception):
                    body = json.loads(result.body)
                    if isinstance(body, dict) and body.get("code") == "package_exists":
                        _KEPT[package_id] = str(pkg["sha256"])
                        keep = True
            return result
        finally:
            if not keep:
                _KEPT.pop(package_id, None)
                await asyncio.to_thread(part.unlink, missing_ok=True)
            _PROGRESS.update(phase="idle", done=0, total=0, name="")
