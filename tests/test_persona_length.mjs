import assert from 'node:assert/strict';
import { test } from 'node:test';
import { state, loadState, DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { initVoiceSettings, loadSessionVoiceSelect } from '../frontend/src/controllers/voice_settings.js';

function element(value = '') {
  const listeners = {};
  return { value, disabled: false, children: [], classList: { toggle() {}, contains() { return false; } },
    style: { setProperty() {} }, addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
    async fire(name) { for (const fn of listeners[name] || []) await fn(); },
    async click() { await this.fire('click'); }, appendChild(child) { this.children.push(child); },
    querySelectorAll() { return []; }, set innerHTML(html) { this.children = []; } };
}

function select() {
  const field = element(); let value = '';
  Object.defineProperty(field, 'innerHTML', { set(html) {
    this.children = html.includes('option value=""') ? [{ value: '' }] : [];
    value = '';
  } });
  Object.defineProperty(field, 'value', { get() { return value; }, set(next) {
    value = this.children.some(child => String(child.value) === String(next)) ? String(next) : '';
  } });
  return field;
}

function toastHarness(t) {
  const toasts = [];
  const shown = [];
  t.after(() => { for (const el of shown) clearTimeout(el._timer); });
  const toastRoot = { querySelectorAll: () => [], appendChild(el) {
    shown.push(el); toasts.push({ msg: el.dataset.msg, type: el.className });
  } };
  const node = () => ({ ...element(), dataset: {}, remove() {}, classList: { add() {}, remove() {}, contains() { return false; } } });
  globalThis.document = { getElementById: id => (id === 'toastRoot' ? toastRoot : null), createElement: node };
  const stored = new Map();
  globalThis.localStorage = {
    getItem: key => stored.get(key) ?? null,
    setItem: (key, value) => stored.set(key, value),
    removeItem: key => stored.delete(key),
  };
  return toasts;
}

function sessionDom() {
  const fields = { sVoice: '', sSystem: '', sSave: '', sTemp: '.7', sTopP: '1', sMaxTokens: '1024',
    sFreq: '0', sPres: '0', sCtx: '20', sAiAdaptiveVoice: 'true',
    sTtsSpeed: '1', sTtsTopK: '15', sTtsTopP: '1', sTtsTemp: '1' };
  const dom = Object.fromEntries(Object.entries(fields).map(([key, value]) => [key, element(value)]));
  dom.sVoice = select();
  return dom;
}

test('an over-long persona is refused before saving, and a failed persona write is not called saved', async (t) => {
  const toasts = toastHarness(t);
  loadState();
  const session = { id: 'p', title: 'P', settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 3 }, messages: [] };
  state.sessions = [session]; state.activeId = session.id;
  const dom = sessionDom();
  const calls = [];
  let personaStatus = 200;
  globalThis.fetch = async (url, options = {}) => {
    calls.push(`${options.method || 'GET'} ${url}`);
    if (url === '/api/characters/3/system-prompt') {
      return { ok: personaStatus === 200, status: personaStatus,
        json: async () => (personaStatus === 200 ? {} : { detail: '角色包是只读的' }) };
    }
    return { ok: true, status: 200, json: async () => url === '/api/voice/profiles'
      ? { profiles: [{ id: 3, name: '角色', system_prompt: '旧人设' }], active_profile_id: 3 } : {} };
  };
  initVoiceSettings(dom, {});
  await loadSessionVoiceSelect(session);
  dom.sSystem.value = '长'.repeat(20001);
  await dom.sSave.click();
  assert.equal(toasts.at(-1).msg, '「角色人设」最多 20000 字，现在有 20001 字，请删减后再保存。设置未保存。');
  assert.ok(!calls.some(call => call.startsWith('PUT') || call.startsWith('POST')));

  personaStatus = 500;
  dom.sSystem.value = '新人设';
  await dom.sSave.click();
  assert.equal(toasts.at(-1).type, 'toast toast-error');
  assert.match(toasts.at(-1).msg, /^人设没有保存：/);
  assert.match(toasts.at(-1).msg, /其他会话设置已保存$/);
  assert.doesNotMatch(toasts.at(-1).msg, /设置已保存。；/);
  dom.sSystem.value = '';
  await dom.sSave.click();
  assert.equal(toasts.at(-1).type, 'toast toast-error');
  assert.equal(toasts.at(-1).msg, '人设没有保存：人设提示词不能为空。其他会话设置已保存');
});

test('creating a voice with an over-long reference text is refused before the request', async (t) => {
  const toasts = toastHarness(t);
  const s = { id: 'c', title: 'C', settings: { ...DEFAULT_SESSION_SETTINGS }, messages: [] };
  Object.assign(state, { sessions: [s], activeId: s.id });
  const dom = {
    sVoice: select(), sVoiceDelete: element(), sSystem: element(), sCustomVoiceBox: element(),
    sCvCreate: element(), sCvName: element('新音色'), sCvGpt: element('a.ckpt'), sCvSovits: element('a.pth'),
    sCvRef: element('ref.wav'), sCvPromptText: element('あ'.repeat(501)), sCvLang: element('ja'),
  };
  const posts = [];
  globalThis.fetch = async (url, options = {}) => {
    if (options.method === 'POST') posts.push(url);
    return { ok: true, status: 200, json: async () => ({ profiles: [], active_profile_id: null }) };
  };
  initVoiceSettings(dom, {});
  await dom.sCvCreate.click();
  assert.equal(toasts.at(-1).msg, '「参考音频里说的话」最多 500 字，现在有 501 字，请删减后再保存。');
  assert.deepEqual(posts, []);
});

test('unchanged over-long persona saves other settings, and failed saves do not claim saved', async (t) => {
  const toasts = toastHarness(t);
  loadState();
  const session = { id: 'u', title: 'U', settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 4 }, messages: [] };
  state.sessions = [session]; state.activeId = session.id;
  const dom = sessionDom();
  const calls = [];
  let mode = 'ok';
  globalThis.fetch = async (url, options = {}) => {
    calls.push(`${options.method || 'GET'} ${url}`);
    if (mode === 'offline') throw new TypeError('Failed to fetch');
    if (url === '/api/characters/4/system-prompt') return { ok: false, status: 500, json: async () => ({ detail: '角色包是只读的' }) };
    return { ok: true, status: 200, json: async () => url === '/api/voice/profiles'
      ? { profiles: [{ id: 4, name: '角色', system_prompt: '原'.repeat(20001) }], active_profile_id: 4 } : {} };
  };
  initVoiceSettings(dom, {});
  await loadSessionVoiceSelect(session);
  dom.sTemp.value = '0.9';
  await dom.sSave.click();
  assert.equal(session.settings.temperature, 0.9);
  assert.equal(toasts.at(-1).type, 'toast toast-success');
  assert.ok(calls.includes('POST /api/chat/sessions') && !calls.includes('PUT /api/characters/4/system-prompt'));
  mode = 'offline';
  dom.sSystem.value = '新人设';
  await dom.sSave.click();
  assert.equal(toasts.at(-1).type, 'toast toast-error');
  assert.doesNotMatch(toasts.at(-1).msg, /其他会话设置已保存/);
  assert.equal(toasts.at(-1).msg.split('无法连接本机服务').length - 1, 1);
  mode = 'ok';
  globalThis.localStorage.setItem('gal2voice.chat.v1', '{"otherTab":true}');
  await dom.sSave.click();
  assert.match(toasts.at(-1).msg, /其他会话设置也未保存/);
  assert.doesNotMatch(toasts.at(-1).msg, /其他会话设置已保存/);
});
