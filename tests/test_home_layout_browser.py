"""Render the real home modules: long excerpts must leave room for recents."""

from functools import partial
import errno
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import threading
import tempfile
import time
import warnings

import pytest


ROOT = Path(__file__).resolve().parents[1]
SYSTEM_TEMP = tempfile.gettempdir()


class LayoutServer(ThreadingHTTPServer):
    # Chrome opens several connections at once for the module graph. Windows refuses
    # connections beyond the listen backlog (5 by default) instead of queueing them,
    # which left the page blank whenever the accept loop fell briefly behind.
    request_queue_size = 64


def make_layout_server(handler):
    # Windows can allocate port 0 from a range containing browser-blocked ports.
    # Bind an unused high loopback port for this isolated, source-only fixture.
    for _ in range(50):
        try:
            return LayoutServer(("127.0.0.1", 20000 + secrets.randbelow(10000)), handler)
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, 10048):
                raise
    raise RuntimeError("Could not bind a high loopback port for browser tests")


def _describe_hang(exc, command, seconds):
    screenshot = next((item.split("=", 1)[1] for item in command if item.startswith("--screenshot=")), None)
    stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()[-400:]
    return (f"Chrome was still running after {exc.timeout} s (returned after {seconds:.0f} s) and was started again: "
            f"stdout {len(exc.stdout or b'')} bytes, screenshot {'written' if screenshot and os.path.exists(screenshot) else 'missing'}, "
            f"stderr tail: {stderr or '(empty)'}")


