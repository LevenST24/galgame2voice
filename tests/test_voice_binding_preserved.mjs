import assert from 'node:assert/strict';
import { test } from 'node:test';
import { state, loadState, exportSessionData, DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { mergeChatBackup } from '../frontend/src/chat_backup.js';
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

for (const profiles of [[{ id: 1, name: '其他角色' }], []]) {
  test(`deleted binding remains selected until an explicit choice (${profiles.length} available)`, async () => {
    globalThis.document = { getElementById() { return null; }, createElement() { return element(); } };
    const stored = new Map();
    globalThis.localStorage = {
      getItem: key => stored.get(key) ?? null,
      setItem: (key, value) => stored.set(key, value),
      removeItem: key => stored.delete(key),
    };
    loadState();
    const session = { id: 's', title: 'S', settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 99 }, messages: [] };
    state.sessions = [session]; state.activeId = session.id;
    const fields = { sVoice: '', sSystem: '', sSave: '', sTemp: '.7', sTopP: '1', sMaxTokens: '1024',
      sFreq: '0', sPres: '0', sCtx: '20', sAiAdaptiveVoice: 'true',
      sTtsSpeed: '1', sTtsTopK: '15', sTtsTopP: '1', sTtsTemp: '1' };
    const dom = Object.fromEntries(Object.entries(fields).map(([key, value]) => [key, element(value)]));
    dom.sVoice = select();
    const writes = [];
    globalThis.fetch = async (url, options = {}) => {
      if (url === '/api/chat/sessions') writes.push(JSON.parse(options.body));
      return { ok: true, status: 200, json: async () => url === '/api/voice/profiles'
        ? { profiles, active_profile_id: profiles[0]?.id ?? null } : {} };
    };
    initVoiceSettings(dom, {});
    await loadSessionVoiceSelect(session);
    assert.equal(dom.sVoice.value, '99');
    assert.equal(dom.sVoice.children.find(option => String(option.value) === '99').disabled, true);
    dom.sTemp.value = '.8';
    await dom.sSave.click();
    assert.equal(session.settings.voiceProfileId, 99);
    assert.equal(writes.at(-1).voice_profile_id, 99);
    assert.equal(writes.at(-1).settings.temperature, 0.8);
    dom.sVoice.value = '';
    await dom.sVoice.fire('change');
    await dom.sSave.click();
    assert.equal(session.settings.voiceProfileId, null);
    assert.equal(writes.at(-1).voice_profile_id, null);
  });
}

test('delete button follows real voice selection and recomputes after failed delete', async (t) => {
  t.mock.method(console, 'error', () => {});
  globalThis.document = { getElementById: () => null, createElement: () => element() };
  globalThis.window = { confirm: () => true };
  const s = { id: 'd', title: 'D', settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 99 }, messages: [] };
  Object.assign(state, { sessions: [s], activeId: s.id });
  const dom = { sVoice: select(), sVoiceDelete: element(), sSystem: element(), sCustomVoiceBox: element() };
  const list = { profiles: [{ id: 1, name: 'A' }, { id: 2, name: 'B' }], active_profile_id: 1 };
  let failDelete = null;
  globalThis.fetch = (url, opts = {}) => (opts.method === 'DELETE'
    ? new Promise((resolve) => { failDelete = () => resolve({ ok: false, status: 500, json: async () => ({}) }); })
    : Promise.resolve({ ok: true, status: 200, json: async () => (url === '/api/voice/profiles' ? list : {}) }));
  initVoiceSettings(dom, {});
  await loadSessionVoiceSelect(s);
  for (const val of ['99', '', '__custom__', '2']) {
    if (val !== '99') dom.sVoice.value = val;
    if (val !== '99') await dom.sVoice.fire('change');
    assert.equal(dom.sVoiceDelete.disabled, val !== '2');
  }
  for (const next of ['2', '__custom__']) {
    const attempt = dom.sVoiceDelete.click();
    dom.sVoice.value = next;
    await dom.sVoice.fire('change');
    failDelete();
    await attempt;
    assert.equal(dom.sVoiceDelete.disabled, next !== '2');
  }
});

