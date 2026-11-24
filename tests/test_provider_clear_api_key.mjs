import assert from 'node:assert/strict';
import test from 'node:test';
import { fakeDocument } from './helpers/adv_dom.mjs';

test('a saved API key can be cleared from the provider form', async (t) => {
  const previousDocument = globalThis.document;
  const previousFetch = globalThis.fetch;
  const previousConfirm = globalThis.confirm;
  const doc = fakeDocument();
  globalThis.document = doc;
  let keySaved = true;
  const calls = [];
  const questions = [];
  globalThis.confirm = (text) => { questions.push(text); return true; };
  globalThis.fetch = async (url, init = {}) => {
    calls.push(`${init.method || 'GET'} ${url}`);
    if (url === '/api/providers/deepseek/api_key' && init.method === 'DELETE') keySaved = false;
    return {
      ok: true,
      status: 200,
      json: async () => ({
        providers: [{
          id: 'deepseek', name: 'DeepSeek', api_base_url: 'https://api.deepseek.com/v1',
          chat_model: 'deepseek-chat', api_key: keySaved ? 'sk-****3456' : '', is_active: true,
        }],
        presets: [],
      }),
    };
  };
  const { createProviderSettingsController } = await import('../frontend/src/controllers/provider_settings.js');
  const keys = ['gProvider', 'gApiKey', 'gKeyStatusText', 'gBtnClearKey'];
  const tags = ['select', 'input', 'span', 'button'];
  const dom = Object.fromEntries(keys.map((key, index) => [key, doc.createElement(tags[index])]));
  const controller = createProviderSettingsController(dom, {});
  t.after(() => {
    controller.dispose();
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
    globalThis.confirm = previousConfirm;
  });

  await controller.loadProviders();
  dom.gProvider.value = 'deepseek';
  controller.renderLoadedProvider(false);
  assert.equal(dom.gBtnClearKey.classList.contains('hidden'), false);
  assert.match(dom.gKeyStatusText.textContent, /已配置密钥/);

  dom.gBtnClearKey.click();
  for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));

  assert.ok(calls.includes('DELETE /api/providers/deepseek/api_key'));
  assert.match(questions[0], /DeepSeek/);
  assert.match(questions[0], /当前生效/);
  assert.equal(dom.gProvider.value, 'deepseek');
  assert.equal(dom.gBtnClearKey.classList.contains('hidden'), true);
  assert.match(dom.gKeyStatusText.textContent, /未配置/);
});

test('nothing is cleared when the question is declined', async (t) => {
  const previousDocument = globalThis.document;
  const previousFetch = globalThis.fetch;
  const previousConfirm = globalThis.confirm;
  const doc = fakeDocument();
  globalThis.document = doc;
  const calls = [];
  globalThis.confirm = () => false;
  globalThis.fetch = async (url, init = {}) => {
    calls.push(`${init.method || 'GET'} ${url}`);
    return {
      ok: true,
      status: 200,
      json: async () => ({
        providers: [{ id: 'deepseek', name: 'DeepSeek', chat_model: 'deepseek-chat', api_key: 'sk-****3456', is_active: false }],
        presets: [],
      }),
    };
  };
  const { createProviderSettingsController } = await import('../frontend/src/controllers/provider_settings.js');
  const dom = { gProvider: doc.createElement('select'), gBtnClearKey: doc.createElement('button') };
  const controller = createProviderSettingsController(dom, {});
  t.after(() => {
    controller.dispose();
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
    globalThis.confirm = previousConfirm;
  });
  await controller.loadProviders();
  dom.gProvider.value = 'deepseek';
  await controller.clearStoredKey();
  assert.equal(calls.some((c) => c.startsWith('DELETE')), false);
});
