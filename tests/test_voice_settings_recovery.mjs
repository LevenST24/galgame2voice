import assert from 'node:assert/strict';
import { state, loadState, DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { initVoiceSettings, createCustomVoice, loadSessionVoiceSelect, handleRescanCharacters, populateScanOptions, invalidateScannedModels, ensureSessionVoice } from '../frontend/src/controllers/voice_settings.js';
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
  return { value, disabled: false, textContent: '', children: [], style: { setProperty() {} },
    get innerHTML() { return this.html || ''; },
    set innerHTML(html) { this.html = html; this.children = []; },
    addEventListener: (name, handler) => { (listeners[name] ||= []).push(handler); },
    async fire(name) { for (const handler of listeners[name] || []) await handler(); },
    async click() { if (!this.disabled) await this.fire('click'); },
    appendChild(child) { this.children.push(child); }, querySelectorAll: () => [],
    classList: { contains: () => false, toggle() {} } };
}
function setup(callbacks = {}) {
  loadState();
  const first = { id: 'first', title: 'First', settings: { ...DEFAULT_SESSION_SETTINGS }, messages: [] };
  const second = { id: 'second', title: 'Second', settings: { ...DEFAULT_SESSION_SETTINGS }, messages: [] };
  state.sessions = [first, second]; state.activeId = first.id;
  const dom = Object.fromEntries(Object.entries({ sCvName: 'New voice', sCvGpt: 'voice.ckpt',
    sCvSovits: 'voice.pth', sCvRef: 'ref.wav', sCvPromptText: 'こんにちは', sCvLang: 'ja',
    sCvCreate: '', sVoice: '', sVoiceRescan: '', sSave: '', sReset: '', sSystem: '', sTemp: '.7', sTopP: '1', sMaxTokens: '1024',
    sFreq: '0', sPres: '0', sCtx: '20', sAiAdaptiveVoice: 'true', sTtsSpeed: '1',
    sTtsTopK: '15', sTtsTopP: '1', sTtsTemp: '1' }).map(([name, value]) => [name, element(value)]));
  initVoiceSettings(dom, callbacks);
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
    let closed = 0;
    const { first, dom } = setup({ closeModal() { closed++; } });
    first.settings.voiceProfileId = 1;
    dom.sessionModal = element();
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
  {
    const { first, dom } = setup();
    let rescanCalls = 0;
    globalThis.fetch = async url => {
      if (url === '/api/characters/rescan') {
        rescanCalls++;
        return response({ status: 'ok', discovered_count: 2 });
      }
      if (url.endsWith('/profiles')) {
        return response({ profiles: [{ id: 5, name: 'RescannedChar', system_prompt: 'Hello' }], active_profile_id: 5 });
      }
      return response({});
    };
    await loadSessionVoiceSelect(first);
    await handleRescanCharacters();
    assert.equal(rescanCalls, 1);
    assert.equal(dom.sVoiceRescan.disabled, false);
    assert.equal(dom.sSystem.disabled, true); // first session has no bound voiceProfileId yet
    first.settings.voiceProfileId = 5;
    await loadSessionVoiceSelect(first);
    assert.equal(dom.sSystem.disabled, false);
    assert.equal(dom.sSystem.value, 'Hello');
    passed++;
  }
} finally { globalThis.setTimeout = originalTimeout; }

