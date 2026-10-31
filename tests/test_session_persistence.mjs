import assert from 'node:assert/strict';
import { persistSession, deletePersistedSession, isSessionDeleting, fetchAllSessions, fetchSessionHistoryPage, earliestServerMessageId } from '../frontend/src/session_api.js';
import { mergeChatBackup, exportCompleteChatBackup } from '../frontend/src/chat_backup.js';
import { pickSettings, serverTimestamp } from '../frontend/src/session_data.js';
import { streamChat } from '../frontend/src/ai.js';
import { GREETING, LEGACY_GREETINGS, loadState } from '../frontend/src/store.js';

const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const response = data => ({ ok: true, json: async () => data });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
let passed = 0;

{
  const defaults = { advPlayerName: '我' };
  assert.equal(pickSettings({ advPlayerName: 'Player' }, defaults).advPlayerName, '我');
  assert.equal(pickSettings({ advPlayerName: '小明' }, defaults).advPlayerName, '小明');
  assert.equal(pickSettings({}, defaults).advPlayerName, '我'); passed++;
}

{
  const expected = Date.UTC(2024, 2, 2, 1, 2, 3, 456);
  assert.equal(serverTimestamp('2024-03-02 01:02:03.456'), expected);
  assert.equal(serverTimestamp('2024-03-02T09:02:03.456+08:00'), expected);
  assert.equal(serverTimestamp('2024-03-02T01:02:03.456Z'), expected);
  assert.equal(serverTimestamp('invalid', 12), 12); passed++;
}

