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

test('provider transport diagnostics show the connection problem in the title', () => {
  const guide = '无法与模型服务建立连接。请检查网络、代理和服务地址。';
  const result = formatProviderDiagnostic('gemini', '网络连接失败 (Connection Failed)', guide);
  assert.equal(result.title, '网络连接失败');
  assert.equal(result.guidance, guide);
});

test('provider DNS diagnostics do not collapse into a generic diagnostic heading', () => {
  const result = formatProviderDiagnostic('gemini', '服务器域名解析失败 (DNS Error)', '请检查服务地址或 DNS 设置。');
  assert.equal(result.title, '服务器域名解析失败');
});

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

async function setupProviderFooter(t, { confirmActivation = true } = {}) {
  const settings = await import('../frontend/src/controllers/global_settings.js');
  const previousDocument = globalThis.document, previousFetch = globalThis.fetch;
  const d = fakeDocument();
  globalThis.document = d;
  const toastRoot = d.createElement('div');
  toastRoot.id = 'toastRoot';
  d.body.appendChild(toastRoot);
  const dom = Object.fromEntries(['gProvider', 'gProviderActivate', 'gTgSave', 'gApiKey',
    'gBaseUrl', 'gChatModel', 'gChatModelSelect', 'gReasoningEffort', 'gCustomName',
    'gCustomNameField', 'gBtnResetBaseUrl', 'gTgChatId'].map(key => [key, d.createElement('input')]));
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
        return response({ provider: { ...providers[0], is_active: confirmActivation } });
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
  assert.equal(h.calls.filter(c => c.url === '/api/providers/openai/activate').length, 0);
  assert.ok(h.calls.some(c => c.url === '/api/config' && c.method === 'POST'));
  assert.ok(h.messages().some(msg => /全局配置.*模型.*保存.*启用/.test(msg)));
  await h.save();
  assert.equal(h.writes().length, 1, 'successful save must clear pending model edits');
});

test('provider confirmation does not activate an atomically saved model twice', async t => {
  const h = await setupProviderFooter(t);
  h.edit('gApiKey', 'synthetic-confirmed-key');
  h.fail('/api/providers/openai/activate');
  await h.settings.saveGlobalConfig();
  assert.equal(h.calls.filter(call => call.url.endsWith('/activate')).length, 0);
  assert.equal(h.writes().length, 1);
  assert.ok(h.calls.some(call => call.url === '/api/config' && call.method === 'POST'));
  assert.ok(h.messages().some(message => /全局配置和模型设置已保存/.test(message)));
  assert.equal(h.dom.gApiKey.value, '');
});

test('provider confirmation verifies a lost non-secret save without resending it', async t => {
  const h = await setupProviderFooter(t);
  const fetch = globalThis.fetch;
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/providers/openai') {
      const listed = await (await fetch('/api/providers')).json();
      return { ok: true, status: 200, json: async () => ({ provider: listed.providers[0] }) };
    }
    const result = await fetch(url, options);
    if (url === '/api/providers' && options.method === 'POST') throw new TypeError('Failed to fetch');
    return result;
  };
  h.edit('gChatModel', 'saved-before-disconnect');
  await h.settings.saveGlobalConfig();
  assert.equal(h.writes().length, 1);
  assert.ok(h.messages().some(message => /全局配置和模型设置已保存/.test(message)));
  assert.equal(h.settings.getCurrentActiveProvider().chat_model, 'saved-before-disconnect');
  await h.settings.saveGlobalConfig();
  assert.equal(h.writes().length, 1);
});

