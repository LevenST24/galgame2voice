"""Real fetch cancellation and independent engine state rendering in Chromium."""

from functools import partial
import json
from pathlib import Path
import threading

import pytest

from test_home_layout_browser import browser_path, make_layout_server, run_layout_browser
from test_precision_dashboard_browser import PrecisionHandler

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = """
import {initGlobalSettings,openGlobalSettings} from '/frontend/src/controllers/global_settings.js';
import {openModal,closeModal} from '/frontend/src/settings_ui.js';
const nativeFetch=globalThis.fetch.bind(globalThis),counts={},signals=[],aborts=[];
globalThis.fetch=async (url,options={})=>{
  if(options.method&&options.method!=='GET')throw new Error('Unexpected settings write');
  if(url==='/api/system/status'||url==='/api/cache/stats'){
    const name=url.endsWith('/stats')?'cache':'status';counts[name]=(counts[name]||0)+1;
    if(counts[name]===1){
      signals.push(options.signal);
      try{return await nativeFetch('/hold-'+name,options);}
      catch(error){aborts.push(error.name);throw error;}
    }
    return Response.json(name==='cache'?{total_entries:2,total_size_mb:2}:{app:{pid:222,uptime_seconds:12}});
  }
  if(url==='/api/system/engine-status'){
    counts.engine=(counts.engine||0)+1;
    return Response.json({state:'ready',ready:true,message:'已独立确认就绪'});
  }
  return Response.json({});
};
const ids=['globalModal','gDashSovitsBadge','gDashSovitsStatus','gDashSovitsUrl','gDashCacheVal','gDashUptimeVal'];
const dom=Object.fromEntries(ids.map(id=>[id,document.getElementById(id)]));
const wait=()=>new Promise(resolve=>setTimeout(resolve,20));
const until=async check=>{for(let i=0;i<150;i++){if(check())return;await wait();}throw new Error('UI condition did not finish');};
const result={};
try{
  initGlobalSettings(dom,{openModal,closeModal});openGlobalSettings();
  await until(()=>signals.length===2&&dom.gDashSovitsBadge.textContent==='在线');
  result.readyBeforeStorage=true;
  const close=dom.globalModal.querySelector('[data-close]'),r=close.getBoundingClientRect();
  const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
  if(!r.width||!r.height||close.closest('[inert]')||!close.contains(hit))throw new Error('Close is not reachable');
  close.click();await until(()=>aborts.length===2);
  result.nativeAborts=aborts;result.signalsAborted=signals.every(signal=>signal.aborted);
  openGlobalSettings();await until(()=>dom.gDashCacheVal.textContent.startsWith('2 个文件'));
  await nativeFetch('/release-held',{method:'POST'});await wait();await wait();
  result.cache=dom.gDashCacheVal.textContent;result.uptime=dom.gDashUptimeVal.textContent;
  result.badge=dom.gDashSovitsBadge.textContent;result.counts=counts;
  const card=dom.gDashSovitsBadge.closest('.status-card-box'),rect=card.getBoundingClientRect();
  result.overflow=card.scrollWidth-card.clientWidth;result.left=rect.left;result.right=rect.right;result.viewport=innerWidth;
}catch(error){result.error=String(error.stack||error);}
await nativeFetch('/result',{method:'POST',body:JSON.stringify(result)});
"""


class TelemetryHandler(PrecisionHandler):
    def __init__(self, *args, ready, release, result, **kwargs):
        self.ready, self.release, self.result = ready, release, result
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == "/hold-image":
            self.ready.wait(20)
            self.send_response(204)
            self.end_headers()
        elif self.path in {"/hold-status", "/hold-cache"}:
            self.release.wait(15)
            data = {"app": {"pid": 111, "uptime_seconds": 1}} if self.path.endswith("status") else {"total_entries": 1, "total_size_mb": 1}
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/result":
            self.result.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.ready.set()
        elif self.path == "/release-held":
            self.release.set()
        else:
            self.send_error(404)
            return
        self.send_response(204)
        self.end_headers()


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_slow_diagnostics_do_not_hold_engine_readiness_or_survive_close(tmp_path, width, height):
    executable = browser_path()
    if not executable:
        pytest.skip("Telemetry ownership needs an installed Chromium browser")
    page = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    page = page.replace('src="/src/main.js"', 'src="/fixture.js"').replace('"/src/', '"/frontend/src/')
    frame = f'<body style="margin:0"><iframe src="/case" style="border:0;display:block;width:{width}px;height:{height}px"></iframe><img src="/hold-image" hidden></body>'
    ready, release, result = threading.Event(), threading.Event(), {}
    server = make_layout_server(partial(TelemetryHandler, page=frame.encode(), case_page=page.encode(), script=SCRIPT.encode(),
                                       ready=ready, release=release, result=result))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        rendered = run_layout_browser([
            executable, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-sync",
            "--no-proxy-server", "--disable-background-networking", "--disable-extensions", "--force-device-scale-factor=1",
            f"--user-data-dir={tmp_path/'browser'}", f"--window-size={max(600,width+100)},{height+100}",
            "--dump-dom", f"--screenshot={tmp_path/'telemetry.png'}", f"http://127.0.0.1:{server.server_port}/",
        ], reset=lambda: (ready.clear(), release.clear(), result.clear()))
    finally:
        ready.set()
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    (tmp_path / "rendered.html").write_bytes(rendered.stdout)
    (tmp_path / "browser.log").write_bytes(rendered.stderr)
    assert rendered.returncode == 0 and result and "error" not in result, result or rendered.stderr[-1000:]
    assert result["readyBeforeStorage"] and result["signalsAborted"]
    assert result["nativeAborts"] == ["AbortError", "AbortError"]
    assert result["counts"] == {"status": 2, "cache": 2, "engine": 2}
    assert result["badge"] == "在线" and result["cache"].startswith("2 个文件") and "PID: 222" in result["uptime"]
    assert result["viewport"] == width and result["overflow"] <= 1 and result["left"] >= 0 and result["right"] <= width