const profiles = [
  { id: 1, name: 'A', system_prompt: 'A original' },
  { id: 2, name: 'B', system_prompt: 'B original' },
  { id: 3, name: 'C', system_prompt: 'C original' },
];
globalThis.window = { confirm: () => true };
function profileResponses(writes, available = profiles) {
  return async (url, options = {}) => {
    if (options.method === 'PUT') writes.push({ url, body: JSON.parse(options.body) });
    if (url === '/api/voice/profiles') return response({ profiles: available, active_profile_id: 1 });
    return response({ discovered_count: available.length });
  };
}
async function editB(dom) {
  dom.sVoice.value = '2';
  await dom.sVoice.fire('change');
  dom.sSystem.value = 'Only for B';
}
{
  const { first, dom } = setup(); const writes = [];
  first.settings.voiceProfileId = 1;
  globalThis.fetch = profileResponses(writes);
  await loadSessionVoiceSelect(first); await editB(dom);
  await dom.sVoiceRescan.click();
  assert.equal(dom.sVoice.value, '2'); assert.equal(dom.sSystem.value, 'Only for B');
  await dom.sSave.click();
  assert.deepEqual(writes, [{ url: '/api/characters/2/system-prompt', body: { system_prompt: 'Only for B' } }]);
  assert.equal(first.settings.voiceProfileId, 2); passed++;
}
{
  const { first, dom } = setup(); const writes = [];
  first.settings.voiceProfileId = 1;
  globalThis.fetch = profileResponses(writes);
  await loadSessionVoiceSelect(first); await editB(dom);
  dom.sSystem.value = '';
  await handleRescanCharacters();
  assert.equal(dom.sVoice.value, '2'); assert.equal(dom.sSystem.value, '');
  await dom.sSave.click();
  assert.equal(writes.length, 0); assert.equal(dom.sSystem.value, ''); passed++;
}
{
  const { first, dom } = setup(); const writes = [];
  first.settings.voiceProfileId = 1;
  globalThis.fetch = profileResponses(writes);
  await loadSessionVoiceSelect(first); await editB(dom);
  globalThis.fetch = profileResponses(writes, [profiles[0]]);
  await handleRescanCharacters(); await dom.sSave.click();
  assert.equal(dom.sVoice.value, '2'); assert.equal(dom.sSystem.value, 'Only for B');
  assert.equal(writes.length, 0); assert.equal(first.settings.voiceProfileId, 1); passed++;
}
{
  const { first, dom } = setup(); const writes = [];
  first.settings.voiceProfileId = 1;
  globalThis.fetch = profileResponses(writes);
  await loadSessionVoiceSelect(first); await editB(dom);
  globalThis.fetch = async url => url === '/api/characters/rescan'
    ? response({ discovered_count: 3 }) : response({ detail: '暂时无法读取音色列表' }, 503);
  await handleRescanCharacters();
  assert.equal(dom.sVoice.value, '2'); assert.equal(dom.sSystem.value, 'Only for B');
  assert.equal(dom.sVoiceRescan.disabled, false); passed++;
}
{
  const { first, dom } = setup(); const writes = [];
  first.settings.voiceProfileId = 1;
  globalThis.fetch = profileResponses(writes);
  await loadSessionVoiceSelect(first); await editB(dom);
  const respond = globalThis.fetch; let complete;
  globalThis.fetch = (url, options) => url === '/api/characters/rescan'
    ? new Promise(resolve => { complete = resolve; }) : respond(url, options);
  const pending = handleRescanCharacters(); await tick();
  dom.sVoice.value = '3'; await dom.sVoice.fire('change'); dom.sSystem.value = 'Only for C';
  complete(response({ discovered_count: 3 })); await pending;
  assert.equal(dom.sVoice.value, '3'); assert.equal(dom.sSystem.value, 'Only for C');
  await dom.sSave.click();
  assert.equal(writes[0].url, '/api/characters/3/system-prompt'); passed++;
}
{
  const { first, dom } = setup(); const writes = [];
  first.settings.voiceProfileId = 1;
  globalThis.fetch = profileResponses(writes);
  await loadSessionVoiceSelect(first); await editB(dom);
  dom.sVoice.value = '1'; // A programmatic change must not silently write B's draft.
  await dom.sSave.click();
  assert.equal(writes.length, 0); assert.equal(dom.sSystem.value, 'Only for B'); passed++;
}
{
  const { first, dom } = setup(); const writes = [];
  first.settings.voiceProfileId = 1;
  globalThis.fetch = profileResponses(writes);
  await loadSessionVoiceSelect(first);
  let complete;
  globalThis.fetch = () => new Promise(resolve => { complete = resolve; });
  const pending = loadSessionVoiceSelect(first); await tick();
  await editB(dom);
  complete(response({ profiles, active_profile_id: 1 })); await pending;
  assert.equal(dom.sVoice.value, '2'); assert.equal(dom.sSystem.value, 'Only for B'); passed++;
}
{
  const { first, dom } = setup(); const writes = [];
  first.settings.voiceProfileId = 1;
  globalThis.fetch = profileResponses(writes);
  await loadSessionVoiceSelect(first);
  globalThis.fetch = profileResponses(writes, [{ ...profiles[0], system_prompt: 'Updated in the rescanned package' }]);
  await handleRescanCharacters();
  assert.equal(dom.sVoice.value, '1');
  assert.equal(dom.sSystem.value, 'Updated in the rescanned package');
  await dom.sSave.click();
  assert.equal(writes.length, 0); passed++;
}
// A slow save must only acknowledge the submitted draft, not later edits.
for (const edit of [
  dom => { dom.sSystem.value = 'Another unsaved draft'; },
  dom => { dom.sSystem.value = 'Original before save'; },
  dom => { dom.sTemp.value = '.9'; },
  async dom => { dom.sVoice.value = '2'; await dom.sVoice.fire('change'); },
]) {
  let closed = 0;
  const { first, dom } = setup({ closeModal() { closed++; } });
  dom.sessionModal = element();
  first.settings.voiceProfileId = 1;
  const available = [{ ...profiles[0], system_prompt: 'Original before save' }, { ...profiles[1] }];
  globalThis.fetch = profileResponses([], available);
  await loadSessionVoiceSelect(first);
  dom.sSystem.value = 'Submitted draft';
  let complete; const writes = [];
  const respond = profileResponses(writes, available);
  globalThis.fetch = (url, options) => url.endsWith('/system-prompt')
    ? new Promise(resolve => { complete = resolve; writes.push(JSON.parse(options.body)); })
    : respond(url, options);
  const pending = dom.sSave.click(); await tick();
  await edit(dom);
  const remainingPrompt = dom.sSystem.value;
  complete(response({ status: 'ok' })); await pending;
  assert.equal(closed, 0, 'saving an earlier draft must keep the newer editor open');
  assert.equal(dom.sSystem.value, remainingPrompt, 'voice synchronization must preserve later edits');
  assert.equal(writes[0].system_prompt, 'Submitted draft');
  assert.equal(first.settings.temperature, .7);
  assert.equal(dom.sSave.disabled, false); passed++;
}
{
  let closed = 0, complete;
  const { first, dom } = setup({ closeModal() { closed++; } });
  first.settings.voiceProfileId = 1;
  globalThis.fetch = profileResponses([], profiles);
  await loadSessionVoiceSelect(first);
  const respond = globalThis.fetch;
  globalThis.fetch = (url, options) => url === '/api/chat/sessions'
    ? new Promise(resolve => { complete = resolve; }) : respond(url, options);
  const pending = dom.sSave.click(); await tick();
  await openSessionSettings(); await tick(); // Close/reopen while persistence is pending.
  complete(response({})); await pending;
  assert.equal(closed, 0, 'an older save must not close a newly opened settings panel'); passed++;
}
{
  let closed = 0;
  const { first, dom } = setup({ closeModal() { closed++; } });
  first.settings.voiceProfileId = 1;
  globalThis.fetch = profileResponses([], profiles);
  await loadSessionVoiceSelect(first);
  await dom.sSave.click();
  assert.equal(closed, 1, 'unchanged successful saves should still close the panel'); passed++;
}
{
  const { first, dom } = setup();
  first.settings.voiceProfileId = 1;
  globalThis.fetch = profileResponses([], profiles);
  await loadSessionVoiceSelect(first); await editB(dom);
  dom.sTemp.value = '1.5';
  await dom.sReset.click();
  assert.equal(dom.sVoice.value, '2', 'resetting inference parameters must preserve the role');
  assert.equal(dom.sSystem.value, 'Only for B');
  assert.equal(Number(dom.sTemp.value), DEFAULT_SESSION_SETTINGS.temperature); passed++;
}
for (const draft of ['', 'Custom persona draft']) {
  const { first, dom } = setup();
  first.settings.voiceProfileId = 1;
  globalThis.fetch = profileResponses([], profiles);
  await loadSessionVoiceSelect(first);
  if (draft) { dom.sVoice.value = '__custom__'; await dom.sVoice.fire('change'); }
  const previous = dom.sVoice.value;
  dom.sSystem.value = draft;
  let confirmations = 0;
  window.confirm = () => { confirmations++; return false; };
  dom.sVoice.value = draft ? '' : '2'; await dom.sVoice.fire('change');
  assert.equal(confirmations, 1, 'cleared and custom persona drafts also need protection');
  assert.equal(dom.sVoice.value, previous);
  assert.equal(dom.sSystem.value, draft);
  window.confirm = () => true; passed++;
}
{
  let closed = 0; const writes = [];
  const { first, dom } = setup({ closeModal() { closed++; } });
  first.settings.voiceProfileId = 1;
  globalThis.fetch = profileResponses([], profiles);
  await loadSessionVoiceSelect(first);
  dom.sVoice.value = '__custom__'; await dom.sVoice.fire('change');
  globalThis.fetch = async (url, options) => { writes.push([url, options]); return response({}); };
  await dom.sSave.click();
  assert.equal(closed, 0, 'the session save button must not discard an uncreated voice');
  assert.equal(first.settings.voiceProfileId, 1);
  assert.equal(dom.sCvName.value, 'New voice');
  assert.equal(dom.sSave.disabled, false);
  assert.deepEqual(writes, []); passed++;
}
// Simulate actual select behavior: rebuilding options otherwise selects the first file.
function modelSelect() {
  const select = element();
  Object.defineProperty(select, 'innerHTML', { set() { this.children = []; this.value = ''; } });
  select.appendChild = function (option) { this.children.push(option); if (this.children.length === 1) this.value = option.value; };
  return select;
}
const models = {
  gpt_weights: ['a.ckpt', 'b.ckpt'].map(path => ({ path, name: path })),
  sovits_weights: ['a.pth', 'b.pth'].map(path => ({ path, name: path })),
  audio_files: ['a.wav', 'b.wav'].map(path => ({ path, name: path })),
};
{
  const { dom } = setup();
  for (const key of ['sCvGpt', 'sCvSovits', 'sCvRef']) dom[key] = modelSelect();
  invalidateScannedModels();
  globalThis.fetch = async () => response(models);
  await populateScanOptions();
  dom.sCvGpt.value = 'b.ckpt'; dom.sCvSovits.value = 'b.pth'; dom.sCvRef.value = 'b.wav';
  await populateScanOptions();
  assert.deepEqual([dom.sCvGpt.value, dom.sCvSovits.value, dom.sCvRef.value], ['b.ckpt', 'b.pth', 'b.wav']); passed++;

  invalidateScannedModels();
  globalThis.fetch = async () => response({ ...models, gpt_weights: [models.gpt_weights[0]] });
  await populateScanOptions(); await populateScanOptions();
  assert.equal(dom.sCvGpt.value, '', 'a missing selected file must require an explicit replacement');
  assert.equal(dom.sCvSovits.value, 'b.pth'); passed++;

  invalidateScannedModels();
  globalThis.fetch = async () => response({ detail: 'scan unavailable' }, 503);
  await populateScanOptions();
  assert.equal(dom.sCvSovits.value, 'b.pth', 'transient scan failures must preserve existing choices'); passed++;
}
{
  const { dom } = setup();
  dom.sCvGpt = modelSelect(); invalidateScannedModels();
  let complete, requests = 0;
  globalThis.fetch = () => { requests++; return new Promise(resolve => { complete = resolve; }); };
  const first = populateScanOptions(); const second = populateScanOptions();
  assert.equal(requests, 1, 'concurrent callers must share the same scan');
  invalidateScannedModels();
  globalThis.fetch = async () => response(models);
  await populateScanOptions();
  complete(response({ detail: 'older scan failed' }, 503)); await Promise.all([first, second]);
  assert.equal(dom.sCvGpt.value, 'a.ckpt', 'a stale error must not clear newer scan results'); passed++;
}
{
  // The voice list failed to load: saving other settings must keep the character binding.
  const { first, dom } = setup();
  first.settings.voiceProfileId = 1;
  const saves = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/voice/profiles') throw new TypeError('Failed to fetch');
    if (url === '/api/chat/sessions') saves.push(JSON.parse(options.body));
    return response({});
  };
  await loadSessionVoiceSelect(first);
  dom.sTemp.value = '0.9';
  await dom.sSave.click();
  assert.equal(first.settings.voiceProfileId, 1);
  assert.equal(first.settings.temperature, 0.9);
  assert.equal(saves.at(-1).voice_profile_id, 1); passed++;
}
{
  // Deleting a voice also clears the server copies, or the next sync binds it again.
  const { first, second, dom } = setup();
  dom.sVoiceDelete = element();
  initVoiceSettings(dom);
  first.settings.voiceProfileId = 5; second.settings.voiceProfileId = 5;
  const saves = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/voice/profiles') return response({ profiles: [{ id: 5, name: 'Old' }, { id: 6, name: 'Other' }], active_profile_id: 6 });
    if (url === '/api/chat/sessions') saves.push(JSON.parse(options.body));
    return response({});
  };
  await loadSessionVoiceSelect(first);
  dom.sVoice.value = '5';
  await dom.sVoiceDelete.click(); await tick();
  assert.deepEqual(saves.map(s => [s.id, s.voice_profile_id]).sort(), [['first', null], ['second', null]]); passed++;
}
{
  // A binding found dead (404) is cleared on the server too.
  const { first } = setup();
  first.settings.voiceProfileId = 9;
  const saves = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/voice/profiles') return response({ profiles: [{ id: 1, name: 'A' }], active_profile_id: 1 });
    if (url === '/api/voice/switch') return response({ detail: 'Profile not found' }, 404);
    if (url === '/api/chat/sessions') saves.push(JSON.parse(options.body));
    return response({});
  };
  await ensureSessionVoice(first, { silent: true }); await tick();
  assert.equal(first.settings.voiceProfileId, null);
  assert.deepEqual(saves.map(s => [s.id, s.voice_profile_id]), [['first', null]]); passed++;
}
{
  // A character created without a persona still saves its session settings cleanly.
  let closed = 0;
  const { first, dom } = setup({ closeModal() { closed++; } });
  first.settings.voiceProfileId = 4;
  dom.sessionModal = element();
  globalThis.fetch = async url => url === '/api/voice/profiles'
    ? response({ profiles: [{ id: 4, name: 'No persona', system_prompt: '' }], active_profile_id: 4 }) : response({});
  await loadSessionVoiceSelect(first);
  dom.sTopP.value = '0.8';
  await dom.sSave.click();
  assert.equal(first.settings.topP, 0.8);
  assert.equal(closed, 1); passed++;
}
console.log(`Voice settings recovery: ${passed} scenarios passed`);
