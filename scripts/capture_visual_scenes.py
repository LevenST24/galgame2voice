"""Asset-free headless browser visual scene capture harness (T12 step 1)."""

from __future__ import annotations

import argparse
from array import array
import base64
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence
import urllib.request
import wave
import zlib

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.smoke_windows_ui import find_browser, make_cdp_client, stop_browser_process, wait_for_initial_navigation  # noqa: E402

FORBIDDEN_PORTS: frozenset[int] = frozenset({8080, 8081, 9880, 18080, 18996, 18997, 18998})
SCENES: tuple[str, ...] = ("stage", "settings", "log", "loading", "error")
VIEWPORTS: tuple[tuple[str, int, int], ...] = (("1920x1080", 1920, 1080), ("1366x768", 1366, 768), ("narrow_900x700", 900, 700))
DEFAULT_OUTPUT_DIR = Path(".local/visual-captures")
_PROTECTED = ("characters", "backgrounds", "skins", "galgame2voice", "frontend", "data", "audio", "scripts", "tests", "docs", ".claude", ".git", ".github")


def resolve_output_dir(output_dir: Path, project_root: Path = PROJECT_ROOT) -> Path:
    root, raw = Path(project_root).resolve(), Path(output_dir)
    resolved = (raw if raw.is_absolute() else (root / raw)).resolve()
    if resolved == root or str(resolved).lower().replace("\\", "/").startswith("e:/yuzusoft"):
        raise ValueError(f"Refusing protected output directory: {resolved}")
    for rel in _PROTECTED:
        guard = (root / rel).resolve()
        if resolved == guard or resolved.is_relative_to(guard):
            raise ValueError(f"Refusing protected output directory inside '{rel}': {resolved}")
    return resolved


def build_capture_plan(output_dir: Path, *, scenes: Sequence[str] = SCENES, viewports: Sequence[tuple[str, int, int]] = VIEWPORTS) -> list[dict[str, Any]]:
    if not scenes or any(s not in SCENES for s in scenes):
        raise ValueError(f"Unknown scene in {list(scenes)!r}; expected subset of {SCENES}")
    known_vp = {vp[0] for vp in VIEWPORTS}
    if not viewports or any(vp[0] not in known_vp or vp[1] <= 0 or vp[2] <= 0 for vp in viewports):
        raise ValueError(f"Unknown or invalid viewport in {list(viewports)!r}")
    out = Path(output_dir)
    return [{"scene": s, "viewport_id": vp_id, "width": w, "height": h, "path": out / vp_id / f"{s}.png"} for s in scenes for vp_id, w, h in viewports]


def validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535) or port in FORBIDDEN_PORTS:
        raise ValueError(f"Port {port!r} is invalid or in FORBIDDEN_PORTS {sorted(FORBIDDEN_PORTS)}")
    return port


def allocate_ephemeral_ports(count: int = 3, *, exclude: Sequence[int] = ()) -> list[int]:
    sockets, ports, blocked, base = [], [], set(FORBIDDEN_PORTS).union(exclude), (os.getpid() * 17) % 10000
    try:
        for step in range(10000):
            if len(ports) >= count:
                return ports
            cand = 20000 + (base + step) % 10000
            if cand not in blocked:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.bind(("127.0.0.1", cand))
                    sockets.append(sock)
                    ports.append(validate_port(cand))
                except OSError:
                    sock.close()
        raise RuntimeError(f"Could not allocate {count} free ports in 20000..29999")
    finally:
        for sock in sockets:
            sock.close()


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_synthetic_png(path: Path, width: int, height: int, boxes: Sequence[tuple[int, int, int, int, tuple[int, int, int, int]]]) -> None:
    buf = bytearray(width * height * 4)
    for bx, by, bw, bh, rgba in boxes:
        x0, y0, x1, y1 = max(0, bx), max(0, by), min(width, bx + bw), min(height, by + bh)
        if x1 > x0 and y1 > y0:
            tile, span = bytes(rgba) * (x1 - x0), (x1 - x0) * 4
            for y in range(y0, y1):
                buf[(y * width + x0) * 4 : (y * width + x0) * 4 + span] = tile
    mv, stride = memoryview(buf), width * 4
    raw = b"".join(b"\x00" + bytes(mv[y * stride : (y + 1) * stride]) for y in range(height))
    path.parent.mkdir(parents=True, exist_ok=True)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(raw, 6)) + _png_chunk(b"IEND", b""))


