import assert from 'node:assert/strict';
import { state, loadState, saveState, getStorageStatus, getDamagedSessionData, resumeLocalSaving,
  exportSessionData, prepareSessionImport, applySessionImport } from '../frontend/src/store.js';
import { initSessionBackup } from '../frontend/src/controllers/session_backup.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

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
{
  const session = (id, advReplyFormat) => ({ id, title: id, createdAt: 1, updatedAt: 2,
    settings: { temperature: 0.7, ...(advReplyFormat === undefined ? {} : { advReplyFormat }) },
    messages: [{ id: `m_${id}`, role: 'user', content: '台词', ts: 1 }] });
  // 'old' was the stored default before per-line replies became the default; only 'legacy' opts out.
  storage({ sessions: [session('s_legacy', 'legacy'), session('s_old', 'old'), session('s_invalid', 'bogus'),
    session('s_missing')], activeId: 's_legacy', global: {} });
  loadState();
  const expected = ['legacy', 'lines', 'lines', 'lines'];
  assert.deepEqual(state.sessions.map(s => s.settings.advReplyFormat), expected);
  state.activeId = 's_invalid';
  assert.equal(state.sessions.find(s => s.id === 's_invalid').settings.advReplyFormat, 'lines');
  assert.equal(state.sessions.find(s => s.id === 's_legacy').settings.advReplyFormat, 'legacy');
  state.activeId = 's_legacy';
  const exported = JSON.parse(exportSessionData());
  assert.equal(exported.activeId, 's_legacy');
  assert.deepEqual(exported.sessions.map(s => s.settings.advReplyFormat), expected);
  state.activeId = 's_invalid';
  const incoming = prepareSessionImport(JSON.stringify(exported));
  assert.deepEqual(incoming.sessions.map(s => s.settings.advReplyFormat), expected);
  assert.notEqual(incoming.sessions[0].id, 's_legacy'); passed++;
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
{
  const data = saved();
  const lines = [{ index: 0, zh: '你好', ja: 'こんにちは', emotion: 'happy', act: { face: 'smile' }, audio: { status: 'skipped', chunks: [] } }];
  data.sessions[0].messages.push({ id: 'm_reply', role: 'assistant', content: '你好', japanese: 'こんにちは', ts: 2, lines });
  storage(data); loadState();
  const { fileInput } = uiFixture();
  let request;
  globalThis.fetch = async (_url, options) => { request = JSON.parse(options.body); return { ok: true, json: async () => ({ status: 'ok' }) }; };
  await fileInput.handlers.change();
  assert.deepEqual(request.sessions[0].messages[1].lines, lines); passed++;
}

const retryBackup = JSON.stringify({
  format: 'galgame2voice.chat', version: 1, activeId: 's_old', global: {},
  sessions: [{
    id: 's_old', title: '要恢复的对话', createdAt: 1, updatedAt: 2,
    settings: { advPlayerName: '小明' },
    messages: [{ id: 'm1', role: 'user', content: '还在吗', ts: 1 }],
  }],
});

function importDom() {
  const doc = fakeDocument();
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  const input = doc.createElement('input');
  input.id = 'gImportChatFile';
  const button = doc.createElement('button');
  button.id = 'gImportChats';
  doc.body.append(toasts, input, button);
  globalThis.document = doc;
  let imported = 0;
  initSessionBackup({ onImport: () => imported++, isBusy: () => false });
  const choose = async (text = retryBackup) => {
    input.files = [{ size: text.length, text: async () => text }];
    const listeners = [...(input.listeners.get('change') || [])];
    assert.equal(listeners.length, 1);
    await Promise.all(listeners.map((listener) => listener({ type: 'change' })));
  };
  const toastText = () => toasts.querySelectorAll('.toast').map((toast) => toast.textContent);
  const clearToasts = () => {
    for (const toast of toasts.querySelectorAll('.toast')) clearTimeout(toast._timer);
  };
  return { choose, toastText, clearToasts, imported: () => imported };
}

// retrying an import after a lost response reuses the session id
{
  storage(saved()); loadState();
  const before = state.sessions.map((session) => session.id);
  const { choose, toastText, clearToasts, imported } = importDom();
  let imports = 0;
  const posted = [];
  globalThis.fetch = async (url, options = {}) => {
    if (String(url) === '/api/chat/import' && options.method === 'POST') {
      imports += 1;
      posted.push(JSON.parse(options.body).sessions.map((item) => item.id));
      if (imports === 1) {
        const error = new Error('等待服务响应超时。请打开「运行状态」检查；保存结果可能尚未确认，请刷新配置后再决定是否重试。');
        error.code = 'REQUEST_TIMEOUT';
        throw error;
      }
      return { ok: true, status: 200, json: async () => ({ status: 'ok', count: 1 }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  await choose();
  await choose();
  const toasts = toastText();
  clearToasts();
  assert.equal(imports, 2);
  assert.equal(posted[0][0], posted[1][0], '同一次备份在响应丢失后重试，应再使用已经提交过的会话编号，而不是再插入一份');
  assert.match(posted[0][0], /^s_import_[a-zA-Z0-9_]+$/);
  assert.ok(posted[0][0].length <= 128);
  assert.ok(toasts.some((message) => message === '导入结果未确认，请刷新页面查看会话是否已经导入；再次导入同一个文件不会重复写入。'));
  assert.equal(imported(), 1);
  assert.deepEqual(state.sessions.map((session) => session.id).slice(1), before);
  passed++;
}

// a 409 from the server tells the user the session already exists and nothing was written
{
  storage(saved()); loadState();
  const before = JSON.stringify(state.sessions);
  const { choose, toastText, clearToasts, imported } = importDom();
  globalThis.fetch = async (url, options = {}) => {
    if (String(url) === '/api/chat/import' && options.method === 'POST') {
      return {
        ok: false, status: 409,
        json: async () => ({ detail: '导入的会话已存在，原记录未修改。请刷新查看后再决定是否导入另一份副本。' }),
      };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  await choose();
  const toasts = toastText();
  clearToasts();
  assert.equal(imported(), 0);
  assert.equal(JSON.stringify(state.sessions), before);
  assert.ok(toasts.some((message) => message.includes('已存在') && message.includes('没有写入')));
  passed++;
}
// A fake server that only replaces the network: the real route rule is "any existing id rolls back the whole batch".
function importServer() {
  const ids = new Set();
  const calls = { persisted: [], deleted: [] };
  const state_ = { loseNextResponse: false };
  globalThis.fetch = async (url, options = {}) => {
    const target = String(url);
    if (target === '/api/chat/import' && options.method === 'POST') {
      const sessions = JSON.parse(options.body).sessions;
      if (sessions.some((item) => ids.has(item.id))) {
        return { ok: false, status: 409, json: async () => ({ detail: '导入的会话已存在' }) };
      }
      for (const item of sessions) ids.add(item.id);
      if (state_.loseNextResponse) {
        state_.loseNextResponse = false;
        const error = new Error('等待服务响应超时。');
        error.code = 'REQUEST_TIMEOUT';
        throw error;
      }
      return { ok: true, status: 200, json: async () => ({ status: 'ok', count: sessions.length }) };
    }
    if (target === '/api/chat/sessions' && options.method === 'POST') {
      calls.persisted.push(JSON.parse(options.body).id);
      return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
    }
    if (target.startsWith('/api/chat/sessions/') && options.method === 'DELETE') {
      const id = decodeURIComponent(target.slice('/api/chat/sessions/'.length));
      ids.delete(id);
      calls.deleted.push(id);
      return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  return { ids, calls, state: state_ };
}

const backupOf = (sessions) => JSON.stringify({ format: 'galgame2voice.chat', version: 1, global: {}, sessions });
const sessionOf = (id, texts) => ({
  id, title: id, createdAt: 1, updatedAt: 2, settings: {},
  messages: texts.map((content, index) => ({ id: `${id}_m${index}`, role: 'user', content, ts: index + 1 })),
});

// importing a newer backup where only one session grew is not blocked by the older import
{
  storage(saved()); loadState();
  const server = importServer();
  const { choose, toastText, clearToasts, imported } = importDom();
  await choose(backupOf([sessionOf('s_a', ['甲']), sessionOf('s_b', ['乙'])]));
  await choose(backupOf([sessionOf('s_a', ['甲']), sessionOf('s_b', ['乙', '乙又说了一句'])]));
  const toasts = toastText();
  clearToasts();
  assert.equal(imported(), 2, `较新的备份应能导入；提示：${toasts.join(' | ')}`);
  assert.equal(server.ids.size, 4);
  assert.ok(state.sessions.some((session) => session.messages.some((message) => message.content === '乙又说了一句')));
  passed++;
}

// the same backup file lacking timestamps, imported again after a lost response, adds nothing twice
{
  storage(saved()); loadState();
  const server = importServer();
  const { choose, clearToasts, imported } = importDom();
  const noTimes = backupOf([{ id: 's_t', title: '没有时间的对话', settings: {}, messages: [{ id: 'm_t', role: 'user', content: '你好' }] }]);
  const realNow = Date.now;
  let clock = 1_000_000;
  Date.now = () => (clock += 1000);
  try {
    server.state.loseNextResponse = true;
    await choose(noTimes);
    await choose(noTimes);
  } finally { Date.now = realNow; }
  clearToasts();
  assert.equal(server.ids.size, 1, '同一个文件重试后，服务端只应有一份会话');
  assert.equal(imported(), 0);
  assert.equal(state.sessions.filter((session) => session.title === '没有时间的对话').length, 0);
  passed++;
}

// deleting an imported session and importing the same file again must leave it saveable
{
  storage(saved()); loadState();
  const server = importServer();
  const { choose, clearToasts, imported } = importDom();
  const { deletePersistedSession, persistSession } = await import('../frontend/src/session_api.js');
  const { deleteSession, getSession } = await import('../frontend/src/store.js');
  const text = backupOf([sessionOf('s_d', ['要删的对话'])]);
  await choose(text);
  const first = state.sessions[0];
  assert.ok(first.id.startsWith('s_import_'));
  await deletePersistedSession(first.id);
  deleteSession(first.id);
  await choose(text);
  clearToasts();
  assert.equal(imported(), 2);
  const restored = getSession(first.id);
  assert.ok(restored, '再次导入后应恢复这个会话');
  await persistSession(restored);
  assert.deepEqual(server.calls.persisted, [first.id]);
  passed++;
}
console.log(`Session backup and recovery: ${passed} scenarios passed`);
