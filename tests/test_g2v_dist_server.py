"""Tests for scripts/g2v_dist/g2v_dist.py distribution server and admin CLI."""

import base64
import hashlib
import io
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import urllib.error
import urllib.request
import zipfile

import pytest

from scripts.g2v_dist.g2v_dist import (
    CodeFormatError,
    create_server,
    encode_code,
    main as dist_main,
    parse_code,
    resolve_client_ip,
)


def _make_zip(path: Path, *, manifest_path: str = "pkg/manifest.json", name: str = "测试角色") -> tuple[bytes, str]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(manifest_path, json.dumps({"name": name}, ensure_ascii=False))
        zf.writestr("pkg/voice.bin", b"synthetic-character-package-data-" * 2500)
    raw = buf.getvalue()
    path.write_bytes(raw)
    return raw, hashlib.sha256(raw).hexdigest()


def _req(
    url: str, *, method: str = "GET", body: bytes | None = None, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    r = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        data = exc.read()
        exc.close()
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, data


@pytest.fixture()
def dist_env(tmp_path: Path):
    now = [1_700_000_000.0]
    state_dir = tmp_path / "state"
    httpd = create_server(state_dir, host="127.0.0.1", port=0, prefix="/_g2v", clock=lambda: now[0])
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield {
            "store": httpd.store,
            "state_dir": state_dir,
            "now": now,
            "base_url": f"http://{host}:{port}/_g2v",
            "tmp_path": tmp_path,
        }
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_encode_and_parse_code_roundtrip_and_invalid_formats() -> None:
    secret = secrets.token_bytes(32)
    code = encode_code("https://example.com/_g2v/", secret)
    assert code.startswith("G2V1.") and "=" not in code
    url, code_secret = parse_code(f" \n\t {code} \r\n ")
    assert url == "https://example.com/_g2v" and encode_code(url, code_secret) == code
    for bad in (
        "",
        "G2V1.two",
        f"{code}=",
        "G2V1.aHR0cHM6Ly9leGFtcGxlLmNvbQ.c2hvcnQ",
        "G2V1.bm90LWEtdXJs.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    ):
        with pytest.raises(CodeFormatError) as exc_info:
            parse_code(bad)
        assert any("\u4e00" <= ch <= "\u9fff" for ch in str(exc_info.value))


def test_redeem_filters_allowed_packages_and_unifies_401(dist_env) -> None:
    store, now, base_url, tmp_path = dist_env["store"], dist_env["now"], dist_env["base_url"], dist_env["tmp_path"]
    _make_zip(tmp_path / "a.zip", name="角色A")
    _make_zip(tmp_path / "b.zip", name="角色B")
    store.add_package(tmp_path / "a.zip", package_id="pkg_a")
    store.add_package(tmp_path / "b.zip", package_id="pkg_b")

    _, secret_a = parse_code(store.create_code(server_url=base_url, packages=["pkg_a"], max_downloads=2)[0])
    st, _, body = _req(f"{base_url}/v1/redeem", method="POST", body=json.dumps({"code_secret": secret_a}).encode())
    assert st == 200 and [p["id"] for p in json.loads(body)["packages"]] == ["pkg_a"]

    exp_code, _ = store.create_code(server_url=base_url, allow_all=True, expires_days=1)
    rev_code, rev_id = store.create_code(server_url=base_url, allow_all=True, expires_days=10)
    store.revoke_code(rev_id)
    now[0] += 2 * 86400
    for bad_sec in (
        parse_code(exp_code)[1],
        parse_code(rev_code)[1],
        parse_code(encode_code(base_url, secrets.token_bytes(32)))[1],
    ):
        st_bad, _, raw_bad = _req(
            f"{base_url}/v1/redeem", method="POST", body=json.dumps({"code_secret": bad_sec}).encode()
        )
        assert st_bad == 401 and json.loads(raw_bad) == {"error": "兑换码无效或已过期"}
    assert _req(f"{base_url}/v1/redeem", method="POST", body=b"{" + b" " * 5000 + b"}")[0] == 413


def test_download_full_package_sha256_cache_control_and_quota(dist_env) -> None:
    store, base_url, tmp_path = dist_env["store"], dist_env["base_url"], dist_env["tmp_path"]
    raw_bytes, expected_sha = _make_zip(tmp_path / "heroine.zip", name="女主")
    store.add_package(tmp_path / "heroine.zip", package_id="heroine")
    _, secret = parse_code(store.create_code(server_url=base_url, packages=["heroine"], max_downloads=2)[0])
    auth = {"Authorization": f"Bearer {secret}"}

    for _ in range(2):
        st, hdrs, content = _req(f"{base_url}/v1/packages/heroine", headers=auth)
        assert st == 200 and content == raw_bytes and hashlib.sha256(content).hexdigest() == expected_sha
        assert hdrs.get("etag") == f'"{expected_sha}"' and hdrs.get("accept-ranges") == "bytes"
        assert hdrs.get("cache-control") == "private, no-store"
    st_403, _, err_raw = _req(f"{base_url}/v1/packages/heroine", headers=auth)
    assert st_403 == 403 and json.loads(err_raw) == {"error": "这个角色包的下载次数已用完"}


def test_range_resume_without_extra_count_and_24h_window(dist_env) -> None:
    store, now, base_url, tmp_path = dist_env["store"], dist_env["now"], dist_env["base_url"], dist_env["tmp_path"]
    raw_bytes, _ = _make_zip(tmp_path / "vp.zip")
    store.add_package(tmp_path / "vp.zip", package_id="vp")
    _, secret = parse_code(store.create_code(server_url=base_url, packages=["vp"], max_downloads=1)[0])
    auth = {"Authorization": f"Bearer {secret}"}

    assert _req(f"{base_url}/v1/packages/vp", headers={**auth, "Range": "bytes=100-199"})[0] == 403
    st0, h0, p0 = _req(f"{base_url}/v1/packages/vp", headers={**auth, "Range": "bytes=0-99"})
    assert st0 == 206 and p0 == raw_bytes[:100] and h0.get("cache-control") == "private, no-store"

    now[0] += 3600
    st1, h1, p1 = _req(f"{base_url}/v1/packages/vp", headers={**auth, "Range": "bytes=100-"})
    assert st1 == 206 and h1.get("cache-control") == "private, no-store" and p0 + p1 == raw_bytes

    now[0] += 24 * 3600 + 1
    assert _req(f"{base_url}/v1/packages/vp", headers={**auth, "Range": "bytes=100-199"})[0] == 403


def test_rate_limit_blocks_after_11_failures_and_client_ip_trust(dist_env) -> None:
    base_url = dist_env["base_url"]
    bad_body = json.dumps({"code_secret": "bad"}).encode()
    hdrs = {"Content-Type": "application/json", "CF-Connecting-IP": "198.51.100.10"}
    for _ in range(11):
        assert _req(f"{base_url}/v1/redeem", method="POST", body=bad_body, headers=hdrs)[0] == 401
    st_429, h_429, _ = _req(f"{base_url}/v1/redeem", method="POST", body=bad_body, headers=hdrs)
    assert st_429 == 429 and int(h_429["retry-after"]) > 0

    assert (
        resolve_client_ip("127.0.0.1", {"CF-Connecting-IP": "198.51.100.1", "X-Forwarded-For": "198.51.100.2"})
        == "198.51.100.1"
    )
    assert resolve_client_ip("::1", {"X-Forwarded-For": "198.51.100.2, 10.0.0.1"}) == "198.51.100.2"
    assert (
        resolve_client_ip("203.0.113.77", {"CF-Connecting-IP": "198.51.100.1", "X-Forwarded-For": "198.51.100.2"})
        == "203.0.113.77"
    )


def test_package_id_traversal_returns_404_without_reading_outside_state_dir(dist_env) -> None:
    store, base_url, state_dir = dist_env["store"], dist_env["base_url"], dist_env["state_dir"]
    (state_dir / "outside.zip").write_bytes(b"SENSITIVE-OUTSIDE")
    (state_dir / "outside.json").write_text(
        json.dumps({"id": "outside", "name": "o", "size": 17, "sha256": "0" * 64}), encoding="utf-8"
    )
    _, secret = parse_code(store.create_code(server_url=base_url, allow_all=True)[0])
    for bad_id in ("../outside", "..%2foutside", "a/b", "a..b", "..", "UPPER"):
        st, _, body = _req(f"{base_url}/v1/packages/{bad_id}", headers={"Authorization": f"Bearer {secret}"})
        assert st == 404 and b"SENSITIVE-OUTSIDE" not in body


def test_admin_cli_workflow_and_no_plaintext_secret_in_db(dist_env) -> None:
    state_dir, base_url, tmp_path, clock = (
        dist_env["state_dir"],
        dist_env["base_url"],
        dist_env["tmp_path"],
        dist_env["store"]._clock,
    )
    zip_path = tmp_path / "Sakura Pack!.zip"
    _make_zip(zip_path, manifest_path="nested/manifest.json", name="樱")

    assert (
        dist_main(["--state-dir", str(state_dir), "add-package", str(zip_path)], stdout=io.StringIO(), clock=clock) == 0
    )
    out_code = io.StringIO()
    assert (
        dist_main(
            [
                "--state-dir",
                str(state_dir),
                "new-code",
                "--server-url",
                base_url,
                "--packages",
                "sakura_pack",
                "--note",
                "好友",
            ],
            stdout=out_code,
            clock=clock,
        )
        == 0
    )
    _, code_secret = parse_code(out_code.getvalue())
    raw_secret = base64.urlsafe_b64decode(code_secret + "=")
    db_bytes = (state_dir / "dist.db").read_bytes()
    assert code_secret.encode() not in db_bytes and raw_secret not in db_bytes

    with sqlite3.connect(state_dir / "dist.db") as c:
        code_id = c.execute("SELECT code_id FROM codes").fetchone()[0]
    out_list = io.StringIO()
    assert dist_main(["--state-dir", str(state_dir), "list"], stdout=out_list, clock=clock) == 0
    assert (
        "sakura_pack" in out_list.getvalue()
        and code_id in out_list.getvalue()
        and code_secret not in out_list.getvalue()
    )

    assert dist_main(["--state-dir", str(state_dir), "revoke", code_id], stdout=io.StringIO(), clock=clock) == 0
    assert (
        _req(f"{base_url}/v1/redeem", method="POST", body=json.dumps({"code_secret": code_secret}).encode())[0] == 401
    )
    assert (
        dist_main(["--state-dir", str(state_dir), "remove-package", "sakura_pack"], stdout=io.StringIO(), clock=clock)
        == 0
    )


def test_refuses_non_loopback_bind_unless_allow_public(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="非回环地址"):
        create_server(tmp_path / "s1", host="0.0.0.0", port=0, allow_public=False)
    httpd = create_server(tmp_path / "s2", host="0.0.0.0", port=0, allow_public=True)
    httpd.server_close()
