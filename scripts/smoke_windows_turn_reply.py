"""Out-of-band business integration driver for frozen Galgame2Voice portable package.

Exercises end-to-end conversation turn-reply linkage and single-line voice regeneration
against the pristine packaged Galgame2Voice.exe distribution without modifying core code.
"""

from __future__ import annotations

import argparse
import builtins
import codecs
import errno
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.request
import wave
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify business turn-reply link & voice repair against frozen Galgame2Voice portable package.",
        add_help=True,
    )
    parser.add_argument("--archive", required=True, type=Path, help="Pristine Galgame2Voice portable release zip.")
    parser.add_argument("--expected-sha", required=True, type=str, help="Expected SHA256 checksum of the archive.")
    parser.add_argument("--scratch", required=True, type=Path, help="Dedicated scratch directory for test isolation.")
    parser.add_argument(
        "--standins-script",
        default=Path(__file__).resolve().with_name("dev_standins.py"),
        type=Path,
        help="Path to scripts/dev_standins.py (default: sibling dev_standins.py).",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        type=str,
        help="Python interpreter (default: sys.executable).",
    )
    parser.add_argument(
        "--with-browser-ui",
        action="store_true",
        default=False,
        help="Execute real headless browser UI acceptance against restarted App 2 before exit.",
    )
    return parser.parse_args(argv)


def validate_scratch_path(raw_scratch: Path) -> Path:
    curr = raw_scratch
    for p in [curr, *curr.parents]:
        if p.exists() and (p.is_symlink() or getattr(p, "is_junction", lambda: False)()):
            raise ValueError(f"Scratch path component {p} is a symlink/junction")
    resolved = raw_scratch.resolve()
    if resolved == resolved.parent:
        raise ValueError(f"Scratch path {resolved} cannot be filesystem root")
    if resolved.exists():
        if not resolved.is_dir():
            raise ValueError(f"Scratch path {resolved} exists and is not a directory")
        if any(resolved.iterdir()):
            raise ValueError(f"Scratch directory {resolved} exists and is not empty")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def reserve_port() -> int:
    for _ in range(50):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                port = 20000 + secrets.randbelow(10000)
                s.bind(("127.0.0.1", port))
                return port
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, 10048):
                raise
    raise RuntimeError("Could not reserve an unused high loopback port.")


def verify_port_released(port: int, deadline: float) -> dict[str, Any]:
    history: list[dict[str, Any]] = []
    t_start = time.monotonic()
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                s.bind(("127.0.0.1", port))
            # Successfully bound and closed
            duration = time.monotonic() - t_start
            return {
                "success": True,
                "attempts": len(history) + 1,
                "duration_seconds": round(duration, 4),
                "history": history,
            }
        except OSError as exc:
            err_num = getattr(exc, "errno", None) or getattr(exc, "winerror", None)
            history.append({
                "elapsed": round(time.monotonic() - t_start, 4),
                "error": str(exc),
                "errno": err_num,
            })
            if err_num not in (errno.EADDRINUSE, 10048):
                raise
            rem = deadline - time.monotonic()
            if rem <= 0.05:
                raise TimeoutError(
                    f"Port {port} not released within cleanup budget (history: {history})"
                ) from exc
            time.sleep(min(0.2, rem))


def assert_port_released(port: int, deadline: float | None = None) -> dict[str, Any]:
    effective_deadline = deadline if deadline is not None else (time.monotonic() + 1.0)
    return verify_port_released(port, deadline=effective_deadline)


def verify_sha256(archive: Path, expected: str) -> str:
    digest = hashlib.sha256()
    with archive.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    actual, exp = digest.hexdigest().lower(), expected.strip().lower()
    if actual != exp:
        raise ValueError(f"Archive SHA256 mismatch: expected {exp}, got {actual}")
    return actual