test('provider confirmation retains a new key when the write outcome is unknown', async t => {
  const h = await setupProviderFooter(t);
  const fetch = globalThis.fetch;
  globalThis.fetch = async (url, options = {}) => {
    const result = await fetch(url, options);
    if (url === '/api/providers' && options.method === 'POST') throw new TypeError('Failed to fetch');
    return result;
  };
  h.edit('gApiKey', 'synthetic-unknown-key');
  await h.settings.saveGlobalConfig();
  assert.equal(h.writes().length, 1);
  assert.equal(h.dom.gApiKey.value, 'synthetic-unknown-key');
  assert.equal(h.dom.gTgSave.disabled, false);
  assert.ok(h.messages().some(message => /保存结果尚未确认.*输入已保留/.test(message)));
  assert.ok(!h.calls.some(call => call.url === '/api/config' && call.method === 'POST'));
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

test('switching providers clears model edits', async t => {
    const h = await setupProviderFooter(t);
    h.edit('gApiKey', 'discarded-key');
    h.dom.gProvider.value = 'gemini';
    h.dom.gProvider.dispatchEvent({ type: 'change' });
    h.calls.length = 0;
    await h.save();
    assert.equal(h.calls.filter(c => c.url.startsWith('/api/providers')).length, 0);
});

function holdModelRequest(t, matches) {
  const fetch = globalThis.fetch;
  let entered, release, held = false;
  const started = new Promise(resolve => { entered = resolve; });
  const waiting = new Promise(resolve => { release = resolve; });
  globalThis.fetch = async (url, options = {}) => {
    if (!held && matches(url, options)) {
      held = true; entered(); await waiting;
    }
    return fetch(url, options);
  };
  t.after(release);
  return { started, release };
}

test('model draft survives edits during save, activation and list refresh', async t => {
  for (const phase of ['save', 'activate', 'refresh']) await t.test(phase, { timeout: 3000 }, async t => {
    const h = await setupProviderFooter(t, { confirmActivation: phase !== 'activate' });
    h.edit('gChatModel', 'submitted-model');
    const gate = holdModelRequest(t, (url, options) => phase === 'save'
      ? url === '/api/providers' && options.method === 'POST'
      : phase === 'activate' ? url.endsWith('/activate') : url === '/api/providers' && !options.method);
    const saving = h.settings.saveGlobalConfig();
    await gate.started;
    h.edit('gChatModel', 'new-unsaved-model');
    h.edit('gApiKey', 'synthetic-new-unsaved-key');
    h.dom.gApiKey.type = 'text';
    h.edit('gBaseUrl', 'https://new-draft.test/v1');
    h.edit('gReasoningEffort', 'high', 'change');
    gate.release(); await saving;
    assert.equal(h.dom.gChatModel.value, 'new-unsaved-model');
    assert.equal(h.dom.gApiKey.value, 'synthetic-new-unsaved-key');
    assert.equal(h.dom.gApiKey.type, 'text', 'refresh must preserve the draft input visibility');
    assert.equal(h.dom.gBaseUrl.value, 'https://new-draft.test/v1');
    assert.equal(h.dom.gReasoningEffort.value, 'high');
    assert.ok(h.messages().some(message => /当前修改尚未保存/.test(message)));
    await h.settings.saveGlobalConfig();
    assert.equal(h.writes().length, 2);
    assert.equal(h.writes()[1].body.chat_model, 'new-unsaved-model');
    assert.equal(h.writes()[1].body.api_key, 'synthetic-new-unsaved-key');
    assert.equal(h.writes()[1].body.api_base_url, 'https://new-draft.test/v1');
    assert.equal(h.writes()[1].body.reasoning_effort, 'high');
  });
});

test('model draft survives save-and-activate button refresh', { timeout: 3000 }, async t => {
  const h = await setupProviderFooter(t);
  h.edit('gChatModel', 'submitted-model');
  const gate = holdModelRequest(t, (url, options) => url === '/api/providers' && options.method === 'POST');
  const saving = h.settings.handleActivateProvider();
  await gate.started;
  h.edit('gChatModel', 'next-model');
  gate.release(); await saving;
  assert.equal(h.dom.gChatModel.value, 'next-model');
  assert.ok(h.messages().some(message => /当前修改尚未保存/.test(message)));
  await h.settings.saveGlobalConfig();
  assert.equal(h.writes().at(-1).body.chat_model, 'next-model');
});

test('model draft provider switch is preserved while the previous model saves', { timeout: 3000 }, async t => {
  const h = await setupProviderFooter(t);
  h.edit('gChatModel', 'submitted-model');
  const gate = holdModelRequest(t, (url, options) => url === '/api/providers' && options.method === 'POST');
  const saving = h.settings.saveGlobalConfig();
  await gate.started;
  h.dom.gProvider.value = 'gemini'; h.dom.gProvider.dispatchEvent({ type: 'change' });
  h.edit('gApiKey', 'synthetic-gemini-key'); h.edit('gChatModel', 'gemini-draft');
  gate.release(); await saving;
  assert.equal(h.dom.gProvider.value, 'gemini');
  assert.equal(h.dom.gChatModel.value, 'gemini-draft');
  assert.equal(h.dom.gApiKey.value, 'synthetic-gemini-key');
  await h.settings.saveGlobalConfig();
  assert.equal(h.writes().at(-1).body.id, 'gemini');
  assert.equal(h.writes().at(-1).body.chat_model, 'gemini-draft');
});

test('model draft is retained when reopening or refreshing the provider form', async t => {
  const h = await setupProviderFooter(t);
  h.edit('gApiKey', 'synthetic-pending-key');
  await h.settings.loadProviders();
  assert.equal(h.dom.gApiKey.value, 'synthetic-pending-key');
  await h.settings.saveGlobalConfig();
  assert.equal(h.writes()[0].body.api_key, 'synthetic-pending-key');
});

test('a failed provider refresh keeps the cached configuration but reports uncertainty until recovery', async t => {
  const h = await setupProviderFooter(t);
  const originalFetch = globalThis.fetch;
  const active = h.settings.getCurrentActiveProvider();
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/providers' && !options.method) throw new TypeError('synthetic connection failure');
    return originalFetch(url, options);
  };
  await h.settings.loadProviders();
  assert.equal(h.settings.hasProviderReadError(), true);
  assert.equal(h.settings.getCurrentActiveProvider(), active, 'failed reads must not discard the last known provider');
  globalThis.fetch = originalFetch;
  await h.settings.loadProviders();
  assert.equal(h.settings.hasProviderReadError(), false);
  assert.equal(h.settings.getCurrentActiveProvider().id, active.id);
});

test('model draft typed during a provider refresh is retained', { timeout: 3000 }, async t => {
  const h = await setupProviderFooter(t);
  const gate = holdModelRequest(t, (url, options) => url === '/api/providers' && !options.method);
  const loading = h.settings.loadProviders();
  await gate.started;
  h.edit('gChatModel', 'typed-during-refresh');
  gate.release(); await loading;
  assert.equal(h.dom.gChatModel.value, 'typed-during-refresh');
  await h.settings.saveGlobalConfig();
  assert.equal(h.writes()[0].body.chat_model, 'typed-during-refresh');
});

test('model draft reset-to-default URL participates in footer saving', async t => {
  const h = await setupProviderFooter(t);
  h.edit('gBaseUrl', 'https://custom-base.test/v1');
  await h.settings.saveGlobalConfig();
  h.dom.gBtnResetBaseUrl.click();
  await h.settings.saveGlobalConfig();
  assert.equal(h.writes().length, 2);
  assert.equal(h.writes()[1].body.api_base_url, 'https://api.openai.com/v1');
});

test('model draft ignores a stale provider response after a newer save', { timeout: 3000 }, async t => {
  const h = await setupProviderFooter(t);
  const fetch = globalThis.fetch;
  let release, entered, first = true;
  const started = new Promise(resolve => { entered = resolve; });
  const blocked = new Promise(resolve => { release = resolve; });
  t.after(release);
  globalThis.fetch = async (url, options = {}) => {
    const response = await fetch(url, options);
    if (first && url === '/api/providers' && !options.method) {
      first = false; entered(); await blocked;
    }
    return response;
  };
  const oldRefresh = h.settings.loadProviders(); await started;
  h.edit('gChatModel', 'newer-saved-model');
  await h.settings.saveGlobalConfig();
  release(); await oldRefresh;
  assert.equal(h.settings.getCurrentActiveProvider().chat_model, 'newer-saved-model');
  assert.equal(h.dom.gChatModel.value, 'newer-saved-model');
});

test('model draft save buttons stay busy together and recover', { timeout: 3000 }, async t => {
  const h = await setupProviderFooter(t);
  h.edit('gChatModel', 'submitted-model');
  const gate = holdModelRequest(t, (url, options) => url === '/api/providers' && options.method === 'POST');
  const saving = h.settings.handleActivateProvider(); await gate.started;
  const during = [h.dom.gProviderActivate.disabled, h.dom.gTgSave.disabled];
  gate.release(); await saving;
  assert.deepEqual(during, [true, true]);
  assert.equal(h.dom.gProviderActivate.disabled, false);
  assert.equal(h.dom.gTgSave.disabled, false);
});

test('model draft reports partial success when the global config fails', async t => {
  const h = await setupProviderFooter(t);
  h.edit('gChatModel', 'saved-model');
  h.fail('/api/config');
  await h.settings.saveGlobalConfig();
  assert.ok(h.messages().some(message => /模型设置已保存并启用.*全局配置.*失败/.test(message)));
  h.fail('');
  await h.settings.saveGlobalConfig();
  assert.equal(h.writes().length, 1, 'retrying global config must not repeat an already saved model write');
});

test('model draft global edits made during model saving wait for the next save', { timeout: 3000 }, async t => {
  const h = await setupProviderFooter(t);
  h.edit('gChatModel', 'submitted-model'); h.edit('gTgChatId', 'submitted-name');
  const gate = holdModelRequest(t, (url, options) => url === '/api/providers' && options.method === 'POST');
  const saving = h.settings.saveGlobalConfig(); await gate.started;
  h.edit('gTgChatId', 'new-unsaved-name');
  gate.release(); await saving;
  const configs = () => h.calls.filter(call => call.method === 'POST' && call.url === '/api/config');
  assert.equal(configs()[0].body.telegram_chat_id, 'submitted-name');
  assert.equal(h.dom.gTgChatId.value, 'new-unsaved-name');
  assert.ok(h.messages().some(message => /当前修改尚未保存/.test(message)));
  await h.settings.saveGlobalConfig();
  assert.equal(configs().at(-1).body.telegram_chat_id, 'new-unsaved-name');
  assert.equal(h.writes().length, 1);
});

test('footer reports save or unconfirmed activation failures and allows retry', async t => {
  for (const url of ['/api/providers', '/api/providers/openai/activate']) await t.test(url, async t => {
    const h = await setupProviderFooter(t, { confirmActivation: !url.endsWith('/activate') });
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
