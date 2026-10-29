"""All settings pages share one reachable dialog, including keyboard navigation."""

import pytest

from test_engine_status_browser import render_engine_case
from test_settings_navigation_browser import SCRIPT as NAVIGATION_SCRIPT


SCRIPT = NAVIGATION_SCRIPT.split("const state={};")[0] + """
const state={left:0,right:innerWidth,viewport:innerWidth,overflow:0};
try {
  await wait();await wait();
  const center=document.getElementById('globalModal');
  const oneDialog=()=>{
    const visibleDialogs=[...document.querySelectorAll('[role="dialog"]')].filter(el=>
      el.getClientRects().length && !el.closest('[hidden],.hidden'));
    if(visibleDialogs.length!==1 || visibleDialogs[0]!==center.querySelector('.modal'))
      throw new Error('Settings must share a single dialog: '+visibleDialogs.map(el=>el.getAttribute('aria-label')));
    if(document.activeElement.closest('[inert]'))throw new Error('Focus is inert');
  };
  click('.home-gear'); await wait();oneDialog();
  const shell=center.querySelector('.modal');
  const size=()=>[shell.offsetWidth,shell.offsetHeight].join(',');
  const initialSize=size();
  click('#gOpenChatExperience'); await wait();oneDialog();
  if(!visible(center))throw new Error('Experience opened a second settings window');
  document.querySelector('#cfg-advPlayerName').value='未保存名字';
  click('#gOpenCharacterSettings'); await wait();oneDialog();
  if(!visible(center) || !visible(document.getElementById('sessionModal')))
    throw new Error('Character settings left the shared center');
  document.getElementById('sSystem').value='保留跨页的角色草稿';
  if(initialSize!==size())throw new Error('Settings shell changed size: '+initialSize+' -> '+size());
  click('[data-gtab="models"]');await wait();oneDialog();
  click('#gOpenCharacterSettings');await wait();oneDialog();
  if(document.getElementById('sSystem').value!=='保留跨页的角色草稿')throw new Error('Session draft lost across tabs');
  document.querySelector('[data-gtab="memory"]').scrollIntoView({block:'nearest',inline:'nearest'});
  click('[data-gtab="memory"]');await wait();click('#gOpenSessionSettings');await wait();oneDialog();
  if(document.getElementById('gp-session').classList.contains('hidden'))
    throw new Error('Memory page shortcut did not return to character settings');
  click('#gOpenChatExperience');await wait();oneDialog();
  if(document.querySelector('#cfg-advPlayerName').value==='未保存名字')throw new Error('Unsaved experience preview leaked across pages');
  // Embedded experience settings must let keyboard focus reach the shared navigation.
  const models=document.querySelector('[data-gtab="models"]');models.focus();
  if(document.activeElement!==models)throw new Error('Embedded editor trapped focus');
  click('#globalModal [data-home]');await wait();
  if(visible(center) || document.querySelector('.adv-stage').closest('[inert]'))throw new Error('Home is blocked');
  if(!document.querySelector('.adv-stage').classList.contains('is-home'))throw new Error('Not on home');
  if(!document.querySelector('.home-quote')?.textContent.includes('这段记录会保留'))throw new Error('History lost');
  click('.home-continue');await wait();await wait();
  click('.adv-qm-btn-config');await wait();oneDialog();
  click('#gOpenChatExperience');await wait();
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));await wait();
  if(visible(center))throw new Error('Escape did not close the shared center');
  if(document.querySelector('.adv-stage').closest('[inert]'))throw new Error('Stage stayed inert');
  click('.adv-qm-btn-config');await wait();click('#gOpenChatExperience');await wait();
  const speed=document.getElementById('cfg-advTextIntervalMs');
  speed.value='45';speed.dispatchEvent(new Event('input',{bubbles:true}));
  click('#gExperienceSave');await wait();
  const store=await import('/frontend/src/store.js');
  if(visible(center) || store.getActive().settings.advTextIntervalMs!==45)
    throw new Error('Shared experience save did not persist and close');
  click('.adv-qm-btn-config');await wait();click('#gOpenChatExperience');await wait();oneDialog();
  if(document.getElementById('cfg-advTextIntervalMs').value!=='45')throw new Error('Saved experience was not restored');
  const editor=document.querySelector('.is-embedded .adv-config-dialog');
  if(editor.scrollWidth>editor.clientWidth+1)throw new Error('Experience controls overflow the shared page');
  if(document.querySelector('#gTabs [aria-selected="true"]').dataset.gtab!=='experience')
    throw new Error('Navigation selection does not match the displayed page');
  state.ok=true;
} catch(error) {state.error=error.stack;}
state.overflow=document.documentElement.scrollWidth-innerWidth;
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(state);parent.document.body.append(output);
"""


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_settings_pages_stay_in_one_dialog_and_return_home(tmp_path, width, height):
    data = render_engine_case(tmp_path, width, height, SCRIPT, 9000)
    assert "error" not in data, data
    assert data["ok"] is True


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_return_home_rejects_a_late_character_editor_open(tmp_path, width, height):
    script = NAVIGATION_SCRIPT.split("const state={};")[0] + """
const state={left:0,right:innerWidth,viewport:innerWidth,overflow:0};
try {
  await wait();await wait();click('.home-gear');await wait();await wait();
  const voice=await import('/frontend/src/controllers/voice_settings.js');
  const apiFetch=globalThis.fetch;
  globalThis.fetch=(url,options)=>String(url).includes('/api/voice/profiles')
    ?Promise.resolve(Response.json({profiles:[],active_profile_id:null})):apiFetch(url,options);
  await voice.fetchVoiceProfiles();
  let release;
  globalThis.fetch=(url,options)=>String(url).includes('/api/voice/profiles')
    ?new Promise(resolve=>{release=resolve;}):apiFetch(url,options);
  const pending=voice.openSessionSettings();
  if(!release)throw new Error('Expected pending character catalog');
  click('#globalModal [data-home]');await wait();
  release(Response.json({profiles:[{id:2,name:'迟到的角色'}],active_profile_id:2}));
  await pending;await wait();
  if(visible(document.getElementById('globalModal')) || visible(document.getElementById('sessionModal')))
    throw new Error('Late character read reopened settings');
  const stage=document.querySelector('.adv-stage');
  if(!stage.classList.contains('is-home') || stage.closest('[inert]'))throw new Error('Home is not usable');
  state.ok=true;
} catch(error) {state.error=error.stack;}
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(state);parent.document.body.append(output);
"""
    data = render_engine_case(tmp_path, width, height, script, 5000)
    assert "error" not in data, data
    assert data["ok"] is True


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
@pytest.mark.parametrize("outcome", ["failed-retry", "newer-draft", "late-success", "late-failure"])
def test_experience_save_owns_confirmation_and_current_editor(tmp_path, width, height, outcome):
    script = NAVIGATION_SCRIPT.split("const state={};")[0] + """
const state={left:0,right:innerWidth,viewport:innerWidth,overflow:0},outcome=__OUTCOME__;
try{
  await wait();await wait();click('.home-gear');await wait();click('#gOpenChatExperience');await wait();
  const center=document.getElementById('globalModal'),save=document.getElementById('gExperienceSave');
  const name=document.getElementById('cfg-advPlayerName'),writes=[];
  const apiFetch=globalThis.fetch;let complete,automatic=false;
  globalThis.fetch=(url,options={})=>{
    if(String(url)==='/api/chat/sessions'&&options.method==='POST'){
      writes.push(JSON.parse(options.body));
      return automatic?Promise.resolve(Response.json({status:'ok'})):new Promise(resolve=>{complete=resolve;});
    }
    return apiFetch(url,options);
  };
  name.value='本次提交的名字';click('#gExperienceSave');await wait();
  if(writes.length!==1||!complete)throw new Error('Expected one owned settings write');
  if(!visible(center)||!save.disabled)throw new Error('Settings closed before confirmation');
  save.click();await wait();
  if(writes.length!==1)throw new Error('Repeated save produced a duplicate write');
  if(outcome==='failed-retry'){
    complete(Response.json({detail:'合成的保存失败'},{status:500}));await wait();
    if(!visible(center)||save.disabled||name.value!=='本次提交的名字')throw new Error('Failed save discarded the editor');
    if(!document.getElementById('cfg-global-error').textContent.includes('尚未确认'))throw new Error('Failure was silent');
    const error=document.getElementById('cfg-global-error'),r=error.getBoundingClientRect();
    const body=center.querySelector(':scope > .modal > .modal-body').getBoundingClientRect();
    if(r.top<body.top-1||r.bottom>body.bottom+1||!error.contains(document.elementFromPoint(r.left+r.width/2,r.top+r.height/2)))
      throw new Error('Failure is outside the visible settings area');
    automatic=true;click('#gExperienceSave');await wait();
    if(visible(center)||writes.length!==2)throw new Error('Explicit retry did not confirm and close');
  }else if(outcome==='newer-draft'){
    name.value='保存期间的新修改';complete(Response.json({status:'ok'}));await wait();
    if(!visible(center)||save.disabled||name.value!=='保存期间的新修改')throw new Error('New draft was lost');
    if(!document.getElementById('cfg-global-error').textContent.includes('新增'))throw new Error('New draft was not explained');
    automatic=true;click('#gExperienceSave');await wait();
    if(visible(center)||writes.length!==2||writes[0].settings.advPlayerName!=='本次提交的名字'
       ||writes[1].settings.advPlayerName!=='保存期间的新修改')throw new Error('Writes did not preserve submission order');
  }else{
    click('#globalModal [data-home]');await wait();
    click('.home-gear');await wait();click('#gOpenChatExperience');await wait();
    name.value='重新打开后的草稿';
    complete(outcome==='late-failure'?Response.json({detail:'迟到的保存失败'},{status:500}):Response.json({status:'ok'}));
    await wait();
    if(!visible(center)||name.value!=='重新打开后的草稿'||save.disabled)throw new Error('Old write changed the new editor');
    if(!document.getElementById('cfg-global-error').hidden)throw new Error('Old status leaked into the new editor');
    if(outcome==='late-failure'&&!document.getElementById('toastRoot').textContent.includes('尚未确认保存'))
      throw new Error('Closed editor lost its save failure');
  }
  state.ok=true;state.writes=writes.length;
}catch(error){state.error=error.stack;}
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(state);parent.document.body.append(output);
""".replace("__OUTCOME__", repr(outcome))
    data = render_engine_case(tmp_path, width, height, script, 7000)
    assert "error" not in data, data
    assert data["ok"] is True


