"""Check the redesigned conversation controls using an isolated mock session."""

import pytest

from test_engine_status_browser import render_engine_case
from test_settings_navigation_browser import SCRIPT as NAVIGATION_SCRIPT


SCRIPT = NAVIGATION_SCRIPT.split("const state={};")[0] + """
const state={left:0,right:innerWidth,viewport:innerWidth,overflow:0};
try {
  await wait(); await wait();
  // Let home finish fading in, then leave it and open the drawer in one task:
  // a loaded runner can fire many timers before Chrome paints the next frame.
  settle(); click('.home-continue'); click('[data-action="sessions"]'); await wait();
  const drawer=document.querySelector('.adv-drawer');
  const rect=drawer.getBoundingClientRect();
  state.drawerVisible=rect.width>0 && rect.left>=0 && rect.right<=innerWidth;
  state.drawerWidth=rect.width;
  state.labels=[...document.querySelectorAll('.adv-qm-text')].map(el=>el.textContent);
  click('.adv-drawer > button'); await wait();
  state.drawerClosed=!document.querySelector('.adv-drawer');
  click('.adv-qm-btn-config'); await wait();
  state.settingsOpen=!document.getElementById('globalModal').classList.contains('hidden');
  state.overflow=document.documentElement.scrollWidth-innerWidth;
} catch(error) {state.error=String(error.stack||error);}
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(state);parent.document.body.append(output);
"""


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
# Without a flag Chrome follows the OS animation setting, so pin both modes.
@pytest.mark.parametrize("browser_args", [("--force-prefers-no-reduced-motion",), ("--force-prefers-reduced-motion",)],
                         ids=["motion", "reduced-motion"])
def test_conversation_drawer_and_settings_are_reachable(tmp_path, width, height, browser_args):
    state = render_engine_case(tmp_path, width, height, SCRIPT, 7000, browser_args)
    assert "error" not in state, state["error"]  # pytest truncates the whole state in CI logs
    assert state["drawerVisible"] and state["drawerClosed"] and state["settingsOpen"], state
    assert state["overflow"] == 0
    assert {"自动", "快进", "记录", "重播", "隐藏", "设置"}.issubset(state["labels"])
    if width > 620:
        assert state["drawerWidth"] <= 400


HISTORY_SCRIPT = NAVIGATION_SCRIPT.split("const state={};")[0] + """
const state={left:0,right:innerWidth,viewport:innerWidth,overflow:0};
try {
  await wait();await wait();click('.home-continue');await wait();await wait();
  click('.adv-qm-btn-log');await wait();
  const panel=document.querySelector('.adv-log-overlay'),r=panel.getBoundingClientRect();
  state.title=panel.querySelector('.adv-log-title').textContent;
  state.entries=[...panel.querySelectorAll('.adv-log-entry')].map(e=>e.textContent);
  state.textWidths=[...panel.querySelectorAll('.adv-log-text-wrap')].map(e=>e.getBoundingClientRect().width);
  state.panelWidth=r.width;state.panelVisible=r.left>=0 && r.right<=innerWidth && r.top>=0 && r.bottom<=innerHeight;
  click('.adv-log-close-btn');await wait();
  state.closed=!document.querySelector('.adv-log-overlay');
  state.overflow=document.documentElement.scrollWidth-innerWidth;
} catch(error) {state.error=String(error.stack||error);}
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(state);parent.document.body.append(output);
"""


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_history_is_readable_and_can_be_closed(tmp_path, width, height):
    state = render_engine_case(tmp_path, width, height, HISTORY_SCRIPT, 7000)
    assert "error" not in state, state
    assert state["title"] == "对话记录"
    assert len(state["entries"]) == 2
    assert state["panelVisible"] and state["closed"] and state["overflow"] == 0
    if width > 620:
        assert len(state["textWidths"]) == 2 and all(0 < w <= 840 for w in state["textWidths"])


