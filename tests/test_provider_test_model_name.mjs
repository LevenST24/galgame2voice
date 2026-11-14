import assert from 'node:assert/strict';
import test from 'node:test';
import { fakeDocument } from './helpers/adv_dom.mjs';

async function shown(t, model, models) {
  const previousDocument = globalThis.document;
  const previousFetch = globalThis.fetch;
  const doc = fakeDocument();
  globalThis.document = doc;
  globalThis.fetch = async (url) => ({
    ok: true,
    status: 200,
    json: async () => (url === '/api/providers/test'
      ? { success: true, latency_ms: 52, models }
      : { providers: [], presets: [] }),
  });
  const { createProviderSettingsController } = await import('../frontend/src/controllers/provider_settings.js');
  const keys = [
    'gProvider', 'gBaseUrl', 'gChatModel', 'gApiKey', 'gProviderTest',
    'gProviderTestResult', 'gTestResultTitle', 'gTestResultLatency', 'gTestResultBody',
  ];
  const tags = ['select', 'input', 'input', 'input', 'button', 'div', 'div', 'div', 'div'];
  const dom = Object.fromEntries(keys.map((key, index) => [key, doc.createElement(tags[index])]));
  dom.gProvider.value = 'openai';
  dom.gBaseUrl.value = 'https://api.example.test/v1';
  dom.gChatModel.value = model;
  const controller = createProviderSettingsController(dom, {});
  t.after(() => {
    controller.dispose();
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
  });
  dom.gProviderTest.click();
  await new Promise((resolve) => setImmediate(resolve));
  return { title: dom.gTestResultTitle.textContent, body: dom.gTestResultBody.textContent };
}

test('model missing from a non-empty list is not reported ready', async (t) => {
  const view = await shown(t, 'no-such-model', ['standin', 'alpha', 'beta', 'gamma']);
  assert.equal(view.title, '模型列表里没有「no-such-model」');
  assert.equal(
    view.body,
    '地址和密钥可用，但对方返回的模型列表里没有这个名字，聊天时可能失败。列表里有：standin、alpha、beta…',
  );
});

test('typed name with an empty model list is not called ready', async (t) => {
  const view = await shown(t, 'no-such-model', []);
  assert.equal(view.title, '地址和密钥可用');
  assert.equal(view.body, '对方没有返回模型列表，还不能确认「no-such-model」这个名字。');
});

test('listed or blank model name stays successful without an unchecked ready claim', async (t) => {
  const listed = await shown(t, 'Standin', ['models/standin']);
  assert.equal(listed.title, '连通性测试通过');
  assert.equal(listed.body, '已成功连接至目标端点，模型 Standin 就绪。 (发现 1 个可用模型)');
  const blank = await shown(t, '', ['standin']);
  assert.equal(blank.title, '连通性测试通过');
  assert.equal(blank.body, '已成功连接至目标端点。 (发现 1 个可用模型)');
});