{
  const first = deferred();
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, body: options.body && JSON.parse(options.body) });
    return calls.length === 1 ? first.promise : response({});
  };
  const session = { id: 'ordered', title: '旧标题', settings: { temperature: 0.2 } };
  const older = persistSession(session);
  session.title = '新标题'; session.settings.temperature = 0.8;
  const newer = persistSession(session);
  session.title = '未保存草稿';
  await tick(); assert.equal(calls.length, 1);
  first.resolve(response({})); await Promise.all([older, newer]);
  assert.deepEqual(calls.map(call => call.body.title), ['旧标题', '新标题']);
  assert.deepEqual(calls.map(call => call.body.settings.temperature), [0.2, 0.8]); passed++;
}
{
  const save = deferred(); const calls = [];
  globalThis.fetch = async (_url, options) => { calls.push(options.method); return options.method === 'POST' ? save.promise : response({ status: 'deleted' }); };
  const saving = persistSession({ id: 'deletion', settings: {} });
  const removing = deletePersistedSession('deletion');
  assert.equal(deletePersistedSession('deletion'), removing);
  assert.equal(isSessionDeleting('deletion'), true);
  await assert.rejects(persistSession({ id: 'deletion' }), /删除/);
  assert.deepEqual(calls, ['POST']);
  save.resolve(response({})); await Promise.all([saving, removing]);
  assert.deepEqual(calls, ['POST', 'DELETE']);
  assert.equal(isSessionDeleting('deletion'), false);
  await assert.rejects(persistSession({ id: 'deletion' }), /删除/); passed++;
}
{
  globalThis.fetch = async () => { throw new TypeError('offline'); };
  await assert.rejects(deletePersistedSession('retry'));
  assert.equal(isSessionDeleting('retry'), false);
  globalThis.fetch = async () => response({});
  await persistSession({ id: 'retry', settings: {} });
  await deletePersistedSession('retry'); passed++;
}
{
  const first = deferred(); let count = 0;
  globalThis.fetch = async () => ++count === 1 ? first.promise : response({});
  const failed = persistSession({ id: 'failed-save' });
  const rejected = assert.rejects(failed);
  const next = persistSession({ id: 'failed-save', title: 'Retry' });
  first.reject(new Error('offline')); await rejected; await next;
  assert.equal(count, 2); passed++;
}
{
  const calls = [];
  globalThis.fetch = async url => {
    calls.push(url);
    return response({ sessions: [{ id: calls.length }], has_more: calls.length === 1 });
  };
  assert.deepEqual(await fetchAllSessions(), [{ id: 1 }, { id: 2 }]);
  assert.match(calls[1], /offset=200/); passed++;
}
const message = (n, content = `消息 ${n}`) => ({ id: `m_${n}`, role: 'user', content, ts: n });
const session = (id, messages) => ({ id, title: id, messages, settings: { temperature: 0.7 }, createdAt: 1, updatedAt: 2 });
const backup = sessions => ({ format: 'galgame2voice.chat', version: 1, sessions, global: {}, activeId: sessions[0]?.id });
{
  const full = backup(Array.from({ length: 80 }, (_, index) => session(`s_${index}`, Array.from({ length: 600 }, (_, n) => message(n)))));
  const local = backup([session('s_0', full.sessions[0].messages.slice(-100))]);
  const result = mergeChatBackup(full, local);
  const exported = JSON.parse(result.content);
  assert.equal(exported.sessions.length, 80);
  assert.equal(exported.sessions[0].messages.length, 600);
  assert.equal(result.browserCopies, 0); passed++;
}
{
  const server = backup([session('same', [message(1, '你好'), message(2, '你好')])]);
  const local = backup([session('same', [message(3, '你好'), message(4, '你好'), message(5, '尚未同步')])]);
  const original = JSON.stringify([server, local]);
  const result = mergeChatBackup(server, local);
  const exported = JSON.parse(result.content);
  assert.equal(result.browserCopies, 1);
  assert.deepEqual(exported.sessions.map(s => s.messages.length), [2, 3]);
  assert.match(exported.sessions[1].title, /浏览器副本/);
  assert.equal(JSON.stringify([server, local]), original); passed++;
}
{
  const full = backup([session('repeat', [message(1, '同一句'), message(2, '同一句')])]);
  const local = backup([session('repeat', [message(8, '同一句'), message(9, '同一句')])]);
  assert.equal(JSON.parse(mergeChatBackup(full, local).content).sessions[0].messages.length, 2);
  local.sessions[0].messages.push(message(10, '同一句'));
  assert.equal(mergeChatBackup(full, local).browserCopies, 1); passed++;
}
{
  const server = backup([session('old', [message(1)])]);
  const local = backup([session('local-only', [message(2)])]);
  local.sessions[0].settings.apiKey = 'private'; local.sessions[0].messages[0].audioUrls = ['/audio/file.wav'];
  const result = mergeChatBackup(server, local);
  assert.equal(result.count, 2);
  assert.ok(!result.content.includes('private') && !result.content.includes('/audio/'));
  assert.equal(JSON.parse(result.content).activeId, 'local-only'); passed++;
}
{
  globalThis.fetch = async () => { throw new TypeError('offline'); };
  await assert.rejects(exportCompleteChatBackup()); passed++;
}
{
  const local = backup([session('no-voice', [{...message(1, '不能丢弃的无语音回复'), role:'assistant', noVoice:true}])]);
  const result = mergeChatBackup(backup([session('no-voice', [])]), local);
  assert.equal(result.browserCopies, 1);
  assert.ok(result.content.includes('不能丢弃的无语音回复')); passed++;
}
{
  // LOG and the title screen print the greeting as plain text, and the old chat layout is gone.
  assert.doesNotMatch(GREETING, /\*\*|顶栏|侧边栏|右上角/);
  assert.match(GREETING, /设置中心/);
  assert.match(GREETING, /返回开始页/);
  assert.doesNotMatch(GREETING, /CONFIG 面板的底部/);
  // A session that still holds an older greeting is not exported as an extra browser copy.
  for (const greeting of [GREETING, ...LEGACY_GREETINGS]) {
    const local = backup([session('greeted', [{ ...message(0, greeting), role: 'assistant', noVoice: true }, message(1)])]);
    const result = mergeChatBackup(backup([session('greeted', [message(1)])]), local);
    assert.equal(result.browserCopies, 0);
  }
  passed++;
}
{
  assert.equal(earliestServerMessageId([{ id: 'm_stream_1' }, { id: 'm_42' }, { id: 'm_15' }, { id: 'm_99' }]), 15);
  assert.equal(earliestServerMessageId([{ id: 'm_local_abc' }]), null);
  const calls = [];
  globalThis.fetch = async url => {
    calls.push(url);
    return response({ messages: [{ id: 5, role: 'user', content_chinese: '更早消息' }], has_more: true, oldest_id: 5 });
  };
  const page = await fetchSessionHistoryPage('s_long', { limit: 100, beforeId: 15 });
  assert.equal(page.has_more, true);
  assert.equal(page.oldest_id, 5);
  assert.match(calls[0], /session_id=s_long/);
  assert.match(calls[0], /before_id=15/);
  passed++;
}
{
  const bodies = [];
  globalThis.fetch = async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    return new Response('event: done\ndata: {"chinese":"收到"}\n\n', { headers: { 'Content-Type': 'text/event-stream' } });
  };
  const sessions = [
    { id: 's_legacy', settings: { advReplyFormat: 'legacy' } },
    { id: 's_old', settings: { advReplyFormat: 'old' } },
    { id: 's_lines', settings: { advReplyFormat: 'lines', advOutfit: 'casual' } },
    { id: 's_unset', settings: {} },
  ];
  for (const active of sessions) {
    await new Promise((resolve) => streamChat({
      prompt: '你好', sessionId: active.id, settings: active.settings,
      onChunk() {}, onEnd: () => resolve(), fetch: globalThis.fetch,
    }));
  }
  // Only the explicit opt-out asks the server for the old whole-reply format.
  assert.deepEqual(bodies.map(body => [body.session_id, body.reply_format, body.outfit]), [
    ['s_legacy', 'old', undefined], ['s_old', 'lines', undefined],
    ['s_lines', 'lines', 'casual'], ['s_unset', 'lines', undefined],
  ]); passed++;
}

