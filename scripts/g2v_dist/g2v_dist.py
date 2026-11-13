"""Character package redemption code distribution server and admin CLI (stdlib only)."""

import argparse
import base64
from collections import namedtuple
from contextlib import closing, contextmanager, suppress
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
from urllib.parse import urlsplit
import zipfile

MAX_BODY_BYTES, CHUNK_BYTES = 4096, 65536
DEFAULT_HOST, DEFAULT_PORT, DEFAULT_PREFIX, DEFAULT_STATE_DIR = "127.0.0.1", 8765, "/_g2v", "/var/lib/g2v-dist"
PACKAGE_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_B64_RE, _RANGE_RE = re.compile(r"^[A-Za-z0-9_-]+$"), re.compile(r"^bytes=(\d+)-(\d*)$")
logger = logging.getLogger("g2v_dist")
ParsedCode = namedtuple("ParsedCode", ("server_url", "code_secret"))


class CodeFormatError(ValueError):
    """Malformed redemption code, server URL, or secret."""


def _b64d(seg: str, err: str) -> bytes:
    if not seg or len(seg) % 4 == 1 or not _B64_RE.fullmatch(seg):
        raise CodeFormatError(err)
    raw = base64.urlsafe_b64decode((seg + "=" * ((4 - len(seg) % 4) % 4)).encode("ascii"))
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != seg:
        raise CodeFormatError(err)
    return raw


def _norm_url(url: str) -> str:
    s = url.strip() if isinstance(url, str) else ""
    with suppress(ValueError):
        p = urlsplit(s)
        _ = p.port
        if (
            s
            and not any(c.isspace() for c in s)
            and p.scheme in ("http", "https")
            and p.hostname
            and not p.query
            and not p.fragment
        ):
            return s.rstrip("/")
    raise CodeFormatError("服务器地址必须为合法的 http(s) 根地址")


def encode_code(url: str, secret: bytes | bytearray | str) -> str:
    raw = (
        bytes(secret)
        if isinstance(secret, (bytes, bytearray))
        else _b64d(secret.strip(), "密钥编码无效")
        if isinstance(secret, str)
        else b""
    )
    if len(raw) != 32:
        raise CodeFormatError("密钥格式无效：必须为 32 字节随机密钥")
    u_b64 = base64.urlsafe_b64encode(_norm_url(url).encode("utf-8")).rstrip(b"=").decode("ascii")
    return f"G2V1.{u_b64}.{base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')}"


