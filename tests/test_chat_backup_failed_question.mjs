// A question that failed or was stopped before any reply stays in the browser for 重新发送,
// but the server drops that user row. The complete export must not then copy the whole
// session again as a "（浏览器副本）".
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mergeChatBackup } from '../frontend/src/chat_backup.js';

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

test('a failed question is only ignored at the end', () => {
  const result = mergeChatBackup(serverData(), localData([
    answered[0],
    { id: 'm9', role: 'user', content: '离线时写的', ts, failed: { error: 'x', code: 'NETWORK_ERROR' } },
    answered[1],
  ]));
  assert.equal(result.browserCopies, 1);
});

test('a session that only exists in the browser keeps its failed question', () => {
  const server = serverData();
  server.sessions = [];
  const result = mergeChatBackup(server, localData([...answered,
    { id: 'm3', role: 'user', content: '今天去哪？', ts: ts + 10, failed: { error: 'x', code: 'NETWORK_ERROR' } }]));
  const [session] = JSON.parse(result.content).sessions;
  assert.equal(session.messages.at(-1).content, '今天去哪？');
});