def safe_extract_zip(archive: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    resolved_target = target_dir.resolve()
    with zipfile.ZipFile(archive, "r") as z:
        for member in z.infolist():
            name = member.filename
            if name.startswith("/") or name.startswith("\\") or ".." in name.replace("\\", "/").split("/"):
                raise ValueError(f"Illegal zip member name: {name}")
            dest = (resolved_target / name).resolve()
            if not dest.is_relative_to(resolved_target):
                raise ValueError(f"Zip member {name!r} escapes extraction target: {dest}")
        z.extractall(resolved_target)
    install_dir = resolved_target / "Galgame2Voice"
    if not (install_dir / "Galgame2Voice.exe").is_file():
        raise FileNotFoundError(f"Missing Galgame2Voice.exe in extracted archive: {install_dir}")
    return install_dir


def create_synthetic_fixtures(app_root: Path) -> None:
    synth = app_root / "synthetic"
    synth.mkdir(parents=True, exist_ok=True)
    (synth / "gpt.ckpt").write_bytes(b"SYNTHETIC_GPT_PLACEHOLDER")
    (synth / "sovits.pth").write_bytes(b"SYNTHETIC_SOVITS_PLACEHOLDER")
    with wave.open(str(synth / "ref.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * (16000 * 4))


def stop_process(proc: subprocess.Popen | None, deadline: float) -> int | None:
    if proc is None:
        return None
    if proc.poll() is not None:
        return proc.returncode

    rem = deadline - time.monotonic()
    if rem <= 0:
        try:
            proc.kill()
        except OSError:
            pass
        return proc.poll()

    try:
        proc.terminate()
        term_wait = min(5.0, rem * 0.7)
        return proc.wait(timeout=term_wait)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
        except OSError:
            pass
        rem_kill = deadline - time.monotonic()
        if rem_kill <= 0:
            return proc.poll()
        try:
            return proc.wait(timeout=min(3.0, rem_kill))
        except (subprocess.TimeoutExpired, OSError):
            return proc.poll()


class CaseInsensitiveDict(dict):
    """Case-insensitive dictionary for HTTP response headers."""

    def __init__(self, data: Any = None, **kwargs: Any) -> None:
        super().__init__()
        if data:
            items = data.items() if hasattr(data, "items") else data
            for k, v in items:
                self[k] = v
        for k, v in kwargs.items():
            self[k] = v

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(key.lower() if isinstance(key, str) else key, value)

    def __getitem__(self, key: Any) -> Any:
        return super().__getitem__(key.lower() if isinstance(key, str) else key)

    def __contains__(self, key: Any) -> bool:
        return super().__contains__(key.lower() if isinstance(key, str) else key)

    def get(self, key: Any, default: Any = None) -> Any:
        return super().get(key.lower() if isinstance(key, str) else key, default)


def request_http(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    data: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req_headers = {"Accept-Encoding": "identity", **(headers or {})}
    if body is not None and "Content-Type" not in req_headers:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)

    deadline = time.monotonic() + timeout
    with opener.open(req, timeout=timeout) as resp:
        fp = getattr(resp, "fp", None)
        raw = getattr(fp, "raw", None)
        sock_obj = getattr(raw, "_sock", None)

        chunks: list[bytes] = []
        while time.monotonic() < deadline:
            if getattr(resp, "isclosed", lambda: False)() or getattr(resp, "length", None) == 0:
                break
            if sock_obj is not None and getattr(sock_obj, "fileno", lambda: 0)() == -1:
                break

            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError(f"HTTP read to {url} exceeded wall-clock budget of {timeout}s")
            if sock_obj is not None:
                sock_obj.settimeout(max(0.1, rem))
            try:
                chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
            except (socket.timeout, TimeoutError):
                raise TimeoutError(f"HTTP read to {url} exceeded wall-clock budget of {timeout}s") from None
            if not chunk:
                break
            chunks.append(chunk)

            if getattr(resp, "isclosed", lambda: False)() or getattr(resp, "length", None) == 0:
                break
            if sock_obj is not None and getattr(sock_obj, "fileno", lambda: 0)() == -1:
                break
        else:
            raise TimeoutError(f"HTTP read to {url} exceeded wall-clock budget of {timeout}s")

        return resp.status, CaseInsensitiveDict(resp.headers), b"".join(chunks)


def wait_for_health(
    opener: urllib.request.OpenerDirector, url: str, proc: subprocess.Popen, timeout: float = 15.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Process {proc.pid} exited prematurely with code {proc.returncode}")
        try:
            status, _, _ = request_http(opener, "GET", url, timeout=1.5)
            if status == 200:
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    raise TimeoutError(f"Health check at {url} timed out after {timeout}s")


def read_sse_bounded(
    host: str,
    port: int,
    path: str,
    payload: dict,
    app_base: str,
    sse_timeout: float = 45.0,
) -> tuple[int, int, str, str, bool, list[dict], float, float]:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Origin": app_base,
        "Accept": "text/event-stream",
        "Accept-Encoding": "identity",
        "Content-Length": str(len(body)),
    }
    deadline = time.monotonic() + sse_timeout
    conn = http.client.HTTPConnection(host, port, timeout=min(10.0, sse_timeout))
    conn.connect()
    sock = conn.sock
    assert sock is not None, "Failed to establish transport socket"

    resp: http.client.HTTPResponse | None = None
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"SSE stream initiation failed with HTTP {resp.status}")

        user_msg_id: int | None = None
        line_idx: int | None = None
        ja_text: str | None = None
        emotion: str = "gentle"
        done_seen = False
        events: list[dict] = []
        cur_event: str | None = None
        first_line_time: float | None = None
        close_time: float | None = None

        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        buffer = ""

        while time.monotonic() < deadline:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError(f"SSE stream reading timed out after {sse_timeout}s")
            sock.settimeout(max(0.1, rem))
            try:
                chunk = resp.read1(4096)
            except (socket.timeout, TimeoutError):
                raise TimeoutError(f"SSE stream reading timed out after {sse_timeout}s") from None
            if not chunk:
                break

            buffer += decoder.decode(chunk)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.rstrip("\r")
                if not line:
                    cur_event = None
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    cur_event = line[6:].strip()
                    continue
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    try:
                        pdata = json.loads(data_str)
                    except json.JSONDecodeError:
                        pdata = {"raw": data_str}
                    t_now = time.time()
                    events.append({"event": cur_event, "data": pdata, "t": t_now})
                    if cur_event == "turn_started":
                        user_msg_id = int(pdata.get("user_message_id", 0))
                    elif cur_event == "line":
                        if line_idx is None:
                            line_idx = int(pdata.get("line_index", pdata.get("index", 0)))
                            ja_text = pdata.get("ja")
                            emotion = pdata.get("emotion") or emotion
                            first_line_time = t_now
                    elif cur_event == "done":
                        done_seen = True

            if user_msg_id and user_msg_id > 0 and line_idx is not None and ja_text:
                if done_seen:
                    raise RuntimeError(f"done event was received before or with first line: {events}")
                close_time = time.time()
                return user_msg_id, line_idx, ja_text, emotion, done_seen, events, first_line_time, close_time

        if not user_msg_id or line_idx is None or not ja_text:
            raise RuntimeError(f"Did not receive turn_started and first line in SSE stream (events: {events})")
        close_time = time.time()
        return user_msg_id, line_idx, ja_text, emotion, done_seen, events, first_line_time or close_time, close_time
    finally:
        close_errs: list[Exception] = []
        if resp is not None:
            try:
                resp.close()
            except Exception as e:
                close_errs.append(e)
        try:
            sock.close()
        except Exception as e:
            close_errs.append(e)
        try:
            conn.close()
        except Exception as e:
            close_errs.append(e)

        if close_errs:
            active_exc = sys.exception()
            eg_type = getattr(builtins, "ExceptionGroup", None)
            if active_exc is not None:
                if eg_type is not None:
                    raise eg_type("SSE stream failure and cleanup errors", [active_exc, *close_errs]) from active_exc
                err_summary = "; ".join(str(e) for e in close_errs)
                raise RuntimeError(f"SSE stream failure with cleanup errors: {err_summary}") from active_exc
            if len(close_errs) == 1:
                raise close_errs[0]
            if eg_type is not None:
                raise eg_type("SSE cleanup errors", close_errs)
            err_summary = "; ".join(str(e) for e in close_errs)
            raise RuntimeError(f"Multiple SSE cleanup errors: {err_summary}")


def verify_sqlite_turn_cardinality(
    db_path: Path,
    session_id: str,
    u_id: int,
    assistant_id: int,
    l_idx: int,
    repair_audio_url: str,
) -> dict[str, Any]:
    assert db_path.is_file(), f"Database file missing: {db_path}"
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA user_version;")
        ver = cur.fetchone()[0]
        assert ver == 7, f"Expected user_version 7, got {ver}"

        cur.execute("PRAGMA table_info(messages);")
        cols = {r[1] for r in cur.fetchall()}
        assert "reply_to_message_id" in cols and "lines_json" in cols

        # Strict global check: total message rows across entire DB must remain exactly 2
        cur.execute("SELECT count(*) FROM messages;")
        total_in_db = cur.fetchone()[0]
        assert total_in_db == 2, f"Expected exactly 2 total messages in database, got {total_in_db}"

        cur.execute(
            "SELECT id, role, lines_json, reply_to_message_id FROM messages WHERE session_id = ? ORDER BY id ASC;",
            (session_id,),
        )
        rows = cur.fetchall()
        assert len(rows) == 2, f"Expected exactly 2 messages in session {session_id}, got {len(rows)}"
        u_row, a_row = rows[0], rows[1]
        assert u_row[0] == u_id and u_row[1] == "user"
        assert a_row[0] == assistant_id and a_row[1] == "assistant" and a_row[3] == u_id
        a_lines = json.loads(a_row[2]) if a_row[2] else []
        a_line = next((it for it in a_lines if it.get("index") == l_idx), None)
        assert a_line is not None and a_line["audio"]["chunks"][0]["audio_url"] == repair_audio_url
        return {
            "user_version": ver,
            "columns": sorted(list(cols)),
            "total_messages": total_in_db,
            "rows_count": len(rows),
            "assistant_reply_to": a_row[3],
            "verified_audio_url": repair_audio_url,
        }
    finally:
        conn.close()


def run(argv: list[str] | None = None) -> int:
    t_start = time.monotonic()
    total_deadline = t_start + 180.0
    work_deadline = t_start + 160.0

    args = parse_args(argv)
    scratch = validate_scratch_path(args.scratch)

    archive = args.archive.resolve()
    standins_script = args.standins_script.resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"Archive not found: {archive}")
    if not standins_script.is_file():
        raise FileNotFoundError(f"Standins script not found: {standins_script}")

    llm_port, tts_port, app_port = reserve_port(), reserve_port(), reserve_port()
    app_base = f"http://127.0.0.1:{app_port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    standins_proc, app_proc1, app_proc2 = None, None, None
    standins_f, app1_f, app2_f = None, None, None
    res_path = scratch / "result.json"

    evidence: dict = {
        "passed": False,
        "archive": str(archive),
        "expected_sha": args.expected_sha,
        "ports": {"app": app_port, "llm": llm_port, "tts": tts_port},
    }
    main_error: str | None = None
    cleanup_errors: list[str] = []
    pid1, ret1, pid2, ret2 = None, None, None, None
    ret_standin = None

    def check_work_budget(step_name: str) -> float:
        rem = work_deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError(f"Work budget (160s) exceeded before {step_name}")
        return rem

    try:
        check_work_budget("sha256")
        act_sha = verify_sha256(archive, args.expected_sha)
        evidence["archive_sha256"] = act_sha

        check_work_budget("safe_extract")
        app_root = safe_extract_zip(archive, scratch / "install")
        create_synthetic_fixtures(app_root)

        with zipfile.ZipFile(archive, "r") as z:
            namelist = set(z.namelist())
            if "Galgame2Voice/galgame2voice/static/index.html" in namelist:
                index_zip_path = "Galgame2Voice/galgame2voice/static/index.html"
                zip_static_prefix = "Galgame2Voice/galgame2voice/static"
            elif "Galgame2Voice/static/index.html" in namelist:
                index_zip_path = "Galgame2Voice/static/index.html"
                zip_static_prefix = "Galgame2Voice/static"
            else:
                raise FileNotFoundError("Could not find static/index.html in zip archive")

            exp_index = z.read(index_zip_path)
            m_js = re.search(r'src="(/static/assets/[^\"]+\.js)"', exp_index.decode("utf-8"))
            m_css = re.search(r'href="(/static/assets/[^\"]+\.css)"', exp_index.decode("utf-8"))
            if not m_js or not m_css:
                raise RuntimeError("Could not find JS/CSS asset links in static/index.html")
            js_url_path, css_url_path = m_js.group(1), m_css.group(1)
            js_subpath = js_url_path[len("/static/"):].lstrip("/")
            css_subpath = css_url_path[len("/static/"):].lstrip("/")
            exp_js = z.read(f"{zip_static_prefix}/{js_subpath}")
            exp_css = z.read(f"{zip_static_prefix}/{css_subpath}")
            evidence["static_hashes"] = {
                "index_html": hashlib.sha256(exp_index).hexdigest(),
                "js": hashlib.sha256(exp_js).hexdigest(),
                "css": hashlib.sha256(exp_css).hexdigest(),
            }

        check_work_budget("standins_start")
        standins_f = (scratch / "standins.log").open("wb")
        standins_cmd = [
            args.python, str(standins_script),
            "--llm-port", str(llm_port), "--tts-port", str(tts_port),
            "--llm-format", "lines", "--seed", "1", "--first-token-ms", "100",
            "--tokens-per-sec", "10", "--tts-base-ms", "5000", "--tts-latency-factor", "0",
        ]
        standins_proc = subprocess.Popen(
            standins_cmd, stdout=standins_f, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        wait_for_health(opener, f"http://127.0.0.1:{tts_port}/health", standins_proc, timeout=min(15.0, check_work_budget("standins_health")))
        evidence["standins_pid"] = standins_proc.pid

        env = {
            k: v for k, v in os.environ.items()
            if k.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PROGRAMDATA", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}
        }
        env.update({
            "PATH": str(Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32"),
            "TEMP": str(scratch), "TMP": str(scratch),
            "GALGAME2VOICE_AUTH_DISABLED": "1",
            "GALGAME2VOICE_GPT_SOVITS_BASE_URL": f"http://127.0.0.1:{tts_port}",
        })
        for bad_key in ["GALGAME2VOICE_PROJECT_ROOT", "GALGAME2VOICE_DB_PATH", "DB_PATH"]:
            env.pop(bad_key, None)

        check_work_budget("app1_start")
        executable = app_root / "Galgame2Voice.exe"
        app1_f = (scratch / "app1.log").open("wb")
        app_cmd = [str(executable), "--no-engine", "--no-browser", "--host", "127.0.0.1", "--port", str(app_port)]
        app_proc1 = subprocess.Popen(
            app_cmd, cwd=str(app_root), env=env, stdout=app1_f, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        wait_for_health(opener, f"{app_base}/api/health", app_proc1, timeout=min(15.0, check_work_budget("app1_health")))
        pid1 = app_proc1.pid
        evidence["pid1"] = pid1

        # Static assets exact byte comparison
        check_work_budget("static_check")
        st_i, h_i, b_i = request_http(opener, "GET", f"{app_base}/", timeout=min(5.0, check_work_budget("get_index")))
        st_j, h_j, b_j = request_http(opener, "GET", f"{app_base}{js_url_path}", timeout=min(5.0, check_work_budget("get_js")))
        st_c, h_c, b_c = request_http(opener, "GET", f"{app_base}{css_url_path}", timeout=min(5.0, check_work_budget("get_css")))

        evidence["static_verification"] = {
            "index_html": {
                "status": st_i,
                "content_type": h_i.get("Content-Type"),
                "actual_length": len(b_i),
                "expected_length": len(exp_index),
                "actual_sha256": hashlib.sha256(b_i).hexdigest(),
                "expected_sha256": hashlib.sha256(exp_index).hexdigest(),
            },
            "js": {
                "url": js_url_path,
                "status": st_j,
                "content_type": h_j.get("Content-Type"),
                "actual_length": len(b_j),
                "expected_length": len(exp_js),
                "actual_sha256": hashlib.sha256(b_j).hexdigest(),
                "expected_sha256": hashlib.sha256(exp_js).hexdigest(),
            },
            "css": {
                "url": css_url_path,
                "status": st_c,
                "content_type": h_c.get("Content-Type"),
                "actual_length": len(b_c),
                "expected_length": len(exp_css),
                "actual_sha256": hashlib.sha256(b_c).hexdigest(),
                "expected_sha256": hashlib.sha256(exp_css).hexdigest(),
            },
        }

        assert st_i == 200, f"Index HTML status expected 200, got {st_i}"
        assert b_i == exp_index, f"Index HTML byte mismatch: act_len={len(b_i)}, exp_len={len(exp_index)}"

        assert st_j == 200, f"Static JS status expected 200, got {st_j}"
        assert b_j == exp_js, f"Static JS byte mismatch: act_len={len(b_j)}, exp_len={len(exp_js)}"
        ct_js = (h_j.get("Content-Type") or "").lower()
        assert "javascript" in ct_js, f"Static JS MIME expected 'javascript', got {ct_js!r}"

        assert st_c == 200, f"Static CSS status expected 200, got {st_c}"
        assert b_c == exp_css, f"Static CSS byte mismatch: act_len={len(b_c)}, exp_len={len(exp_css)}"
        ct_css = (h_c.get("Content-Type") or "").lower()
        assert "css" in ct_css, f"Static CSS MIME expected 'css', got {ct_css!r}"

        # Config & Provider & Voice setup
        check_work_budget("config_setup")
        cfg1 = json.loads(request_http(opener, "POST", f"{app_base}/api/config",
            data={"settings": {"allow_private_llm_endpoints": True, "gpt_sovits_url": f"http://127.0.0.1:{tts_port}"}},
            headers={"Origin": app_base}, timeout=min(5.0, check_work_budget("cfg1")))[2].decode("utf-8"))
        assert cfg1.get("status") == "success"

        prov = json.loads(request_http(opener, "PUT", f"{app_base}/api/providers/openai",
            data={"name": "Synthetic standin", "api_base_url": f"http://127.0.0.1:{llm_port}/v1", "api_key": "standin", "chat_model": "standin"},
            headers={"Origin": app_base}, timeout=min(5.0, check_work_budget("prov")))[2].decode("utf-8"))
        assert prov.get("status") in ("success", "created")

        cfg2 = json.loads(request_http(opener, "POST", f"{app_base}/api/config",
            data={"settings": {"active_provider_id": "openai"}}, headers={"Origin": app_base},
            timeout=min(5.0, check_work_budget("cfg2")))[2].decode("utf-8"))
        assert cfg2.get("status") == "success"

        prof = json.loads(request_http(opener, "POST", f"{app_base}/api/voice/profiles",
            data={
                "name": "合成验收角色", "gpt_weights_path": "synthetic/gpt.ckpt",
                "sovits_weights_path": "synthetic/sovits.pth", "ref_audio_path": "synthetic/ref.wav",
                "prompt_text": "こんにちは。", "refer_language": "ja", "text_lang": "ja", "system_prompt": "合成验收角色。",
            }, headers={"Origin": app_base}, timeout=min(5.0, check_work_budget("prof")))[2].decode("utf-8"))
        profile_id = int(prof.get("id", 0))
        assert profile_id > 0
        evidence["profile_id"] = profile_id

        sw = json.loads(request_http(opener, "POST", f"{app_base}/api/voice/switch",
            data={"profile_id": profile_id, "force": True}, headers={"Origin": app_base},
            timeout=min(5.0, check_work_budget("switch")))[2].decode("utf-8"))
        assert sw.get("status") == "switched"

        sess = json.loads(request_http(opener, "POST", f"{app_base}/api/chat/sessions",
            data={"id": "portable_turn_reply", "title": "合成中断验收", "voice_profile_id": profile_id, "settings": {"voiceProfileId": profile_id}},
            headers={"Origin": app_base}, timeout=min(5.0, check_work_budget("sess")))[2].decode("utf-8"))
        assert sess.get("status") == "ok", f"Expected status 'ok', got {sess}"
        s_obj = sess.get("session") or {}
        assert s_obj.get("id") == "portable_turn_reply", f"Expected session.id 'portable_turn_reply', got {s_obj}"
        assert s_obj.get("voice_profile_id") == profile_id, f"Expected voice_profile_id {profile_id}, got {s_obj}"

        # SSE Stream
        synth_prompt = "合成验收提问"
        sse_budget = min(45.0, check_work_budget("sse_stream"))
        u_id, l_idx, ja_text, emotion, done_seen, events, first_line_t, close_t = read_sse_bounded(
            "127.0.0.1", app_port, "/api/chat/stream",
            payload={
                "prompt": synth_prompt, "session_id": "portable_turn_reply",
                "reply_format": "lines", "provider_id": "openai",
                "voice_profile_id": profile_id, "character_name": "合成验收角色",
            },
            app_base=app_base, sse_timeout=sse_budget,
        )
        assert not done_seen, "done event was received unexpectedly before stream close"
        evidence.update({
            "user_message_id": u_id, "line_index": l_idx, "ja_text": ja_text,
            "emotion": emotion, "sse_done_seen": done_seen, "sse_events": events,
            "sse_first_line_time": first_line_t, "sse_close_time": close_t,
        })

        # Bounded turn-reply query
        lookup_budget = min(15.0, check_work_budget("turn_reply_lookup"))
        lookup_end, assistant_id, turn_hist = time.monotonic() + lookup_budget, None, []
        while time.monotonic() < lookup_end:
            rem_req = min(3.0, max(0.5, lookup_end - time.monotonic()))
            st, _, b = request_http(opener, "GET", f"{app_base}/api/chat/turn-reply?session_id=portable_turn_reply&user_message_id={u_id}",
                                    headers={"Origin": app_base}, timeout=rem_req)
            assert st == 200
            td = json.loads(b.decode("utf-8"))
            turn_hist.append(td)
            assert td.get("session_id") == "portable_turn_reply" and td.get("user_message_id") == u_id
            if td.get("status") == "ready":
                assistant_id = int(td.get("message_id", 0))
                break
            time.sleep(0.2)
        if not assistant_id or assistant_id <= 0:
            raise TimeoutError(f"Assistant turn-reply did not become ready (history: {turn_hist})")
        evidence.update({"assistant_message_id": assistant_id, "turn_lookup_history": turn_hist})

        # Single regenerate-line repair
        check_work_budget("regenerate_line")
        regen_st, _, regen_b = request_http(
            opener, "POST", f"{app_base}/api/voice/regenerate-line",
            data={
                "session_id": "portable_turn_reply", "message_id": assistant_id, "line_index": l_idx,
                "text": ja_text, "voice_profile_id": profile_id, "emotion": emotion, "ai_adaptive_voice": False,
            },
            headers={"Origin": app_base}, timeout=min(30.0, check_work_budget("regen_req")),
        )
        assert regen_st == 200, f"regenerate-line failed: {regen_b!r}"
        regen_d = json.loads(regen_b.decode("utf-8"))
        assert regen_d.get("persisted") is True and regen_d.get("session_id") == "portable_turn_reply"
        assert regen_d.get("message_id") == assistant_id and regen_d.get("line_index") == l_idx
        assert regen_d.get("voice_profile_id") == profile_id
        repair_audio_url = regen_d["audio"]["chunks"][0]["audio_url"]
        assert repair_audio_url
        evidence.update({
            "regenerate_line_request_count": 1,
            "regenerate_line_response": regen_d,
            "repair_audio_url": repair_audio_url,
        })

        # Stop app 1 cleanly
        check_work_budget("stop_app1")
        app1_deadline = time.monotonic() + min(10.0, check_work_budget("wait_app1"))
        ret1 = stop_process(app_proc1, deadline=app1_deadline)
        if app_proc1.poll() is None:
            raise RuntimeError(f"app_proc1 (PID {pid1}) did not exit cleanly")
        evidence["ret1"] = ret1

        # Start app 2 with same port and database
        check_work_budget("app2_start")
        app2_f = (scratch / "app2.log").open("wb")
        app_proc2 = subprocess.Popen(
            app_cmd, cwd=str(app_root), env=env, stdout=app2_f, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        wait_for_health(opener, f"{app_base}/api/health", app_proc2, timeout=min(15.0, check_work_budget("app2_health")))
        pid2 = app_proc2.pid
        assert pid2 != pid1, f"Expected new PID on restart, got {pid2}"
        evidence["pid2"] = pid2

        # Verify history & repair audio
        check_work_budget("history_check")
        h_st, _, h_b = request_http(opener, "GET", f"{app_base}/api/chat/history?session_id=portable_turn_reply&limit=100",
                                    headers={"Origin": app_base}, timeout=min(5.0, check_work_budget("get_history")))
        assert h_st == 200
        h_msgs = [m for m in json.loads(h_b.decode("utf-8")).get("messages", []) if m.get("id") == assistant_id]
        assert len(h_msgs) == 1
        m_line = next((it for it in (h_msgs[0].get("lines") or []) if it.get("index") == l_idx), None)
        assert m_line is not None and m_line.get("ja") == ja_text
        assert m_line["audio"]["chunks"][0]["audio_url"] == repair_audio_url

        check_work_budget("audio_fetch")
        au_st, _, au_b = request_http(opener, "GET", f"{app_base}{repair_audio_url}", timeout=min(10.0, check_work_budget("get_audio")))
        assert au_st == 200 and len(au_b) >= 12 and au_b[0:4] == b"RIFF" and au_b[8:12] == b"WAVE"
        evidence["audio_verification"] = {"status": au_st, "size": len(au_b), "riff": "RIFF", "wave": "WAVE"}

        # SQLite direct read-only verification
        check_work_budget("sqlite_check")
        db_path = app_root / "data" / "galgame2voice.db"
        evidence["sqlite_verification"] = verify_sqlite_turn_cardinality(
            db_path=db_path,
            session_id="portable_turn_reply",
            u_id=u_id,
            assistant_id=assistant_id,
            l_idx=l_idx,
            repair_audio_url=repair_audio_url,
        )

        if getattr(args, "with_browser_ui", False):
            check_work_budget("browser_ui_acceptance")
            try:
                from scripts.smoke_windows_ui import run_browser_ui_acceptance
            except ModuleNotFoundError:
                from smoke_windows_ui import run_browser_ui_acceptance

            ui_budget = min(40.0, check_work_budget("browser_ui_run"))
            ui_evidence = run_browser_ui_acceptance(
                base_url=app_base,
                scratch=scratch / "browser_ui",
                target_session_id="portable_turn_reply",
                prompt_text=synth_prompt,
                repair_audio_url=repair_audio_url,
                timeout=ui_budget,
            )
            evidence["browser_ui_verification"] = ui_evidence

            # Post-UI assertions: verify database cardinality and upstream generation state
            # were not mutated by browser initialization, metadata sync, or page navigation.
            check_work_budget("browser_ui_post_check")
            evidence["browser_ui_post_sqlite"] = verify_sqlite_turn_cardinality(
                db_path=db_path,
                session_id="portable_turn_reply",
                u_id=u_id,
                assistant_id=assistant_id,
                l_idx=l_idx,
                repair_audio_url=repair_audio_url,
            )
            post_st, _, post_b = request_http(
                opener,
                "GET",
                f"{app_base}/api/chat/history?session_id=portable_turn_reply&limit=100",
                headers={"Origin": app_base},
                timeout=min(5.0, check_work_budget("post_get_history")),
            )
            assert post_st == 200, f"Expected 200 on post-UI history check, got {post_st}"
            post_msgs = json.loads(post_b.decode("utf-8")).get("messages", [])
            assert len(post_msgs) == 2, f"Expected exactly 2 history messages post-UI, got {len(post_msgs)}"
            assert post_msgs[0].get("id") == u_id and post_msgs[1].get("id") == assistant_id

    except Exception as exc:
        main_error = f"{type(exc).__name__}: {exc}"
    finally:
        # Resource cleanup strictly bounded by remaining time until total_deadline (180s)
        for p_name, proc in [("app_proc2", app_proc2), ("app_proc1", app_proc1), ("standins_proc", standins_proc)]:
            if proc is not None:
                try:
                    ec = stop_process(proc, deadline=total_deadline)
                    if p_name == "app_proc2":
                        ret2 = ec
                    elif p_name == "app_proc1" and ret1 is None:
                        ret1 = ec
                    elif p_name == "standins_proc":
                        ret_standin = ec
                    if proc.poll() is None:
                        cleanup_errors.append(f"{p_name} did not exit after cleanup attempt")
                except Exception as ce:
                    cleanup_errors.append(f"Failed stopping {p_name}: {ce}")

        evidence.update({"ret1": ret1, "ret2": ret2, "ret_standin": ret_standin})

        for fname, fhandle in [("standins_log", standins_f), ("app1_log", app1_f), ("app2_log", app2_f)]:
            if fhandle and not fhandle.closed:
                try:
                    fhandle.close()
                except Exception as fe:
                    cleanup_errors.append(f"Failed closing {fname}: {fe}")

        rebind_results = {}
        for p_name, port in [("app", app_port), ("llm", llm_port), ("tts", tts_port)]:
            try:
                rebind_res = verify_port_released(port, deadline=total_deadline)
                rebind_results[p_name] = rebind_res
            except Exception as pe:
                cleanup_errors.append(f"Port {port} ({p_name}) rebind check failed: {pe}")
                rebind_results[p_name] = f"error: {pe}"
        evidence["port_rebind_results"] = rebind_results

        if cleanup_errors:
            evidence["cleanup_errors"] = cleanup_errors
        if main_error:
            evidence["main_error"] = main_error

    all_passed = (main_error is None) and (len(cleanup_errors) == 0)
    evidence["passed"] = all_passed
    try:
        res_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as we:
        cleanup_errors.append(f"Failed to write result.json: {we}")
        all_passed = False

    if not all_passed:
        err_msg = f"Driver failed. Main error: {main_error}; Cleanup errors: {cleanup_errors}"
        print(f"FAIL: {err_msg}", file=sys.stderr)
        return 1

    print(f"PASS: Out-of-band business driver completed successfully. Results saved in {res_path}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
