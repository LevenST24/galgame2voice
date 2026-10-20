import assert from 'node:assert/strict';
import { persistSession, deletePersistedSession, isSessionDeleting, fetchAllSessions, fetchSessionHistoryPage, earliestServerMessageId } from '../frontend/src/session_api.js';
import { mergeChatBackup, exportCompleteChatBackup } from '../frontend/src/chat_backup.js';
import { serverTimestamp } from '../frontend/src/session_data.js';
import { streamChat } from '../frontend/src/ai.js';

const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const response = data => ({ ok: true, json: async () => data });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
let passed = 0;

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
    { id: 's_old', settings: { advReplyFormat: 'old' } },
    { id: 's_lines', settings: { advReplyFormat: 'lines', advOutfit: 'casual' } },
  ];
  let active = sessions[0];
  const turn = () => new Promise((resolve) => streamChat({
    prompt: '你好', sessionId: active.id, settings: active.settings,
    onChunk() {}, onEnd: () => resolve(), fetch: globalThis.fetch,
  }));
  await turn();
  active = sessions[1];
  await turn();
  assert.deepEqual(bodies.map(body => [body.session_id, body.reply_format, body.outfit]), [
    ['s_old', 'old', undefined], ['s_lines', 'lines', 'casual'],
  ]); passed++;
}
console.log(`Session persistence and complete backups: ${passed} scenarios passed`);
