"""Native fetch must abort heartbeat-only replies while allowing slow voice work."""

from functools import partial
import html
import json
from pathlib import Path
import re
import threading
import time

import pytest

from test_home_layout_browser import browser_path, make_layout_server, run_layout_browser
from test_precision_dashboard_browser import PrecisionHandler


SCRIPT = """
import {streamChat} from '/frontend/src/ai.js';
const nativeFetch=globalThis.fetch.bind(globalThis),mode=__MODE__;
const view=document.createElement('main');view.style.cssText='padding:32px;font:18px sans-serif;white-space:pre-wrap';
document.body.replaceChildren(view);
let signal,calls=0;
const result=await new Promise(resolve=>streamChat({prompt:'synthetic',idleTimeoutMs:1000,
  fetch:(url,options)=>{calls++;signal=options.signal;return nativeFetch(url,options);},
  onChunk:text=>{view.textContent=text;},onEnd:(text,meta)=>resolve({text,...meta}),
}));
const disconnected=result.error?await (await nativeFetch('/confirm-disconnect')).json():false;
const data={...result,calls,signalAborted:signal.aborted,upstreamDisconnected:disconnected,mode};
view.textContent=(result.text||'尚未收到正文')+'\\n'+(result.error||'正常完成');
const output=document.createElement('pre');output.id='stream-result';output.hidden=true;
output.textContent=JSON.stringify(data);parent.document.body.append(output);
await nativeFetch('/finished',{method:'POST'});
"""


class ProgressHandler(PrecisionHandler):
    def __init__(self, *args, mode, page_ready, disconnected, **kwargs):
        self.mode, self.page_ready, self.disconnected = mode, page_ready, disconnected
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == "/hold-image":
            self.page_ready.wait(20)
            self.send_response(204)
            self.end_headers()
        elif self.path == "/confirm-disconnect":
            result = self.disconnected.wait(2)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/finished":
            self.page_ready.set()
            self.send_response(204)
            self.end_headers()
            return
        if self.path != "/api/chat/stream":
            self.send_error(404)
            return
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("X-G2V-Progress-Policy", "1")
        self.send_header("X-G2V-Voice-Job-Timeout-Ms", "5000")
        self.end_headers()

        def send(event, data):
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        started = time.monotonic()
        repeated = False
        try:
            if self.mode in {"slow-voice", "voice-stuck"}:
                send("voice_job", {"index": 0, "status": "started"})
            if self.mode == "partial":
                send("text", {"delta_chinese": "已经收到的内容。"})
            while time.monotonic() - started < 15:
                elapsed = time.monotonic() - started
                if self.mode == "slow-voice" and elapsed >= 1.5:
                    send("voice_job", {"index": 0, "status": "finished"})
                    send("audio_chunk", {"index": 0, "audio_url": "/audio/synthetic.wav"})
                    send("done", {"chinese": "慢语音正常完成。"})
                    return
                if self.mode == "voice-stuck" and elapsed >= 3 and not repeated:
                    repeated = True
                    send("voice_job", {"index": 0, "status": "started"})
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self.disconnected.set()


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("mode", ["heartbeat-only", "partial", "slow-voice", "voice-stuck"])
def test_native_fetch_uses_progress_budget_and_releases_the_connection(tmp_path, mode):
    executable = browser_path()
    if not executable:
        pytest.skip("Native stream cancellation needs Chromium")
    root = Path(__file__).resolve().parents[1]
    page = (root / "frontend/index.html").read_text(encoding="utf-8")
    page = page.replace('src="/src/main.js"', 'src="/fixture.js"').replace('"/src/', '"/frontend/src/')
    frame = '<body><iframe src="/case" style="border:0;width:100%;height:800px"></iframe><img src="/hold-image" hidden></body>'
    page_ready, disconnected = threading.Event(), threading.Event()
    server = make_layout_server(partial(ProgressHandler, mode=mode, page_ready=page_ready, disconnected=disconnected,
        page=frame.encode(), case_page=page.encode(), script=SCRIPT.replace("__MODE__", json.dumps(mode)).encode()))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = run_layout_browser([
            executable, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-sync",
            "--no-proxy-server", "--disable-background-networking", "--disable-extensions",
            f"--user-data-dir={tmp_path/'browser'}", "--window-size=1280,900", "--dump-dom",
            f"--screenshot={tmp_path/'stream-progress.png'}", f"http://127.0.0.1:{server.server_port}/",
        ], reset=lambda: (page_ready.clear(), disconnected.clear()))
    finally:
        page_ready.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    markup = result.stdout.decode("utf-8", errors="replace")
    (tmp_path / "rendered.html").write_text(markup, encoding="utf-8")
    (tmp_path / "browser.log").write_bytes(result.stderr)
    match = re.search(r'<pre id="stream-result" hidden(?:="")?>(.*?)</pre>', markup, re.S)
    assert result.returncode == 0 and match, markup[-3000:]
    data = json.loads(html.unescape(match[1]))
    assert data["calls"] == 1, data
    if mode == "slow-voice":
        assert data["error"] is None and data["text"] == "慢语音正常完成。", data
        assert data["audioUrls"] == ["/audio/synthetic.wav"]
    else:
        assert data["cancelled"] is False and data["errorCode"] == "stream_idle_timeout", data
        assert data["signalAborted"] is True and data["upstreamDisconnected"] is True, data
        if mode == "partial":
            assert data["text"] == "已经收到的内容。", data
