import assert from 'node:assert/strict';
import test from 'node:test';
import {
  initGlobalSettings,
  openGlobalSettings,
  handleProviderTest,
  onProviderChange,
  loadProviders,
} from '../frontend/src/controllers/global_settings.js';
import { openModal, closeModal } from '../frontend/src/settings_ui.js';

const settle = () => new Promise(resolve => setImmediate(resolve));
const response = (data, status = 200) => ({
  ok: status < 400,
  status,
  json: async () => data,
});

function element(tag = 'div', initialValue = '') {
  const classes = new Set(['hidden']);
  const listeners = {};
  let _value = initialValue;
  let _innerHTML = '';
  const children = [];

  const el = {
    tag,
    textContent: '',
    disabled: false,
    hidden: true,
    inert: false,
    type: 'text',
    children,
    dataset: {},
    get className() {
      return [...classes].join(' ');
    },
    set className(val) {
      classes.clear();
      if (val) val.split(/\s+/).filter(Boolean).forEach(c => classes.add(c));
    },
    get innerHTML() {
      return _innerHTML;
    },
    set innerHTML(val) {
      _innerHTML = String(val);
      if (val === '') children.length = 0;
    },
    get value() {
      if (tag === 'select') {
        const selected = children.find(c => c.selected);
        return selected ? selected.value : (children[0]?.value || '');
      }
      return _value;
    },
    set value(v) {
      _value = String(v);
      if (tag === 'select') {
        for (const c of children) c.selected = (c.value === String(v));
      }
    },
    classList: {
      add: c => classes.add(c),
      remove: c => classes.delete(c),
      toggle: (c, force) => (force ?? !classes.has(c)) ? classes.add(c) : classes.delete(c),
      contains: c => classes.has(c),
    },
    addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); },
    dispatch: (name = 'input') => Promise.all((listeners[name] || []).map(fn => fn({ target: el }))),
    appendChild: function(child) {
      children.push(child);
      if (tag === 'select' && child.selected) {
        for (const c of children) {
          if (c !== child) c.selected = false;
        }
      }
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    focus: () => {},
    select: () => {},
  };
  return el;
}

function fixture(t) {
  const toasts = [];
  const toastRoot = {
    querySelectorAll: () => [],
    appendChild(el) {
      toasts.push(el.textContent);
      queueMicrotask(() => clearTimeout(el._timer));
    },
  };

  globalThis.document = {
    getElementById: id => (id === 'toastRoot' ? toastRoot : null),
    createElement: tag => {
      const el = element(tag);
      if (tag === 'option') el.selected = false;
      return el;
    },
    activeElement: null,
  };
  globalThis.localStorage = { getItem: () => null, setItem: () => {} };

  const dom = {
    globalModal: element('div'),
    gProvider: element('select', 'openai'),
    gBaseUrl: element('input', 'https://api.openai.com/v1'),
    gChatModel: element('input', 'gpt-4o'),
    gApiKey: element('input', 'sk-openai-key'),
    gReasoningEffort: element('select', ''),
    gCustomName: element('input', ''),
    gProviderTest: element('button'),
    gProviderTestResult: element('div'),
    gTestResultTitle: element('div'),
    gTestResultLatency: element('div'),
    gTestResultBody: element('div'),
    gProviderActiveBadge: element('div'),
    gProviderActive: element('div'),
    gProviderDesc: element('div'),
    gProviderKeyTag: element('div'),
    gKeyStatusText: element('div'),
    gApiKeyTip: element('div'),
    gBtnResetBaseUrl: element('button'),
    gChatModelSelect: element('select'),
  };

  initGlobalSettings(dom, { openModal, closeModal });
  t.after(() => {
    closeModal(dom.globalModal);
    globalThis.document = { getElementById: () => null, createElement: tag => element(tag), activeElement: null };
  });

  return { dom, toasts };
}

