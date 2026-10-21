import assert from 'node:assert/strict';
import test from 'node:test';
import { setup } from './helpers/adv_main.mjs';
import { tick } from './helpers/chat_stream.mjs';

const olderPage = () => ({ messages: [{ id: 10, role: 'user', content_chinese: '更早消息' }], has_more: false, oldest_id: 10 });
function history(t, fetchPage) {
  const h = setup(t, { fetchPage });
  Object.assign(h.getActive(), { messages: [{ id: 'm_20', role: 'user', content: '已有消息' }], oldestMessageId: 20, hasMoreHistory: true });
  h.app.adv.action('log'); return h;
}
test('LOG keeps the live reply, blocks paging during generation and loads older history after done', async t => {
  let requests = 0;
  const h = history(t, async () => { requests++; return olderPage(); });
  h.app.sendMessage('用户提问');
  await h.stream.requests[0].onChunk('正在回复');
  await h.app.loadOlderSessionHistory(h.getActive());
  assert.equal(requests, 0);
  assert.equal(h.app.adv.backlog.moreButton.disabled, true);
  await h.stream.requests[0].onEnd('完整回复');
  const log = h.app.adv.backlog;
  assert.equal(log.moreButton.disabled, false);
  await log.moreButton.click(); await tick();
  const texts = log.element.querySelectorAll('.adv-log-primary').map(e => e.textContent);
  assert.ok(texts.includes('完整回复')); assert.ok(texts.includes('更早消息'));
});
test('pending history blocks send and duplicate paging while preserving the ADV draft', async t => {
  let complete, requests = 0;
  const h = history(t, () => { requests++; return new Promise(resolve => { complete = resolve; }); });
  h.app.adv.action('close_overlay');
  const input = h.app.adv.textbox.inputElement; input.value = '待发送'; input.dispatchEvent({ type: 'input' });
  const pending = h.app.loadOlderSessionHistory(h.getActive()); await tick();
  input.dispatchEvent({ type: 'keydown', key: 'Enter' });
  await h.app.loadOlderSessionHistory(h.getActive());
  assert.equal(requests, 1); assert.equal(h.stream.requests.length, 0); assert.equal(input.value, '待发送');
  assert.equal(h.app.adv.stage.stage.querySelector('.adv-player-send-btn').disabled, true);
  complete(olderPage()); await pending;
  assert.equal(h.app.adv.stage.stage.querySelector('.adv-player-send-btn').disabled, false);
});
test('failed history loading recovers without losing the session or draft', async t => {
  let fail = true; const toasts = [];
  const h = setup(t, { fetchPage: async () => { if (fail) throw new Error('offline'); return olderPage(); }, callbacks: { showToast: text => toasts.push(text) } });
  Object.assign(h.getActive(), { messages: [{ id: 'm_20', role: 'user', content: '已有消息' }], oldestMessageId: 20, hasMoreHistory: true });
  const input = h.app.adv.textbox.inputElement; input.value = '可以重试'; input.dispatchEvent({ type: 'input' });
  await h.app.loadOlderSessionHistory(h.getActive());
  assert.equal(h.getActive().loadingOlder, false); assert.equal(input.value, '可以重试');
  assert.match(toasts[0], /加载更早消息失败/);
  fail = false; await h.app.loadOlderSessionHistory(h.getActive());
  assert.equal(h.getActive().messages.length, 2);
});
