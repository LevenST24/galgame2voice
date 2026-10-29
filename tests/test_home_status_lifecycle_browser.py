"""A reopened home owns its status check even while an older check is pending."""

import pytest

from test_engine_status_browser import render_engine_case


SCRIPT = """
import {createStage} from '/frontend/src/adv/stage.js';
import {createTitleScreen} from '/frontend/src/adv/title.js';
const stage=createStage(document.getElementById('advRoot'));
const requests=[];
const ui=createTitleScreen(stage.overlay,{actions:{
  listSessions:()=>[],getActiveId:()=>null,
  getStatus:()=>new Promise(resolve=>requests.push(resolve)),
}});
const state={left:0,right:innerWidth,viewport:innerWidth,overflow:0};
const ready={modelLoaded:true,hasModel:true,model:{state:'ok'},engine:{state:'ready'}};
const stale={modelLoaded:true,hasModel:false,model:{state:'error'},engine:{state:'unavailable'}};
try {
  ui.open();ui.close();ui.open();
  if(requests.length!==2)throw new Error('Old request blocked reopening');
  requests[0](stale);await Promise.resolve();await Promise.resolve();
  ui.refresh();
  if(requests.length!==2)throw new Error('Old completion cleared the current request');
  requests[1](ready);await Promise.resolve();await Promise.resolve();
  if(!ui.element.querySelector('.home-pill-text')?.textContent.includes('对话 可用'))
    throw new Error('Current result did not update the reopened home');
  if(ui.element.querySelector('.home-first-title'))throw new Error('Stale result replaced the current home');
  state.ok=true;
}catch(error){state.error=String(error.stack||error);}
finally{ui.destroy();stage.destroy();}
state.overflow=document.documentElement.scrollWidth-innerWidth;
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(state);parent.document.body.append(output);
"""


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_reopened_home_status_has_independent_request(tmp_path, width, height):
    data = render_engine_case(tmp_path, width, height, SCRIPT, 5000)
    assert "error" not in data, data
    assert data["ok"] is True
