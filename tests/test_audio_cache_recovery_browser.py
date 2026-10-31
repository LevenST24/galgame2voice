"""Use native browser decoding and CacheStorage with isolated synthetic audio."""

from functools import partial
import json
import threading

import pytest

from test_home_layout_browser import LayoutHandler, browser_path, make_layout_server, run_layout_browser


SCRIPT = """
import {putCachedAudioBlob,getCachedAudioBlob,clearMemAudioCache,pruneAudioCacheStorage} from '/frontend/src/cache.js';
import {StreamAudioController} from '/frontend/src/audio_player.js';
const nativeFetch=globalThis.fetch.bind(globalThis);
const result={overflow:0,left:0,right:innerWidth,viewport:innerWidth};
try {
  const tier=__TIER__;
  const url='/audio/cache/native-recovery.wav';
  const broken=new Blob(['synthetic corrupt WAV'],{type:'audio/wav'});
  const buffer=new ArrayBuffer(44+800*2), view=new DataView(buffer);
  const text=(offset,value)=>{for(let i=0;i<value.length;i++)view.setUint8(offset+i,value.charCodeAt(i));};
  text(0,'RIFF'); view.setUint32(4,buffer.byteLength-8,true); text(8,'WAVE');
  text(12,'fmt '); view.setUint32(16,16,true); view.setUint16(20,1,true); view.setUint16(22,1,true);
  view.setUint32(24,8000,true); view.setUint32(28,16000,true); view.setUint16(32,2,true); view.setUint16(34,16,true);
  text(36,'data'); view.setUint32(40,1600,true);
  for(let i=0;i<800;i++)view.setInt16(44+2*i,Math.round(8000*Math.sin(2*Math.PI*440*i/8000)),true);
  const good=new Blob([buffer],{type:'audio/wav'});
  await putCachedAudioBlob(url,broken);
  await putCachedAudioBlob('user_recording_native',new Blob(['original native recording']));
  if(tier==='legacy-disk') {
    const cache=await caches.open('gal2voice-audio-v1');
    const [request]=await cache.keys();
    await cache.put(request,new Response(broken,{headers:{'Content-Type':'audio/wav','X-Cached-At':'1700000000000'}}));
  }
  if(tier==='disk'||tier==='legacy-disk')clearMemAudioCache();
  const calls=[],errors=[];
  globalThis.fetch=async (url,options={})=>{
    calls.push({url,method:options.method||'GET',cache:options.cache});return new Response(good);
  };
  const player=new StreamAudioController({fetchTimeoutMs:5000});player.startSession('native-cache-recovery');
  const context=player.ctx,decode=context.decodeAudioData.bind(context);
  context.decodeAudioData=async data=>{
    try{
      const decoded=await decode(data);
      result.samples=decoded.length;result.channels=decoded.numberOfChannels;result.sampleRate=decoded.sampleRate;
      return decoded;
    }
    catch(error){errors.push(error.name);throw error;}
  };
  result.playback=await player.playLine(0,[{url,blob:tier==='provided-blob'?broken:null}]);
  await player.close();
  result.errors=errors;result.calls=calls;
  // Wait for the optional background disk write without performing a new put.
  let stored=null;
  for(let attempt=0;attempt<30;attempt++) {
    clearMemAudioCache();stored=await getCachedAudioBlob(url);
    if(stored?.size===good.size)break;
    await new Promise(resolve=>setTimeout(resolve,25));
  }
  result.storedBytes=stored?.size;
  result.recording=await (await getCachedAudioBlob('user_recording_native')).text();
  await pruneAudioCacheStorage(0);clearMemAudioCache();
  result.recordingAfterPrune=await (await getCachedAudioBlob('user_recording_native')).text();
  result.generatedAfterPrune=Boolean(await getCachedAudioBlob(url));
} catch(error){result.error=String(error.stack||error);}
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(result);parent.document.body.append(output);
await nativeFetch('/result',{method:'POST',body:JSON.stringify(result)});
"""


class AudioRecoveryHandler(LayoutHandler):
    def __init__(self, *args, ready, result, **kwargs):
        self.ready, self.result = ready, result
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == "/hold-image":
            # Hold the load event until native decoding/storage finishes. A
            # virtual clock would advance JS deadlines while native I/O waits.
            self.ready.wait(timeout=15)
            self.send_response(204)
            self.end_headers()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path != "/result":
            self.send_error(404)
            return
        self.result.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(204)
        self.end_headers()
        self.ready.set()


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("tier", ["memory", "disk", "provided-blob", "legacy-disk"])
def test_native_audio_decode_recovers_only_the_invalid_url(tmp_path, tier):
    executable = browser_path()
    if not executable:
        pytest.skip("Native audio recovery needs an installed Chromium browser")
    script = SCRIPT.replace("__TIER__", json.dumps(tier))
    page = ('<!doctype html><meta charset="utf-8"><body><img src="/hold-image" hidden>'
            f'<script type="module">{script}</script></body>').encode()
    ready, result = threading.Event(), {}
    server = make_layout_server(partial(AudioRecoveryHandler, page=page, ready=ready, result=result))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        rendered = run_layout_browser([
            executable, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-sync",
            "--mute-audio", "--autoplay-policy=no-user-gesture-required",
            "--no-default-browser-check", "--no-proxy-server", "--disable-background-networking", "--disable-extensions",
            f"--user-data-dir={tmp_path/'browser'}", "--dump-dom", f"http://127.0.0.1:{server.server_port}/",
        ], reset=lambda: (ready.clear(), result.clear()))
    finally:
        ready.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    (tmp_path / "rendered.html").write_bytes(rendered.stdout)
    (tmp_path / "browser.log").write_bytes(rendered.stderr)
    assert rendered.returncode == 0 and result, rendered.stderr.decode("utf-8", errors="replace")[-1000:]
    assert "error" not in result, result
    assert result["playback"] == "ended"
    assert result["samples"] == result["sampleRate"] // 10 and result["channels"] == 1
    assert result["errors"] == ["EncodingError"]
    assert result["calls"] == [{"url": "/audio/cache/native-recovery.wav", "method": "GET", "cache": "no-store"}]
    assert result["storedBytes"] == 1644
    assert result["recording"] == "original native recording"
    assert result["recordingAfterPrune"] == "original native recording"
    assert result["generatedAfterPrune"] is False
