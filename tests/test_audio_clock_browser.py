"""Check sentence start against a native, explicitly paused AudioContext clock."""

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
import {StreamAudioController} from '/frontend/src/audio_player.js';
const nativeFetch=globalThis.fetch.bind(globalThis),mode=__MODE__,result={};
const player=new StreamAudioController(),starts=[];
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const until=async check=>{
  const deadline=performance.now()+4000;
  while(!check()){if(performance.now()>deadline)throw new Error('Audio condition did not finish');await wait(10);}
};
try{
  const wav=new Blob([Uint8Array.from(atob(__WAV__),c=>c.charCodeAt(0))],{type:'audio/wav'});
  player.startSession('native-clock');await player.ctx.resume();
  player.currentBufferLeadSec=0.3;
  const pending=player.playLine(0,[{blob:wav}],{onStart:()=>starts.push(player.ctx.currentTime)});
  await until(()=>player._linePlayState?.activeSources.length===1);
  const scheduled=player._linePlayState.activeSources[0].startTime;
  player.pause();await until(()=>player.ctx.state==='suspended');
  const frozen=player.ctx.currentTime;
  if(frozen>=scheduled)throw new Error('Test did not pause before the scheduled sound');
  await wait(450);
  result.clockFrozen=Math.abs(player.ctx.currentTime-frozen)<0.001;
  result.pausedStarts=starts.length;
  if(starts.length)throw new Error('Text started while native audio was paused');
  if(mode==='resume'){
    player.resume();await until(()=>starts.length===1);
    result.startedAtAudioTime=starts[0]>=scheduled;
    result.outcome=await pending;
  }else{
    if(mode==='stop')player.stopLine({fadeMs:0});
    else player.startSession('replacement');
    result.outcome=await pending;
    await player.ctx.resume();await wait(400);
    result.cancelledStarts=starts.length;
    result.noOwnedLine=player._linePlayState===null;
  }
}catch(error){result.error=String(error.stack||error);}
finally{await player.close();}
await nativeFetch('/result',{method:'POST',body:JSON.stringify(result)});
"""


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("mode", ["resume", "stop", "new-session"])
def test_native_audio_clock_gates_text_start_and_cancellation(tmp_path, mode):
    browser = browser_path()
    if not browser:
        pytest.skip("Native audio clock validation needs an installed Chromium browser")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x20\x03" * 8000)
    script = SCRIPT.replace("__MODE__", json.dumps(mode)).replace(
        "__WAV__", json.dumps(base64.b64encode(output.getvalue()).decode()))
    page = ('<!doctype html><meta charset="utf-8"><body><img src="/hold-image" hidden>'
            f'<script type="module">{script}</script></body>').encode()
    ready, result = threading.Event(), {}
    server = make_layout_server(partial(AudioRecoveryHandler, page=page, ready=ready, result=result))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        rendered = run_layout_browser([
            browser, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-sync",
            "--mute-audio", "--autoplay-policy=no-user-gesture-required", "--no-default-browser-check",
            "--no-proxy-server", "--disable-background-networking", "--disable-extensions",
            f"--user-data-dir={tmp_path/'browser'}", "--dump-dom", f"http://127.0.0.1:{server.server_port}/",
        ], reset=lambda: (ready.clear(), result.clear()))
    finally:
        ready.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    (tmp_path / "rendered.html").write_bytes(rendered.stdout)
    (tmp_path / "browser.log").write_bytes(rendered.stderr)
    (tmp_path / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    assert rendered.returncode == 0 and result and "error" not in result, result or rendered.stderr[-1000:]
    assert result["clockFrozen"] is True and result["pausedStarts"] == 0
    if mode == "resume":
        assert result["startedAtAudioTime"] is True and result["outcome"] == "ended"
    else:
        assert result["outcome"] == "interrupted"
        assert result["cancelledStarts"] == 0 and result["noOwnedLine"] is True
