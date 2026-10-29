"""Real DOM reflow must not bypass the first text reveal gate."""

import pytest

from test_engine_status_browser import render_engine_case


SCRIPT = """
import {mountAdv} from '/frontend/src/adv/adv_app.js';
import {DEFAULT_SESSION_SETTINGS} from '/frontend/src/store.js';
globalThis.fetch=async()=>Response.json({});
const host=document.createElement('div');document.body.replaceChildren(host);
const dom={root:host};
for(const key of ['sidebar','messages','input','micBtn'])dom[key]=document.createElement(key==='input'?'textarea':'div');
const handlers=new Map(),plays=[];
let overlayChange;
const session={id:'synthetic-reflow',messages:[],settings:{...DEFAULT_SESSION_SETTINGS,advTextIntervalMs:0}};
const controller={busy:true,canRetry:()=>false,stop(){},on(name,fn){
  if(!handlers.has(name))handlers.set(name,new Set());handlers.get(name).add(fn);
  return ()=>handlers.get(name).delete(fn);
}};
const emit=(name,event)=>{for(const fn of handlers.get(name)||[])fn(event);};
const audio={supportsStartNotification:true,playLine(index,chunks,options){
  return new Promise(resolve=>plays.push({options,resolve}));
},stopLine(){plays.at(-1)?.resolve('interrupted');}};
const app=mountAdv(dom,{controller,audio,getSession:()=>session,voiceEnabled:()=>true,
  getCharacter:()=>({id:1,name:'测试角色'}),loadPortrait:async()=>null,
  saveSettings:patch=>Object.assign(session.settings,patch),
  subscribeOverlay:fn=>{overlayChange=fn;return()=>{};},subscribe:()=>()=>{},
});
const primary=host.querySelector('.adv-primary-text'),subtitle=host.querySelector('.adv-subtitle-text');
const empty=()=>{if(primary.textContent||subtitle.textContent)throw new Error('Premature text: '+primary.textContent);};
const start=id=>emit('turnstart',{originId:session.id,assistantMessage:{id},userMessage:{id:'synthetic-user'}});
const line=id=>emit('line',{messageId:id,line:{index:0,zh:'等待同步的新台词。',ja:'音声を待ちます。',emotion:'neutral',act:{}}});
const ready=id=>emit('line_audio',{messageId:id,line:0,status:'ready',chunks:[{audio_url:'/synthetic.wav'}]});
const resize=()=>window.dispatchEvent(new Event('resize'));
const result={};
try{
  start('one');line('one');empty();resize();empty();app.action('swap_languages');empty();
  await new Promise(resolve=>setTimeout(resolve,2200));empty();
  result.generationDelayHeld=true;
  result.generationReflowHeld=true;
  ready('one');empty();resize();empty();
  result.decodeReflowHeld=true;
  plays.at(-1).options.onStart();
  if(!primary.textContent.includes('音声を待ちます'))throw new Error('Updated language was not revealed at playback start');
  result.updatedLanguageAtStart=true;
  start('two');empty();overlayChange(true);empty();overlayChange(false);empty();
  app.action('swap_languages');empty();
  result.previousReplyNotRestored=true;
  line('two');ready('two');
  const stale=plays.at(-1).options.onStart;
  overlayChange(true);empty();overlayChange(false);empty();
  stale();empty();
  plays.at(-1).options.onStart();
  if(!primary.textContent.includes('等待同步的新台词'))throw new Error('Resumed playback did not reveal the current sentence');
  result.resumedCurrentSentence=true;result.playCalls=plays.length;
  const rect=app.stage.stage.getBoundingClientRect();
  result.left=rect.left;result.right=rect.right;result.viewport=innerWidth;
  result.overflow=document.documentElement.scrollWidth-innerWidth;
}catch(error){result.error=String(error.stack||error);}
finally{app.unmount();}
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(result);parent.document.body.append(output);
"""


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_reflow_holds_text_until_audio_start_in_the_real_dom(tmp_path, width, height):
    data = render_engine_case(tmp_path, width, height, SCRIPT, 6000)
    assert "error" not in data, data
    for key in (
        "generationDelayHeld", "generationReflowHeld", "decodeReflowHeld", "updatedLanguageAtStart",
        "previousReplyNotRestored", "resumedCurrentSentence",
    ):
        assert data[key] is True, (key, data)
    assert data["playCalls"] == 3


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_disabling_voice_under_settings_reveals_pending_line_after_close(tmp_path, width, height):
    script = SCRIPT.split("const result={};")[0].replace(
        "const app=mountAdv", "let voiceEnabled=true;\nconst app=mountAdv",
    ).replace("voiceEnabled:()=>true", "voiceEnabled:()=>voiceEnabled") + """
const result={};
try {
  start('pending');line('pending');empty();
  overlayChange(true);
  voiceEnabled=false;app.update();empty();
  overlayChange(false);
  if(!primary.textContent.includes('等待同步的新台词'))throw new Error('Sentence remains hidden after disabling voice');
  ready('pending');
  if(plays.length)throw new Error('Late audio played after voice was disabled');
  result.ok=true;
  const rect=app.stage.stage.getBoundingClientRect();
  result.left=rect.left;result.right=rect.right;result.viewport=innerWidth;
  result.overflow=document.documentElement.scrollWidth-innerWidth;
}catch(error){result.error=String(error.stack||error);}
finally{app.unmount();}
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(result);parent.document.body.append(output);
"""
    data = render_engine_case(tmp_path, width, height, script, 5000)
    assert "error" not in data, data
    assert data["ok"] is True


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_voice_button_completes_pending_advance_in_the_real_dom(tmp_path, width, height):
    script = SCRIPT.split("const result={};")[0].replace(
        "advTextIntervalMs:0", "advTextIntervalMs:0,advInterruptVoice:false",
    ).replace(
        "const app=mountAdv", "let voiceEnabled=true;\nconst app=mountAdv",
    ).replace(
        "voiceEnabled:()=>true", "voiceEnabled:()=>voiceEnabled,toggleVoice:()=>{voiceEnabled=!voiceEnabled;}",
    ) + """
const result={};
try {
  start('pending');line('pending');ready('pending');plays.at(-1).options.onStart();
  emit('line',{messageId:'pending',line:{index:1,zh:'已经进入第二句。',ja:'次の文章。',emotion:'neutral',act:{}}});
  app.action('advance');
  if(!primary.textContent.includes('等待同步的新台词'))throw new Error('Advanced before voice ended');
  app.action('voice_mode');
  if(!primary.textContent.includes('已经进入第二句'))throw new Error('Voice button lost the pending advance');
  await Promise.resolve();await Promise.resolve();
  if(!primary.textContent.includes('已经进入第二句'))throw new Error('Late playback completion advanced twice');
  result.ok=true;
  const rect=app.stage.stage.getBoundingClientRect();
  result.left=rect.left;result.right=rect.right;result.viewport=innerWidth;
  result.overflow=document.documentElement.scrollWidth-innerWidth;
}catch(error){result.error=String(error.stack||error);}
finally{app.unmount();}
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(result);parent.document.body.append(output);
"""
    data = render_engine_case(tmp_path, width, height, script, 5000)
    assert "error" not in data, data
    assert data["ok"] is True
