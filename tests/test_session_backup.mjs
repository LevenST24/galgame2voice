import assert from 'node:assert/strict';
import { state, loadState, saveState, getStorageStatus, getDamagedSessionData, resumeLocalSaving,
  exportSessionData, prepareSessionImport, applySessionImport } from '../frontend/src/store.js';
import { initSessionBackup } from '../frontend/src/controllers/session_backup.js';

const KEY = 'gal2voice.chat.v1';
let passed = 0;
class Storage {
  values = new Map();
  limit = Infinity;
  getItem(key) { return this.values.get(key) ?? null; }
  removeItem(key) { this.values.delete(key); }
  setItem(key, value) {
    const bytes = [...this.values].filter(([stored]) => stored !== key).reduce((sum, [, data]) => sum + data.length, 0) + value.length;
    if (bytes > this.limit) { const error = new Error('quota'); error.name = 'QuotaExceededError'; throw error; }
    this.values.set(key, value);
  }
}
const saved = () => ({ sessions: [{ id: 's_old', title: '聊天', createdAt: 1, updatedAt: 2,
  settings: { voiceProfileId: 99, temperature: 0.7, apiKey: 'must-not-export' },
  messages: [{ id: 'm_old', role: 'user', content: 'Original message', ts: 1 }] }], activeId: 's_old', global: {} });
const storage = raw => {
  globalThis.localStorage = new Storage();
  if (raw !== undefined) localStorage.setItem(KEY, typeof raw === 'string' ? raw : JSON.stringify(raw));
  return localStorage;
};
globalThis.document = { getElementById: () => null };

{
  const many = saved();
  many.sessions = Array.from({ length: 1001 }, (_, index) => ({ ...saved().sessions[0], id: `s_${index}` }));
  storage(many); loadState();
  assert.equal(state.sessions.length, 1001);
  assert.equal(getStorageStatus().damaged, false);
  assert.equal(JSON.parse(exportSessionData()).sessions.length, 1001); passed++;
}

