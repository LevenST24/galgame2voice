import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { setup } from './helpers/adv_main.mjs';
import { needsModelSettings } from '../frontend/src/ui.js';

test('LOG retains the failure reason and retry after reopening a stored failed session', async t => {
  const h = setup(t); h.app.sendMessage('保留提问');
  await h.stream.requests[0].onEnd('', { error: '<b>密钥缺失</b>', errorCode: 'API_KEY_MISSING' });
  const user = h.getActive().messages[0];
  await h.app.switchSession('s_1'); await h.app.switchSession('s_0');
  h.app.adv.action('log');
  const log = h.app.adv.backlog;
  assert.equal(log.element.querySelector('.adv-log-failure').textContent, '未收到回复：<b>密钥缺失</b>');
  assert.equal(log.element.querySelectorAll('b').length, 0);
  const buttons = log.element.querySelectorAll('.adv-error-btn');
  assert.deepEqual(buttons.map(b => b.textContent), ['重新发送', '去设置']);
  buttons[0].click();
  assert.equal(h.app.adv.backlog, null); assert.equal(h.stream.requests.length, 2);
  assert.equal(h.getActive().messages[0], user); assert.equal(h.getActive().messages.length, 1);
});
test('older failed messages have no retry; only the latest is eligible', t => {
  const h = setup(t);
  h.getActive().messages.push({ id: 'old', role: 'user', content: '旧问题', failed: { error: '离线' } },
    { id: 'latest', role: 'user', content: '新问题', failed: { error: '超时' } });
  h.app.adv.action('log');
  const entries = h.app.adv.backlog.element.querySelectorAll('.adv-log-entry');
  assert.deepEqual(entries[0].querySelectorAll('.adv-error-btn').map(b => b.textContent), ['去设置']);
  assert.deepEqual(entries[1].querySelectorAll('.adv-error-btn').map(b => b.textContent), ['重新发送', '去设置']);
});
test('error codes decide model-settings toast actions; text is the fallback', () => {
  assert.equal(needsModelSettings('anything', 'PRIVATE_ENDPOINT_BLOCKED'), true);
  assert.equal(needsModelSettings('请求过于频繁', 'OPENAI_RATE_LIMIT'), false);
  assert.equal(needsModelSettings('API Key 无效', null), true);
  assert.equal(needsModelSettings('连接中断，请检查服务与网络后再试。', null), false);
});
test('toast positioning uses stage geometry above the ADV band', () => {
  const css = readFileSync(new URL('../frontend/src/adv/app.css', import.meta.url), 'utf8');
  assert.ok(css.includes('.toast-root { bottom: calc((100dvh - 1080 * var(--u)) / 2 + 300 * var(--u))'));
});
