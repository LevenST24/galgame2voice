"""Selection errors reach the right settings and permit an explicit voice retry."""

import json

import pytest

from test_engine_status_browser import render_engine_case
from test_settings_navigation_browser import SCRIPT as NAVIGATION_SCRIPT


API = """
let selectionCalls=0,savedBinding=null,switchedBinding=null;
const selectionBodies=[],selectionBaseFetch=globalThis.fetch,selectionKind=__KIND__;
globalThis.fetch=async(url,options={})=>{
  const path=new URL(typeof url==='string'?url:url.url,location.href).pathname;
  if(path==='/api/chat/stream'){
    selectionCalls++;const body=JSON.parse(options.body);selectionBodies.push(body);
    const error=selectionKind==='voice'?{error:'所选音色已不存在，请在角色与会话中重新选择。',error_code:'VOICE_PROFILE_NOT_FOUND'}
      :{error:'指定的模型配置已不存在，请在对话模型中重新选择。',error_code:'PROVIDER_NOT_FOUND'};
    const events=selectionCalls===1?[['error',error]]:[
      ['line',{index:0,zh:'重新选择后继续聊天。',ja:'選び直して続けます。',emotion:'neutral',act:{}}],
      ['line_audio',{line:0,status:'skipped',chunks:[]}],
      ['done',{chinese:'重新选择后继续聊天。',japanese:'選び直して続けます。'}],
    ];
    return new Response(events.map(([event,data])=>`event: ${event}\\ndata: ${JSON.stringify(data)}\\n\\n`).join(''));
  }
  if(selectionCalls&&path==='/api/voice/profiles')return Response.json({profiles:[{id:2,name:'恢复角色',system_prompt:''}],active_profile_id:switchedBinding});
  if(path==='/api/chat/sessions'&&options.method==='POST'){
    const body=JSON.parse(options.body);savedBinding=body.voice_profile_id;
    return Response.json({session:body});
  }
  if(path==='/api/voice/switch'&&options.method==='POST'){
    const want=JSON.parse(options.body).profile_id;
    if(selectionCalls&&want!==2)return Response.json({detail:'所选音色已不存在'},{status:404});
    switchedBinding=want;return Response.json({profile:'恢复角色',profile_id:switchedBinding});
  }
  return selectionBaseFetch(url,options);
};
"""

RECOVERY = """
// Without reduced motion, as on the Linux CI runner, the player buttons transition every property,
// visibility included, and the hit tests below could run before that transition had ticked.
document.head.appendChild(Object.assign(document.createElement('style'),{textContent:'*{transition:none!important}'}));
if(!state.error)try{
  click('.home-continue');await wait();await wait();
  const input=document.querySelector('.adv-player-textarea');
  input.value='这条问题需要保留';input.dispatchEvent(new Event('input',{bubbles:true}));
  click('.adv-player-send-btn');await wait();await wait();
  const failure=document.querySelector('.adv-error-message');
  if(!failure.textContent.includes('已不存在'))throw new Error('Missing selection failure');
  const button=label=>[...document.querySelectorAll('.adv-error-actions .adv-error-btn')].find(el=>el.textContent===label);
  const action=button('去设置'),rect=action.getBoundingClientRect();
  if(!rect.width||!rect.height||!action.contains(document.elementFromPoint(rect.left+rect.width/2,rect.top+rect.height/2)))throw new Error('Settings action is not reachable');
  action.click();await wait();await wait();
  state.selectionSettingsCorrect=selectionKind==='voice'
    ?visible(document.getElementById('sessionModal'))&&visible(document.getElementById('globalModal'))
    :visible(document.getElementById('globalModal'))&&!document.getElementById('gp-models').classList.contains('hidden');
  state.noAutomaticResend=selectionCalls===1;
  if(selectionKind==='voice'){
    const before=await import('/frontend/src/store.js');
    if(before.getActive().settings.voiceProfileId!==1)throw new Error('Missing voice binding was silently replaced');
    const select=document.getElementById('sVoice');
    const available=[...select.options].map(option=>option.value);
    if(!available.includes('2'))throw new Error('Replacement voice absent from editor: '+JSON.stringify({available,settings:state.selectionSettingsCorrect}));
    select.value='2';select.dispatchEvent(new Event('change',{bubbles:true}));
    click('#sSave');await wait();await wait();
    if(visible(document.getElementById('sessionModal')))throw new Error('Saved character panel did not close');
    if(savedBinding!==2||switchedBinding!==2)throw new Error('The replacement voice was not saved and selected: '+JSON.stringify({savedBinding,switchedBinding}));
    const retry=button('重新发送'),r=retry.getBoundingClientRect();
    if(!r.width||!r.height||retry.closest('[inert]')||!retry.contains(document.elementFromPoint(r.left+r.width/2,r.top+r.height/2)))throw new Error('Explicit retry is not reachable');
    retry.click();await wait();await wait();
    if(selectionCalls!==2||selectionBodies[1].voice_profile_id!==2)throw new Error('Retry did not use the explicit replacement');
    if(!document.querySelector('.adv-primary-text').textContent.includes('重新选择后继续聊天'))throw new Error('Recovered reply is absent');
    const {state:store}=await import('/frontend/src/store.js');
    const questions=store.sessions.find(session=>session.id===store.activeId).messages.filter(message=>message.role==='user'&&message.content==='这条问题需要保留');
    state.recoveredWithoutDuplicate=questions.length===1&&!questions[0].failed;
  }
}catch(error){state.error=String(error.stack||error);}
"""


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("kind", ["voice", "provider"])
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_selection_failure_opens_relevant_settings_without_resubmitting(tmp_path, kind, width, height):
    script = NAVIGATION_SCRIPT.replace("await import('/frontend/src/main.js');",
        API.replace("__KIND__", json.dumps(kind)) + "\nawait import('/frontend/src/main.js');")
    script = script.replace("const output=document.createElement('pre');", RECOVERY + "\nconst output=document.createElement('pre');")
    data = render_engine_case(tmp_path, width, height, script, 10000)
    assert "error" not in data, data.get("error")
    assert data["selectionSettingsCorrect"] is True
    assert data["noAutomaticResend"] is True
    if kind == "voice":
        assert data["recoveredWithoutDuplicate"] is True
