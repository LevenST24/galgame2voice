"""Home must distinguish unreadable provider configuration from an empty list."""

import pytest

from test_engine_status_browser import render_engine_case
from test_settings_navigation_browser import SCRIPT as NAVIGATION_SCRIPT


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_provider_read_failure_and_retry_on_home(tmp_path, width, height):
    original = "else if(path==='/api/providers') result={providers:[{id:'custom',name:'测试模型',provider_type:'custom',chat_model:'synthetic',base_url:'https://synthetic.invalid/v1',is_active:true}],presets:[]};"
    replacement = "else if(path==='/api/providers') {if(!globalThis.retryProviders)throw new TypeError('synthetic service failure'); result={providers:[{id:'custom',name:'测试模型',provider_type:'custom',chat_model:'synthetic',base_url:'https://synthetic.invalid/v1',is_active:true}],presets:[]};}"
    prefix = NAVIGATION_SCRIPT.split("const state={};")[0]
    assert original in prefix
    script = prefix.replace(original, replacement) + """
const state={left:0,right:innerWidth,viewport:innerWidth,overflow:0};
try {
  const pill=()=>document.querySelector('.home-pill-text');
  for(let i=0;i<35&&!pill()?.textContent.includes('状态未确认');i++)await wait();
  if(!pill()?.textContent.includes('状态未确认'))throw new Error('Failed read was not shown as uncertain');
  if(document.querySelector('.home-first-title'))throw new Error('Failed read was treated as missing configuration');
  if(!document.querySelector('.home-quote')?.textContent.includes('这段记录会保留'))throw new Error('History is hidden');
  click('.home-pill');await wait();
  if(!document.querySelector('.home-popover')?.textContent.includes('无法读取'))throw new Error('Failure explanation is missing');
  globalThis.retryProviders=true;
  click('.home-popover .home-link');await wait();
  const settings=await import('/frontend/src/controllers/global_settings.js');
  for(let i=0;i<20&&settings.hasProviderReadError();i++)await wait();
  if(settings.hasProviderReadError())throw new Error('Provider retry did not recover');
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));await wait();
  for(let i=0;i<20&&!pill()?.textContent.includes('对话 可用');i++)await wait();
  if(!pill()?.textContent.includes('对话 可用'))throw new Error('Home status did not recover');
  if(!document.querySelector('.home-quote')?.textContent.includes('这段记录会保留'))throw new Error('History changed during retry');
  state.ok=true;
}catch(error){state.error=String(error.stack||error);}
state.overflow=document.documentElement.scrollWidth-innerWidth;
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(state);parent.document.body.append(output);
"""
    data = render_engine_case(tmp_path, width, height, script, 12000)
    assert "error" not in data, data
    assert data["ok"] is True