@pytest.mark.requires_piped_subprocess
def test_experience_save_invalidates_an_older_startup_snapshot(tmp_path):
    script = NAVIGATION_SCRIPT.split("const state={};")[0].replace(
        "await import('/frontend/src/main.js');", """
const {DEFAULT_SESSION_SETTINGS}=await import('/frontend/src/store.js');
localStorage.setItem('gal2voice.chat.v1',JSON.stringify({
  activeId:'nav',global:{voiceMode:true},sessions:[{id:'nav',title:'启动时编辑的会话',
    createdAt:Date.now(),updatedAt:Date.now(),settings:{...DEFAULT_SESSION_SETTINGS},messages:[]}]
}));
const apiFetch=globalThis.fetch;let deliverList;
globalThis.fetch=(url,options={})=>String(url).startsWith('/api/chat/sessions?')&&!options.method
  ?new Promise(resolve=>{deliverList=resolve;}):apiFetch(url,options);
await import('/frontend/src/main.js');
""") + """
const state={left:0,right:innerWidth,viewport:innerWidth,overflow:0};
try{
  await wait();
  if(!deliverList)throw new Error('Expected an outstanding startup snapshot');
  const settings=await import('/frontend/src/controllers/global_settings.js');
  settings.openGlobalSettings('experience');await wait();
  document.getElementById('cfg-advPlayerName').value='启动时刚保存的名字';
  click('#gExperienceSave');await wait();
  deliverList(Response.json({sessions:[{id:'nav',title:'启动时编辑的会话',message_count:0,
    settings:{...DEFAULT_SESSION_SETTINGS,advPlayerName:'迟到的旧名字'}}],has_more:false}));
  await wait();await wait();
  const {getActive}=await import('/frontend/src/store.js');
  if(getActive().settings.advPlayerName!=='启动时刚保存的名字')throw new Error('Old startup settings replaced the saved edit');
  state.ok=true;
}catch(error){state.error=error.stack;}
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(state);parent.document.body.append(output);
"""
    data = render_engine_case(tmp_path, 1280, 900, script, 5000)
    assert "error" not in data, data
    assert data["ok"] is True