def run_layout_browser(command, reset=None):
    """Runs Chrome once more after a hang or a blank virtual-time dump.

    A test whose server keeps state between requests (posted results, events the page sets)
    passes `reset` to clear it, so the second run does not start from what the first one left.
    """
    def launch():
        # Chrome's own cache filenames can exceed Windows MAX_PATH when a browser
        # profile sits under a long source-snapshot/pytest directory.
        with tempfile.TemporaryDirectory(prefix="g2v-browser-", dir=os.environ.get("G2V_BROWSER_PROFILE_ROOT") or SYSTEM_TEMP,
                                         ignore_cleanup_errors=True) as profile:
            launched = [f"--user-data-dir={profile}" if item.startswith("--user-data-dir=") else item for item in command]
            return subprocess.run(launched, capture_output=True, timeout=40,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def relaunch():
        if reset:
            reset()
        return launch()

    started = time.monotonic()
    try:
        result = launch()
    except subprocess.TimeoutExpired as exc:
        # Windows CI sometimes keeps Chrome running for the whole 40 s, even past the 20 s
        # image hold in test_stream_progress_browser. The cause is not known: report what
        # Chrome had produced, then start it once more.
        warnings.warn(_describe_hang(exc, command, time.monotonic() - started), stacklevel=2)
        return relaunch()
    # Under load the virtual-time budget can run out while Chrome is still on about:blank:
    # CI's blank screenshots are about 6 KB, the size of an about:blank capture, and the
    # dump has no result node. Retry such a dump once with a fresh profile.
    if (result.returncode == 0 and any(item.startswith("--virtual-time-budget=") for item in command)
            and not re.search(rb'<pre id="[^"]*result"', result.stdout)):
        result = relaunch()
    return result


PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/frontend/src/styles.css">
<link rel="stylesheet" href="/frontend/src/adv/adv.css">
<style>.adv-layer-bg {background: linear-gradient(135deg,#574451,#e09b77 65%,#c6d8d0)}</style>
<body><div id="root"></div><pre id="layout-result" hidden></pre>
<script type="module">
import {createStage} from '/frontend/src/adv/stage.js';
import {createTitleScreen} from '/frontend/src/adv/title.js';
const input = __INPUT__;
const stage = createStage(document.getElementById('root'));
const sessions = [0,1,2,3].map(i => ({id: String(i), title: '测试会话',
  characterName: i ? '最近的角色' : '当前角色', settings: {voiceProfileId:i+1},
  hasUserMessages: true, updatedAt: Date.now(),
  excerpt: {zh: input.zh, ja: input.ja, user:'下午好啊'}}));
const ui = createTitleScreen(stage.overlay, {actions: {
  listSessions: () => sessions, getActiveId: () => '0',
  getStatus: async () => ({modelLoaded:true,hasModel:true,model:{state:'ok'},
    engine:{state: input.voiceReady ? 'ready' : 'unavailable'}}),
}});
ui.open();
await new Promise(resolve => setTimeout(resolve,100));
await document.fonts.ready;
const rect = selector => {
  const r = document.querySelector(selector).getBoundingClientRect();
  return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height};
};
const lineCount = selector => rect(selector).height /
  parseFloat(getComputedStyle(document.querySelector(selector)).lineHeight);
const result = {stage:rect('.adv-stage'),actions:rect('.home-actions'),
  recents:rect('.home-recents'),primary:rect('.home-primary'),
  quoteLines:lineCount('.home-quote'),jaLines:lineCount('.home-ja'),
  quoteText:document.querySelector('.home-quote').textContent,
  jaText:document.querySelector('.home-ja').textContent,
  viewportWidth:innerWidth,documentWidth:document.documentElement.scrollWidth};
document.getElementById('layout-result').textContent=JSON.stringify(result);
document.documentElement.dataset.layoutReady='true';
</script></body></html>"""


class LayoutHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, page, **kwargs):
        self.page = page
        super().__init__(*args, **kwargs)

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/":
            body, mime = self.page, "text/html; charset=utf-8"
        else:
            # This test server exposes only frontend source, never data or keys.
            source = (ROOT / self.path.lstrip("/")).resolve()
            if not source.is_relative_to((ROOT / "frontend/src").resolve()) or source.suffix not in {".js", ".css"} or not source.is_file():
                self.send_error(404)
                return
            body = source.read_bytes()
            mime = "text/javascript" if source.suffix == ".js" else "text/css"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.end_headers()
        self.wfile.write(body)


def browser_path():
    for name in ("google-chrome", "chromium", "chromium-browser", "msedge"):
        executable = shutil.which(name)
        if executable:
            return executable
    candidates = [
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    return next((str(path) for path in candidates if path.is_file()), None)


def test_layout_server_queues_a_burst_of_browser_connections():
    server = make_layout_server(BaseHTTPRequestHandler)
    clients = []
    try:
        # Nothing accepts yet, as when the accept loop falls behind a burst of module requests.
        for _ in range(12):
            client = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
            clients.append(client)
    finally:
        for client in clients:
            client.close()
        server.server_close()
    assert len(clients) == 12


BLANK_DUMP = b"<html><head></head><body></body></html>"
RESULT_DUMP = b'<pre id="engine-result">{}</pre>'


@pytest.mark.parametrize("dumps,launches", [
    ([RESULT_DUMP], 1),
    ([BLANK_DUMP, RESULT_DUMP], 2),
    ([BLANK_DUMP, BLANK_DUMP, RESULT_DUMP], 2),
])
def test_run_layout_browser_retries_a_blank_virtual_time_dump_once(monkeypatch, dumps, launches):
    steps = []

    def fake_run(command, **kwargs):
        steps.append("launch")
        return subprocess.CompletedProcess(command, 0, dumps[steps.count("launch") - 1], b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_layout_browser(["chrome", "--virtual-time-budget=5000", "--dump-dom", "http://127.0.0.1:9/"],
                       reset=lambda: steps.append("reset"))
    assert steps == ["launch", "reset", "launch"][:2 * launches - 1]


def test_run_layout_browser_reports_and_retries_a_hung_chrome_once(tmp_path, monkeypatch):
    screenshot = tmp_path / "shot.png"
    command = ["chrome", "--dump-dom", f"--screenshot={screenshot}", "http://127.0.0.1:9/"]
    outcomes = [subprocess.TimeoutExpired(command, 40, b"partial", b"[ERROR:gpu] stuck\n"),
                subprocess.CompletedProcess(command, 0, RESULT_DUMP, b"")]
    steps = []

    def fake_run(command, **kwargs):
        steps.append("launch")
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.warns(UserWarning, match=r"stdout 7 bytes, screenshot missing, stderr tail: \[ERROR:gpu\] stuck"):
        assert run_layout_browser(command, reset=lambda: steps.append("reset")).stdout == RESULT_DUMP
    # The state the first run left on the server is cleared before Chrome starts again.
    assert steps == ["launch", "reset", "launch"]

    # A second hang is not retried again.
    outcomes[:] = [subprocess.TimeoutExpired(command, 40), subprocess.TimeoutExpired(command, 40)]
    with pytest.warns(UserWarning), pytest.raises(subprocess.TimeoutExpired):
        run_layout_browser(command)
    assert outcomes == []


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height,long_text,voice_ready", [
    (1920,1080,False,False),
    (2048,1152,True,True),
    (1366,768,True,False),
])
def test_home_excerpt_leaves_space_for_actions_and_recents(tmp_path, width, height, long_text, voice_ready):
    executable = browser_path()
    if not executable:
        pytest.skip("home layout needs an installed Chrome/Chromium/Edge browser")
    zh = ("下午好。这个时间点来店里，想必是想在午后安静地喝杯咖啡吧。"
          "如果您还没决定好要点什么，我可以为您推荐。") if long_text else "先说好，这次你可不许偷吃我那份。"
    ja = ("こんにちは。この時間にいらっしゃるということは、午後のひとときを静かにコーヒーと一緒に過ごしたいということでしょうか。") if long_text else "先に言っとくけど、ぼくの分は食べちゃだめ！"
    page = PAGE.replace("__INPUT__", json.dumps({"zh":zh,"ja":ja,"voiceReady":voice_ready}, ensure_ascii=False)).encode()
    server = make_layout_server(partial(LayoutHandler, page=page))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    screenshot = tmp_path / "home.png"
    # Match the portable smoke's isolated browser flags on restricted Windows hosts.
    command = [executable, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-sync", "--no-default-browser-check", "--no-proxy-server",
               "--disable-background-networking", "--disable-extensions", "--force-device-scale-factor=1",
               f"--user-data-dir={tmp_path / 'browser'}", f"--window-size={width},{height}",
               "--virtual-time-budget=5000", "--dump-dom", f"--screenshot={screenshot}",
               f"http://127.0.0.1:{server.server_port}/"]
    try:
        run = run_layout_browser(command)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    output = run.stdout.decode("utf-8", errors="replace")
    (tmp_path / "rendered.html").write_text(output, encoding="utf-8")
    (tmp_path / "browser.log").write_bytes(run.stderr)
    print(f"Home layout evidence: {tmp_path}")
    match = re.search(r'<pre id="layout-result" hidden(?:="")?>(.*?)</pre>', output, re.S)
    assert run.returncode == 0 and match, run.stderr.decode("utf-8", errors="replace")[-2000:]
    result = json.loads(html.unescape(match[1]))
    (tmp_path / "layout.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    assert result["quoteText"] == f"「{zh}」" and result["jaText"] == ja
    assert result["documentWidth"] <= result["viewportWidth"]
    unit = result["stage"]["width"] / 1920
    gap = (result["recents"]["top"] - result["actions"]["bottom"]) / unit
    assert result["quoteLines"] <= 2.05 and result["jaLines"] <= 1.05, result
    assert gap >= 80, result
    assert result["actions"]["bottom"] < result["stage"]["bottom"], result
