import assert from 'node:assert/strict';
import test from 'node:test';

import { fakeDocument, fakeClock, Element } from './helpers/adv_dom.mjs';
import { fakeChatStream } from './helpers/chat_stream.mjs';
import { createConversation } from '../frontend/src/conversation.js';

// Add the browser append API used by the chat renderer to this ADV DOM fake.
Element.prototype.append = function (...nodes) { nodes.forEach(node => this.appendChild(node)); };

// Setup fake document globally in Node environment before importing UI code
const doc = fakeDocument();
globalThis.document = doc;

const { BUILTIN_PRESETS, formatProviderDiagnostic } = await import('../frontend/src/settings.js');
const { needsModelSettings } = await import('../frontend/src/ui.js');
const { mountAdv } = await import('../frontend/src/adv/adv_app.js');
const { DEFAULT_SESSION_SETTINGS } = await import('../frontend/src/store.js');

function setup(t, options = {}) {
  const d = fakeDocument();
  globalThis.document = d;
  const clock = fakeClock(), frames = new Map(), listeners = new Map(), mediaListeners = new Set();
  const viewport = d.createElement('div');
  let frameId = 0, voiceEnd, overlayChange, subscription;
  const sessions = [0, 1].map(n => ({ id: `s_${n}`, settings: { ...DEFAULT_SESSION_SETTINGS, advEnabled: true, ...options.settings }, messages: [] }));
  let active = sessions[0], character = { id: 1, name: '测试角色' }, voiceEnabled = options.voice !== false;
  class Image extends Element {
    constructor() { super('img', d); }
    decode() { return Promise.resolve(); }
  }
  const controller = {
    busy: false, sent: [], retried: [], stopped: 0,
    on(type, fn) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(fn); return () => listeners.get(type).delete(fn); },
    send(text) { if (this.busy || active.loadingOlder || active.deleting) return false; this.sent.push(text); return true; },
    canRetry(user) { return !this.busy && !active.loadingOlder && !active.deleting && !!user?.failed && active.messages.at(-1) === user; },
    retry(user) { if (this.canRetry(user)) this.retried.push(user); }, stop() { this.stopped++; },
  };
  const audio = {
    played: [], stopped: [],
    playLine(index, chunks, opts) { this.played.push({ index, chunks, opts }); return new Promise(resolve => { voiceEnd = resolve; }); },
    stopLine(opts) { this.stopped.push(opts); voiceEnd?.('interrupted'); },
  };
  const dom = Object.fromEntries(['root', 'sidebar', 'messages', 'input', 'micBtn'].map(key => [key, d.createElement(key === 'input' ? 'textarea' : 'div')]));
  for (const el of Object.values(dom)) d.body.appendChild(el);
  const mql = { matches: false, addEventListener: (_, fn) => mediaListeners.add(fn), removeEventListener: (_, fn) => mediaListeners.delete(fn) };
  const calls = { settings: 0, models: 0, newSession: 0, history: 0, mic: 0 };
  const app = mountAdv(dom, {
    controller, audio, clock,
    getSession: () => active, voiceEnabled: () => voiceEnabled,
    getCharacter: () => character, ImageClass: Image,
    loadPortrait: options.loadPortrait || (async () => null),
    raf: fn => { frames.set(++frameId, fn); return frameId; }, cancelRaf: id => frames.delete(id),
    matchMedia: () => mql, viewport,
    saveSettings: patch => { Object.assign(active.settings, patch); },
    onSettings: () => { calls.settings++; }, onModelSettings: () => { calls.models++; },
    onNewSession: () => { calls.newSession++; }, onMic: () => { calls.mic++; },
    subscribeOverlay: fn => { overlayChange = fn; return () => { overlayChange = null; }; },
    subscribe: fn => { subscription = fn; return () => { subscription = null; }; },
  });
  t.after(() => app.unmount());
  const emit = (type, event) => { for (const fn of [...listeners.get(type) || []]) fn(event); };
  const start = (id = 'turn_0') => {
    controller.busy = true;
    const userMessage = { id: 'user_0', role: 'user', content: '问题', recording: 'keep' };
    active.messages.push(userMessage);
    emit('turnstart', { originId: active.id, assistantMessage: { id }, userMessage });
    return userMessage;
  };
  return { app, controller, calls, emit, start, active };
}

test('BUILTIN_PRESETS gemini defaults reasoning_effort to low', () => {
  const gemini = BUILTIN_PRESETS.find(p => p.id === 'gemini');
  assert.ok(gemini, 'gemini preset must exist in BUILTIN_PRESETS');
  assert.equal(gemini.default_reasoning_effort, 'low');
});

