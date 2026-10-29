"""An unconfigured model must not hide the user's existing conversation."""

import pytest

from test_engine_status_browser import render_engine_case
from test_settings_navigation_browser import SCRIPT as NAVIGATION_SCRIPT


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_existing_conversation_remains_reachable_without_a_model(tmp_path, width, height):
    script = NAVIGATION_SCRIPT.split("const state={};")[0].replace(
        "result={providers:[{id:'custom',name:'测试模型',provider_type:'custom',chat_model:'synthetic',base_url:'https://synthetic.invalid/v1',is_active:true}],presets:[]}",
        "result={providers:[],presets:[]}",
    ) + """
const state={left:0,right:innerWidth,viewport:innerWidth,overflow:0};
try {
  for(let i=0;i<20&&!document.querySelector('.home-existing');i++)await wait();
  if(!document.querySelector('.home-first-title'))throw new Error('Model setup guidance is missing');
  const button=document.querySelector('.home-existing');
  if(!button)throw new Error('Existing conversation button is missing');
  button.scrollIntoView({block:'nearest',inline:'nearest'});
  click('.home-existing');await wait();
  if(document.querySelector('.adv-stage').classList.contains('is-home'))throw new Error('Conversation did not open');
  const {getActive}=await import('/frontend/src/store.js');
  const active=getActive();
  if(active?.id!=='nav'||!active.messages.some(message=>message.content.includes('测试聊天记录')))
    throw new Error('Existing conversation was not preserved');
  state.ok=true;
}catch(error){state.error=String(error.stack||error);}
state.overflow=document.documentElement.scrollWidth-innerWidth;
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(state);parent.document.body.append(output);
"""
    data = render_engine_case(tmp_path, width, height, script, 7000)
    assert "error" not in data, data
    assert data["ok"] is True