def create_synthetic_character(characters_dir: Path, char_id: str = "synth_heroine") -> Path:
    char_dir, layers_dir = Path(characters_dir) / char_id, Path(characters_dir) / char_id / "portrait" / "layers" / "c1"
    layers_dir.mkdir(parents=True, exist_ok=True)
    pcm = array("h", (round(600 * math.sin(2 * math.pi * 220 * i / 16000)) for i in range(16000 * 4)))
    if sys.byteorder != "little":
        pcm.byteswap()
    with wave.open(str(char_dir / "gentle.wav"), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(pcm.tobytes())
    (char_dir / "manifest.json").write_text(json.dumps({
        "id": char_id, "name": "合成测试角色", "is_default": True, "default_emotion": "gentle", "gpt_weights": "standin_gpt.bin", "sovits_weights": "standin_sovits.bin",
        "system_prompt": "你是用于界面视觉采集的合成测试角色。", "emotions": {"gentle": {"audio": "gentle.wav", "text": "こんにちは、テスト用の音声です。", "lang": "ja"}},
    }, ensure_ascii=False), encoding="utf-8")
    specs = {
        "body": (10, [(150, 340, 300, 860, (74, 112, 168, 255)), (175, 70, 250, 270, (56, 72, 112, 255)), (205, 110, 190, 210, (252, 228, 210, 255))]),
        "blush": (20, [(218, 224, 34, 16, (246, 146, 160, 180)), (348, 224, 34, 16, (246, 146, 160, 180))]),
        "eyes_open": (30, [(232, 185, 36, 22, (42, 64, 120, 255)), (332, 185, 36, 22, (42, 64, 120, 255))]),
        "eyes_closed": (30, [(230, 196, 40, 5, (60, 50, 55, 255)), (330, 196, 40, 5, (60, 50, 55, 255))]),
        "mouth_closed": (40, [(280, 268, 40, 6, (206, 102, 108, 255))]), "mouth_open": (40, [(276, 260, 48, 24, (214, 92, 104, 255))]),
    }
    layers_meta = {}
    for name, (z, boxes) in specs.items():
        write_synthetic_png(layers_dir / f"{name}.png", 600, 1200, boxes)
        layers_meta[name] = {"canvas": "c1", "rect": {"x": 0, "y": 0, "width": 600, "height": 1200}, "z": z, "opacity": 1, "file": f"layers/c1/{name}.png"}
    links = {"eyesClosed": False, "blink": {"closed": "blink_closed"}, "mouth": {"closed": "normal", "open": "mouth_open"}}
    (char_dir / "portrait" / "layers.json").write_text(json.dumps({
        "version": 3, "defaultOutfit": "default", "defaultPose": "a", "canvases": {"c1": {"width": 600, "height": 1200}},
        "outfits": {"default": {"name": "常服", "icon": "🎒"}}, "poses": {"a": {"name": "默认站姿", "canvas": "c1"}},
        "faces": {"normal": {"label": "平常", "emotion": "gentle", "weights": {"gentle": 3}}, "smile": {"label": "微笑", "emotion": "happy", "weights": {"happy": 3}}},
        "layers": layers_meta, "looks": {"a/default": {
            "pose": "a", "outfit": "default", "crop": {"left": 0, "top": 0, "width": 600, "height": 1200},
            "faceRect": {"x": 150, "y": 60, "width": 300, "height": 300}, "headAnchor": {"x": 300, "y": 80}, "body": ["body"], "blush": ["blush"], "defaultFace": "normal",
            "faces": {
                "normal": {"layers": ["eyes_open", "mouth_closed"], **links}, "smile": {"layers": ["eyes_open", "mouth_open"], **links},
                "blink_closed": {"frameOnly": True, "layers": ["eyes_closed", "mouth_closed"], "eyesClosed": True},
                "mouth_open": {"frameOnly": True, "layers": ["eyes_open", "mouth_open"], "eyesClosed": False},
            },
        }},
    }, ensure_ascii=False), encoding="utf-8")
    return char_dir


def require_browser(explicit_browser: Path | None = None) -> Path:
    cand = Path(explicit_browser) if explicit_browser is not None else find_browser()
    if cand is None or not Path(cand).is_file():
        detail = f"指定的浏览器不存在：{explicit_browser}" if explicit_browser is not None else "未找到可用的 Chrome / Chromium / Edge 浏览器"
        sys.stderr.write(f"错误：{detail}；请安装浏览器或通过 --browser <可执行文件路径> 指定。\n")
        raise SystemExit(2)
    return Path(cand).resolve()


def _http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"} if body else {}, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_ready(url: str, proc: subprocess.Popen[Any], timeout: float = 25.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Subprocess {proc.pid} exited ({proc.returncode}) while waiting for {url}")
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for {url}")


