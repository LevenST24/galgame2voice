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
    globalThis.localStorage = { getItem() { return null; }, setItem() {} };
    const session = { id: 's', title: 'S', settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: 99 }, messages: [] };
    state.sessions = [session]; state.activeId = session.id;
    const fields = { sVoice: '', sSystem: '', sSave: '', sTemp: '.7', sTopP: '1', sMaxTokens: '1024',
      sFreq: '0', sPres: '0', sCtx: '20', sAiAdaptiveVoice: 'true', sAdvReplyFormat: 'lines',
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
