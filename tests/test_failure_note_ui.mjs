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
test('error codes decide model-settings toast actions; text without a code does not', () => {
  assert.equal(needsModelSettings('anything', 'PRIVATE_ENDPOINT_BLOCKED'), true);
  assert.equal(needsModelSettings('请求过于频繁', 'OPENAI_RATE_LIMIT'), false);
  assert.equal(needsModelSettings('API Key 无效', null), false);
  assert.equal(needsModelSettings('对话已被删除或清空，本次回复未保存。', 'turn_unavailable'), false);
  assert.equal(needsModelSettings('连接中断，请检查服务与网络后再试。', null), false);
  assert.equal(needsModelSettings('连接超时，响应里提到 API Key', 'NETWORK_TIMEOUT'), false);
  assert.equal(needsModelSettings('连接超时，响应里提到 API Key', 'CONNECTION_RESET'), false);
  assert.equal(needsModelSettings('连接超时，响应里提到 API Key', 'NETWORK_CONNECTION_FAILED'), false);
  assert.equal(needsModelSettings('连接超时，响应里提到 API Key', 'BAD_GATEWAY'), false);
  assert.equal(needsModelSettings('连接超时，响应里提到 API Key', 'SERVER_OVERLOADED'), false);
  assert.equal(needsModelSettings('连接超时，响应里提到 API Key', 'REQUEST_TIMEOUT'), false);
});

test('missing or ambiguous voice selections open character settings while missing providers open model settings', () => {
  assert.equal(needsModelSettings('音色已不存在', 'VOICE_PROFILE_NOT_FOUND'), false);
  assert.equal(needsModelSettings('角色名不唯一', 'VOICE_PROFILE_AMBIGUOUS'), false);
  assert.equal(needsModelSettings('模型配置已不存在', 'PROVIDER_NOT_FOUND'), true);
});
test('toast positioning uses stage geometry above the ADV band', () => {
  const css = readFileSync(new URL('../frontend/src/adv/app.css', import.meta.url), 'utf8');
  assert.ok(css.includes('.toast-root { bottom: calc((100dvh - 1080 * var(--u)) / 2 + 300 * var(--u))'));
});

test('partial-reply disconnect keeps received lines and guides user to continue instead of resending', async t => {
  const h = setup(t);
  h.app.sendMessage('第一句问候');
  await h.stream.requests[0].event('line', { index: 0, zh: '前半句台词', ja: '前半', emotion: 'calm' });
  await h.stream.requests[0].event('error', {
    error_code: 'stream_incomplete',
    retryable: true,
    summary_zh: '流式连接提前断开',
    action_zh: '无需修改配置，可直接重新发送尝试重试。',
  });
  const note = h.dom.advRoot.querySelector('.adv-error-message')?.textContent || '';
  assert.match(note, /已收到的台词保留在「对话记录」里/);
  assert.match(note, /继续/);
  assert.doesNotMatch(note, /重新发送/);
  assert.equal(needsModelSettings(note, 'stream_incomplete'), false);
  assert.equal(needsModelSettings('等待超时', 'stream_idle_timeout'), false);

  const stageBtns = h.dom.advRoot.querySelectorAll('.adv-error-btn');
  assert.deepEqual(stageBtns.map(b => b.textContent), ['继续', '去设置']);
  stageBtns[0].click();
  assert.equal(h.getActive().messages.length, 2);
  assert.equal(h.getActive().messages[1].content, '前半句台词');
});

test('CONTEXT_LENGTH_EXCEEDED and MAX_TOKENS_TOO_LARGE open 角色与会话 from the stage and the toast', async t => {
  const toasts = [];
  const calls = [];
  const h = setup(t, {
    callbacks: {
      showToast: (msg, type, opts) => toasts.push({ msg, type, opts }),
      openGlobalSettings: tab => calls.push(['global', tab]),
      openSessionSettings: () => calls.push(['session']),
    },
  });

  for (const [code, summary_zh, action_zh] of [
    [
      'CONTEXT_LENGTH_EXCEEDED',
      '对话上下文超出模型最大限制',
      '对话上下文超出当前模型的长度限制。请在「角色与会话 → 对话高级参数」中调小「携带上下文条数」，或点击「新建会话」开启新对话。',
    ],
    [
      'MAX_TOKENS_TOO_LARGE',
      '「最大生成长度」超出当前模型上限',
      '「最大生成长度」超过了当前模型单次能输出的上限。请在「角色与会话 → 对话高级参数」中把「最大生成长度」调小（例如 4096）后重试。',
    ],
  ]) {
    calls.length = 0;
    h.app.sendMessage(`提问-${code}`);
    await h.stream.requests.at(-1).event('error', {
      error_code: code,
      diagnostic_code: code,
      retryable: false,
      summary_zh,
      action_zh,
    });

    const stageBtns = h.dom.advRoot.querySelectorAll('.adv-error-btn');
    assert.deepEqual(stageBtns.map(b => b.textContent), ['重新发送', '去设置']);
    stageBtns[1].click();
    assert.deepEqual(calls, [['session']], `${code} stage 去设置 should open session settings`);


    toasts.length = 0;
    calls.length = 0;
    h.app.sendMessage(`半截提问-${code}`);
    const toastReq = h.stream.requests.at(-1);
    await toastReq.event('line', { index: 0, zh: '半句台词', ja: '半分', emotion: 'calm' });
    await toastReq.event('error', {
      error_code: code,
      diagnostic_code: code,
      retryable: false,
      summary_zh,
      action_zh,
    });
    assert.equal(toasts.length, 1, `${code} non-inline failure should emit toast`);
    assert.ok(toasts[0].opts?.onAction, `${code} toast should provide an action button`);
    assert.notEqual(toasts[0].opts.actionLabel, '配置对话模型');
    toasts[0].opts.onAction();
    assert.deepEqual(calls, [['session']], `${code} toast action should open session settings`);
  }
});