// The export button calls exportCompleteChatBackup, which reads /api/chat/export
// and the in-memory browser snapshot. A prefix must not drop the newer settings.
function installChatStorage(session) {
  const values = new Map([['gal2voice.chat.v1', JSON.stringify({
    sessions: [session], activeId: session.id, global: {},
  })]]);
  globalThis.localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  };
}
const prefixMessages = [
  { id: 'm_10', role: 'user', content: '今天去哪', japanese: '', ts: 1_700_000_000_000, lines: null },
  { id: 'm_11', role: 'assistant', content: '去江边走走吧', japanese: '海辺へ', ts: 1_700_000_002_000, lines: null },
];
async function exportPrefix(localSettings, localUpdatedAt) {
  installChatStorage({
    id: 's_backup', title: '晚上的对话', createdAt: 1_700_000_000_000, updatedAt: localUpdatedAt,
    settings: localSettings,
    messages: [{ id: 'm_local_user', role: 'user', content: '今天去哪', ts: 1_700_000_000_000 }],
  });
  loadState();
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return response({
      format: 'galgame2voice.chat', version: 1, exportedAt: '2024-06-01T00:00:02.000Z',
      activeId: 's_backup', global: {},
      sessions: [{
        id: 's_backup', title: '晚上的对话', createdAt: 1_700_000_000_000, updatedAt: 1_700_000_500_000,
        settings: { advPlayerName: '小明', advWindowOpacity: 40, voiceProfileId: 3 },
        messages: prefixMessages,
      }],
    });
  };
  const result = await exportCompleteChatBackup();
  assert.deepEqual(calls, ['/api/chat/export']);
  const exported = JSON.parse(result.content).sessions[0];
  return { result, exported };
}
{
  // complete export keeps newer server settings when local messages are only a prefix
  const { result, exported } = await exportPrefix(
    { advPlayerName: '旧名', advWindowOpacity: 80, voiceProfileId: null }, 1_700_000_100_000);
  assert.equal(result.browserCopies, 0);
  assert.deepEqual(exported.messages.map(message => message.content), ['今天去哪', '去江边走走吧']);
  assert.equal(exported.settings.advPlayerName, '小明');
  assert.equal(exported.settings.advWindowOpacity, 40);
  assert.equal(exported.settings.voiceProfileId, 3);
  passed++;
}
{
  // complete export keeps newer local settings when the browser copy was saved later
  const { result, exported } = await exportPrefix(
    { advPlayerName: '新名', advWindowOpacity: 30, voiceProfileId: 8 }, 1_700_000_900_000);
  assert.equal(result.browserCopies, 0);
  assert.deepEqual(exported.messages.map(message => message.content), ['今天去哪', '去江边走走吧']);
  assert.equal(exported.settings.advPlayerName, '新名');
  assert.equal(exported.settings.advWindowOpacity, 30);
  assert.equal(exported.settings.voiceProfileId, 8);
  passed++;
}
console.log(`Session persistence and complete backups: ${passed} scenarios passed`);
