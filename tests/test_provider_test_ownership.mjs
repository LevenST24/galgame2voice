import assert from 'node:assert/strict';
import test from 'node:test';
import {
  initGlobalSettings,
  openGlobalSettings,
  handleProviderTest,
  handleActivateProvider,
  saveGlobalConfig,
  loadGlobalConfig,
  getCurrentActiveProvider,
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
    removeEventListener: (name, fn) => {
      if (listeners[name]) {
        const idx = listeners[name].indexOf(fn);
        if (idx >= 0) listeners[name].splice(idx, 1);
      }
    },
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

function fixture(t, extraCallbacks = {}) {
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
    gProviderActivate: element('button', '保存并启用'),
    gTgSave: element('button', '保存设置'),
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

  initGlobalSettings(dom, { openModal, closeModal, ...extraCallbacks });
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
  assert.ok(toasts.some(msg => /连通性测试成功.*45 ms/.test(msg)));
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

test('calling initGlobalSettings twice with same controls does not issue duplicate test requests on single click', async t => {
  const testCalls = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url.includes('/api/providers/test')) {
      testCalls.push({ url, aborted: options.signal?.aborted });
      return response({ success: true, latency_ms: 40 });
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

  // Re-initialize with the SAME dom controls
  initGlobalSettings(dom, { openModal, closeModal });

  // User clicks the test button ONCE
  await dom.gProviderTest.dispatch('click');

  assert.equal(testCalls.length, 1, 'single click must issue exactly one test request, not duplicate requests from stacked listeners');
});

test('reinit disposes previous controller: events on old DOM cannot mutate new controls or issue tests', async t => {
  const testCalls = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url.includes('/api/providers/test')) {
      testCalls.push({ url, options });
      return response({ success: true, latency_ms: 40 });
    }
    if (url === '/api/providers') {
      return response({
        providers: [{ id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o' }],
        presets: [],
      });
    }
    return response({});
  };

  const fixture1 = fixture(t);
  openModal(fixture1.dom.globalModal);
  await loadProviders();

  const fixture2 = fixture(t);
  openModal(fixture2.dom.globalModal);
  await loadProviders();

  // Now click old button on fixture1
  await fixture1.dom.gProviderTest.dispatch('click');

  assert.equal(testCalls.length, 0, 'disposed old DOM button must not issue tests');
  assert.equal(fixture2.dom.gProviderTest.disabled, false, 'disposed old DOM event must not disable new button');
});

test('reinit disposes previous controller: old in-flight test resolving after reinit cannot mutate new DOM or toasts', async t => {
  let resolveOldTest;
  const oldTestPromise = new Promise(resolve => { resolveOldTest = resolve; });
  t.after(() => resolveOldTest?.(response({})));

  globalThis.fetch = async (url) => {
    if (url.includes('/api/providers/test')) {
      return oldTestPromise;
    }
    if (url === '/api/providers') {
      return response({
        providers: [{ id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o' }],
        presets: [],
      });
    }
    return response({});
  };

  const fixture1 = fixture(t);
  openModal(fixture1.dom.globalModal);
  await loadProviders();

  // Start test on fixture1
  const pending1 = handleProviderTest();
  assert.equal(fixture1.dom.gProviderTest.disabled, true);

  // Re-initialize with new fixture2
  const fixture2 = fixture(t);
  openModal(fixture2.dom.globalModal);
  await loadProviders();

  // Resolve old test with success
  resolveOldTest(response({ success: true, latency_ms: 35, models: ['gpt-4o'] }));
  await pending1;

  // fixture2 DOM and toasts must NOT be mutated by old test
  assert.equal(fixture2.dom.gProviderTestResult.classList.contains('hidden'), true, 'new result box remains hidden');
  assert.equal(fixture2.dom.gTestResultBody.textContent, '', 'new result body remains empty');
  assert.equal(fixture2.toasts.length, 0, 'no toast leaked from old disposed test');
});

test('reinit disposes previous controller: in-flight save resolving with acknowledged success cannot mutate new coordinator', async t => {
  let releaseOldSave;
  const oldSavePromise = new Promise(resolve => { releaseOldSave = resolve; });
  t.after(() => releaseOldSave?.(response({})));

  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/providers' && options.method === 'POST') {
      return oldSavePromise;
    }
    if (url === '/api/providers') {
      return response({
        providers: [
          { id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o', api_base_url: 'https://api.openai.com/v1' },
          { id: 'custom', name: '自定义模型', is_active: false, chat_model: 'old-draft', api_base_url: 'https://synthetic.invalid/v1' },
        ],
        presets: [],
      });
    }
    return response({});
  };

  let badgeUpdates1 = 0;
  let badgeUpdates2 = 0;

  // Initialize fixture 1
  const fixture1 = fixture(t, { updateBadge: () => { badgeUpdates1++; } });
  openModal(fixture1.dom.globalModal);
  await loadProviders();

  // Switch to custom model and start save
  fixture1.dom.gProvider.value = 'custom';
  fixture1.dom.gChatModel.value = 'old-draft-1';
  fixture1.dom.gBaseUrl.value = 'https://synthetic.invalid/v1';
  const pendingSave1 = handleActivateProvider();
  await settle();

  assert.equal(fixture1.dom.gProviderActivate.disabled, true, 'fixture1 activate button busy while saving');

  // Re-initialize with fixture 2 before network response arrives
  const fixture2 = fixture(t, { updateBadge: () => { badgeUpdates2++; } });
  openModal(fixture2.dom.globalModal);
  await loadProviders();

  // Prepare a new draft on fixture 2
  fixture2.dom.gProvider.value = 'openai';
  fixture2.dom.gChatModel.value = 'new-active-draft-2';

  // Now release the old save response with confirmed active provider
  const badgeUpdatesBeforeRelease = badgeUpdates2;
  releaseOldSave(response({
    provider: {
      id: 'custom',
      name: '自定义模型',
      api_base_url: 'https://synthetic.invalid/v1',
      chat_model: 'old-draft-1',
      is_active: true,
    },
  }));
  const saveResult = await pendingSave1;
  await settle();

  // Assert: fixture 2 remained completely owned and untouched by old late response
  assert.equal(saveResult, true, 'underlying server write was acknowledged, write semantics preserved');
  assert.equal(badgeUpdates2, badgeUpdatesBeforeRelease, 'new coordinator badge must not receive callback from disposed save');
  assert.deepEqual(fixture2.toasts, [], 'new coordinator must not receive stale saved-model toast');
  assert.equal(fixture2.dom.gChatModel.value, 'new-active-draft-2', 'new draft must not be overwritten or cleared');
  assert.equal(fixture2.dom.gProviderActivate.disabled, false, 'new coordinator activate button must remain usable');
  assert.equal(fixture2.dom.gTgSave.disabled, false, 'new coordinator footer save button must remain usable');
  assert.notEqual(getCurrentActiveProvider()?.chat_model, 'old-draft-1', 'active provider facade must not reflect old disposed model');
});

test('reinit disposes previous controller: in-flight save rejecting or unconfirmed cannot leak error toast or disable new controls', async t => {
  let rejectOldSave;
  const oldSavePromise = new Promise((_, reject) => { rejectOldSave = reject; });
  t.after(() => rejectOldSave?.(new Error('cleanup')));

  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/providers' && options.method === 'POST') {
      return oldSavePromise;
    }
    if (url === '/api/providers') {
      return response({
        providers: [
          { id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o', api_base_url: 'https://api.openai.com/v1' },
          { id: 'custom', name: '自定义模型', is_active: false, chat_model: 'old-draft', api_base_url: 'https://synthetic.invalid/v1' },
        ],
        presets: [],
      });
    }
    return response({});
  };

  let badgeUpdates2 = 0;
  const fixture1 = fixture(t);
  openModal(fixture1.dom.globalModal);
  await loadProviders();

  fixture1.dom.gProvider.value = 'custom';
  fixture1.dom.gChatModel.value = 'failing-draft';
  fixture1.dom.gBaseUrl.value = 'https://synthetic.invalid/v1';
  const pendingSave1 = handleActivateProvider();
  await settle();

  // Re-initialize with fixture 2
  const fixture2 = fixture(t, { updateBadge: () => { badgeUpdates2++; } });
  openModal(fixture2.dom.globalModal);
  await loadProviders();

  fixture2.dom.gChatModel.value = 'stable-model';

  // Reject the old save with an unconfirmed network failure
  const unconfirmedError = new Error('网络连接中断，无法确认写入结果');
  unconfirmedError.code = 'PROVIDER_UNCONFIRMED';
  rejectOldSave(unconfirmedError);
  const saveResult = await pendingSave1;
  await settle();

  assert.equal(saveResult, false, 'failed save returns false');
  assert.deepEqual(fixture2.toasts, [], 'new coordinator must not receive error toast from disposed save');
  assert.equal(fixture2.dom.gProviderActivate.disabled, false, 'new coordinator activate button must not be disabled');
  assert.equal(fixture2.dom.gTgSave.disabled, false, 'new coordinator footer save button must not be disabled');
  assert.equal(fixture2.dom.gChatModel.value, 'stable-model', 'new coordinator draft preserved');
});

test('reinit disposes previous controller: in-flight footer save cannot leak success toast or clear new busy state', async t => {
  let releaseOldProviderSave;
  let releaseOldConfigSave;
  let releaseNewProviderSave;
  let releaseNewConfigSave;

  const oldProviderPromise = new Promise(resolve => { releaseOldProviderSave = resolve; });
  const oldConfigPromise = new Promise(resolve => { releaseOldConfigSave = resolve; });
  const newProviderPromise = new Promise(resolve => { releaseNewProviderSave = resolve; });
  const newConfigPromise = new Promise(resolve => { releaseNewConfigSave = resolve; });

  t.after(() => {
    releaseOldProviderSave?.(response({}));
    releaseOldConfigSave?.(response({}));
    releaseNewProviderSave?.(response({}));
    releaseNewConfigSave?.(response({}));
  });

  let providerPosts = 0;
  let configPosts = 0;
  let currentActive = { id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o', api_base_url: 'https://api.openai.com/v1' };

  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/providers' && options.method === 'POST') {
      providerPosts++;
      const body = JSON.parse(options.body);
      if (body.chat_model === 'old-footer-model') return oldProviderPromise;
      if (body.chat_model === 'new-footer-model') {
        currentActive = { id: 'custom', name: '自定义模型', is_active: true, chat_model: 'new-footer-model', api_base_url: 'https://custom.api/v1' };
        return newProviderPromise;
      }
      throw new Error(`Unexpected provider save: ${body.chat_model}`);
    }
    if (url === '/api/config' && options.method === 'POST') {
      configPosts++;
      if (configPosts === 1) return oldConfigPromise;
      return newConfigPromise;
    }
    if (url === '/api/providers') {
      return response({
        providers: [
          currentActive.id === 'openai' ? currentActive : { id: 'openai', name: 'OpenAI', is_active: false, chat_model: 'gpt-4o', api_base_url: 'https://api.openai.com/v1' },
          currentActive.id === 'custom' ? currentActive : { id: 'custom', name: '自定义模型', is_active: false, chat_model: 'custom-model', api_base_url: 'https://custom.api/v1' },
        ],
        presets: [],
      });
    }
    if (url === '/api/config') {
      return response({ status: 'ok', settings: { audio_retention_minutes: 30 } });
    }
    return response({});
  };

  // 1. Initialize fixture 1 and load both providers and global config
  const fixture1 = fixture(t);
  openModal(fixture1.dom.globalModal);
  await loadProviders();
  await loadGlobalConfig();

  // Make provider dirty on fixture 1
  fixture1.dom.gProvider.value = 'custom';
  fixture1.dom.gChatModel.value = 'old-footer-model';
  fixture1.dom.gBaseUrl.value = 'https://custom.api/v1';
  await fixture1.dom.gChatModel.dispatch('input');

  // Trigger footer save on fixture 1
  const pendingOldFooterSave = saveGlobalConfig();
  await settle();

  // Assert fixture 1 footer save actually started
  assert.equal(providerPosts, 1, 'fixture 1 provider POST must be observed');
  assert.equal(fixture1.dom.gTgSave.disabled, true, 'fixture 1 footer save button busy');
  assert.equal(fixture1.dom.gProviderActivate.disabled, true, 'fixture 1 activate button busy');

  // 2. Re-initialize with fixture 2 and load its configuration
  let badgeUpdates2 = 0;
  const fixture2 = fixture(t, { updateBadge: () => { badgeUpdates2++; } });
  openModal(fixture2.dom.globalModal);
  await loadProviders();
  await loadGlobalConfig();

  // Make provider dirty on fixture 2
  fixture2.dom.gProvider.value = 'custom';
  fixture2.dom.gChatModel.value = 'new-footer-model';
  fixture2.dom.gBaseUrl.value = 'https://custom.api/v1';
  await fixture2.dom.gChatModel.dispatch('input');

  // Trigger genuine new footer save on fixture 2
  const pendingNewFooterSave = saveGlobalConfig();
  await settle();

  // Assert fixture 2 footer save started and set busy state
  assert.equal(providerPosts, 2, 'fixture 2 provider POST must be observed');
  assert.equal(fixture2.dom.gTgSave.disabled, true, 'new coordinator footer save button must be busy');
  assert.equal(fixture2.dom.gProviderActivate.disabled, true, 'new coordinator activate button must be busy');

  // 3. Release old provider and old config saves while new save is in flight
  releaseOldProviderSave(response({
    provider: { id: 'custom', name: '自定义模型', chat_model: 'old-footer-model', is_active: true },
  }));
  await settle();
  assert.equal(configPosts, 1, 'fixture 1 config POST must be observed after provider save');
  releaseOldConfigSave(response({ status: 'ok', settings: { audio_retention_minutes: 30 } }));
  await pendingOldFooterSave;
  await settle();

  // 4. Assert: new coordinator busy state, label, and newer draft SURVIVE old save completion
  assert.equal(fixture2.dom.gTgSave.disabled, true, 'new coordinator save button must remain busy despite old save completion');
  assert.equal(fixture2.dom.gProviderActivate.disabled, true, 'new coordinator activate button must remain busy');
  assert.equal(fixture2.dom.gChatModel.value, 'new-footer-model', 'new coordinator model draft must not be overwritten or cleared');
  assert.deepEqual(fixture2.toasts, [], 'new coordinator must not receive toast from disposed footer save');
  assert.equal(badgeUpdates2, 1, 'new coordinator badge must not receive extra callback from old save completion');

  // 5. Release new provider and new config saves to verify normal completion of new save
  releaseNewProviderSave(response({
    provider: { id: 'custom', name: '自定义模型', chat_model: 'new-footer-model', is_active: true },
  }));
  await settle();
  assert.equal(configPosts, 2, 'fixture 2 config POST must be observed after its provider save');
  releaseNewConfigSave(response({ status: 'ok', settings: { audio_retention_minutes: 30 } }));
  await pendingNewFooterSave;
  await settle();

  // Assert: new save finishes normally, unlocks buttons, and displays its own toast
  assert.equal(fixture2.dom.gTgSave.disabled, false, 'new coordinator save button unlocked after completion');
  assert.equal(fixture2.dom.gProviderActivate.disabled, false, 'new coordinator activate button unlocked after completion');
  assert.ok(fixture2.toasts.some(t => t.includes('全局配置和模型设置已保存')), 'new coordinator shows success toast');
  assert.equal(getCurrentActiveProvider()?.chat_model, 'new-footer-model', 'current active provider reflects new model');
});

test('reinit disposes previous controller: in-flight save lacking active confirmation does not issue fallback activation after disposal', async t => {
  let releaseOldSave;
  const oldSavePromise = new Promise(resolve => { releaseOldSave = resolve; });
  t.after(() => releaseOldSave?.(response({})));
  let activationCalls = 0;

  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/providers' && options.method === 'POST') {
      return oldSavePromise;
    }
    if (url.includes('/activate')) {
      activationCalls++;
      return response({ active_provider: { id: 'custom', name: '自定义模型' } });
    }
    if (url === '/api/providers') {
      return response({
        providers: [
          { id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o', api_base_url: 'https://api.openai.com/v1' },
          { id: 'custom', name: '自定义模型', is_active: false, chat_model: 'old-draft', api_base_url: 'https://synthetic.invalid/v1' },
        ],
        presets: [],
      });
    }
    return response({});
  };

  const fixture1 = fixture(t);
  openModal(fixture1.dom.globalModal);
  await loadProviders();

  fixture1.dom.gProvider.value = 'custom';
  fixture1.dom.gChatModel.value = 'fallback-draft';
  fixture1.dom.gBaseUrl.value = 'https://synthetic.invalid/v1';
  const pendingSave1 = handleActivateProvider();
  await settle();

  // Re-initialize with fixture 2
  const fixture2 = fixture(t);
  openModal(fixture2.dom.globalModal);
  await loadProviders();

  // Release old save with is_active: false (would normally require fallback activation)
  releaseOldSave(response({
    provider: {
      id: 'custom',
      name: '自定义模型',
      api_base_url: 'https://synthetic.invalid/v1',
      chat_model: 'fallback-draft',
      is_active: false,
    },
  }));
  await pendingSave1;
  await settle();

  assert.equal(activationCalls, 0, 'disposed controller must not issue fallback activation request');
  assert.deepEqual(fixture2.toasts, [], 'no toasts on new coordinator');
});