test('formatProviderDiagnostic intercepts 400 parameter errors before API key error', () => {
  const thinkingErr = formatProviderDiagnostic('gemini', 'HTTP 400: Thinking level MINIMAL is not supported for this model');
  assert.equal(thinkingErr.title, '模型参数不受支持 (HTTP 400)');
  assert.ok(thinkingErr.guidance.includes('思考强度'));

  const effortErr = formatProviderDiagnostic('openai', "HTTP 400: Invalid parameter: 'reasoning_effort'");
  assert.equal(effortErr.title, '模型参数不受支持 (HTTP 400)');
  assert.ok(effortErr.guidance.includes('思考强度'));

  const keyErr = formatProviderDiagnostic('gemini', 'HTTP 400: API key not valid. Please pass a valid API key.');
  assert.equal(keyErr.title, 'API Key 凭据无效或未授权 (HTTP 400/401)');
});

test('needsModelSettings considers empty_reply a model setting issue', () => {
  assert.equal(needsModelSettings('任何信息', 'empty_reply'), true);
  assert.equal(needsModelSettings('模型把生成长度用完了，却没有写出回复', null), true);
  assert.equal(needsModelSettings('模型这次没有返回任何内容', null), true);
});

test('ADV textbox displays empty_reply with 重新发送 and 去设置 actions', t => {
  const h = setup(t);
  const userMessage = h.start('turn_empty');
  const errorReason = '模型这次没有返回任何内容，可以重新发送或换个模型试试。';
  userMessage.failed = { error: errorReason, code: 'empty_reply' };
  h.controller.busy = false;
  h.emit('error', { originId: h.active.id, error: errorReason, kind: 'empty_reply', code: 'empty_reply' });
  assert.equal(h.app.player.state, 'error');
  assert.ok(h.app.stage.stage.querySelector('.adv-error-message').textContent.includes(errorReason));

  const actions = h.app.stage.stage.querySelector('.adv-error-actions').children;
  assert.equal(actions.length, 2);
  assert.equal(actions[0].textContent, '重新发送');
  assert.equal(actions[1].textContent, '去设置');

  actions[0].click();
  assert.equal(h.controller.retried.length, 1);
  assert.equal(h.controller.retried[0], userMessage);

  actions[1].click();
  assert.equal(h.calls.models, 1);
});

test('provider editor loads and saves 思考强度, defaulting only new Gemini to low', async t => {
  const { initGlobalSettings, loadProviders, onProviderChange, handleActivateProvider, handleProviderTest } =
    await import('../frontend/src/controllers/global_settings.js');
  const d = fakeDocument();
  globalThis.document = d;
  const dom = Object.fromEntries(['gProvider', 'gReasoningEffort', 'gProviderActivate', 'gProviderTest',
    'gProviderTestResult', 'gBaseUrl', 'gChatModel', 'gApiKey'].map(key => [key, d.createElement('input')]));
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  let providers = [], saved, tested;
  const response = data => ({ ok: true, status: 200, json: async () => data });
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/providers/test') {
      tested = JSON.parse(options.body);
      return response({ success: true, message: 'ok' });
    }
    if (url.endsWith('/activate')) return response({ active_provider: providers[0] });
    if (options.method === 'POST') {
      saved = JSON.parse(options.body); providers = [{ ...saved, api_key: '****' }];
      return response({ provider: saved });
    }
    return response({ providers, presets: BUILTIN_PRESETS });
  };
  initGlobalSettings(dom);
  dom.gProvider.value = 'gemini';
  await loadProviders();
  assert.equal(dom.gReasoningEffort.value, 'low');
  dom.gApiKey.value = 'synthetic';
  await handleActivateProvider();
  assert.equal(saved.reasoning_effort, 'low');
  for (const value of ['high', null, undefined]) {
    providers = [{ id: 'gemini', api_key: '****', reasoning_effort: value }];
    await loadProviders();
    assert.equal(dom.gReasoningEffort.value, value || '');
    onProviderChange();
    assert.equal(dom.gReasoningEffort.value, value || '');
  }
  dom.gReasoningEffort.value = 'medium';
  await handleActivateProvider();
  assert.equal(saved.reasoning_effort, 'medium');
  assert.equal(dom.gReasoningEffort.value, 'medium');
  await handleProviderTest();
  assert.equal(tested.reasoning_effort, 'medium');
  dom.gReasoningEffort.value = '';
  await handleActivateProvider();
  assert.equal(saved.reasoning_effort, null);
  assert.equal(dom.gReasoningEffort.value, '');
});

