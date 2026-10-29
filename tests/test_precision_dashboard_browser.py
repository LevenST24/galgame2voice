"""Render precision states in the production settings markup and styles."""

from functools import partial
import html
import json
from pathlib import Path
import re
import threading

import pytest

from test_home_layout_browser import LayoutHandler, browser_path, make_layout_server, run_layout_browser


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = """
import {initGlobalSettings,fetchSystemTelemetry} from '/frontend/src/controllers/global_settings.js';
const known=__KNOWN__;
globalThis.fetch=async url=>new Response(JSON.stringify(url.endsWith('/system/status') ? {
  gpt_sovits:{status:'reachable',base_url:'http://127.0.0.1:19230'},
  hardware:{inference_precision:known?'FP16':null,precision_source:known?'managed_launch':'unknown',
    next_inference_precision:'CPU',precision_pending_restart:known?true:null,configured_precision:'cpu'}
} : url.endsWith('/engine-status') ? {state:'ready',ready:true,message:'测试引擎已就绪'} : {}));
const ids=['gDashPrecisionBadge','gDashPrecisionVal','gDashDeviceVal','gBtnTogglePrecisionText',
  'gBtnTogglePrecision','gDashSovitsBadge','gDashSovitsStatus','gDashSovitsUrl'];
initGlobalSettings(Object.fromEntries(ids.map(id=>[id,document.getElementById(id)])));
document.getElementById('globalModal').classList.remove('hidden');
await fetchSystemTelemetry();
await document.fonts.ready;
await new Promise(resolve=>setTimeout(resolve,350));
const badge=document.getElementById('gDashPrecisionBadge');
const card=badge.closest('.status-card-box');
const rect=card.getBoundingClientRect();
const output=document.createElement('pre'); output.id='precision-result'; output.hidden=true;
output.textContent=JSON.stringify({badge:badge.textContent,
  sub:document.getElementById('gDashDeviceVal').textContent,
  overflow:card.scrollWidth-card.clientWidth, left:rect.left, right:rect.right, viewport:innerWidth});
parent.document.body.append(output);
"""


class PrecisionHandler(LayoutHandler):
    def __init__(self, *args, script, case_page, **kwargs):
        self.script = script
        self.case_page = case_page
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == "/case":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(self.case_page)
        elif self.path == "/fixture.js":
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.end_headers()
            self.wfile.write(self.script)
        else:
            super().do_GET()


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height,known", [(1280, 900, True), (1280, 900, False)])
def test_precision_card_shows_pending_or_unknown_without_overflow(tmp_path, width, height, known):
    browser = browser_path()
    if not browser:
        pytest.skip("Dashboard rendering needs an installed Chrome/Chromium/Edge")
    page = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    page = page.replace('src="/src/main.js"', 'src="/fixture.js"').replace('"/src/', '"/frontend/src/')
    # Chromium can enforce a minimum outer window width. An exact-size same-origin
    # frame tests the real responsive viewport and keeps the whole screenshot visible.
    frame = f'<body style="margin:0"><iframe src="/case" style="border:0;display:block;width:{width}px;height:{height}px"></iframe></body>'
    handler = partial(PrecisionHandler, page=frame.encode(), case_page=page.encode(),
                      script=SCRIPT.replace("__KNOWN__", json.dumps(known)).encode())
    server = make_layout_server(handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = run_layout_browser([
            browser, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-sync",
            "--no-default-browser-check", "--no-proxy-server", "--disable-background-networking", "--disable-extensions",
            "--force-device-scale-factor=1", f"--user-data-dir={tmp_path/'browser'}", f"--window-size={max(width+100,600)},{height+100}",
            "--virtual-time-budget=5000", "--dump-dom", f"--screenshot={tmp_path/'dashboard.png'}",
            f"http://127.0.0.1:{server.server_port}/",
        ])
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    markup = result.stdout.decode("utf-8", errors="replace")
    (tmp_path / "rendered.html").write_text(markup, encoding="utf-8")
    (tmp_path / "browser.log").write_bytes(result.stderr)
    match = re.search(r'<pre id="precision-result" hidden(?:="")?>(.*?)</pre>', markup, re.S)
    assert result.returncode == 0 and match, result.stderr.decode("utf-8", errors="replace")[-1000:]
    data = json.loads(html.unescape(match[1]))
    (tmp_path / "layout.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    assert data["viewport"] == width
    assert data["badge"] == ("FP16" if known else "待确认")
    assert ("重启" if known else "外部引擎") in data["sub"]
    assert data["overflow"] <= 1 and data["left"] >= 0 and data["right"] <= data["viewport"], data
