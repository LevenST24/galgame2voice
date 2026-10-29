"""Paint synthetic layers in a real browser; never load installed artwork."""

import base64
from functools import partial
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import urllib.request

import pytest

from test_engine_status_browser import ROOT
from test_home_layout_browser import browser_path, make_layout_server
from test_precision_dashboard_browser import PrecisionHandler

websocket_client = pytest.importorskip("websockets.sync.client")
connection_closed = pytest.importorskip("websockets.exceptions").ConnectionClosed


SCRIPT = r"""
import {createSpriteView} from '/frontend/src/adv/sprite.js';
import {normalizePortrait} from '/frontend/src/sprite_state.js';
const result={viewport:innerWidth,overflow:0,left:0,right:innerWidth};
try {
  document.documentElement.style.setProperty('--u','1px');
  document.body.style.cssText='margin:0;background:#000';
  const style=document.createElement('link');style.rel='stylesheet';
  style.href='/frontend/src/adv/sprite.css';
  await new Promise((resolve,reject)=>{style.onload=resolve;style.onerror=reject;document.head.append(style);});
  const host=document.createElement('div');host.style.cssText='position:relative;width:800px;height:600px';
  const sprite=document.createElement('div'),fx=document.createElement('div'),face=document.createElement('div');
  sprite.style.cssText='position:absolute;inset:0';
  face.style.cssText='position:absolute;left:500px;top:50px;width:450px;height:450px;transform:scale(.2);transform-origin:top left';
  host.append(sprite,fx,face);document.body.replaceChildren(host);
  const image=(w,h,color)=>'data:image/svg+xml,'+encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}"><rect width="100%" height="100%" fill="${color}"/></svg>`);
  const rect=(x,y,width,height)=>({x,y,width,height});
  const model=normalizePortrait({enabled:true,version:3,defaultOutfit:'uniform',defaultPose:'pose',
    outfits:{uniform:{}},poses:{pose:{}},faces:{first:{},second:{}},
    layers:{body:{file:image(100,200,'white'),rect:rect(100,50,100,200),z:0},
      eye1:{file:image(20,10,'red'),rect:rect(130,80,20,10),z:1},
      eye2:{file:image(20,10,'lime'),rect:rect(130,80,20,10),z:1},
      mouth:{file:image(20,10,'blue'),rect:rect(140,110,20,10),z:2}},
    looks:{'pose/uniform':{crop:{left:100,top:50,width:100,height:200},
      faceRect:{x:20,y:20,width:50,height:50},headAnchor:{x:30,y:30},
      stage:{scale:__SCALE__,offset:{x:250,y:50}},defaultFace:'first',body:['body'],
      faces:{first:{layers:['eye1','mouth']},second:{layers:['eye2','mouth']}}}}});
  const view=createSpriteView({spriteLayer:sprite,fxLayer:fx,faceSlot:face});view.setModel(model);
  const input=faceId=>({isV3:true,lookKey:'pose/uniform',state:{face:faceId}});
  const first=await view.apply(input('first'),{reducedMotion:true});
  if(first.error||first.stale) throw new Error('First expression did not render');
  if(__SWITCH__) {
    const second=await view.apply(input('second'),{reducedMotion:true});
    if(second.error||second.stale) throw new Error('Second expression did not render');
  }
  result.face=view.getVisibleFace();
} catch(error) {result.error=String(error.stack||error);}
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(result);parent.document.body.append(output);
"""


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("switch", [False, True])
@pytest.mark.parametrize("scale", [1, 1.5])
def test_browser_paints_expression_at_shared_body_coordinates(tmp_path, switch, scale):
    executable = browser_path()
    if not executable:
        pytest.skip("Synthetic portrait rendering needs Chrome, Chromium or Edge")
    page = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    page = page.replace('src="/src/main.js"', 'src="/fixture.js"').replace('"/src/', '"/frontend/src/')
    frame = '<body style="margin:0"><iframe src="/case" style="border:0;width:800px;height:600px"></iframe></body>'
    script = SCRIPT.replace("__SWITCH__", str(switch).lower()).replace("__SCALE__", str(scale))
    server = make_layout_server(partial(PrecisionHandler, page=frame.encode(), case_page=page.encode(),
                                        script=script.encode()))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with tempfile.TemporaryDirectory(prefix="g2v-sprite-", dir=os.environ.get("G2V_BROWSER_PROFILE_ROOT"),
                                         ignore_cleanup_errors=True) as profile:
            process = subprocess.Popen(
                [executable, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
                 "--disable-sync", "--no-proxy-server", "--disable-background-networking", "--disable-extensions",
                 "--remote-debugging-port=0", "--remote-allow-origins=http://127.0.0.1",
                 f"--user-data-dir={profile}", f"http://127.0.0.1:{server.server_port}/"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                deadline = time.monotonic() + 20
                port_file = Path(profile) / "DevToolsActivePort"
                port = None
                # The browser may still hold the file open or have written it only partly.
                while port is None and process.poll() is None and time.monotonic() < deadline:
                    try:
                        port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
                    except (OSError, IndexError, ValueError):
                        time.sleep(0.05)
                assert port, "Browser debugging endpoint did not start"
                with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
                    targets = json.load(response)
                target = next(item for item in targets if item["type"] == "page")
                with websocket_client.connect(target["webSocketDebuggerUrl"], origin="http://127.0.0.1",
                                              open_timeout=5, close_timeout=2, max_size=16 * 1024 * 1024) as ws:
                    sequence = 0

                    def command(method, params=None):
                        nonlocal sequence
                        sequence += 1
                        ws.send(json.dumps({"id": sequence, "method": method, "params": params or {}}))
                        while True:
                            response = json.loads(ws.recv(timeout=10))
                            if response.get("id") == sequence:
                                assert "error" not in response, response
                                return response["result"]

                    command("Emulation.setDeviceMetricsOverride", {"width": 800, "height": 600,
                                                                    "deviceScaleFactor": 1, "mobile": False})
                    data = None
                    while time.monotonic() < deadline and process.poll() is None:
                        value = command("Runtime.evaluate", {"expression": "document.querySelector('#engine-result')?.textContent || null",
                                                             "returnByValue": True})["result"].get("value")
                        if value:
                            data = json.loads(value)
                            break
                        time.sleep(0.05)
                    assert data is not None, "Synthetic portrait page did not complete"
                    image = command("Page.captureScreenshot", {"format": "png"})["data"]
                    (tmp_path / "portrait.png").write_bytes(base64.b64decode(image))
                    points = [[255, 55], [int(250 + 35 * scale), int(50 + 35 * scale)],
                              [int(250 + 45 * scale), int(50 + 65 * scale)],
                              [505, 55], [527, 77], [545, 131]]
                    expression = """(async () => {
                        const image = new Image(); image.src = 'data:image/png;base64,DATA';
                        await image.decode();
                        const canvas = document.createElement('canvas');
                        canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
                        const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0);
                        return POINTS.map(([x,y]) =>
                            Array.from(ctx.getImageData(x, y, 1, 1).data).slice(0, 3));
                    })()""".replace("DATA", image).replace("POINTS", json.dumps(points))
                    pixels = command("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                                         "returnByValue": True})["result"]["value"]
                    try:
                        command("Browser.close")
                    except (EOFError, OSError, TimeoutError, connection_closed):
                        pass
            finally:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert "error" not in data, data
    assert data["face"] == ("second" if switch else "first")
    assert pixels[0] == [255, 255, 255]  # body
    assert pixels[1] == ([0, 255, 0] if switch else [255, 0, 0])  # eyes
    assert pixels[2] == [0, 0, 255]  # mouth
    assert pixels[3] == [255, 255, 255]  # body in the face window
    assert pixels[4] == ([0, 255, 0] if switch else [255, 0, 0])  # eyes in the face window
    assert pixels[5] == [0, 0, 255]  # mouth in the face window
