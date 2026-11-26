import assert from 'node:assert/strict';
import { test } from 'node:test';
import { state, DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { initVoiceSettings, loadSessionVoiceSelect } from '../frontend/src/controllers/voice_settings.js';

function element(value = '') {
  const listeners = {};
  return { value, disabled: false, children: [], classList: { toggle() {}, contains() { return false; } },
    style: { setProperty() {} }, addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
    async fire(name) { for (const fn of listeners[name] || []) await fn(); },
    async click() { await this.fire('click'); }, appendChild(child) { this.children.push(child); },
    querySelectorAll() { return []; }, set innerHTML(html) { this.children = []; } };
}

for (const lang of ['zh', 'en', 'ja']) {
  test(`a ${lang} reference clip sets prompt_lang only; the voiced Japanese line stays ja`, async (t) => {
    t.mock.method(console, 'error', () => {});
    globalThis.document = { getElementById: () => null, createElement: () => element() };
    globalThis.window = { confirm: () => true };
    globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
    const s = { id: 's_cv_lang', title: '音色', createdAt: 1, updatedAt: 1,
      settings: { ...DEFAULT_SESSION_SETTINGS }, messages: [] };
    Object.assign(state, { sessions: [s], activeId: s.id });
    const dom = {
      sVoice: element(), sVoiceDelete: element(), sSystem: element(), sCustomVoiceBox: element(),
      sCvCreate: element(), sCvName: element('参考音色'), sCvGpt: element('gpt.ckpt'),
      sCvSovits: element('sovits.pth'), sCvRef: element('ref.wav'), sCvPromptText: element('参考文本'),
      sCvLang: element(lang),
    };
    let posted = null;
    globalThis.fetch = async (url, opts = {}) => {
      if (url === '/api/voice/profiles' && opts.method === 'POST') {
        posted = JSON.parse(opts.body);
        return { ok: true, status: 201, json: async () => ({ id: 9 }) };
      }
      if (url === '/api/voice/profiles') {
        return { ok: true, status: 200, json: async () => ({ profiles: [{ id: 9, name: '参考音色' }], active_profile_id: 9 }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    };
    initVoiceSettings(dom, {});
    await loadSessionVoiceSelect(s);
    await dom.sCvCreate.click();
    assert.equal(posted?.prompt_lang, lang);
    assert.equal(posted?.text_lang, 'ja');
  });
}