test('cross-provider switch invalidates in-flight test and prevents result/toast/button pollution', async t => {
  let resolveTest;
  const testPromise = new Promise(resolve => { resolveTest = resolve; });
  t.after(() => resolveTest?.(response({})));

  globalThis.fetch = async url => {
    if (url.includes('/api/providers/test')) return testPromise;
    if (url === '/api/providers') {
      return response({
        providers: [
          { id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o' },
          { id: 'deepseek', name: 'DeepSeek', is_active: false, chat_model: 'deepseek-chat' },
        ],
        presets: [],
      });
    }
    return response({});
  };

  const { dom, toasts } = fixture(t);
  openModal(dom.globalModal);
  await loadProviders();

  // 1. Start test on OpenAI
  dom.gProvider.value = 'openai';
  const testing = handleProviderTest();
  assert.equal(dom.gProviderTest.disabled, true, 'button busy during test');

  // 2. User switches dropdown to DeepSeek while OpenAI test is in flight
  dom.gProvider.value = 'deepseek';
  onProviderChange();

  // Switched provider must restore button so DeepSeek can be tested immediately
  assert.equal(dom.gProviderTest.disabled, false, 'switching provider must restore test button availability');
  assert.equal(dom.gProviderTestResult.classList.contains('hidden'), true, 'switching provider resets result box');

  // 3. Stale OpenAI test finishes with failure
  resolveTest(response({ success: false, error: 'Incorrect API key provided' }));
  await testing;

  // 4. Assert: DeepSeek view is NOT polluted by OpenAI's late response
  assert.equal(dom.gProviderTestResult.classList.contains('hidden'), true, 'result box must remain hidden for DeepSeek');
  assert.equal(dom.gTestResultBody.textContent, '', 'result body must not be polluted by OpenAI diagnostic');
  assert.equal(toasts.length, 0, 'no toast should be emitted for an invalidated provider test');
});

test('editing model draft while test is in flight invalidates old test and enables immediate new test', async t => {
  let resolveTest;
  const testPromise = new Promise(resolve => { resolveTest = resolve; });
  t.after(() => resolveTest?.(response({})));

  globalThis.fetch = async url => {
    if (url.includes('/api/providers/test')) return testPromise;
    if (url === '/api/providers') {
      return response({
        providers: [{ id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o' }],
        presets: [],
      });
    }
    return response({});
  };

  const { dom, toasts } = fixture(t);
  openModal(dom.globalModal);
  await loadProviders();

  // 1. Start test with gpt-4o
  dom.gChatModel.value = 'gpt-4o';
  const testing = handleProviderTest();

  // 2. User modifies chat model input during test
  dom.gChatModel.value = 'gpt-4o-mini-edited';
  await dom.gChatModel.dispatch('input');

  // Editing draft must restore test button availability
  assert.equal(dom.gProviderTest.disabled, false, 'editing draft must allow user to immediately test new input');

  // 3. Late test response for old draft arrives
  resolveTest(response({ success: true, latency_ms: 80, models: ['gpt-4o'] }));
  await testing;

  // 4. Old response must not publish outcome for previous draft
  assert.equal(dom.gProviderTestResult.classList.contains('hidden'), true, 'stale test must not unhide result box');
  assert.equal(toasts.length, 0, 'no toast should be emitted for an edited draft');
});

test('editing api_key draft while test is in flight invalidates old test', async t => {
  let resolveTest;
  const testPromise = new Promise(resolve => { resolveTest = resolve; });
  t.after(() => resolveTest?.(response({})));

  globalThis.fetch = async url => {
    if (url.includes('/api/providers/test')) return testPromise;
    if (url === '/api/providers') {
      return response({
        providers: [{ id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o' }],
        presets: [],
      });
    }
    return response({});
  };

  const { dom, toasts } = fixture(t);
  openModal(dom.globalModal);
  await loadProviders();

  // 1. Start test with old key
  dom.gApiKey.value = 'sk-old-wrong-key';
  const testing = handleProviderTest();

  // 2. User pastes new key
  dom.gApiKey.value = 'sk-new-valid-key';
  await dom.gApiKey.dispatch('input');

  assert.equal(dom.gProviderTest.disabled, false, 'key edit enables immediate new test');

  // 3. Stale 401 returns for old key
  resolveTest(response({ success: false, error: '401 Unauthorized: Invalid key' }));
  await testing;

  // 4. Assert: 401 must not be shown to user who already entered new key
  assert.equal(dom.gProviderTestResult.classList.contains('hidden'), true, 'stale 401 must not be published');
  assert.equal(toasts.length, 0, 'no toast for stale key test');
});

test('closing modal while test is in flight invalidates test and prevents toast outside modal', async t => {
  let resolveTest;
  const testPromise = new Promise(resolve => { resolveTest = resolve; });
  t.after(() => resolveTest?.(response({})));

  globalThis.fetch = async url => {
    if (url.includes('/api/providers/test')) return testPromise;
    if (url === '/api/providers') {
      return response({
        providers: [{ id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o' }],
        presets: [],
      });
    }
    return response({});
  };

  const { dom, toasts } = fixture(t);
  openModal(dom.globalModal);
  await loadProviders();

  // 1. Start test
  const testing = handleProviderTest();

  // 2. Close modal while test is in flight
  closeModal(dom.globalModal);

  // 3. Test resolves with success
  resolveTest(response({ success: true, latency_ms: 100 }));
  await testing;

  // 4. Must not pop toast outside modal
  assert.equal(toasts.length, 0, 'closing modal must prevent toast leakage to stage');

  // 5. Reopen modal: must have clean state, not unhidden stale card
  openGlobalSettings();
  await settle();
  assert.equal(dom.gProviderTestResult.classList.contains('hidden'), true, 'reopening starts with clean test result box');
});

test('older test finally cannot clear busy state of a newer test', async t => {
  let resolveTest1, resolveTest2;
  const testPromise1 = new Promise(resolve => { resolveTest1 = resolve; });
  const testPromise2 = new Promise(resolve => { resolveTest2 = resolve; });
  t.after(() => { resolveTest1?.(response({})); resolveTest2?.(response({})); });

  let callCount = 0;
  globalThis.fetch = async url => {
    if (url.includes('/api/providers/test')) {
      callCount++;
      return callCount === 1 ? testPromise1 : testPromise2;
    }
    if (url === '/api/providers') {
      return response({
        providers: [{ id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o' }],
        presets: [],
      });
    }
    return response({});
  };

  const { dom, toasts } = fixture(t);
  openModal(dom.globalModal);
  await loadProviders();

  // 1. Initiate Test 1
  dom.gChatModel.value = 'model-1';
  const test1 = handleProviderTest();

  // 2. User changes model to model-2 and starts Test 2 immediately
  dom.gChatModel.value = 'model-2';
  await dom.gChatModel.dispatch('input');
  const test2 = handleProviderTest();

  assert.equal(dom.gProviderTest.disabled, true, 'Test 2 is currently running and busy');
  assert.match(dom.gProviderTest.innerHTML, /测试中/, 'button shows testing indicator for Test 2');

  // 3. Test 1 finishes first!
  resolveTest1(response({ success: false, error: 'model-1 failed' }));
  await test1;

  // Its finally block MUST NOT clear the busy state of Test 2!
  assert.equal(dom.gProviderTest.disabled, true, 'Test 1 finally must not clear Test 2 busy state');
  assert.match(dom.gProviderTest.innerHTML, /测试中/, 'Test 1 finally must not clear Test 2 innerHTML');

  // 4. Test 2 finishes with success
  resolveTest2(response({ success: true, latency_ms: 50, models: ['model-2'] }));
  await test2;

  // Now Test 2 finishes and properly restores button and renders result
  assert.equal(dom.gProviderTest.disabled, false, 'Test 2 completion restores button');
  assert.equal(dom.gProviderTestResult.classList.contains('hidden'), false, 'Test 2 result is rendered');
  assert.match(dom.gTestResultBody.textContent, /model-2/, 'Test 2 result body rendered');
  assert.equal(toasts.length, 1, 'only Test 2 emitted a toast');
});

test('normal current test success renders result box, latency, and success toast', async t => {
  globalThis.fetch = async url => {
    if (url.includes('/api/providers/test')) {
      return response({ success: true, latency_ms: 45, models: ['gpt-4o', 'gpt-4o-mini'] });
    }
    if (url === '/api/providers') {
      return response({
        providers: [{ id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o' }],
        presets: [],
      });
    }
    return response({});
  };

  const { dom, toasts } = fixture(t);
  openModal(dom.globalModal);
  await loadProviders();

  await handleProviderTest();

  assert.equal(dom.gProviderTestResult.classList.contains('hidden'), false);
  assert.match(dom.gProviderTestResult.className, /test-result-success/);
  assert.equal(dom.gTestResultTitle.textContent, '连通性测试通过');
  assert.equal(dom.gTestResultLatency.textContent, '耗时 45ms');
  assert.match(dom.gTestResultBody.textContent, /2 个可用模型/);
  assert.ok(toasts.some(msg => /连通性测试成功.*45ms/.test(msg)));
});

test('normal current test failure renders error title, guidance, and error toast', async t => {
  globalThis.fetch = async url => {
    if (url.includes('/api/providers/test')) {
      return response({ success: false, error: '401 invalid api key', diagnostic: '请前往控制台检查' });
    }
    if (url === '/api/providers') {
      return response({
        providers: [{ id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o' }],
        presets: [],
      });
    }
    return response({});
  };

  const { dom, toasts } = fixture(t);
  openModal(dom.globalModal);
  await loadProviders();

  await handleProviderTest();

  assert.equal(dom.gProviderTestResult.classList.contains('hidden'), false);
  assert.match(dom.gProviderTestResult.className, /test-result-error/);
  assert.match(dom.gTestResultTitle.textContent, /API Key/);
  assert.match(dom.gTestResultBody.textContent, /请前往控制台检查/);
  assert.ok(toasts.some(msg => /连通性测试未通过/.test(msg)));
});
