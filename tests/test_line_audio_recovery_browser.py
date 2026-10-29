"""Reach the LOG repair controls and play the repaired line in Chromium."""

import base64
from functools import partial
import io
import json
import threading
import wave

import pytest

from test_audio_cache_recovery_browser import AudioRecoveryHandler
from test_home_layout_browser import browser_path, make_layout_server, run_layout_browser


SCRIPT = """
import {createStage} from '/frontend/src/adv/stage.js';
import {createBacklog} from '/frontend/src/adv/backlog.js';
import {createLineAudioRecovery} from '/frontend/src/controllers/line_audio_recovery.js';
import {StreamAudioController} from '/frontend/src/audio_player.js';
const nativeFetch=globalThis.fetch.bind(globalThis),mode=__MODE__;
const result={},oldTime=Date.now()-9*86400000;
const session={id:'repair-browser',settings:{voiceProfileId:7},messages:[{id:'m_42',serverMessageId:42,role:'assistant',
  content:'原始中文',ts:oldTime,lines:[{index:0,zh:'原始中文',ja:'こんにちは。',emotion:'neutral',act:{},audio:{status:'failed',chunks:[]}}]}]};
const posts=[],gets=[];let saves=0,requestSignal,release;
const line=session.messages[0].lines[0],response={session_id:session.id,message_id:42,line_index:0,voice_profile_id:7,persisted:true,
  audio:{status:'ready',generated_at:Date.now(),voice_profile_id:7,voice_name:'当前测试音色',
    chunks:[{index:0,line:0,audio_url:'/audio/repair_browser.wav',sentence:line.ja}]}};
const wav=new Blob([Uint8Array.from(atob(__WAV__),c=>c.charCodeAt(0))],{type:'audio/wav'});
globalThis.fetch=async (url,options={})=>{
  if(url==='/api/voice/regenerate-line'){
    posts.push(JSON.parse(options.body));requestSignal=options.signal;
    if(mode==='cancel')await new Promise(resolve=>{release=resolve;});
    return Response.json(response);
  }
  if(url==='/audio/repair_browser.wav'){gets.push(url);return new Response(wav);}
  throw new Error('Unexpected request '+url);
};
const wait=()=>new Promise(resolve=>setTimeout(resolve,20));
const until=async check=>{for(let i=0;i<150;i++){if(check())return;await wait();}throw new Error('Condition did not finish');};
const player=new StreamAudioController({fetchTimeoutMs:3000});player.startSession('browser-repair');
let log;
try {
  const stage=createStage(document.getElementById('fixture-root'));
  const recover=createLineAudioRecovery({getSession:()=>session,getVoiceProfiles:()=>[{id:7,name:'当前测试音色'}],saveState:()=>{saves++;return true;}});
  log=createBacklog(stage.overlay,{getSession:()=>session,getSettings:()=>session.settings,getCharacter:()=>({id:7,name:'当前测试音色'}),
    getRecoveryVoice:()=>({id:7,name:'当前测试音色'}),voiceEnabled:()=>true,regenerateLineAudio:recover,audio:player,onClose:()=>log.destroy()});
  await document.fonts.ready;await wait();
  const click=selector=>{
    const el=document.querySelector(selector),r=el?.getBoundingClientRect();
    const hit=r&&document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
    if(!el||el.disabled||el.hidden||el.closest('[inert]')||!r.width||!r.height||!el.contains(hit))
      throw new Error('Unreachable control '+selector+' hit='+hit?.className);
    el.click();
  };
  result.namedVoice=document.querySelector('.adv-log-regenerate-btn').title;
  click('.adv-log-regenerate-btn');await until(()=>posts.length===1);
  if(mode==='cancel'){
    click('.adv-log-close-btn');await until(()=>requestSignal.aborted);
    release();await wait();await wait();
    result.cancelled=requestSignal.aborted;result.oldAudioKept=line.audio.status==='failed';result.noWrites=saves===0;result.noDownloads=gets.length===0;
  }else{
    await until(()=>line.audio.status==='ready');await wait();
    const replay=document.querySelector('.adv-log-replay-btn');
    result.replayEnabled=!replay.disabled;result.noAutomaticDownloads=gets.length===0;
    const row=document.querySelector('.adv-log-entry-controls'),rect=row.getBoundingClientRect();
    result.overflow=row.scrollWidth-row.clientWidth;result.left=rect.left;result.right=rect.right;result.viewport=innerWidth;
    click('.adv-log-replay-btn');await until(()=>!replay.classList.contains('is-playing'));
    result.played=gets.length===1&&player._linePlayState===null;
    result.generatedAt=line.audio.generated_at;result.historyTimestamp=session.messages[0].ts;result.originalTimestamp=oldTime;
    result.saves=saves;
  }
  result.posts=posts;
}catch(error){result.error=String(error.stack||error);}
finally{if(mode==='cancel')log?.destroy();await player.close();}
const output=document.createElement('pre');output.id='line-recovery-result';output.hidden=true;output.textContent=JSON.stringify(result);parent.document.body.append(output);
await nativeFetch('/result',{method:'POST',body:JSON.stringify(result)});
"""


