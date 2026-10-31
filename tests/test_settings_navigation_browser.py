"""Exercise production settings routes and the return-home path in a real DOM."""

import pytest

from test_engine_status_browser import render_engine_case


SCRIPT = """
globalThis.fetch=async (url,options={})=>{
  const path=new URL(typeof url==='string'?url:url.url,location.href).pathname;
  let result={status:'ok'};
  if(path==='/api/chat/sessions') result={sessions:[{id:'nav',title:'保留的测试聊天',voice_profile_id:1,message_count:2,updated_at:'2026-10-25T10:00:00Z'}]};
  else if(path.startsWith('/api/chat/history')) result={messages:[
    {id:1,role:'user',content_chinese:'测试聊天记录',created_at:'2026-10-25T10:00:00Z'},
    {id:2,role:'assistant',content_chinese:'这段记录会保留。',created_at:'2026-10-25T10:00:00Z'}],has_more:false};
  else if(path==='/api/voice/profiles') result={profiles:[{id:1,name:'测试角色',work:null}],active_profile_id:1};
  else if(path==='/api/providers') result={providers:[{id:'custom',name:'测试模型',provider_type:'custom',chat_model:'synthetic',base_url:'https://synthetic.invalid/v1',is_active:true}],presets:[]};
  else if(path==='/api/config') result={status:'ok',settings:{gpt_sovits_url:'http://127.0.0.1:19872',inference_precision:'cpu'}};
  else if(path==='/api/system/engine-status') result={state:'ready',ready:true,message:'语音引擎已就绪。',base_url:'http://127.0.0.1:19872'};
  else if(path==='/api/system/status') result={gpt_sovits:{status:'reachable',base_url:'http://127.0.0.1:19872'}};
  else if(path.includes('portrait')) result={enabled:false};
  else if(path.includes('background')) result={backgrounds:[]};
  else if(path==='/api/auth/status') result={auth_required:false};
  return Response.json(result);
};
await import('/frontend/src/main.js');
const wait=()=>new Promise(resolve=>setTimeout(resolve,150));
const visible=el=>el && !el.hidden && !el.classList.contains('hidden');
// Under reduced motion styles.css gives every element a 0.01ms transition, and a
// transition only advances when Chrome paints. A loaded runner can fire many
// timers before the next frame, so finish transitions before hit-testing.
const settle=()=>document.getAnimations().forEach(a=>{if(a instanceof CSSTransition)a.finish();});
const click=selector=>{
  settle();
  const el=document.querySelector(selector);if(!el)throw new Error('Missing '+selector);
  const r=el.getBoundingClientRect(),hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
  if(el.disabled || el.closest('[inert]') || r.width<=0 || r.height<=0 || !el.contains(hit))
    throw new Error('Control is not reachable: '+selector+'; inert='+el.closest('[inert]')?.className+'; hit='+hit?.className+'; rect='+JSON.stringify(r)+'; styles='+JSON.stringify([el,el.parentElement,el.parentElement.parentElement,el.parentElement.parentElement.parentElement].map(e=>({cls:e.className,pe:getComputedStyle(e).pointerEvents,visibility:getComputedStyle(e).visibility,opacity:getComputedStyle(e).opacity}))));
  el.click();
};
const state={};
try {
  await wait();await wait();
  const stage=document.querySelector('.adv-stage');
  state.startedHome=stage.classList.contains('is-home');
  click('.home-gear'); await wait();
  state.center=visible(document.getElementById('globalModal'));
  click('#gOpenChatExperience'); await wait();
  const reading=document.querySelector('.adv-config-overlay');
  state.reading=visible(reading);
  state.centerShared=visible(document.getElementById('globalModal'));
  const homeButton=document.querySelector('#globalModal [data-home]');
  const rect=homeButton.getBoundingClientRect();
  state.homeButtonVisible=rect.height>=20 && rect.bottom<=innerHeight && rect.right<=innerWidth;
  click('[data-gtab="dashboard"]'); await wait();
  state.readingClosed=!visible(reading);
  state.centerAgain=visible(document.getElementById('globalModal'));
  click('#gOpenCharacterSettings'); await wait();
  state.character=visible(document.getElementById('sessionModal'));
  state.duplicateReading=document.querySelector('#sessionModal #s-advWindowOpacity')!==null;
  click('#globalModal [data-home]'); await wait();
  state.returnedHome=stage.classList.contains('is-home') && !stage.closest('[inert]')
    && !visible(document.getElementById('sessionModal')) && !visible(reading);
  state.historyPreserved=document.querySelector('.home-quote')?.textContent.includes('这段记录会保留');
  click('.home-continue'); await wait();await wait();
  state.enteredChat=!stage.classList.contains('is-home');
  click('.adv-qm-btn-config'); await wait();
  state.chatUsesCenter=visible(document.getElementById('globalModal')) && !visible(reading);
  click('#globalModal [data-home]'); await wait();
  state.finalHome=stage.classList.contains('is-home') && !stage.closest('[inert]');
  state.overflow=document.documentElement.scrollWidth-innerWidth;
  state.left=0;state.right=innerWidth;state.viewport=innerWidth;
} catch(error) {
  state.error=error.stack;state.homeText=document.querySelector('.adv-layer-overlay')?.textContent?.slice(-1500);
  const store=await import('/frontend/src/store.js');state.sessions=store.state.sessions;
  state.viewport=innerWidth;state.overflow=0;state.left=0;state.right=innerWidth;
}
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(state);parent.document.body.append(output);
"""


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_one_settings_entry_and_return_home_preserves_history(tmp_path, width, height):
    data = render_engine_case(tmp_path, width, height, SCRIPT, 6000)
    assert "error" not in data, data
    assert all(data[key] for key in ("startedHome", "center", "reading", "centerShared", "homeButtonVisible",
                                    "readingClosed", "centerAgain", "character", "returnedHome",
                                    "historyPreserved", "enteredChat", "chatUsesCenter", "finalHome")), data
    assert data["duplicateReading"] is False


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_return_home_cancels_native_audio_preparation_before_unlock(tmp_path, width, height):
    script = SCRIPT.replace("const output=document.createElement('pre');", """
if(!state.error) {
  click('.home-gear'); await wait();
  const {streamAudioController:audio}=await import('/frontend/src/audio_player.js');
  const context=audio.ensureContext({resumeIfSuspended:false});
  await context.suspend();
  let finishResume, outcome, oldFetches=0;
  context.resume=()=>new Promise(resolve=>{finishResume=resolve;});
  const apiFetch=globalThis.fetch;
  globalThis.fetch=(url,options)=>{
    if(String(url).includes('/cancelled-sentence.wav')) oldFetches++;
    return apiFetch(url,options);
  };
  const pending=audio.playLine(0,['/cancelled-sentence.wav']).then(value=>{outcome=value;});
  click('#globalModal [data-home]'); await wait();
  state.cancelledBeforeUnlock=outcome==='interrupted';
  finishResume(); await pending; await wait();
  state.noOldDownloads=oldFetches===0;
  state.finalHome=document.querySelector('.adv-stage').classList.contains('is-home');
  await audio.close();
}
const output=document.createElement('pre');
""")
    data = render_engine_case(tmp_path, width, height, script, 8000)
    assert "error" not in data, data
    assert data["cancelledBeforeUnlock"] is True
    assert data["noOldDownloads"] is True
    assert data["finalHome"] is True