def parse_code(text: str) -> ParsedCode:
    s = text.strip() if isinstance(text, str) else ""
    parts = s.split(".") if s and not any(c.isspace() for c in s) else []
    if len(parts) != 3 or parts[0] != "G2V1":
        raise CodeFormatError("兑换码格式无效，应为 G2V1.<地址>.<密钥>")
    try:
        url = _norm_url(_b64d(parts[1], "兑换码地址段编码无效").decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise CodeFormatError("兑换码地址段不是有效的 UTF-8 文本") from exc
    if len(_b64d(parts[2], "兑换码密钥段编码无效")) != 32:
        raise CodeFormatError("兑换码密钥长度必须为 32 字节")
    return ParsedCode(url, parts[2])


def _fmt_utc(ts: float | None) -> str:
    return "永不过期" if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DistStore:
    def __init__(self, state_dir: str | os.PathLike[str], *, clock=time.time) -> None:
        self.state_dir = Path(state_dir)
        self.packages_dir, self.db_path = self.state_dir / "packages", self.state_dir / "dist.db"
        self._clock, self._lock = clock, threading.RLock()
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(
                "PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS codes (code_id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL UNIQUE, allowed_packages TEXT NOT NULL, max_downloads INTEGER NOT NULL, expires_at REAL, revoked INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);"
                "CREATE TABLE IF NOT EXISTS downloads (code_id TEXT NOT NULL, package_id TEXT NOT NULL, download_count INTEGER NOT NULL DEFAULT 0, last_download_at REAL, PRIMARY KEY (code_id, package_id)); CREATE TABLE IF NOT EXISTS rate_limits (ip TEXT PRIMARY KEY, fail_count INTEGER NOT NULL DEFAULT 0, window_start REAL NOT NULL DEFAULT 0, blocked_until REAL);"
            )

    @contextmanager
    def _conn(self, write: bool = False):
        with self._lock, closing(sqlite3.connect(self.db_path, timeout=10.0)) as c, c:
            c.row_factory = sqlite3.Row
            if write:
                c.execute("BEGIN IMMEDIATE")
            yield c

    def add_package(
        self, zip_file: str | os.PathLike[str], *, package_id: str | None = None, name: str | None = None
    ) -> dict:
        src = Path(zip_file)
        if not src.is_file():
            raise ValueError(f"找不到角色包文件: {src}")
        pkg_id = (
            package_id
            if package_id is not None
            else re.sub(r"[^a-z0-9_-]+", "_", src.stem.strip().lower()).strip("_-")[:64].rstrip("_-")
        )
        if not PACKAGE_ID_RE.fullmatch(pkg_id):
            raise ValueError("角色包 ID 无效，只允许 [a-z0-9_-]{1,64}")
        manifest_name = None
        try:
            with zipfile.ZipFile(src) as zf:
                member = next(
                    (
                        n
                        for n in zf.namelist()
                        if not n.endswith("/") and n.replace("\\", "/").rsplit("/", 1)[-1] == "manifest.json"
                    ),
                    None,
                )
                if not member:
                    raise ValueError("角色包 zip 中未找到 manifest.json")
                with suppress(Exception):
                    d = json.loads(zf.read(member).decode("utf-8-sig"))
                    manifest_name = (
                        d["name"].strip() if isinstance(d, dict) and isinstance(d.get("name"), str) else None
                    )
        except zipfile.BadZipFile as exc:
            raise ValueError("无效的 zip 压缩文件") from exc
        zp, jp, tmp = (
            self.packages_dir / f"{pkg_id}.zip",
            self.packages_dir / f"{pkg_id}.json",
            self.packages_dir / f".{pkg_id}.{os.getpid()}.tmp",
        )
        h, total = hashlib.sha256(), 0
        with self._lock:
            with src.open("rb") as rf, tmp.open("wb") as wf:
                while chunk := rf.read(CHUNK_BYTES):
                    h.update(chunk)
                    total += wf.write(chunk)
            tmp.replace(zp)
            meta = {
                "id": pkg_id,
                "name": (name and name.strip()) or manifest_name or pkg_id,
                "size": total,
                "sha256": h.hexdigest(),
                "created_at": _fmt_utc(self._clock()),
            }
            jp.write_text(json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8")
        return {**meta, "zip_path": zp}

    def remove_package(self, pkg_id: str) -> None:
        with self._lock:
            if not (
                targets := [
                    p
                    for ext in (".json", ".zip")
                    if PACKAGE_ID_RE.fullmatch(pkg_id) and (p := self.packages_dir / f"{pkg_id}{ext}").is_file()
                ]
            ):
                raise ValueError(f"角色包不存在或 ID 无效: {pkg_id}")
            for p in targets:
                p.unlink()

    def get_package(self, pkg_id: str) -> dict | None:
        zp, jp = self.packages_dir / f"{pkg_id}.zip", self.packages_dir / f"{pkg_id}.json"
        with suppress(Exception):
            d = json.loads(jp.read_text(encoding="utf-8"))
            if zp.is_file():
                return {
                    "id": pkg_id,
                    "name": str(d.get("name") or pkg_id),
                    "size": int(d.get("size", zp.stat().st_size)),
                    "sha256": str(d["sha256"]),
                    "created_at": str(d.get("created_at") or ""),
                    "zip_path": zp,
                }
        return None

    def list_packages(self) -> list[dict]:
        return [
            m
            for f in sorted(self.packages_dir.glob("*.json"))
            if PACKAGE_ID_RE.fullmatch(f.stem) and (m := self.get_package(f.stem)) is not None
        ]

    def create_code(
        self,
        *,
        server_url: str,
        packages: list[str] | None = None,
        allow_all: bool = False,
        max_downloads: int = 3,
        expires_days: float | None = 30.0,
        note: str = "",
    ) -> tuple[str, str]:
        if isinstance(max_downloads, bool) or not isinstance(max_downloads, int) or max_downloads < 1:
            raise ValueError("最多下载次数必须大于等于 1")
        cleaned = (
            None if allow_all or packages is None else list(dict.fromkeys(p.strip() for p in packages if p.strip()))
        )
        if cleaned is not None and (not cleaned or any(not PACKAGE_ID_RE.fullmatch(p) for p in cleaned)):
            raise ValueError("角色包 ID 列表无效")
        if expires_days is not None and (
            isinstance(expires_days, bool) or not (0 < float(expires_days) < float("inf"))
        ):
            raise ValueError("有效天数必须大于 0")
        now, raw_secret = float(self._clock()), secrets.token_bytes(32)
        expires_at = None if expires_days is None else now + float(expires_days) * 86400.0
        full_code, sec_hash = encode_code(server_url, raw_secret), hashlib.sha256(raw_secret).hexdigest()
        with self._conn(True) as c:
            c.execute(
                "INSERT INTO codes VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    sec_hash[:8],
                    sec_hash,
                    "*" if cleaned is None else json.dumps(cleaned, ensure_ascii=False),
                    max_downloads,
                    expires_at,
                    note.strip(),
                    now,
                ),
            )
        return full_code, sec_hash[:8]

    def revoke_code(self, code_id: str) -> None:
        cid = code_id.strip().lower()
        with self._conn(True) as c:
            if c.execute("UPDATE codes SET revoked = 1 WHERE code_id = ?", (cid,)).rowcount == 0:
                raise ValueError(f"未找到兑换码编号: {cid}")

    def authenticate(self, ip: str, code_secret: str) -> tuple[int, sqlite3.Row | dict, dict[str, str]]:
        now, raw = float(self._clock()), b""
        with suppress(CodeFormatError):
            raw = _b64d(code_secret.strip(), "") if code_secret else b""
        with self._conn(True) as c:
            rl = c.execute(
                "SELECT fail_count, window_start, blocked_until FROM rate_limits WHERE ip = ?", (ip,)
            ).fetchone()
            if rl and rl["blocked_until"] is not None and now < float(rl["blocked_until"]):
                return (
                    429,
                    {"error": "请求过于频繁，请稍后再试"},
                    {"Retry-After": str(max(1, -int((now - float(rl["blocked_until"])) // 1)))},
                )
            row = (
                c.execute("SELECT * FROM codes WHERE secret_hash = ?", (hashlib.sha256(raw).hexdigest(),)).fetchone()
                if len(raw) == 32
                else None
            )
            if row is not None and not row["revoked"] and (row["expires_at"] is None or now < float(row["expires_at"])):
                return 200, row, {}
            cnt, ws = (
                (int(rl["fail_count"]) + 1, float(rl["window_start"]))
                if rl and 0 <= now - float(rl["window_start"]) < 3600.0
                else (1, now)
            )
            c.execute(
                "INSERT OR REPLACE INTO rate_limits VALUES (?, ?, ?, ?)",
                (ip, cnt, ws, now + 3600.0 if cnt > 10 else None),
            )
        return 401, {"error": "兑换码无效或已过期"}, {}

    def authorize_download(self, code: sqlite3.Row, pkg_id: str, *, is_resume: bool) -> str | None:
        now, cid = float(self._clock()), str(code["code_id"])
        if not is_resume:
            with self._conn(True) as c:
                c.execute("INSERT OR IGNORE INTO downloads VALUES (?, ?, 0, NULL)", (cid, pkg_id))
                if (
                    c.execute(
                        "UPDATE downloads SET download_count = download_count + 1, last_download_at = ? WHERE code_id = ? AND package_id = ? AND download_count < ?",
                        (now, cid, pkg_id, int(code["max_downloads"])),
                    ).rowcount
                    == 0
                ):
                    return "这个角色包的下载次数已用完"
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT download_count, last_download_at FROM downloads WHERE code_id = ? AND package_id = ?",
                (cid, pkg_id),
            ).fetchone()
        if row is None or int(row["download_count"]) <= 0 or row["last_download_at"] is None:
            return "断点续传无效：尚未发起过完整下载"
        return (
            None
            if 0 <= now - float(row["last_download_at"]) <= 86400.0
            else "断点续传已超过 24 小时有效期，请重新发起完整下载"
        )


def _is_loopback_ip(ip_str: str) -> bool:
    with suppress(ValueError):
        a = ipaddress.ip_address(ip_str.strip())
        return (a.ipv4_mapped or a).is_loopback if isinstance(a, ipaddress.IPv6Address) else a.is_loopback
    return False


def resolve_client_ip(peer_ip: str, headers) -> str:
    peer = (peer_ip or "").strip() or "127.0.0.1"
    if _is_loopback_ip(peer) and headers:
        lower = {k.lower(): v for k, v in reversed(list(headers.items())) if isinstance(k, str) and isinstance(v, str)}
        for hdr in ("cf-connecting-ip", "x-forwarded-for"):
            if first := lower.get(hdr, "").split(",", 1)[0].strip():
                return first
    return peer


class DistHTTPServer(ThreadingHTTPServer):
    daemon_threads = allow_reuse_address = True


class DistRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60  # seconds per socket read/write, so a stalled client does not hold its thread forever

    def log_message(self, format: str, *args: object) -> None:
        return

    def _ip(self) -> str:
        return resolve_client_ip(self.client_address[0] if self.client_address else "127.0.0.1", self.headers)

    def _log(self, code_id: str, status: int, nbytes: int) -> None:
        logger.info(
            "%s ip=%s code=%s %s %s status=%d bytes=%d",
            _fmt_utc(self.server.store._clock()),
            self._ip(),
            code_id or "-",
            self.command or "-",
            (self.path or "").split("?", 1)[0],
            status,
            nbytes,
        )

    def _send_json(self, status: int, payload, *, code_id: str = "-", extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for k, v in (
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "private, no-store"),
            ("Connection", "close"),
            *(extra or {}).items(),
        ):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True
        self._log(code_id, status, len(body))

    def do_POST(self) -> None:
        if (self.path or "").split("?", 1)[0] != f"{self.server.prefix}/v1/redeem":
            return self._send_json(404, {"error": "未找到请求的接口"})
        cl = (self.headers.get("Content-Length") or "").strip()
        if not (cl.isascii() and cl.isdigit()):
            return self._send_json(400, {"error": "Content-Length 请求头无效"})
        if int(cl) > MAX_BODY_BYTES:
            return self._send_json(413, {"error": "请求体超过 4 KiB 上限"})
        payload = None
        with suppress(Exception):
            payload = json.loads((self.rfile.read(int(cl)) if int(cl) else b"").decode("utf-8"))
        if not isinstance(payload, dict):
            return self._send_json(400, {"error": "请求体必须是有效的 JSON 对象"})
        sec = payload.get("code_secret")
        st, rec, extra = self.server.store.authenticate(self._ip(), sec.strip() if isinstance(sec, str) else "")
        if st != 200:
            return self._send_json(st, rec, extra=extra)
        cid, max_dl, raw_a = str(rec["code_id"]), int(rec["max_downloads"]), str(rec["allowed_packages"])
        allowed = None if raw_a == "*" else set(json.loads(raw_a))
        with self.server.store._conn() as c:
            used = {
                str(r["package_id"]): int(r["download_count"])
                for r in c.execute(
                    "SELECT package_id, download_count FROM downloads WHERE code_id = ?", (cid,)
                ).fetchall()
            }
        pkgs = [
            {"id": p["id"], "name": p["name"], "size": p["size"], "sha256": p["sha256"], "remaining": rem}
            for p in self.server.store.list_packages()
            if (allowed is None or p["id"] in allowed) and (rem := max_dl - used.get(p["id"], 0)) > 0
        ]
        self._send_json(200, {"packages": pkgs}, code_id=cid)

    def do_GET(self) -> None:
        raw_path, pkg_prefix = (self.path or "").split("?", 1)[0], f"{self.server.prefix}/v1/packages/"
        if not raw_path.startswith(pkg_prefix) or not PACKAGE_ID_RE.fullmatch(pkg_id := raw_path[len(pkg_prefix) :]):
            return self._send_json(
                404, {"error": "未找到请求的接口" if not raw_path.startswith(pkg_prefix) else "角色包不存在"}
            )
        auth = (self.headers.get("Authorization") or "").strip().split(None, 1)
        st, rec, extra = self.server.store.authenticate(
            self._ip(), auth[1].strip() if len(auth) == 2 and auth[0].lower() == "bearer" else ""
        )
        if st != 200:
            return self._send_json(st, rec, extra=extra)
        cid, raw_a = str(rec["code_id"]), str(rec["allowed_packages"])
        if raw_a != "*" and pkg_id not in json.loads(raw_a):
            return self._send_json(403, {"error": "该兑换码无权下载此角色包"}, code_id=cid)
        if (pkg := self.server.store.get_package(pkg_id)) is None:
            return self._send_json(404, {"error": "角色包不存在"}, code_id=cid)
        with pkg["zip_path"].open("rb") as f:
            size, range_hdr = os.fstat(f.fileno()).st_size, self.headers.get("Range")
            is_range, start, end = False, 0, max(0, size - 1)
            if range_hdr is not None:
                m = _RANGE_RE.fullmatch(range_hdr.strip())
                start, end = (int(m.group(1)), int(m.group(2)) if m.group(2) else size - 1) if m else (0, -1)
                if m is None or size == 0 or start >= size or end < start:
                    return self._send_json(
                        416, {"error": "无效的 Range 请求范围"}, code_id=cid, extra={"Content-Range": f"bytes */{size}"}
                    )
                end, is_range = min(end, size - 1), True
            if err := self.server.store.authorize_download(rec, pkg_id, is_resume=(start > 0)):
                return self._send_json(403, {"error": err}, code_id=cid)
            content_len, status = ((end - start + 1) if size > 0 else 0), (206 if is_range else 200)
            self.send_response(status)
            for k, v in (
                ("Content-Type", "application/zip"),
                ("Content-Length", str(content_len)),
                ("Accept-Ranges", "bytes"),
                ("ETag", f'"{pkg["sha256"]}"'),
                ("Cache-Control", "private, no-store"),
                ("Connection", "close"),
                *((("Content-Range", f"bytes {start}-{end}/{size}"),) if is_range else ()),
            ):
                self.send_header(k, v)
            self.end_headers()
            self.close_connection = True
            sent, rem = 0, content_len
            with suppress(ConnectionError, TimeoutError):
                f.seek(start)
                while rem > 0 and (chunk := f.read(min(CHUNK_BYTES, rem))):
                    self.wfile.write(chunk)
                    sent, rem = sent + len(chunk), rem - len(chunk)
        self._log(cid, status, sent)

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = lambda self: self._send_json(
        404, {"error": "未找到请求的接口或不支持该请求方法"}
    )


def create_server(
    state_dir: str | os.PathLike[str],
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    prefix: str = DEFAULT_PREFIX,
    allow_public: bool = False,
    clock=time.time,
) -> DistHTTPServer:
    h = host.strip().lower()
    if (
        not allow_public
        and h != "localhost"
        and not _is_loopback_ip(h[1:-1] if h.startswith("[") and h.endswith("]") else h)
    ):
        raise ValueError(f"安全限制：拒绝绑定到非回环地址 ({host})。如需绑定请显式传入 --allow-public")
    if isinstance(port, bool) or not isinstance(port, int) or not (0 <= port <= 65535):
        raise ValueError("监听端口必须在 0 到 65535 之间")
    cls = (
        type("DistHTTPServerV6", (DistHTTPServer,), {"address_family": socket.AF_INET6})
        if ":" in host and not host.startswith("[")
        else DistHTTPServer
    )
    srv = cls((host, port), DistRequestHandler)
    srv.store, p = DistStore(state_dir, clock=clock), (prefix or "").strip()
    srv.prefix = "" if not p or p == "/" else ("/" + p.lstrip("/")).rstrip("/")
    return srv


def main(argv: list[str] | None = None, *, stdout=None, stderr=None, clock=time.time) -> int:
    out, err = stdout or sys.stdout, stderr or sys.stderr
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(prog="g2v_dist.py", description="galgame2voice 角色包分发服务与发码工具")
    parser.add_argument("--state-dir", default=os.environ.get("G2V_DIST_STATE_DIR", DEFAULT_STATE_DIR))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_srv = sub.add_parser("serve", parents=[common])
    p_srv.add_argument("--host", default=os.environ.get("G2V_DIST_HOST", DEFAULT_HOST))
    p_srv.add_argument("--port", type=int, default=int(os.environ.get("G2V_DIST_PORT", str(DEFAULT_PORT))))
    p_srv.add_argument("--prefix", default=os.environ.get("G2V_DIST_PREFIX", DEFAULT_PREFIX))
    p_srv.add_argument("--allow-public", action="store_true", default=False)
    p_add = sub.add_parser("add-package", parents=[common])
    p_add.add_argument("zip_path")
    p_add.add_argument("--id", dest="package_id")
    p_add.add_argument("--name")
    sub.add_parser("remove-package", parents=[common]).add_argument("package_id")
    p_code = sub.add_parser("new-code", parents=[common])
    p_code.add_argument("--server-url", required=True)
    grp = p_code.add_mutually_exclusive_group()
    grp.add_argument("--packages")
    grp.add_argument("--all", dest="allow_all", action="store_true")
    p_code.add_argument("--max-downloads", type=int, default=3)
    p_code.add_argument("--expires-days", type=float, default=30.0)
    p_code.add_argument("--note", default="")
    sub.add_parser("list", parents=[common])
    sub.add_parser("revoke", parents=[common]).add_argument("code_id")
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    try:
        if args.cmd == "serve":
            logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
            with create_server(
                args.state_dir,
                host=args.host,
                port=args.port,
                prefix=args.prefix,
                allow_public=args.allow_public,
                clock=clock,
            ) as httpd:
                logger.info(
                    "g2v-dist http://%s:%s%s (state_dir=%s)",
                    *httpd.server_address[:2],
                    httpd.prefix,
                    httpd.store.state_dir,
                )
                with suppress(KeyboardInterrupt):
                    httpd.serve_forever()
            return 0
        store = DistStore(args.state_dir, clock=clock)
        if args.cmd == "add-package":
            m = store.add_package(args.zip_path, package_id=args.package_id, name=args.name)
            print(f"已上架角色包: id={m['id']} name={m['name']} size={m['size']} sha256={m['sha256']}", file=out)
        elif args.cmd == "remove-package":
            store.remove_package(args.package_id)
            print(f"已下架角色包: {args.package_id}", file=out)
        elif args.cmd == "new-code":
            pkgs = [p.strip() for p in args.packages.split(",") if p.strip()] if args.packages is not None else None
            print(
                store.create_code(
                    server_url=args.server_url,
                    packages=pkgs,
                    allow_all=(args.packages is None),
                    max_downloads=args.max_downloads,
                    expires_days=args.expires_days,
                    note=args.note,
                )[0],
                file=out,
            )
        elif args.cmd == "list":
            pkgs, now = store.list_packages(), float(store._clock())
            with store._conn() as c:
                c_rows = c.execute("SELECT * FROM codes ORDER BY created_at, code_id").fetchall()
                used = {
                    (str(r["code_id"]), str(r["package_id"])): int(r["download_count"])
                    for r in c.execute("SELECT code_id, package_id, download_count FROM downloads")
                }
            print(
                "=== 角色包列表 ===\n"
                + (
                    "\n".join(
                        f"- {p['id']}\t名称={p['name']}\t大小={p['size']}\tsha256={p['sha256']}\t上架时间={p['created_at']}"
                        for p in pkgs
                    )
                    or "（暂无已上架角色包）"
                ),
                file=out,
            )
            lines = []
            for r in c_rows:
                cid, max_dl, raw_a, exp = (
                    str(r["code_id"]),
                    int(r["max_downloads"]),
                    str(r["allowed_packages"]),
                    r["expires_at"],
                )
                targets = [p["id"] for p in pkgs] if raw_a == "*" else [str(x) for x in json.loads(raw_a)]
                rem = (
                    ",".join(f"{pid}:{max(0, max_dl - used.get((cid, pid), 0))}/{max_dl}" for pid in targets)
                    or f"每包最多 {max_dl} 次"
                )
                st = "已作废" if r["revoked"] else ("已过期" if exp is not None and now >= float(exp) else "有效")
                lines.append(
                    f"- 编号={cid}\t状态={st}\t允许包={('全部(*)' if raw_a == '*' else ','.join(targets))}\t剩余次数={rem}\t过期时间={_fmt_utc(float(exp) if exp is not None else None)}\t备注={r['note'] or '-'}"
                )
            print("=== 兑换码列表 ===\n" + ("\n".join(lines) or "（暂无兑换码）"), file=out)
        elif args.cmd == "revoke":
            store.revoke_code(args.code_id)
            print(f"已作废兑换码: {args.code_id}", file=out)
        return 0
    except (ValueError, CodeFormatError, OSError) as exc:
        print(f"错误: {exc}", file=err)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