class LineRecoveryHandler(AudioRecoveryHandler):
    def __init__(self, *args, case, **kwargs):
        self.case = case
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == "/case":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(self.case)
        else:
            super().do_GET()


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
@pytest.mark.parametrize("mode", ["success", "cancel"])
def test_log_line_repair_is_reachable_and_owned(tmp_path, width, height, mode):
    executable = browser_path()
    if not executable:
        pytest.skip("LOG repair needs an installed Chromium browser")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x20\x03" * 800)
    script = SCRIPT.replace("__MODE__", json.dumps(mode)).replace("__WAV__", json.dumps(base64.b64encode(output.getvalue()).decode()))
    case = ('<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/frontend/src/styles.css">'
            '<link rel="stylesheet" href="/frontend/src/adv/adv.css"><link rel="stylesheet" href="/frontend/src/adv/app.css">'
            '<body><div id="fixture-root"></div>'
            f'<script type="module">{script}</script></body>').encode()
    page = (f'<body style="margin:0"><iframe src="/case" style="border:0;width:{width}px;height:{height}px"></iframe>'
            '<img src="/hold-image" hidden></body>').encode()
    ready, result = threading.Event(), {}
    server = make_layout_server(partial(LineRecoveryHandler, page=page, case=case, ready=ready, result=result))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        rendered = run_layout_browser([
            executable, "--headless", "--no-sandbox", "--disable-gpu", "--mute-audio", "--autoplay-policy=no-user-gesture-required",
            "--no-first-run", "--disable-sync", "--no-proxy-server", "--disable-background-networking", "--disable-extensions",
            f"--user-data-dir={tmp_path/'browser'}", f"--window-size={max(600,width+100)},{height+100}",
            "--dump-dom", f"--screenshot={tmp_path/'line-repair.png'}", f"http://127.0.0.1:{server.server_port}/",
        ])
    finally:
        ready.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    (tmp_path / "rendered.html").write_bytes(rendered.stdout)
    (tmp_path / "browser.log").write_bytes(rendered.stderr)
    assert rendered.returncode == 0 and result and "error" not in result, result or rendered.stderr[-1000:]
    assert "当前测试音色" in result["namedVoice"]
    assert len(result["posts"]) == 1 and result["posts"][0]["voice_profile_id"] == 7
    assert result["posts"][0]["message_id"] == 42 and result["posts"][0]["text"] == "こんにちは。"
    if mode == "cancel":
        assert result["cancelled"] and result["oldAudioKept"] and result["noWrites"] and result["noDownloads"]
    else:
        assert result["replayEnabled"] and result["noAutomaticDownloads"] and result["played"]
        assert result["historyTimestamp"] == result["originalTimestamp"] and result["saves"] == 1
        assert result["overflow"] <= 1 and result["left"] >= 0 and result["right"] <= width, result
