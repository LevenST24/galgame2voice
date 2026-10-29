"""A failed default-voice save remains visible and recoverable from the home page."""

import json

import pytest

from galgame2voice.services.voice_state import ActiveVoicePersistenceError
from test_engine_status_browser import render_engine_case
from test_settings_navigation_browser import SCRIPT as NAVIGATION_SCRIPT


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_voice_save_failure_on_home_opens_the_same_settings_center(tmp_path, width, height):
    script = (
        NAVIGATION_SCRIPT.split("const state={};")[0]
        + """
const state={left:0,right:innerWidth,viewport:innerWidth,overflow:0};
try{
  await wait();await wait();
  const voices=await import('/frontend/src/controllers/voice_settings.js');
  const store=await import('/frontend/src/store.js');
  const session=store.getActive(),apiFetch=globalThis.fetch;
  let writes=0,recovered=false;
  globalThis.fetch=async(url,options={})=>{
    if(new URL(url,location.href).pathname==='/api/voice/switch'){
      writes++;
      return recovered?Response.json({status:'switched',profile_id:1}):Response.json({detail:{
        error_code:'VOICE_PROFILE_SAVE_FAILED',message:__MESSAGE__,voice_profile_id:1
      }},{status:500});
    }
    return apiFetch(url,options);
  };
  await voices.ensureSessionVoice(session,{silent:true});await wait();
  if(voices.getActiveProfileId()!==null||session.settings.voiceProfileId!==1)
    throw new Error('Unconfirmed default or session binding changed incorrectly');
  const toast=document.querySelector('#toastRoot .toast');
  if(!toast?.textContent.includes('尚未确认保存'))throw new Error('Home swallowed the failure');
  if(writes!==1)throw new Error('Save failure automatically resent a write');
  click('#toastRoot .toast-action-btn');await wait();await wait();
  const center=document.getElementById('globalModal');
  if(!visible(center)||document.getElementById('gp-session').hidden)
    throw new Error('Recovery opened outside the settings center');
  recovered=true;click('#sSave');await wait();await wait();
  if(writes!==2||voices.getActiveProfileId()!==1||visible(center))
    throw new Error('Explicit retry did not confirm the same voice and close settings');
  if(!document.querySelector('.adv-stage').classList.contains('is-home'))
    throw new Error('Saving unexpectedly left the home page');
  click('.home-gear');await wait();click('#globalModal [data-home]');await wait();
  state.ok=!visible(center)&&!document.querySelector('.adv-stage').closest('[inert]');
  state.overflow=document.documentElement.scrollWidth-innerWidth;
}catch(error){state.error=error.stack;}
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(state);parent.document.body.append(output);
""".replace("__MESSAGE__", json.dumps(str(ActiveVoicePersistenceError(1))))
    )
    data = render_engine_case(tmp_path, width, height, script, 7000)
    assert "error" not in data, data
    assert data["ok"] is True