test('empty_reply travels through SSE and conversation to a retryable inline failure; cancel stays cancelled', async () => {
  const stream = fakeChatStream();
  const session = { id: 'session', settings: {}, messages: [] };
  let sequence = 0;
  const controller = createConversation({
    state: { global: { voiceMode: false } }, getActive: () => session, getSession: () => session,
    addMessage(role, content) { const msg = { id: ++sequence, role, content }; session.messages.push(msg); return msg; },
    uid: () => ++sequence, saveState() {}, persistSession: async () => {}, isSessionDeleting: () => false,
    audioPlayer: {}, getCurrentVoice: () => null, fetch: stream.fetch,
  });
  let error, cancelled = 0;
  controller.on('error', event => { error = event; });
  controller.on('cancelled', () => { cancelled++; });
  assert.equal(controller.send('问题'), true);
  const reason = '模型这次没有返回任何内容，可以重新发送或换个模型试试。';
  await stream.requests[0].event('error', { error: reason, kind: 'empty_reply', error_code: 'empty_reply' });
  assert.equal(error.kind, 'empty_reply');
  assert.equal(error.failedInline, true);
  assert.equal(session.messages.length, 1);
  const user = session.messages[0];
  assert.deepEqual(user.failed, { error: reason, code: 'empty_reply' });
  assert.equal(controller.canRetry(user), true);
  assert.equal(controller.retry(user), true);
  assert.equal(session.messages.length, 1);
  controller.stop();
  assert.equal(cancelled, 1);
  assert.equal(user.failed, undefined);
});

async function setupProviderFooter(t) {
  const settings = await import('../frontend/src/controllers/global_settings.js');
  const previousDocument = globalThis.document, previousFetch = globalThis.fetch;
  const d = fakeDocument();
  globalThis.document = d;
  const toastRoot = d.createElement('div');
  toastRoot.id = 'toastRoot';
  d.body.appendChild(toastRoot);
  const dom = Object.fromEntries(['gProvider', 'gProviderActivate', 'gTgSave', 'gApiKey',
    'gBaseUrl', 'gChatModel', 'gChatModelSelect', 'gReasoningEffort', 'gCustomName',
    'gCustomNameField'].map(key => [key, d.createElement('input')]));
  const calls = [];
  let providers = [{ id: 'openai', name: 'OpenAI', api_key: '****',
    api_base_url: 'https://api.openai.com/v1', chat_model: 'gpt-test', is_active: true }];
  let failure = '';
  const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, method: options.method || 'GET', body: options.body && JSON.parse(options.body) });
    if (options.method === 'POST' && url === failure) {
      return response({ detail: '测试服务拒绝保存，请检查配置' }, 400);
    }
    if (url === '/api/providers') {
      if (options.method === 'POST') {
        providers = [{ ...JSON.parse(options.body), api_key: '****' }];
        return response({ provider: providers[0] });
      }
      return response({ providers, presets: BUILTIN_PRESETS });
    }
    if (url.endsWith('/activate')) return response({ active_provider: providers[0] });
    if (url === '/api/config') return response({ status: 'ok', settings: {} });
    return response({});
  };
  t.after(() => {
    for (const toast of toastRoot.children) clearTimeout(toast._timer);
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
  });
  settings.initGlobalSettings(dom);
  dom.gProvider.value = 'openai';
  await settings.loadProviders();
  await settings.loadGlobalConfig();
  calls.length = 0;
  return { dom, calls, settings, toastRoot,
    edit(field, value, type = 'input') {
      dom[field].value = value;
      dom[field].dispatchEvent({ type });
    },
    async save(button = 'gTgSave') {
      dom[button].click();
      await new Promise(resolve => setImmediate(resolve));
    },
    fail(url) { failure = url; },
    writes: () => calls.filter(c => c.method === 'POST' && c.url === '/api/providers'),
    messages: () => toastRoot.children.map(toast => toast.textContent),
  };
}

test('footer saves an edited API key and activates the model', async t => {
  const h = await setupProviderFooter(t);
  h.edit('gApiKey', 'synthetic-footer-key');
  await h.save();
  assert.equal(h.writes().length, 1, 'footer must save the edited model via POST /api/providers');
  assert.equal(h.writes()[0].body.api_key, 'synthetic-footer-key');
  assert.equal(h.calls.filter(c => c.url === '/api/providers/openai/activate').length, 1);
  assert.ok(h.calls.some(c => c.url === '/api/config' && c.method === 'POST'));
  assert.ok(h.messages().some(msg => /全局配置.*模型.*保存.*启用/.test(msg)));
  await h.save();
  assert.equal(h.writes().length, 1, 'successful save must clear pending model edits');
});

