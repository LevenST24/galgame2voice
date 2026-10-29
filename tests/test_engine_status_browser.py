"""The production settings dialog must follow startup without manual refresh."""

from functools import partial
import html
import json
from pathlib import Path
import re
import threading

import pytest

from test_home_layout_browser import browser_path, make_layout_server, run_layout_browser
from test_precision_dashboard_browser import PrecisionHandler


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = """
import {initGlobalSettings,openGlobalSettings} from '/frontend/src/controllers/global_settings.js';
import {openModal,closeModal} from '/frontend/src/settings_ui.js';
let reads=0;
globalThis.fetch=async (url,options={})=>{
  if(options.method && options.method!=='GET') throw new Error('Unexpected write');
  return new Response(JSON.stringify(url.endsWith('/engine-status') ? {
    state:++reads===1?'starting':'ready',ready:reads>1,
    message:reads===1?'引擎正在加载模型，请稍候；文字聊天可继续使用。':'语音引擎已就绪。'
  } : url.endsWith('/system/status') ? {
    gpt_sovits:{status:reads>1?'reachable':'unreachable',base_url:'http://127.0.0.1:19872'}
  } : {}));
};
const ids=['globalModal','gDashSovitsBadge','gDashSovitsStatus','gDashSovitsUrl'];
const dom=Object.fromEntries(ids.map(id=>[id,document.getElementById(id)]));
initGlobalSettings(dom,{openModal,closeModal});
openGlobalSettings();
await new Promise(resolve=>setTimeout(resolve,100));
const initial={badge:dom.gDashSovitsBadge.textContent,message:dom.gDashSovitsStatus.textContent};
await new Promise(resolve=>setTimeout(resolve,5500));
const online={badge:dom.gDashSovitsBadge.textContent,message:dom.gDashSovitsStatus.textContent,reads};
closeModal(dom.globalModal);
await new Promise(resolve=>setTimeout(resolve,5500));
const closedReads=reads;
openGlobalSettings();
await new Promise(resolve=>setTimeout(resolve,100));
const card=dom.gDashSovitsBadge.closest('.status-card-box');
const rect=card.getBoundingClientRect();
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify({initial,online,closedReads,reopenedReads:reads,
  overflow:card.scrollWidth-card.clientWidth,left:rect.left,right:rect.right,viewport:innerWidth});
parent.document.body.append(output);
"""

RECOVERY_SCRIPT = """
import {initGlobalSettings} from '/frontend/src/controllers/global_settings.js';
let reads=0,restarts=0;
globalThis.fetch=async (url,options={})=>{
  if(url.endsWith('/restart_sovits')) {
    restarts++;
    if(__LOST__) throw new TypeError('Failed to fetch');
    return new Response(JSON.stringify({status:'starting'}));
  }
  if(options.method && options.method!=='GET') throw new Error('Unexpected write');
  if(url.endsWith('/engine-status')) {
    if(++reads===1) throw new TypeError('Failed to fetch');
    return new Response(JSON.stringify({state:'ready',ready:true,message:'语音引擎已就绪。'}));
  }
  return new Response('{}');
};
const ids=['globalModal','gBtnRestartSovits','gBtnTogglePrecision',
  'gDashSovitsBadge','gDashSovitsStatus','gDashSovitsUrl'];
const dom=Object.fromEntries(ids.map(id=>[id,document.getElementById(id)]));
initGlobalSettings(dom);
dom.globalModal.classList.remove('hidden');
dom.gBtnRestartSovits.click();
await new Promise(resolve=>setTimeout(resolve,100));
const initial={badge:dom.gDashSovitsBadge.textContent,style:dom.gDashSovitsBadge.className,
  button:dom.gBtnRestartSovits.textContent,busy:dom.gBtnRestartSovits.disabled};
await new Promise(resolve=>setTimeout(resolve,1500));
const online={badge:dom.gDashSovitsBadge.textContent,message:dom.gDashSovitsStatus.textContent,
  busy:dom.gBtnRestartSovits.disabled};
const toasts=[...document.querySelectorAll('#toastRoot .toast')].map(el=>el.textContent);
const card=dom.gDashSovitsBadge.closest('.status-card-box');
const rect=card.getBoundingClientRect();
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify({initial,online,toasts,restarts,
  overflow:card.scrollWidth-card.clientWidth,left:rect.left,right:rect.right,viewport:innerWidth});
parent.document.body.append(output);
"""


def render_engine_case(tmp_path, width, height, script, virtual_ms=14000):
    browser = browser_path()
    if not browser:
        pytest.skip("Settings rendering needs an installed Chromium browser")
    page = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    page = page.replace('src="/src/main.js"', 'src="/fixture.js"').replace('"/src/', '"/frontend/src/')
    frame = f'<body style="margin:0"><iframe src="/case" style="border:0;display:block;width:{width}px;height:{height}px"></iframe></body>'
    server = make_layout_server(partial(
        PrecisionHandler, page=frame.encode(), case_page=page.encode(), script=script.encode()))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = run_layout_browser([
            browser, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-sync",
            "--no-default-browser-check", "--no-proxy-server", "--disable-background-networking", "--disable-extensions",
            "--force-device-scale-factor=1", f"--user-data-dir={tmp_path/'browser'}",
            f"--window-size={max(width+100,600)},{height+100}", f"--virtual-time-budget={virtual_ms}",
            "--dump-dom", f"--screenshot={tmp_path/'engine-status.png'}", f"http://127.0.0.1:{server.server_port}/",
        ])
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    markup = result.stdout.decode("utf-8", errors="replace")
    (tmp_path / "rendered.html").write_text(markup, encoding="utf-8")
    (tmp_path / "browser.log").write_bytes(result.stderr)
    match = re.search(r'<pre id="engine-result" hidden(?:="")?>(.*?)</pre>', markup, re.S)
    assert result.returncode == 0 and match, result.stderr.decode("utf-8", errors="replace")[-1000:]
    data = json.loads(html.unescape(match[1]))
    assert data["viewport"] == width
    assert data["overflow"] <= 1 and data["left"] >= 0 and data["right"] <= width, data
    return data


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_settings_follow_startup_and_stop_refreshing_when_closed(tmp_path, width, height):
    data = render_engine_case(tmp_path, width, height, SCRIPT)
    assert data["initial"]["badge"] == "启动中"
    assert data["online"]["badge"] == "在线"
    assert data["online"]["message"] == "语音引擎已就绪。"
    assert data["online"]["reads"] == 2
    assert data["closedReads"] == 2
    assert data["reopenedReads"] == 3


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height,lost", [(1280, 900, False), (1280, 900, True)])
def test_settings_recover_from_status_or_restart_response_loss(tmp_path, width, height, lost):
    data = render_engine_case(tmp_path, width, height, RECOVERY_SCRIPT.replace("__LOST__", json.dumps(lost)), 4000)
    assert data["initial"]["badge"] == "待确认"
    assert "yellow" in data["initial"]["style"]
    assert "核对状态" in data["initial"]["button"]
    assert data["initial"]["busy"] is True
    assert data["online"]["badge"] == "在线"
    assert data["online"]["busy"] is False
    assert data["restarts"] == 1
    if lost:
        assert "重启请求结果未确认" in data["online"]["message"]
        assert any("当前引擎已在线" in value for value in data["toasts"])
        assert len(data["toasts"]) == 1
        assert not any("启动.bat" in value for value in data["toasts"])
    else:
        assert data["online"]["message"] == "语音引擎已就绪。"
