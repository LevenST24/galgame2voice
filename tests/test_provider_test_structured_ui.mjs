import assert from 'node:assert/strict';
import { test } from 'node:test';

import { fakeDocument } from './helpers/adv_dom.mjs';

function setupController(t, onTestFetch) {
  const previousDocument = globalThis.document;
  const previousFetch = globalThis.fetch;
  const d = fakeDocument();
  globalThis.document = d;

  const toastRoot = d.createElement('div');
  toastRoot.id = 'toastRoot';
  d.body.appendChild(toastRoot);

  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/providers/test') {
      return onTestFetch(url, options);
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ providers: [], presets: [] }),
    };
  };

  const dom = {
    gProvider: d.createElement('select'),
    gBaseUrl: d.createElement('input'),
    gChatModel: d.createElement('input'),
    gChatModelSelect: d.createElement('select'),
    gReasoningEffort: d.createElement('input'),
    gProviderDesc: d.createElement('div'),
    gApiKey: d.createElement('input'),
    gProviderTest: d.createElement('button'),
    gProviderActivate: d.createElement('button'),
    gProviderTestResult: d.createElement('div'),
    gTestResultTitle: d.createElement('div'),
    gTestResultLatency: d.createElement('div'),
    gTestResultBody: d.createElement('div'),
  };
  dom.gProviderTestResult.className = 'test-result-box hidden';

  t.after(() => {
    for (const toast of toastRoot.children) clearTimeout(toast._timer);
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
  });

  return { dom, toastRoot };
}

test('ProviderSettingsController displays structured error_title and diagnostic directly on test failure', async (t) => {
  const { createProviderSettingsController } = await import('../frontend/src/controllers/provider_settings.js');
  const { dom, toastRoot } = setupController(t, async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      success: false,
      error_code: 'QUOTA_EXCEEDED',
      error_title: '后端结构化标题：账户可用余额不足',
      diagnostic: '后端结构化建议：请前往控制台充值后重试。',
      status_code: 402,
      error: '后端结构化标题：账户可用余额不足',
      message: 'HTTP 402 (req_28401)',
    }),
  }));

  const controller = createProviderSettingsController(dom, {});
  t.after(() => controller.dispose());

  await controller.loadProviders();
  dom.gProvider.value = 'deepseek';
  controller.onProviderChange();
  dom.gApiKey.value = 'sk-test-key';

  dom.gProviderTest.click();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(dom.gTestResultTitle.textContent, '后端结构化标题：账户可用余额不足');
  assert.equal(dom.gTestResultBody.textContent, '后端结构化建议：请前往控制台充值后重试。');
  assert.ok(
    toastRoot.children.some((toast) => toast.textContent.includes('连通性测试未通过: 后端结构化标题：账户可用余额不足')),
  );
});

test('ProviderSettingsController preserves structured error_title and diagnostic on non-2xx response', async (t) => {
  const { createProviderSettingsController } = await import('../frontend/src/controllers/provider_settings.js');
  const { dom } = setupController(t, async () => ({
    ok: false,
    status: 502,
    json: async () => ({
      success: false,
      error_code: 'BAD_GATEWAY',
      error_title: '后端非2xx标题：网关连接异常',
      diagnostic: '后端非2xx建议：请检查反向代理源站状态。',
      status_code: 502,
      error: '后端非2xx标题：网关连接异常',
      message: 'Bad Gateway',
    }),
  }));

  const controller = createProviderSettingsController(dom, {});
  t.after(() => controller.dispose());

  await controller.loadProviders();
  dom.gProvider.value = 'openai';
  controller.onProviderChange();
  dom.gApiKey.value = 'sk-test-key';

  dom.gProviderTest.click();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(dom.gTestResultTitle.textContent, '后端非2xx标题：网关连接异常');
  assert.equal(dom.gTestResultBody.textContent, '后端非2xx建议：请检查反向代理源站状态。');
});