def _eval_js(client: Any, expr: str) -> Any:
    res = client.send_command("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
    if "exceptionDetails" in res:
        raise RuntimeError(f"Browser JS error: {res['exceptionDetails']}")
    return res.get("result", {}).get("value")


def _capture_viewports(client: Any, scene: str, out_dir: Path) -> None:
    settle_js = "new Promise(r => { window.scrollTo(0, 0); document.documentElement.scrollTop = 0; document.body.scrollTop = 0; requestAnimationFrame(() => { document.getAnimations().forEach(a => { try { a.finish(); } catch {} }); window.scrollTo(0, 0); setTimeout(r, 80); }); })"
    for vp_id, width, height in VIEWPORTS:
        client.send_command("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
        _eval_js(client, settle_js)
        dest = out_dir / vp_id / f"{scene}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(client.send_command("Page.captureScreenshot", {"format": "png"})["data"]))
    client.send_command("Emulation.setDeviceMetricsOverride", {"width": 1920, "height": 1080, "deviceScaleFactor": 1, "mobile": False})
    _eval_js(client, settle_js)


def _drive_and_capture(client: Any, base_url: str, out_dir: Path) -> None:
    wait_for_initial_navigation(client, f"{base_url}/")
    _eval_js(client, """(async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      window.__g2vWait = async (fn, desc, ms = 12000) => { for (const t0 = Date.now(); Date.now() - t0 < ms; await sleep(60)) { window.scrollTo(0, 0); document.getAnimations().forEach(a => { try { a.finish(); } catch {} }); const v = await fn(); if (v) return v; } throw new Error('Timeout: ' + desc); };
      window.__g2vSend = async (text) => { if (document.querySelector('.adv-player-input-wrap.is-hidden')) document.querySelector('[data-action="input"]').click(); const ta = await window.__g2vWait(() => document.querySelector('.adv-player-textarea'), 'textarea'); ta.value = text; ta.dispatchEvent(new Event('input', { bubbles: true })); document.querySelector('.adv-player-send-btn').click(); window.scrollTo(0, 0); };
      (await window.__g2vWait(() => document.querySelector('[data-session="s_visual_capture"], .home-continue'), 'home')).click();
      await window.__g2vWait(() => document.querySelector('.adv-stage:not(.is-home)'), 'stage');
      await window.__g2vSend('你好，请展示测试台词。');
      await window.__g2vWait(() => { const imgs = [...document.querySelectorAll('.adv-layer-buffer.is-visible img.adv-sprite-layer')]; const p = document.querySelector('.adv-primary-text'), n = document.querySelector('.adv-name-text'); return document.querySelector('.adv-qm-status-dot.state-ok') && document.querySelector('.adv-click-glyph:not(.is-hidden)') && p && p.textContent.trim().length > 4 && n && n.textContent.trim().length > 0 && imgs.length > 0 && imgs.every(i => i.complete && i.naturalWidth > 0); }, 'stage ready');
    })()""")
    _capture_viewports(client, "stage", out_dir)
    _eval_js(client, "(async () => { document.querySelector('.adv-qm-btn-config').click(); await window.__g2vWait(() => document.querySelector('#globalModal:not(.hidden)'), 'settings'); })()")
    _capture_viewports(client, "settings", out_dir)
    _eval_js(client, "(async () => { document.querySelector('#globalModal [data-close]').click(); await window.__g2vWait(() => document.querySelector('#globalModal.hidden'), 'settings close'); })()")
    _eval_js(client, "(async () => { document.querySelector('.adv-qm-btn-log').click(); await window.__g2vWait(() => document.querySelector('.adv-log-overlay:not([hidden]):not(.hidden)') && document.querySelector('.adv-log-entry.is-user') && document.querySelector('.adv-log-entry.is-assistant .adv-log-replay-btn'), 'log'); })()")
    _capture_viewports(client, "log", out_dir)
    _eval_js(client, "(async () => { document.querySelector('.adv-log-close-btn').click(); await window.__g2vWait(() => !document.querySelector('.adv-log-overlay'), 'log close'); })()")
    _eval_js(client, "(async () => { window.__origFetch = window.fetch; window.fetch = (input, init) => String(input.url || input).includes('/api/chat/stream') ? new Promise((_, rej) => { window.__abortHeld = () => rej(new DOMException('Aborted', 'AbortError')); if (init && init.signal) init.signal.addEventListener('abort', window.__abortHeld, { once: true }); }) : window.__origFetch(input, init); await window.__g2vSend('请展示加载等待态。'); await window.__g2vWait(() => document.querySelector('.adv-thinking-dots:not(.is-hidden)'), 'loading'); })()")
    _capture_viewports(client, "loading", out_dir)
    _eval_js(client, "(async () => { window.fetch = window.__origFetch; document.querySelector('[data-action=\"stop\"]').click(); window.__abortHeld(); await window.__g2vWait(() => document.querySelector('.adv-qm-status-dot.state-ok'), 'stop loading'); })()")
    _http_json("DELETE", f"{base_url}/api/providers/openai")
    _http_json("PUT", f"{base_url}/api/providers/openai", {"api_key": "", "is_active": True})
    _eval_js(client, "(async () => { await window.__g2vSend('请展示错误提示态。'); await window.__g2vWait(() => document.querySelector('.adv-error-wrap:not(.is-hidden) .adv-error-btn'), 'error'); })()")
    _capture_viewports(client, "error", out_dir)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture 3 viewports x 5 ADV visual scenes using synthetic assets.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--browser", type=Path, default=None)
    for flag in ("--app-port", "--llm-port", "--tts-port", "--cdp-port"):
        parser.add_argument(flag, type=int, default=None)
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        explicit = [validate_port(p) for p in (args.app_port, args.llm_port, args.tts_port, args.cdp_port) if p is not None]
        if len(explicit) != len(set(explicit)):
            raise ValueError("Explicit ports must be distinct")
        out_dir, browser_exe = resolve_output_dir(args.output_dir), require_browser(args.browser)
        plan = build_capture_plan(out_dir)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    except ValueError as exc:
        sys.stderr.write(f"参数错误：{exc}\n")
        return 2
    auto = iter(allocate_ephemeral_ports(4 - len(explicit), exclude=explicit))
    app_port, llm_port, tts_port, cdp_port = (p if p is not None else next(auto) for p in (args.app_port, args.llm_port, args.tts_port, args.cdp_port))
    procs, client = [], None
    with tempfile.TemporaryDirectory(prefix="g2v_visual_") as tmp:
        tmp_root, flags, static_dst = Path(tmp), getattr(subprocess, "CREATE_NO_WINDOW", 0), Path(tmp) / "galgame2voice" / "static"
        shutil.copytree(PROJECT_ROOT / "galgame2voice" / "static" / "assets", static_dst / "assets")
        shutil.copy2(PROJECT_ROOT / "galgame2voice" / "static" / "index.html", static_dst / "index.html")
        create_synthetic_character(tmp_root / "characters")
        py_paths = [str(PROJECT_ROOT), str(PROJECT_ROOT / ".venv" / "Lib" / "site-packages"), os.environ.get("PYTHONPATH", "")]
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in py_paths if p), "GALGAME2VOICE_PROJECT_ROOT": str(tmp_root), "GALGAME2VOICE_HOST": "127.0.0.1", "GALGAME2VOICE_PORT": str(app_port), "GALGAME2VOICE_AUTH_DISABLED": "1", "GALGAME2VOICE_RATE_LIMIT_DISABLED": "1", "GALGAME2VOICE_GPT_SOVITS_BASE_URL": f"http://127.0.0.1:{tts_port}"}
        try:
            for cmd, ready_url in (
                ([sys.executable, str(PROJECT_ROOT / "scripts" / "dev_standins.py"), "--llm-port", str(llm_port), "--tts-port", str(tts_port), "--llm-format", "lines", "--seed", "42", "--first-token-ms", "40", "--tts-base-ms", "40"], f"http://127.0.0.1:{tts_port}/health"),
                ([sys.executable, "-m", "galgame2voice.main"], f"http://127.0.0.1:{app_port}/api/health"),
                ([str(browser_exe), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-sync", "--no-proxy-server", "--disable-background-networking", "--disable-extensions", "--window-size=1920,1080", f"--remote-debugging-port={cdp_port}", "--remote-allow-origins=*", f"--user-data-dir={tmp_root / 'profile'}", "about:blank"], f"http://127.0.0.1:{cdp_port}/json/version"),
            ):
                procs.append(subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags))
                _wait_ready(ready_url, procs[-1], timeout=30.0)
            base_url = f"http://127.0.0.1:{app_port}"
            _http_json("POST", f"{base_url}/api/config", {"allow_private_llm_endpoints": True})
            _http_json("POST", f"{base_url}/api/providers", {"id": "openai", "api_base_url": f"http://127.0.0.1:{llm_port}/v1", "chat_model": "standin", "api_key": "standin", "is_active": True})
            _http_json("POST", f"{base_url}/api/providers/openai/activate", {})
            pid = _http_json("GET", f"{base_url}/api/characters")["characters"][0]["id"]
            _http_json("POST", f"{base_url}/api/characters/switch", {"character_id": pid, "force": True})
            _http_json("POST", f"{base_url}/api/chat/sessions", {"id": "s_visual_capture", "title": "视觉采集会话", "voice_profile_id": pid, "settings": {"voiceProfileId": pid, "advAutoMode": "normal"}})
            with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=5) as res:
                ws_url = next(t["webSocketDebuggerUrl"] for t in json.load(res) if t.get("type") == "page")
            client = make_cdp_client(ws_url)
            _drive_and_capture(client, base_url, out_dir)
            (out_dir / "capture_manifest.json").write_text(json.dumps({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "note": "本清单与截图仅供人工视觉检查对比，截图成功生成不代表自动通过视觉验收。",
                "viewports": [{"id": v[0], "width": v[1], "height": v[2]} for v in VIEWPORTS],
                "scenes": list(SCENES),
                "captures": [{**item, "path": item["path"].relative_to(out_dir).as_posix()} for item in plan],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            if client is not None:
                client.close()
            for proc in reversed(procs):
                stop_browser_process(proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