test('creating or deleting a voice profile bumps session updatedAt for complete chat backup', async (t) => {
  t.mock.method(console, 'error', () => {});
  globalThis.document = { getElementById: () => null, createElement: () => element() };
  globalThis.window = { confirm: () => true };
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  const serverUpdatedAt = Date.now() - 10_000;
  const s = {
    id: 's_voice_backup', title: '音色备份', createdAt: 1, updatedAt: serverUpdatedAt,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 2 }, messages: [],
  };
  Object.assign(state, { sessions: [s], activeId: s.id });
  const dom = {
    sVoice: select(), sVoiceDelete: element(), sSystem: element(), sCustomVoiceBox: element(),
    sCvCreate: element(), sCvName: element(), sCvGpt: element(), sCvSovits: element(),
    sCvRef: element(), sCvPromptText: element(), sCvLang: element(),
  };
  let profiles = [{ id: 1, name: 'A' }, { id: 2, name: 'B' }];
  globalThis.fetch = async (url, opts = {}) => {
    const method = opts.method || 'GET';
    if (url === '/api/chat/sessions' && method === 'POST') {
      return { ok: false, status: 500, json: async () => ({ detail: '保存失败' }) };
    }
    if (url === '/api/voice/profiles/2' && method === 'DELETE') {
      profiles = profiles.filter((p) => p.id !== 2);
      return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
    }
    if (url === '/api/voice/profiles' && method === 'POST') {
      profiles.push({ id: 7, name: '新音色' });
      return { ok: true, status: 200, json: async () => ({ id: 7 }) };
    }
    if (url === '/api/voice/profiles') {
      return { ok: true, status: 200, json: async () => ({ profiles, active_profile_id: profiles[0]?.id ?? null }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  initVoiceSettings(dom, {});
  await loadSessionVoiceSelect(s);

  // 1. Delete bound voice while /api/chat/sessions fails -> backup must preserve unbound null.
  await dom.sVoiceDelete.click();
  assert.equal(s.settings.voiceProfileId, null);
  let merged = mergeChatBackup({
    format: 'galgame2voice.chat', version: 1,
    sessions: [{ id: s.id, title: s.title, createdAt: 1, updatedAt: serverUpdatedAt,
      settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 2 }, messages: [] }],
  }, JSON.parse(exportSessionData()));
  assert.equal(JSON.parse(merged.content).sessions[0].settings.voiceProfileId, null);

  // 2. Create and bind a custom voice while /api/chat/sessions fails -> backup must preserve id 7.
  const serverAfterDelete = s.updatedAt;
  dom.sCvName.value = '新音色';
  dom.sCvGpt.value = 'gpt.ckpt';
  dom.sCvSovits.value = 'sovits.pth';
  dom.sCvRef.value = 'ref.wav';
  dom.sCvPromptText.value = '你好世界';
  dom.sCvLang.value = 'zh';
  await dom.sCvCreate.click();
  assert.equal(s.settings.voiceProfileId, 7);
  merged = mergeChatBackup({
    format: 'galgame2voice.chat', version: 1,
    sessions: [{ id: s.id, title: s.title, createdAt: 1, updatedAt: serverAfterDelete,
      settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null }, messages: [] }],
  }, JSON.parse(exportSessionData()));
  assert.equal(JSON.parse(merged.content).sessions[0].settings.voiceProfileId, 7);
});

test('a blank, negative or fractional max length is refused instead of saved as another number', async (t) => {
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
  loadState();
  const session = { id: 'mt', title: 'MT', settings: { ...DEFAULT_SESSION_SETTINGS, maxTokens: 2048 }, messages: [] };
  state.sessions = [session]; state.activeId = session.id;
  const fields = { sVoice: '', sSystem: '', sSave: '', sTemp: '.7', sTopP: '1', sMaxTokens: '1024',
    sFreq: '0', sPres: '0', sCtx: '20', sAiAdaptiveVoice: 'true',
    sTtsSpeed: '1', sTtsTopK: '15', sTtsTopP: '1', sTtsTemp: '1' };
  const dom = Object.fromEntries(Object.entries(fields).map(([key, value]) => [key, element(value)]));
  dom.sVoice = select();
  const writes = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/chat/sessions') writes.push(JSON.parse(options.body));
    return { ok: true, status: 200, json: async () => url === '/api/voice/profiles'
      ? { profiles: [], active_profile_id: null } : {} };
  };
  initVoiceSettings(dom, {});
  await loadSessionVoiceSelect(session);
  for (const raw of ['-5', '0', '', '5', '100.5', '40000']) {
    dom.sMaxTokens.value = raw;
    await dom.sSave.click();
    assert.equal(session.settings.maxTokens, 2048, raw);
    assert.match(toasts.at(-1).msg, /16 到 32768 之间的整数/, raw);
  }
  assert.equal(writes.length, 0);
  dom.sMaxTokens.value = '1000';
  await dom.sSave.click();
  assert.equal(session.settings.maxTokens, 1000);
  assert.equal(writes.at(-1).settings.maxTokens, 1000);
});