test('switching provider in dropdown without editing inputs sets providerDirty, saving on footer save; switching back avoids POST', async t => {
  const providerPosts = [];
  const configPosts = [];

  let currentProviders = [
    { id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o', api_base_url: 'https://api.openai.com/v1', api_key: 'sk-****openai' },
    { id: 'deepseek', name: 'DeepSeek', is_active: false, chat_model: 'deepseek-chat', api_base_url: 'https://api.deepseek.com/v1', api_key: 'sk-****1234' },
  ];

  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/providers' && options.method === 'POST') {
      const payload = JSON.parse(options.body);
      providerPosts.push(payload);
      let found = false;
      currentProviders = currentProviders.map(p => {
        if (p.id === payload.id) {
          found = true;
          return { ...p, ...payload, is_active: true };
        }
        return { ...p, is_active: false };
      });
      if (!found) {
        currentProviders.push({ ...payload, is_active: true });
      }
      return response({
        provider: {
          ...payload,
          is_active: true,
        },
      });
    }
    if (url === '/api/config' && options.method === 'POST') {
      configPosts.push(JSON.parse(options.body));
      return response({ status: 'ok', settings: {} });
    }
    if (url === '/api/config') {
      return response({ status: 'ok', settings: { audio_retention_minutes: 30 } });
    }
    if (url === '/api/providers') {
      return response({
        providers: currentProviders,
        presets: [
          { id: 'openai', name: 'OpenAI', default_chat_model: 'gpt-4o', default_base_url: 'https://api.openai.com/v1' },
          { id: 'deepseek', name: 'DeepSeek', default_chat_model: 'deepseek-chat', default_base_url: 'https://api.deepseek.com/v1' },
        ],
      });
    }
    return response({});
  };

  const { dom, toasts } = fixture(t);
  openModal(dom.globalModal);
  await loadGlobalConfig();
  await loadProviders();

  assert.equal(getCurrentActiveProvider()?.id, 'openai');
  assert.equal(dom.gProvider.value, 'openai');

  // 1. User switches dropdown to 'deepseek', but then switches back to original active 'openai' without editing
  dom.gProvider.value = 'deepseek';
  await dom.gProvider.dispatch('change');
  dom.gProvider.value = 'openai';
  await dom.gProvider.dispatch('change');

  // User clicks footer save
  await saveGlobalConfig();
  await settle();

  // Assert: No redundant POST /api/providers request
  assert.equal(providerPosts.length, 0, 'switching to another provider then back to original active provider must not post provider save');
  assert.ok(toasts.some(msg => msg === '全局配置已保存'), 'should show global config saved toast without model change');

  // Clear tracking
  providerPosts.length = 0;
  toasts.length = 0;

  // 2. User switches dropdown to 'deepseek' and triggers change
  dom.gProvider.value = 'deepseek';
  await dom.gProvider.dispatch('change');

  // User does NOT edit any other input, directly clicks footer save
  await saveGlobalConfig();
  await settle();

  // Assert: POST /api/providers was issued for deepseek with is_active: true
  assert.equal(providerPosts.length, 1, 'should have posted provider save for deepseek');
  assert.equal(providerPosts[0].id, 'deepseek');
  assert.equal(providerPosts[0].is_active, true);
  assert.equal(getCurrentActiveProvider()?.id, 'deepseek');
  assert.ok(toasts.some(msg => msg.includes('全局配置和模型设置已保存')), 'should show success toast including model saved');

  // Clear tracking
  providerPosts.length = 0;
  toasts.length = 0;

  // 3. User switches dropdown to 'openai', then immediately back to 'deepseek' (the now-active provider) without editing
  dom.gProvider.value = 'openai';
  await dom.gProvider.dispatch('change');
  dom.gProvider.value = 'deepseek';
  await dom.gProvider.dispatch('change');

  // User clicks footer save
  await saveGlobalConfig();
  await settle();

  // Assert: No redundant POST /api/providers request
  assert.equal(providerPosts.length, 0, 'switching to another provider then back to active provider must not post provider save');
  assert.ok(toasts.some(msg => msg === '全局配置已保存'), 'should show global config saved toast without model change');

  // Clear tracking
  providerPosts.length = 0;
  toasts.length = 0;

  // 4. User switches dropdown to '__custom__' (keyless) and directly clicks footer save
  dom.gProvider.value = '__custom__';
  await dom.gProvider.dispatch('change');

  await saveGlobalConfig();
  await settle();

  // Assert: POST /api/providers was issued for custom with is_active: true
  assert.equal(providerPosts.length, 1, 'should have posted provider save for custom');
  assert.equal(providerPosts[0].id, 'custom');
  assert.equal(providerPosts[0].is_active, true);
  assert.equal(getCurrentActiveProvider()?.id, 'custom');
  assert.ok(toasts.some(msg => msg.includes('全局配置和模型设置已保存')), 'should show success toast including custom model saved');
});

