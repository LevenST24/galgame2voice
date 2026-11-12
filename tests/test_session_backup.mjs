import assert from 'node:assert/strict';
import { state, loadState, saveState, getStorageStatus, getDamagedSessionData, resumeLocalSaving,
  exportSessionData, prepareSessionImport, applySessionImport,
  DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL } from '../frontend/src/store.js';
import { normalizeSnapshot } from '../frontend/src/session_data.js';
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
    settings: { temperature: 0.75, advPlayerName: '旅人', ...(advReplyFormat === undefined ? {} : { advReplyFormat }) },
    messages: [
      { id: `m_${id}`, role: 'user', content: '台词', ts: 1 },
      { id: `m_legacy_${id}`, role: 'assistant', content: '旧整段中文', japanese: '旧版の日本語。', ts: 2 },
    ] });
  // Retired advReplyFormat ('legacy' | 'old' | 'lines' | bogus) must be silently ignored on read and import.
  storage({ sessions: [session('s_legacy', 'legacy'), session('s_old', 'old'), session('s_lines', 'lines'),
    session('s_invalid', 'bogus'), session('s_missing')], activeId: 's_legacy', global: {} });
  loadState();
  assert.ok(!('advReplyFormat' in state.sessions[0].settings));
  assert.deepEqual(state.sessions.map(s => 'advReplyFormat' in s.settings), [false, false, false, false, false]);
  assert.deepEqual(state.sessions.map(s => s.settings.temperature), [0.75, 0.75, 0.75, 0.75, 0.75]);
  assert.deepEqual(state.sessions.map(s => s.settings.advPlayerName), ['旅人', '旅人', '旅人', '旅人', '旅人']);
  assert.equal(state.sessions[0].messages[1].japanese, '旧版の日本語。');
  assert.equal(state.sessions[0].messages[1].lines, undefined);
  const exported = JSON.parse(exportSessionData());
  assert.equal(exported.activeId, 's_legacy');
  assert.deepEqual(exported.sessions.map(s => 'advReplyFormat' in s.settings), [false, false, false, false, false]);
  const rawLegacyBackup = JSON.stringify({
    ...exported,
    sessions: exported.sessions.map((s, idx) => ({
      ...s,
      settings: { ...s.settings, advReplyFormat: ['legacy', 'old', 'lines', 'bogus', undefined][idx] },
    })),
  });
  const incoming = prepareSessionImport(rawLegacyBackup);
  assert.deepEqual(incoming.sessions.map(s => 'advReplyFormat' in s.settings), [false, false, false, false, false]);
  assert.deepEqual(incoming.sessions.map(s => s.settings.advPlayerName), ['旅人', '旅人', '旅人', '旅人', '旅人']);
  assert.equal(incoming.sessions[0].messages[1].content, '旧整段中文');
  assert.equal(incoming.sessions[0].messages[1].lines, undefined);
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
  const state_ = { loseNextResponse: false, delayDelete: false, releaseDelete: null, delayImport: false, releaseImport: null };
  globalThis.fetch = async (url, options = {}) => {
    const target = String(url);
    if (target === '/api/chat/import' && options.method === 'POST') {
      const sessions = JSON.parse(options.body).sessions;
      if (sessions.some((item) => ids.has(item.id))) {
        return { ok: false, status: 409, json: async () => ({ detail: '导入的会话已存在' }) };
      }
      for (const item of sessions) ids.add(item.id);
      if (state_.delayImport) {
        await new Promise((resolve) => { state_.releaseImport = resolve; });
      }
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
      if (state_.delayDelete) {
        await new Promise((resolve) => { state_.releaseDelete = resolve; });
      }
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
// deleting with delayed server ack must not let concurrent re-import lose the restored session or be deleted later
{
  storage(saved()); loadState();
  const server = importServer();
  const { choose, toastText, clearToasts, imported } = importDom();
  const { deletePersistedSession, persistSession } = await import('../frontend/src/session_api.js');
  const { deleteSession, getSession } = await import('../frontend/src/store.js');
  const text = backupOf([sessionOf('s_race', ['竞态测试对话'])]);
  await choose(text);
  assert.equal(imported(), 1);
  const target = state.sessions[0];
  assert.ok(target.id.startsWith('s_import_'));
  const unrelated = state.sessions.find(s => s.id !== target.id);
  assert.ok(unrelated, '应有无关会话A');

  // 服务器端已接收DELETE并删除了ID，但响应延迟返回
  server.state.delayDelete = true;
  const removal = deletePersistedSession(target.id);
  const lateDelete = removal.then(() => deleteSession(target.id));

  // 此时尝试重新导入同一备份：必须互斥拒绝，不能伪成功写入并随后被迟到回执销毁
  await choose(text);
  const toasts = toastText();
  assert.equal(imported(), 1, '挂起删除期间重导不能提示成功');
  assert.ok(toasts.some(t => t.includes('正在保存或删除') || t.includes('正在删除')), `应提示正在删除或保存：${toasts.join(' | ')}`);

  // 释放旧删除回执，删除流程正常收尾
  server.state.releaseDelete();
  await lateDelete;
  assert.equal(getSession(target.id), null, '正常删除收尾后原对象应已被清除');

  // 删除彻底完成后重导：必须成功，本地仅有1份，且能够正常 persistSession
  clearToasts();
  await choose(text);
  assert.equal(imported(), 2, '删除彻底完成后重导应成功');
  const restored = getSession(target.id);
  assert.ok(restored, '重导后会话应恢复');
  assert.equal(state.sessions.filter(s => s.id === target.id).length, 1, '不应有同ID重复会话');
  await persistSession(restored);
  assert.ok(server.calls.persisted.includes(target.id), '恢复后的会话必须能够保存');
  assert.ok(getSession(unrelated.id), '无关会话A应完好保留');
  passed++;
}

// reverse interleaving: deletion and persistence are rejected while import is in-flight, released after import
{
  storage(saved()); loadState();
  const server = importServer();
  const { choose, clearToasts, imported } = importDom();
  const { deletePersistedSession, persistSession } = await import('../frontend/src/session_api.js');
  const { getSession } = await import('../frontend/src/store.js');
  const text = backupOf([sessionOf('s_rev', ['反向交错测试'])]);

  server.state.delayImport = true;
  const importPromise = choose(text);

  await Promise.resolve();
  const incoming = prepareSessionImport(text);
  const targetId = incoming.sessions[0].id;

  await assert.rejects(
    deletePersistedSession(targetId),
    (err) => err.message === '该对话正在导入中，请稍后重试。',
    '导入未结束前删除应被明确拒绝',
  );

  await assert.rejects(
    persistSession({ id: targetId, title: '尝试提前保存', settings: {} }),
    (err) => err.message === '该对话正在导入中，请稍后重试。',
    '导入未结束前保存应被明确拒绝',
  );

  server.state.releaseImport();
  await importPromise;
  clearToasts();
  assert.equal(imported(), 1);
  const importedSession = getSession(targetId);
  assert.ok(importedSession, '导入成功后会话应存在');

  await persistSession(importedSession);
  assert.ok(server.calls.persisted.includes(targetId), '导入完成后应能正常保存');
  passed++;
}

// failed or timed out import releases reservation without permanently blocking subsequent operations
{
  storage(saved()); loadState();
  const server = importServer();
  const { choose, clearToasts } = importDom();
  const { deletePersistedSession } = await import('../frontend/src/session_api.js');
  const text = backupOf([sessionOf('s_fail_release', ['失败释放测试'])]);
  const incoming = prepareSessionImport(text);
  const targetId = incoming.sessions[0].id;

  server.state.loseNextResponse = true;
  await choose(text);
  clearToasts();

  await deletePersistedSession(targetId);
  assert.ok(server.calls.deleted.includes(targetId), '异常结束后预约应已释放，删除可正常发起');
  passed++;
}

// normalizeSnapshot and loadState/exportSessionData must not pollute global settings with advSubtitleLanguage
{
  // 1. normalizeSnapshot directly:
  // global without advSubtitleLanguage
  const snap1 = normalizeSnapshot(
    { sessions: [], global: { voiceMode: true, autoTranslate: false } },
    DEFAULT_SESSION_SETTINGS,
    DEFAULT_GLOBAL
  );
  assert.equal('advSubtitleLanguage' in snap1.global, false, 'global without advSubtitleLanguage should not have it added');
  assert.equal(snap1.global.advSubtitleLanguage, undefined);

  // global with existing polluted advSubtitleLanguage should be cleaned
  const snap2 = normalizeSnapshot(
    { sessions: [], global: { voiceMode: true, advSubtitleLanguage: 'zh' } },
    DEFAULT_SESSION_SETTINGS,
    DEFAULT_GLOBAL
  );
  assert.equal('advSubtitleLanguage' in snap2.global, false, 'global with polluted advSubtitleLanguage should have it stripped');
  assert.equal(snap2.global.advSubtitleLanguage, undefined);

  // session settings with identical primary and subtitle languages are still resolved
  const snap3 = normalizeSnapshot(
    {
      sessions: [{
        id: 's_conflict_zh',
        title: '冲突zh',
        createdAt: 1,
        updatedAt: 2,
        settings: { advPrimaryLanguage: 'zh', advSubtitleLanguage: 'zh' },
        messages: [],
      }],
      global: { voiceMode: true },
    },
    DEFAULT_SESSION_SETTINGS,
    DEFAULT_GLOBAL
  );
  assert.equal(snap3.sessions[0].settings.advSubtitleLanguage, 'ja', 'session zh/zh conflict should resolve subtitle to ja');

  const snap4 = normalizeSnapshot(
    {
      sessions: [{
        id: 's_conflict_ja',
        title: '冲突ja',
        createdAt: 1,
        updatedAt: 2,
        settings: { advPrimaryLanguage: 'ja', advSubtitleLanguage: 'ja' },
        messages: [],
      }],
      global: { voiceMode: true },
    },
    DEFAULT_SESSION_SETTINGS,
    DEFAULT_GLOBAL
  );
  assert.equal(snap4.sessions[0].settings.advSubtitleLanguage, 'zh', 'session ja/ja conflict should resolve subtitle to zh');

  // 2. Real wiring through storage(), loadState() and exportSessionData()
  storage({
    sessions: [{
      id: 's_wire',
      title: '真实链路',
      createdAt: 1,
      updatedAt: 2,
      settings: { advPrimaryLanguage: 'zh', advSubtitleLanguage: 'zh' },
      messages: [{ id: 'm_wire', role: 'user', content: 'test', ts: 1 }],
    }],
    activeId: 's_wire',
    global: { voiceMode: true, advSubtitleLanguage: 'zh' },
  });
  loadState();

  assert.equal('advSubtitleLanguage' in state.global, false, 'state.global should not contain advSubtitleLanguage');
  assert.equal(state.global.advSubtitleLanguage, undefined);
  assert.equal(state.sessions[0].settings.advSubtitleLanguage, 'ja');

  // Verify localStorage rewritten by saveState in loadState is also clean
  const storedJson = JSON.parse(globalThis.localStorage.getItem(KEY));
  assert.equal('advSubtitleLanguage' in storedJson.global, false, 'stored global in localStorage should not contain advSubtitleLanguage');
  assert.equal(storedJson.global.advSubtitleLanguage, undefined);

  const exported = JSON.parse(exportSessionData());
  assert.equal('advSubtitleLanguage' in exported.global, false, 'exported.global should not contain advSubtitleLanguage');
  assert.equal(exported.global.advSubtitleLanguage, undefined);
  assert.equal(exported.sessions[0].settings.advSubtitleLanguage, 'ja');

  // 3. prepareSessionImport strips polluted advSubtitleLanguage from imported backup
  const importedSnapshot = prepareSessionImport(JSON.stringify({
    format: 'galgame2voice.chat',
    version: 1,
    sessions: [{
      id: 's_polluted_import',
      title: '污染备份导入',
      createdAt: 1,
      updatedAt: 2,
      settings: { advPrimaryLanguage: 'zh', advSubtitleLanguage: 'zh' },
      messages: [{ id: 'm_imp', role: 'user', content: '导入测试', ts: 1 }],
    }],
    global: { voiceMode: true, advSubtitleLanguage: 'zh' },
  }));
  assert.equal('advSubtitleLanguage' in importedSnapshot.global, false, 'prepareSessionImport should strip advSubtitleLanguage from global');
  assert.equal(importedSnapshot.global.advSubtitleLanguage, undefined);
  assert.equal(importedSnapshot.sessions[0].settings.advSubtitleLanguage, 'ja');

  passed++;
}

{
  // Old stored data containing obsolete autoTranslate in global or session settings loads safely and drops it
  storage({
    sessions: [{ id: 's_old', title: '聊天', createdAt: 1, updatedAt: 2,
      settings: { voiceProfileId: 99, temperature: 0.7, autoTranslate: true },
      messages: [{ id: 'm_old', role: 'user', content: 'Original message', ts: 1 }] }],
    activeId: 's_old',
    global: { voiceMode: true, autoTranslate: true },
  });
  loadState();
  assert.equal(state.global.autoTranslate, undefined, 'loadState must drop obsolete autoTranslate from global');
  assert.equal(state.sessions[0].settings.autoTranslate, undefined, 'loadState must drop obsolete autoTranslate from session settings');
  passed++;
}

// saveState evicts inactive session messages from oldest to newest on QuotaExceededError while preserving lastLine, settings, and failed user messages
{
  const makeHistory = (prefix, count, finalZh) => Array.from({ length: count }, (_, idx) => ({
    id: `${prefix}_m_${idx}`,
    role: idx % 2 === 0 ? 'user' : 'assistant',
    content: idx === count - 1 ? finalZh : `${prefix} 历史台词内容 ${idx} ${'文本填充'.repeat(20)}`,
    ts: 100 + idx,
    ...(idx % 2 === 1
      ? {
          lines: [{
            index: 0,
            zh: idx === count - 1 ? finalZh : `${prefix} 中文分句 ${idx}`,
            ja: `${prefix} 日本語セリフ ${idx}`,
            emotion: 'gentle',
            act: {},
            audio: { status: 'ready', chunks: [] },
          }],
        }
      : {}),
  }));

  storage({
    sessions: [
      { id: 's_oldest', title: '最早会话', createdAt: 100, updatedAt: 1000, settings: { voiceProfileId: 11, temperature: 0.5 }, messages: [] },
      { id: 's_failed', title: '含失败提问会话', createdAt: 200, updatedAt: 1500, settings: { voiceProfileId: 12, temperature: 0.6 }, messages: [] },
      { id: 's_newer', title: '较新会话', createdAt: 300, updatedAt: 2000, settings: { voiceProfileId: 13, temperature: 0.9 }, messages: [] },
      { id: 's_latest', title: '当前激活会话', createdAt: 400, updatedAt: 3000, settings: { voiceProfileId: 14, temperature: 0.8 }, messages: [] },
    ],
    activeId: 's_oldest',
    global: {},
  });
  loadState();

  const sOldest = state.sessions.find((s) => s.id === 's_oldest');
  const sFailed = state.sessions.find((s) => s.id === 's_failed');
  const sNewer = state.sessions.find((s) => s.id === 's_newer');
  const sLatest = state.sessions.find((s) => s.id === 's_latest');

  // 1. 激活 s_oldest 并载入历史
  state.activeId = 's_oldest';
  sOldest.messages = makeHistory('oldest', 20, '最早会话的最后一句台词');
  sOldest.updatedAt = 1000;
  assert.equal(saveState(), true);

  // 2. 切换到 s_failed，载入历史并附带未成功发送的 failed 用户提问
  state.activeId = 's_failed';
  sFailed.messages = [
    ...makeHistory('failed', 10, '失败会话的助手回复'),
    { id: 'failed_user_1', role: 'user', content: '这是一条发送失败待重试的用户提问', failed: true, ts: 1500 },
  ];
  sFailed.updatedAt = 1500;
  assert.equal(saveState(), true);

  // 3. 切换到 s_newer 并载入历史
  state.activeId = 's_newer';
  sNewer.messages = makeHistory('newer', 20, '较新会话的最后一句台词');
  sNewer.updatedAt = 2000;
  assert.equal(saveState(), true);

  const sizeWithThree = localStorage.getItem(KEY).length;
  const oldestHistoryBytes = JSON.stringify(sOldest.messages).length;
  // 配额设为：容纳不淘汰 s_oldest 时的 4 个完整会话会超限，但淘汰 s_oldest 后可成功写入
  localStorage.limit = sizeWithThree + Math.floor(oldestHistoryBytes * 0.4);

  // 4. 切换到 s_latest 并载入历史（约等于 s_oldest 的体积），触发配额淘汰
  state.activeId = 's_latest';
  sLatest.messages = makeHistory('latest', 20, '当前会话的最新台词');
  sLatest.updatedAt = 3000;
  assert.equal(saveState(), true, '淘汰最早非激活会话后 saveState 应成功');
  assert.equal(getStorageStatus().ok, true);

  // 验证按从旧到新顺序仅淘汰了 s_oldest，保留了其 lastLine 与 settings
  assert.equal(sOldest.messages.length, 0, '最早的非激活会话应被优先淘汰 messages');
  assert.equal(sOldest.lastLine, '最早会话的最后一句台词', '被淘汰会话应保留最后一条消息摘要到 lastLine');
  assert.equal(sOldest.settings.voiceProfileId, 11);
  assert.equal(sOldest.settings.temperature, 0.5);
  assert.ok(sNewer.messages.length > 0, '空间已够时较新的非激活会话尚未被淘汰');
  assert.ok(sFailed.messages.length > 0, '含 failed 用户消息的会话不应被淘汰');
  assert.equal(sLatest.messages.length, 20, '当前激活会话的消息应完整保留');

  // 5. 当前会话继续增长，再次触发配额限制：应跳过 s_failed（虽 updatedAt=1500 更早）而淘汰 s_newer（updatedAt=2000）
  sLatest.messages.push(...makeHistory('latest_more', 20, '当前会话继续新增的台词'));
  sLatest.updatedAt = 4000;
  assert.equal(saveState(), true, '继续淘汰下一个可淘汰非激活会话后 saveState 应成功');
  assert.equal(getStorageStatus().ok, true);

  assert.equal(sNewer.messages.length, 0, '次旧的可淘汰会话 s_newer 应被淘汰');
  assert.equal(sNewer.lastLine, '较新会话的最后一句台词', 's_newer 被淘汰前应保留其最后一条台词摘要');
  assert.equal(sNewer.settings.voiceProfileId, 13);
  assert.equal(sNewer.settings.temperature, 0.9);
  assert.ok(sFailed.messages.length > 0, '含 failed 用户提问的会话绝不能被淘汰');
  assert.equal(sFailed.messages.at(-1).content, '这是一条发送失败待重试的用户提问');
  assert.equal(sLatest.messages.length, 40);

  const persisted = JSON.parse(localStorage.getItem(KEY));
  assert.equal(persisted.sessions.find((s) => s.id === 's_oldest').messages.length, 0);
  assert.equal(persisted.sessions.find((s) => s.id === 's_newer').messages.length, 0);
  assert.equal(persisted.sessions.find((s) => s.id === 's_failed').messages.length, 11);
  assert.equal(persisted.sessions.find((s) => s.id === 's_latest').messages.length, 40);
  passed++;
}

// saveState rolls back in-memory messages and lastLine when eviction still fails due to QuotaExceededError
{
  storage({
    sessions: [
      {
        id: 's_active',
        title: '活跃会话',
        createdAt: 200,
        updatedAt: 2000,
        settings: { voiceProfileId: 1, temperature: 0.7 },
        messages: [{ id: 'm_act_1', role: 'user', content: '活跃提问', ts: 201 }],
      },
      {
        id: 's_inactive',
        title: '历史会话',
        createdAt: 100,
        updatedAt: 1000,
        settings: { voiceProfileId: 2, temperature: 0.5 },
        messages: [
          { id: 'm_inact_1', role: 'user', content: '历史提问', ts: 101 },
          { id: 'm_inact_2', role: 'assistant', content: '历史回答', ts: 102 },
        ],
      },
    ],
    activeId: 's_active',
    global: {},
  });
  loadState();

  const sActive = state.sessions.find((s) => s.id === 's_active');
  const sInactive = state.sessions.find((s) => s.id === 's_inactive');

  assert.equal(sInactive.messages.length, 2);
  assert.equal(sInactive.lastLine, undefined);

  localStorage.limit = 0;
  sActive.messages.push({ id: 'm_act_2', role: 'user', content: '活跃新提问', ts: 202 });

  assert.equal(saveState(), false, '配额耗尽且淘汰后依然超限时 saveState 应返回 false');
  assert.equal(getStorageStatus().ok, false);
  assert.equal(sInactive.messages.length, 2, '淘汰保存失败后非活跃会话内存中的 messages 必须完整回滚');
  assert.equal(sInactive.lastLine, undefined, '淘汰保存失败后非活跃会话内存中的 lastLine 必须回滚');

  const exported = JSON.parse(exportSessionData());
  const exportedInactive = exported.sessions.find((s) => s.id === 's_inactive');
  assert.equal(exportedInactive.messages.length, 2, '导出聊天备份必须包含完整未被清空的非活跃会话消息');

  localStorage.limit = Infinity;
  assert.equal(saveState(), true, '恢复配额后 saveState 应成功');
  assert.equal(getStorageStatus().ok, true);

  const persisted = JSON.parse(localStorage.getItem(KEY));
  const persistedInactive = persisted.sessions.find((s) => s.id === 's_inactive');
  assert.equal(persistedInactive.messages.length, 2, '持久化数据中非活跃会话 messages 应完整保留');
  assert.equal(persistedInactive.lastLine, undefined, '持久化数据中未被淘汰会话不应残留 lastLine');
  passed++;
}

// saveState rolls back multiple inactive sessions and preserves pre-existing lastLine across consecutive eviction failures
{
  storage({
    sessions: [
      {
        id: 's_active',
        title: '活跃会话',
        createdAt: 300,
        updatedAt: 3000,
        settings: { voiceProfileId: 1, temperature: 0.7 },
        messages: [{ id: 'm_act_1', role: 'user', content: '活跃提问', ts: 301 }],
      },
      {
        id: 's_inactive_old',
        title: '较旧历史会话',
        createdAt: 100,
        updatedAt: 1000,
        lastLine: '已有历史摘要',
        settings: { voiceProfileId: 2, temperature: 0.5 },
        messages: [
          { id: 'm_old_1', role: 'user', content: '旧历史提问', ts: 101 },
          { id: 'm_old_2', role: 'assistant', content: '旧历史回答', ts: 102 },
        ],
      },
      {
        id: 's_inactive_new',
        title: '较新历史会话',
        createdAt: 200,
        updatedAt: 2000,
        settings: { voiceProfileId: 3, temperature: 0.6 },
        messages: [
          { id: 'm_new_1', role: 'user', content: '新历史提问', ts: 201 },
          { id: 'm_new_2', role: 'assistant', content: '新历史回答', ts: 202 },
        ],
      },
    ],
    activeId: 's_active',
    global: {},
  });
  loadState();

  const sInactiveOld = state.sessions.find((s) => s.id === 's_inactive_old');
  const sInactiveNew = state.sessions.find((s) => s.id === 's_inactive_new');

  assert.equal(sInactiveOld.messages.length, 2);
  assert.equal(sInactiveOld.lastLine, '已有历史摘要');
  assert.equal(sInactiveNew.messages.length, 2);
  assert.equal(sInactiveNew.lastLine, undefined);

  localStorage.limit = 0;

  // 连续多次调用 saveState 均应安全回滚，不造成内存状态累积损坏
  for (let i = 0; i < 3; i++) {
    assert.equal(saveState(), false, `第 ${i + 1} 次保存失败时应返回 false`);
    assert.equal(sInactiveOld.messages.length, 2, '较旧历史会话 messages 必须保持完整');
    assert.equal(sInactiveOld.lastLine, '已有历史摘要', '已有的 lastLine 必须原样恢复');
    assert.equal(sInactiveNew.messages.length, 2, '较新历史会话 messages 必须保持完整');
    assert.equal(sInactiveNew.lastLine, undefined, '初始无 lastLine 的会话必须还原为 undefined');
  }

  const exported = JSON.parse(exportSessionData());
  assert.equal(exported.sessions.find((s) => s.id === 's_inactive_old').messages.length, 2);
  assert.equal(exported.sessions.find((s) => s.id === 's_inactive_new').messages.length, 2);

  localStorage.limit = Infinity;
  assert.equal(saveState(), true);
  const persisted = JSON.parse(localStorage.getItem(KEY));
  assert.equal(persisted.sessions.find((s) => s.id === 's_inactive_old').messages.length, 2);
  assert.equal(persisted.sessions.find((s) => s.id === 's_inactive_old').lastLine, '已有历史摘要');
  assert.equal(persisted.sessions.find((s) => s.id === 's_inactive_new').messages.length, 2);
  assert.equal(persisted.sessions.find((s) => s.id === 's_inactive_new').lastLine, undefined);
  passed++;
}

console.log(`Session backup and recovery: ${passed} scenarios passed`);
