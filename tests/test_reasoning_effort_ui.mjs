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
const { createMessageEl, needsModelSettings } = await import('../frontend/src/ui.js');
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
    renderHistory: () => { calls.history++; },
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

test('chat view createMessageEl with empty_reply renders 重新发送 and 去设置', () => {
  const calls = [];
  const msg = {
    id: 'm_empty',
    role: 'user',
    content: '你好',
    ts: 0,
    failed: {
      error: '模型把生成长度用完了，却没有写出回复，通常是思考占满了额度。可以在模型设置里把“思考强度”调低，或在会话设置里调大“最大生成长度”。',
      code: 'empty_reply',
    },
  };

  const { el } = createMessageEl(msg, {
    onRetry: m => calls.push(['retry', m]),
    onOpenModels: () => calls.push(['models']),
  });

  assert.ok(el.classList.contains('has-failure'));
  const note = el.querySelector('.msg-failure');
  assert.ok(note);
  assert.equal(note.attributes.role, 'status');
  assert.ok(note.querySelector('.msg-failure-text').textContent.includes('未收到回复：模型把生成长度用完了'));

  const buttons = el.querySelectorAll('.msg-failure-btn');
  assert.deepEqual(buttons.map(b => b.textContent), ['重新发送', '去设置']);

  buttons.forEach(b => b.click());
  assert.deepEqual(calls, [['retry', msg], ['models']]);
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
