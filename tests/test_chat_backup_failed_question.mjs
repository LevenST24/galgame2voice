// A question that failed or was stopped before any reply stays in the browser for 重新发送,
// but the server drops that user row. The complete export must not then copy the whole
// session again as a "（浏览器副本）".
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { exportCompleteChatBackup, mergeChatBackup } from '../frontend/src/chat_backup.js';
import { exportSessionData, loadState } from '../frontend/src/store.js';

const ts = 1_700_000_000_000;
const answered = [
  { id: 'm1', role: 'user', content: '你好', ts },
  { id: 'm2', role: 'assistant', content: '你好呀。', japanese: 'こんにちは。', ts },
];
const serverData = () => ({ format: 'galgame2voice.chat', version: 1, global: {}, sessions: [{
  id: 's1', title: '聊天', createdAt: ts, updatedAt: ts, settings: {},
  messages: answered.map((message, i) => ({ ...message, id: `srv_${i + 1}` })),
}] });
const localData = (messages) => ({ format: 'galgame2voice.chat', version: 1, activeId: 's1', global: {}, sessions: [{
  id: 's1', title: '聊天', createdAt: ts, updatedAt: ts + 10, settings: {}, messages,
}] });
const titles = (result) => JSON.parse(result.content).sessions.map(session => session.title);

test('a failed last question does not duplicate the session', () => {
  const result = mergeChatBackup(serverData(), localData([...answered,
    { id: 'm3', role: 'user', content: '今天去哪？', ts: ts + 10, failed: { error: '网络连接失败', code: 'NETWORK_ERROR' } }]));
  assert.equal(result.browserCopies, 0);
  assert.deepEqual(titles(result), ['聊天']);
});

test('a stopped last question does not duplicate the session', () => {
  const result = mergeChatBackup(serverData(), localData([...answered,
    { id: 'm3', role: 'user', content: '等等', ts: ts + 10, failed: { error: '对话已中断，未收到回复', code: 'cancelled' } }]));
  assert.equal(result.browserCopies, 0);
});

test('an unsaved question that did not fail is still kept as a browser copy', () => {
  const result = mergeChatBackup(serverData(), localData([...answered,
    { id: 'm3', role: 'user', content: '今天去哪？', ts: ts + 10 }]));
  assert.equal(result.browserCopies, 1);
  assert.deepEqual(titles(result), ['聊天', '聊天（浏览器副本）']);
});

test('a stopped question followed by an answered one does not duplicate the session', () => {
  const server = serverData();
  const later = [{ id: 'm4', role: 'user', content: '第二句', ts: ts + 20 },
    { id: 'm5', role: 'assistant', content: '第二句的回复。', japanese: 'ふたつ。', ts: ts + 20 }];
  server.sessions[0].messages.push(...later.map((message, i) => ({ ...message, id: `srv_${i + 3}` })));
  const result = mergeChatBackup(server, localData([...answered,
    { id: 'm3', role: 'user', content: '等等', ts: ts + 10, failed: { error: '对话已中断，未收到回复', code: 'cancelled' } },
    ...later]));
  assert.equal(result.browserCopies, 0);
  assert.deepEqual(titles(result), ['聊天']);
});

test('a session that only exists in the browser keeps its failed question', () => {
  const server = serverData();
  server.sessions = [];
  const result = mergeChatBackup(server, localData([...answered,
    { id: 'm3', role: 'user', content: '今天去哪？', ts: ts + 10, failed: { error: 'x', code: 'NETWORK_ERROR' } }]));
  const [session] = JSON.parse(result.content).sessions;
  assert.equal(session.messages.at(-1).content, '今天去哪？');
  assert.equal('failed' in session.messages.at(-1), false);
});

test('exportCompleteChatBackup ignores a failed last question without leaking failed', async () => {
  const prevStorage = globalThis.localStorage;
  const prevFetch = globalThis.fetch;
  const mem = new Map();
  globalThis.localStorage = { getItem: k => mem.get(k) ?? null, setItem: (k, v) => mem.set(k, v), removeItem: k => mem.delete(k) };
  mem.set('gal2voice.chat.v1', JSON.stringify({
    activeId: 's1', global: {}, sessions: [{
      id: 's1', title: '聊天', createdAt: ts, updatedAt: ts + 10, settings: { voiceProfileId: 7 },
      messages: [...answered, { id: 'm3', role: 'user', content: '今天去哪？', ts: ts + 10,
        failed: { error: '网络连接失败', code: 'NETWORK_ERROR' } }],
    }],
  }));
  globalThis.fetch = async (url) => {
    assert.equal(String(url), '/api/chat/export');
    return { ok: true, status: 200, json: async () => serverData() };
  };
  try {
    loadState();
    const result = await exportCompleteChatBackup([{ id: 7, name: '星野' }]);
    assert.equal(result.browserCopies, 0);
    assert.deepEqual(titles(result), ['聊天']);
    const exported = JSON.parse(result.content);
    assert.equal(exported.sessions[0].characterName, '星野');
    assert.equal(exported.sessions[0].messages.some(m => 'failed' in m), false);
    const localOnly = JSON.parse(exportSessionData([{ id: 7, name: '星野' }]));
    assert.equal(localOnly.sessions[0].characterName, '星野');
    assert.equal(localOnly.sessions[0].messages.some(m => 'failed' in m), false);
  } finally {
    globalThis.localStorage = prevStorage;
    globalThis.fetch = prevFetch;
  }
});
