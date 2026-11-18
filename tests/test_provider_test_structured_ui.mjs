import assert from 'node:assert/strict';
import { test } from 'node:test';

import { fakeDocument } from './helpers/adv_dom.mjs';

async function runProviderTestFailure(t, { ok, status, provider, ...payload }) {
  const { createProviderSettingsController } = await import('../frontend/src/controllers/provider_settings.js');
  const previousDocument = globalThis.document;
  const previousFetch = globalThis.fetch;
  const d = fakeDocument();
  globalThis.document = d;

  const toastRoot = d.createElement('div');
  toastRoot.id = 'toastRoot';
  d.body.appendChild(toastRoot);

  globalThis.fetch = async (url) => (
    url === '/api/providers/test'
      ? { ok, status, json: async () => ({ success: false, error: payload.error_title, ...payload }) }
      : { ok: true, status: 200, json: async () => ({ providers: [], presets: [] }) }
  );

  const dom = Object.fromEntries([
    ['gProvider', 'select'], ['gBaseUrl', 'input'], ['gChatModel', 'input'],
    ['gChatModelSelect', 'select'], ['gReasoningEffort', 'input'], ['gProviderDesc', 'div'],
    ['gApiKey', 'input'], ['gProviderTest', 'button'], ['gProviderActivate', 'button'],
    ['gProviderTestResult', 'div'], ['gTestResultTitle', 'div'], ['gTestResultLatency', 'div'],
    ['gTestResultBody', 'div'],
  ].map(([k, tag]) => [k, d.createElement(tag)]));
  dom.gProviderTestResult.className = 'test-result-box hidden';

  const controller = createProviderSettingsController(dom, {});
  t.after(() => {
    controller.dispose();
    for (const toast of toastRoot.children) clearTimeout(toast._timer);
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
  });

  await controller.loadProviders();
  dom.gProvider.value = provider;
  controller.onProviderChange();
  dom.gApiKey.value = 'sk-test-key';

  dom.gProviderTest.click();
  await new Promise((resolve) => setImmediate(resolve));
  return { dom, toastRoot };
}

test('ProviderSettingsController displays structured error_title and diagnostic directly on test failure', async (t) => {
  const { dom, toastRoot } = await runProviderTestFailure(t, {
    ok: true,
    status: 200,
    provider: 'deepseek',
    error_code: 'QUOTA_EXCEEDED',
    error_title: '后端结构化标题：账户可用余额不足',
    diagnostic: '后端结构化建议：请前往控制台充值后重试。',
    status_code: 402,
    message: 'HTTP 402 (req_28401)',
  });

  assert.equal(dom.gTestResultTitle.textContent, '后端结构化标题：账户可用余额不足');
  assert.equal(dom.gTestResultBody.textContent, '后端结构化建议：请前往控制台充值后重试。');
  assert.ok(
    toastRoot.children.some((toast) => toast.textContent.includes('连通性测试未通过: 后端结构化标题：账户可用余额不足')),
  );
});

test('ProviderSettingsController preserves structured error_title and diagnostic on non-2xx response', async (t) => {
  const { dom } = await runProviderTestFailure(t, {
    ok: false,
    status: 502,
    provider: 'openai',
    error_code: 'BAD_GATEWAY',
    error_title: '后端非2xx标题：网关连接异常',
    diagnostic: '后端非2xx建议：请检查反向代理源站状态。',
    status_code: 502,
    message: 'Bad Gateway',
  });

  assert.equal(dom.gTestResultTitle.textContent, '后端非2xx标题：网关连接异常');
  assert.equal(dom.gTestResultBody.textContent, '后端非2xx建议：请检查反向代理源站状态。');
});