test('footer leaves an untouched or programmatically filled model alone', async t => {
  const h = await setupProviderFooter(t);
  h.dom.gApiKey.value = 'programmatic-value';
  await h.save();
  assert.equal(h.calls.filter(c => c.url.startsWith('/api/providers')).length, 0);
  assert.ok(h.messages().includes('全局配置已保存'));
});

test('footer reports model validation errors without claiming global save success', async t => {
  const h = await setupProviderFooter(t);
  h.edit('gBaseUrl', '');
  await h.save();
  assert.equal(h.writes().length, 0);
  assert.ok(h.messages().some(msg => /模型.*未保存.*API Base URL.*其他设置也还没有保存/.test(msg)));
  assert.ok(!h.messages().some(msg => msg.includes('全局配置已保存')));
  assert.equal(h.dom.gTgSave.disabled, false);
  assert.equal(h.dom.gProviderActivate.disabled, false);
});

test('footer tracks input and change on every model field', async t => {
  const fields = { gApiKey: ['synthetic-key', 'api_key'],
    gBaseUrl: ['https://example.test/v1', 'api_base_url'],
    gChatModel: ['typed-model', 'chat_model'], gChatModelSelect: ['selected-model', 'chat_model'],
    gReasoningEffort: ['high', 'reasoning_effort'], gCustomName: ['测试名称', 'name'] };
  for (const [field, [value, property]] of Object.entries(fields)) {
    for (const event of ['input', 'change']) await t.test(`${field} ${event}`, async t => {
      const h = await setupProviderFooter(t);
      if (field === 'gCustomName') {
        h.dom.gProvider.value = '__custom__';
        h.dom.gProvider.dispatchEvent({ type: 'change' });
      }
      h.edit(field, value, event);
      await h.save();
      assert.equal(h.writes().length, 1);
      // The model dropdown copies its selection into the text field on change.
      if (field !== 'gChatModelSelect' || event === 'change') {
        assert.equal(h.writes()[0].body[property], value);
      }
    });
  }
});

test('switching providers and reloading providers clear model edits', async t => {
  for (const reset of ['switch', 'reload']) await t.test(reset, async t => {
    const h = await setupProviderFooter(t);
    h.edit('gApiKey', 'discarded-key');
    if (reset === 'reload') await h.settings.loadProviders();
    else {
      h.dom.gProvider.value = 'gemini';
      h.dom.gProvider.dispatchEvent({ type: 'change' });
    }
    h.calls.length = 0;
    await h.save();
    assert.equal(h.calls.filter(c => c.url.startsWith('/api/providers')).length, 0);
  });
});

test('footer reports save and activation request failures and allows retry', async t => {
  for (const url of ['/api/providers', '/api/providers/openai/activate']) await t.test(url, async t => {
    const h = await setupProviderFooter(t);
    h.edit('gApiKey', 'synthetic-retry-key');
    h.fail(url);
    await h.save();
    assert.ok(h.messages().some(msg => /模型.*未保存.*测试服务拒绝保存/.test(msg)));
    assert.ok(!h.messages().some(msg => msg.includes('全局配置已保存')));
    assert.equal(h.dom.gApiKey.value, 'synthetic-retry-key');
    assert.equal(h.dom.gTgSave.disabled, false);
    assert.equal(h.dom.gProviderActivate.disabled, false);
    h.fail('');
    await h.save();
    assert.equal(h.writes().length, 2, 'failed request must retain pending edits for retry');
    assert.ok(h.messages().some(msg => /全局配置.*模型.*保存.*启用/.test(msg)));
  });
});

test('save-and-activate button keeps its busy label and clears edits for footer save', async t => {
  const h = await setupProviderFooter(t);
  const originalHtml = '<span>保存并启用</span>';
  h.dom.gProviderActivate.innerHTML = originalHtml;
  h.edit('gApiKey', 'synthetic-button-key');
  const saved = h.save('gProviderActivate');
  assert.equal(h.dom.gProviderActivate.disabled, true);
  assert.match(h.dom.gProviderActivate.innerHTML, /保存并启用中/);
  await saved;
  assert.equal(h.dom.gProviderActivate.innerHTML, originalHtml);
  assert.equal(h.dom.gProviderActivate.disabled, false);
  assert.ok(h.messages().some(msg => msg.startsWith('已成功保存并启用')));
  await h.save();
  assert.equal(h.writes().length, 1);
});