{
  storage(saved()); loadState();
  assert.equal(state.sessions[0].messages[0].content, 'Original message');
  assert.equal(getStorageStatus().ok, true); passed++;
}
{
  storage('{broken'); loadState();
  assert.equal(localStorage.getItem(KEY), '{broken');
  assert.equal(saveState(), false);
  assert.equal(getDamagedSessionData(), '{broken');
  assert.equal(getStorageStatus().damaged, true); passed++;
}
{
  storage('{broken'); localStorage.setItem(`${KEY}.backup`, JSON.stringify(saved())); loadState();
  assert.equal(state.sessions[0].id, 's_old');
  assert.equal(localStorage.getItem(KEY), '{broken'); passed++;
}
{
  assert.equal(resumeLocalSaving(), true);
  assert.equal([...localStorage.values].find(([key]) => key.includes('.damaged.'))[1], '{broken');
  assert.equal(JSON.parse(localStorage.getItem(KEY)).sessions[0].id, 's_old'); passed++;
}
{
  storage('{broken'); loadState(); localStorage.limit = 0;
  assert.throws(resumeLocalSaving, /无法另存/);
  assert.equal(localStorage.getItem(KEY), '{broken');
  assert.equal(getStorageStatus().damaged, true); passed++;
}
for (const malformed of [{ sessions: [null] }, { sessions: [{ ...saved().sessions[0], messages: [null] }] },
  { sessions: [saved().sessions[0], saved().sessions[0]] }, null]) {
  storage(JSON.stringify(malformed)); loadState();
  assert.equal(localStorage.getItem(KEY), JSON.stringify(malformed));
  assert.equal(getStorageStatus().damaged, true); passed++;
}
{
  globalThis.localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  loadState();
  assert.equal(state.sessions.length, 1);
  assert.equal(getStorageStatus().ok, false);
  assert.match(getStorageStatus().message, /导出聊天备份/); passed++;
}
{
  storage(saved()); loadState(); const original = localStorage.getItem(KEY);
  localStorage.limit = original.length;
  state.sessions[0].messages.push({ id: 'm_more', role: 'user', content: 'more text'.repeat(100), ts: 1 });
  assert.equal(saveState(), false);
  assert.equal(localStorage.getItem(KEY), original);
  assert.equal(state.sessions[0].messages.length, 2); passed++;
}
{
  storage(saved()); loadState();
  localStorage.setItem(`${KEY}.backup`, 'x'.repeat(200));
  const primary = localStorage.getItem(KEY);
  localStorage.limit = primary.length + 150;
  state.sessions[0].title += ' more';
  assert.equal(saveState(), true);
  assert.equal(JSON.parse(localStorage.getItem(KEY)).sessions[0].title, '聊天 more'); passed++;
}
{
  storage(saved()); loadState();
  const changed = saved(); changed.sessions[0].title = 'Changed by another tab';
  const raw = JSON.stringify(changed); localStorage.setItem(KEY, raw);
  state.sessions[0].title = 'Unsaved work in this tab';
  assert.equal(saveState(), false);
  assert.equal(localStorage.getItem(KEY), raw);
  assert.match(getStorageStatus().message, /另一网页/);
  assert.ok(exportSessionData().includes('Unsaved work in this tab')); passed++;
}
{
  storage(saved()); loadState();
  state.sessions[0].messages[0].audioUrls = ['https://untrusted.example/recording'];
  state.sessions[0].messages[0].apiKey = 'secret-extra';
  const data = exportSessionData();
  assert.ok(!data.includes('must-not-export') && !data.includes('untrusted.example') && !data.includes('secret-extra'));
  assert.equal(JSON.parse(data).sessions[0].messages[0].content, 'Original message'); passed++;
}
{
  const prior = JSON.stringify(state.sessions);
  const incoming = prepareSessionImport(exportSessionData());
  assert.equal(JSON.stringify(state.sessions), prior);
  assert.ok(incoming.sessions[0].id.startsWith('s_import_'));
  assert.notEqual(incoming.sessions[0].id, state.sessions[0].id);
  assert.equal(incoming.sessions[0].settings.voiceProfileId, null);
  const result = applySessionImport(incoming);
  assert.equal(result.count, 1);
  assert.equal(state.sessions.length, 2);
  assert.equal(state.sessions[1].id, 's_old'); passed++;
}
for (const invalid of ['{broken', 'null', '{"format":"other"}', JSON.stringify({ format: 'galgame2voice.chat', version: 2, ...saved() })]) {
  const before = JSON.stringify(state.sessions);
  assert.throws(() => prepareSessionImport(invalid));
  assert.equal(JSON.stringify(state.sessions), before); passed++;
}

function uiFixture() {
  const elements = new Map();
  for (const id of ['gImportChats', 'gImportChatFile', 'gChatStorageStatus']) {
    elements.set(id, { value: '', disabled: false, handlers: {}, addEventListener(event, handler) { this.handlers[event] = handler; } });
  }
  globalThis.document = { getElementById: id => elements.get(id) ?? null };
  let imported = 0;
  initSessionBackup({ onImport: () => imported++ });
  const fileInput = elements.get('gImportChatFile');
  fileInput.files = [{ size: 100, text: async () => exportSessionData() }];
  return { elements, fileInput, imported: () => imported };
}
{
  storage(saved()); loadState();
  const { elements, fileInput, imported } = uiFixture();
  globalThis.fetch = async () => ({ ok: false, status: 422, json: async () => ({ detail: '备份有误，当前记录未修改' }) });
  const before = JSON.stringify(state.sessions);
  await fileInput.handlers.change();
  assert.equal(elements.get('gImportChats').disabled, false);
  assert.equal(imported(), 0);
  assert.equal(JSON.stringify(state.sessions), before); passed++;
}
{
  storage(saved()); loadState();
  const { elements, fileInput, imported } = uiFixture();
  let request;
  globalThis.fetch = async (_url, options) => { request = JSON.parse(options.body); return { ok: true, json: async () => ({ status: 'ok' }) }; };
  await fileInput.handlers.change();
  assert.equal(imported(), 1);
  assert.equal(request.sessions[0].settings.voiceProfileId, null);
  assert.equal(request.sessions[0].messages[0].content, 'Original message');
  assert.equal(elements.get('gImportChats').disabled, false); passed++;
}
console.log(`Session backup and recovery: ${passed} scenarios passed`);
