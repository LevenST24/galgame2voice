import assert from 'node:assert/strict';
import { state, loadState, DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { initVoiceSettings, createCustomVoice, loadSessionVoiceSelect } from '../frontend/src/controllers/voice_settings.js';
import { persistSession } from '../frontend/src/session_api.js';
import { openSessionSettings } from '../frontend/src/controllers/voice_settings.js';
import { portraitStage } from '../frontend/src/portrait.js';

const storage = new Map();
globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
globalThis.document = { getElementById: () => null, createElement: () => element() };
portraitStage.setCharacter = async () => {};
portraitStage.initDefaultCharacter = async () => {};
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
function element(value = '') {
  const listeners = {};
  return { value, disabled: false, textContent: '', innerHTML: '', children: [], style: { setProperty() {} },
    addEventListener: (name, handler) => { listeners[name] = handler; },
    click: () => listeners.click?.(), appendChild(child) { this.children.push(child); }, querySelectorAll: () => [],
    classList: { contains: () => false, toggle() {} } };
}
function setup() {
  loadState();
  const first = { id: 'first', title: 'First', settings: { ...DEFAULT_SESSION_SETTINGS }, messages: [] };
  const second = { id: 'second', title: 'Second', settings: { ...DEFAULT_SESSION_SETTINGS }, messages: [] };
  state.sessions = [first, second]; state.activeId = first.id;
  const dom = Object.fromEntries(Object.entries({ sCvName: 'New voice', sCvGpt: 'voice.ckpt',
    sCvSovits: 'voice.pth', sCvRef: 'ref.wav', sCvPromptText: 'こんにちは', sCvLang: 'ja',
    sCvCreate: '', sVoice: '', sSave: '', sSystem: '', sTemp: '.7', sTopP: '1', sMaxTokens: '1024',
    sFreq: '0', sPres: '0', sCtx: '20', sAiAdaptiveVoice: 'true', sTtsSpeed: '1',
    sTtsTopK: '15', sTtsTopP: '1', sTtsTemp: '1' }).map(([name, value]) => [name, element(value)]));
  initVoiceSettings(dom);
  return { first, second, dom };
}
let passed = 0;
{
  const { first, second, dom } = setup();
  let complete; const saves = [];
  globalThis.fetch = async (url, options) => {
    if (url === '/api/voice/profiles' && options.method === 'POST') return new Promise(resolve => { complete = resolve; });
    if (url === '/api/chat/sessions') saves.push(JSON.parse(options.body));
    return response({});
  };
  const pending = createCustomVoice(); await tick();
  state.activeId = second.id;
  complete(response({ id: 42, name: 'New voice' })); await pending;
  assert.equal(first.settings.voiceProfileId, 42);
  assert.equal(second.settings.voiceProfileId, null);
  assert.equal(saves[0].id, first.id);
  assert.equal(dom.sCvCreate.disabled, false); passed++;
}
{
  const { first, second } = setup(); let complete; const writes = [];
  globalThis.fetch = async (url, options) => {
    writes.push(url);
    return new Promise(resolve => { complete = resolve; });
  };
  const pending = createCustomVoice(); await tick();
  state.sessions = [second]; state.activeId = second.id;
  complete(response({ id: 43, name: 'New voice' })); await pending;
  assert.equal(first.settings.voiceProfileId, null);
  assert.equal(second.settings.voiceProfileId, null);
  assert.deepEqual(writes, ['/api/voice/profiles']); passed++;
}
{
  const { first, second, dom } = setup(); let complete;
  globalThis.fetch = () => new Promise(resolve => { complete = resolve; });
  const pending = loadSessionVoiceSelect(first);
  state.activeId = second.id; dom.sVoice.value = 'second-selection';
  complete(response({ profiles: [{ id: 1, name: 'Old voice' }], active_profile_id: 1 })); await pending;
  assert.equal(dom.sVoice.value, 'second-selection');
  assert.equal(dom.sVoice.children.length, 0); passed++;
}
{
  const { first, dom } = setup();
  globalThis.fetch = async () => response({ profiles: [{ id: 1, name: 'Active voice' }], active_profile_id: 1 });
  await loadSessionVoiceSelect(first);
  assert.equal(dom.sVoice.children.find(item => item.value === 1).disabled, false); passed++;
}
{
  const { first } = setup(); let body;
  globalThis.fetch = async (_url, options) => { body = options.body; return response({}); };
  const pending = persistSession(first); first.settings.voiceProfileId = 99; await pending;
  assert.equal(JSON.parse(body).voice_profile_id, null);
  assert.equal(JSON.parse(body).settings.voiceProfileId, null); passed++;
}
// Speed up request deadlines only, exercising the actual AbortController/finally paths.
const originalTimeout = globalThis.setTimeout;
globalThis.setTimeout = (callback, ms, ...args) => originalTimeout(callback, ms >= 15000 ? 5 : ms, ...args);
try {
  {
    const { dom } = setup(); let count = 0, signal;
    globalThis.fetch = async (_url, options) => { count++; signal = options.signal; return new Promise(() => {}); };
    const pending = createCustomVoice(); await tick();
    await createCustomVoice(); await pending;
    assert.equal(count, 1); assert.equal(signal.aborted, true);
    assert.equal(dom.sCvCreate.disabled, false); passed++;
  }
  {
    const { dom } = setup(); let signal;
    globalThis.fetch = async (_url, options) => { signal = options.signal; return new Promise(() => {}); };
    await dom.sSave.click();
    assert.equal(signal.aborted, true); assert.equal(dom.sSave.disabled, false); passed++;
  }
  {
    const { first, dom } = setup(); let closed = 0;
    first.settings.voiceProfileId = 1;
    dom.sessionModal = element();
    initVoiceSettings(dom, { closeModal() { closed++; } });
    globalThis.fetch = async url => url.endsWith('/profiles')
      ? response({ profiles: [{ id: 1, name: 'Voice', system_prompt: 'Original prompt' }], active_profile_id: 1 })
      : response({});
    await loadSessionVoiceSelect(first);
    await openSessionSettings();
    dom.sSystem.value = 'Unsaved edited prompt';
    globalThis.fetch = async url => url.endsWith('/system-prompt')
      ? response({ detail: '角色包目录不可写，请检查权限' }, 400) : response({});
    await dom.sSave.click();
    assert.equal(closed, 0);
    assert.equal(dom.sSystem.value, 'Unsaved edited prompt');
    assert.equal(dom.sSave.disabled, false); passed++;
  }
} finally { globalThis.setTimeout = originalTimeout; }
console.log(`Voice settings recovery: ${passed} scenarios passed`);
