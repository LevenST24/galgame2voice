import assert from 'node:assert/strict';
import test from 'node:test';
import {
  initGlobalSettings,
  openGlobalSettings,
  handleProviderTest,
  handleActivateProvider,
  saveGlobalConfig,
  loadGlobalConfig,
  onProviderChange,
  loadProviders,
} from '../frontend/src/controllers/global_settings.js';
import { openModal, closeModal } from '../frontend/src/settings_ui.js';

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
    checked: false,
    hidden: true,
    inert: false,
    type: tag === 'input' ? 'text' : '',
    children,
    dataset: {},
    focusCalls: 0,
    scrollCalls: 0,
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
        return selected ? selected.value : (children[0]?.value || _value);
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
    appendChild(child) {
      children.push(child);
      if (tag === 'select' && child.selected) {
        for (const c of children) {
          if (c !== child) c.selected = false;
        }
      }
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    focus() {
      el.focusCalls += 1;
      if (globalThis.document) globalThis.document.activeElement = el;
    },
    scrollIntoView() {
      el.scrollCalls += 1;
    },
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
    hidden: false,
  };
  globalThis.localStorage = { getItem: () => null, setItem: () => {} };

  const allowCheckbox = element('input');
  allowCheckbox.type = 'checkbox';
  allowCheckbox.checked = false;

  const activateBtn = element('button');
  activateBtn.innerHTML = '<span>保存并启用</span>';

  const testBtn = element('button');
  testBtn.innerHTML = '<span>测试连通性</span>';

  const dom = {
    globalModal: element('div'),
    gProvider: element('select', 'openai'),
    gBaseUrl: element('input', 'https://api.openai.com/v1'),
    gChatModel: element('input', 'gpt-4o'),
    gApiKey: element('input', ''),
    gReasoningEffort: element('select', ''),
    gCustomName: element('input', ''),
    gCustomNameField: element('div'),
    gAllowPrivateEndpoints: allowCheckbox,
    gPrivateEndpointsStatus: element('small'),
    gProviderTest: testBtn,
    gProviderActivate: activateBtn,
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
    gParamSpeed: element('input', '1'),
    gAudioRetention: element('input', '30'),
  };

  initGlobalSettings(dom, { openModal, closeModal });
  t.after(() => {
    closeModal(dom.globalModal);
    globalThis.document = { getElementById: () => null, createElement: tag => element(tag), activeElement: null };
  });

  return { dom, toasts };
}

test('blocks handleProviderTest locally for custom provider with loopback or private URLs when gAllowPrivateEndpoints is unchecked', async t => {
  const testRequests = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url.includes('/api/providers/test')) {
      testRequests.push({ url, body: JSON.parse(options.body) });
      return response({ success: false, error: 'HTTP 400 private endpoint forbidden' }, 400);
    }
    if (url === '/api/providers') {
      return response({
        providers: [
          { id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o', api_base_url: 'https://api.openai.com/v1' },
        ],
        presets: [],
      });
    }
    if (url === '/api/config') {
      return response({ status: 'ok', settings: { allow_private_llm_endpoints: false } });
    }
    return response({});
  };

  const { dom, toasts } = fixture(t);
  openModal(dom.globalModal);
  await loadProviders();
  await loadGlobalConfig();

  assert.equal(dom.gAllowPrivateEndpoints.checked, false);

  // 1. Selecting '__custom__' prefills http://127.0.0.1:11434/v1
  dom.gProvider.value = '__custom__';
  onProviderChange();
  assert.equal(dom.gBaseUrl.value, 'http://127.0.0.1:11434/v1');

  await handleProviderTest();

  assert.equal(testRequests.length, 0, 'must not send /api/providers/test when private endpoints are disabled');
  assert.equal(dom.gAllowPrivateEndpoints.checked, false, 'must never auto-check security toggle');
  assert.equal(dom.gAllowPrivateEndpoints.focusCalls, 1, 'must focus gAllowPrivateEndpoints');
  assert.equal(globalThis.document.activeElement, dom.gAllowPrivateEndpoints);
  assert.equal(dom.gAllowPrivateEndpoints.scrollCalls, 1, 'must scroll gAllowPrivateEndpoints into view');
  assert.equal(dom.gProviderTest.disabled, false, 'test button remains clickable');
  assert.equal(dom.gProviderTestResult.classList.contains('hidden'), false, 'diagnostic box must be shown');
  assert.match(dom.gProviderTestResult.className, /test-result-error/);
  assert.equal(dom.gTestResultTitle.textContent, '尚未开启本机 / 局域网连接权限');
  assert.equal(dom.gTestResultLatency.textContent, '未发送');
  assert.match(dom.gTestResultBody.textContent, /允许连接本机或局域网的模型服务/);
  assert.match(dom.gPrivateEndpointsStatus.textContent, /请先勾选此项/);
  assert.ok(toasts.some(msg => msg.includes('允许连接本机或局域网的模型服务')));

  // 2. Also intercept 'custom' across localhost, .local, RFC1918, CGNAT, and IPv6 loopback/private/mapped URLs
  dom.gProvider.value = 'custom';
  onProviderChange();
  const privateUrls = [
    'http://localhost:8000/v1',
    'http://ollama.localhost:8000/v1',
    'http://ollama.local:11434/v1',
    'http://0.0.0.0:11434/v1',
    'http://192.168.1.20:8000/v1',
    'http://10.0.0.8:8000/v1',
    'http://169.254.10.20:8000/v1',
    'http://172.16.5.4:8000/v1',
    'http://100.64.1.2:8000/v1',
    'http://[::1]:11434/v1',
    'http://[::]:11434/v1',
    'http://[fe80::1]:8000/v1',
    'http://[fd00::1]:8000/v1',
    'http://[::ffff:127.0.0.1]:8000/v1',
  ];

  for (const url of privateUrls) {
    dom.gAllowPrivateEndpoints.focusCalls = 0;
    dom.gProviderTestResult.className = 'test-result-box hidden';
    dom.gBaseUrl.value = url;
    await handleProviderTest();
    assert.equal(testRequests.length, 0, `must block private URL: ${url}`);
    assert.equal(dom.gAllowPrivateEndpoints.checked, false, `must not auto-check for ${url}`);
    assert.equal(dom.gAllowPrivateEndpoints.focusCalls, 1, `must focus checkbox for ${url}`);
    assert.equal(dom.gProviderTestResult.classList.contains('hidden'), false, `must show result box for ${url}`);
  }
});

test('blocks handleActivateProvider locally when gAllowPrivateEndpoints is unchecked', async t => {
  const writeRequests = [];
  globalThis.fetch = async (url, options = {}) => {
    if (options.method === 'POST') {
      writeRequests.push({ url, body: JSON.parse(options.body) });
      return response({
        provider: { id: 'custom', name: '自定义模型', api_base_url: 'http://127.0.0.1:11434/v1', chat_model: 'qwen3:latest', is_active: true },
      });
    }
    if (url === '/api/providers') {
      return response({
        providers: [
          { id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o', api_base_url: 'https://api.openai.com/v1' },
        ],
        presets: [],
      });
    }
    if (url === '/api/config') {
      return response({ status: 'ok', settings: { allow_private_llm_endpoints: false, speed_factor: 1 } });
    }
    return response({});
  };

  const { dom, toasts } = fixture(t);
  openModal(dom.globalModal);
  await loadProviders();
  await loadGlobalConfig();

  dom.gProvider.value = '__custom__';
  onProviderChange();
  dom.gChatModel.value = 'qwen3:latest';
  await dom.gChatModel.dispatch('input');

  const activateResult = await handleActivateProvider();
  assert.equal(activateResult, false, 'handleActivateProvider must return false when intercepted');
  assert.equal(writeRequests.length, 0, 'must not POST /api/providers');
  assert.equal(dom.gAllowPrivateEndpoints.checked, false, 'must not auto-check checkbox');
  assert.equal(dom.gAllowPrivateEndpoints.focusCalls, 1, 'must focus checkbox on activate block');
  assert.equal(dom.gProviderActivate.disabled, false, 'activate button unlocked after interception');
  assert.match(dom.gProviderActivate.innerHTML, /保存并启用/, 'activate button text restored');
  assert.equal(dom.gProviderTestResult.classList.contains('hidden'), false);
  assert.ok(toasts.some(msg => msg.includes('请先勾选「允许连接本机或局域网的模型服务」后再保存启用')));
});

test('blocks footer saveGlobalConfig locally without saving provider or config when gAllowPrivateEndpoints is unchecked', async t => {
  const writeRequests = [];
  globalThis.fetch = async (url, options = {}) => {
    if (options.method === 'POST') {
      writeRequests.push({ url, body: JSON.parse(options.body) });
      return response({
        status: 'ok',
        settings: { allow_private_llm_endpoints: false, speed_factor: 1.2 },
        provider: { id: 'custom', name: '自定义模型', api_base_url: 'http://127.0.0.1:11434/v1', chat_model: 'qwen3:latest', is_active: true },
      });
    }
    if (url === '/api/providers') {
      return response({
        providers: [
          { id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o', api_base_url: 'https://api.openai.com/v1' },
        ],
        presets: [],
      });
    }
    if (url === '/api/config') {
      return response({ status: 'ok', settings: { allow_private_llm_endpoints: false, speed_factor: 1 } });
    }
    return response({});
  };

  const { dom, toasts } = fixture(t);
  openModal(dom.globalModal);
  await loadProviders();
  await loadGlobalConfig();

  dom.gProvider.value = '__custom__';
  onProviderChange();
  dom.gChatModel.value = 'qwen3:latest';
  await dom.gChatModel.dispatch('input');
  dom.gParamSpeed.value = '1.2';
  await dom.gParamSpeed.dispatch('input');

  const footerResult = await saveGlobalConfig();
  assert.notEqual(footerResult, true, 'footer save must not succeed');
  assert.equal(writeRequests.length, 0, 'footer save must not POST /api/providers or /api/config');
  assert.equal(dom.gAllowPrivateEndpoints.checked, false, 'must not auto-check checkbox on footer save');
  assert.equal(dom.gAllowPrivateEndpoints.focusCalls, 1, 'must focus checkbox on footer save');
  assert.equal(dom.gTgSave.disabled, false, 'footer save button unlocked');
  assert.equal(dom.gProviderActivate.disabled, false, 'activate button unlocked');
  assert.equal(dom.gProviderTestResult.classList.contains('hidden'), false);
  assert.ok(
    toasts.some(msg => msg.includes('请先勾选「允许连接本机或局域网的模型服务」后再保存启用；其他设置也还没有保存')),
    `expected footer warning toast, got: ${JSON.stringify(toasts)}`,
  );
});

test('checking gAllowPrivateEndpoints dismisses interception box and allows requests, while public custom URLs are never blocked', async t => {
  const requests = [];
  let allowPrivateSaved = false;
  let storedProviders = [
    { id: 'openai', name: 'OpenAI', is_active: true, chat_model: 'gpt-4o', api_base_url: 'https://api.openai.com/v1' },
  ];

  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    if (url.includes('/api/providers/test') && method === 'POST') {
      const body = JSON.parse(options.body);
      requests.push({ kind: 'test', body });
      return response({ success: true, latency_ms: 12, models: [body.chat_model] });
    }
    if (url === '/api/providers' && method === 'POST') {
      const body = JSON.parse(options.body);
      requests.push({ kind: 'save_provider', body });
      const saved = {
        id: body.id,
        name: body.name,
        api_base_url: body.api_base_url,
        chat_model: body.chat_model,
        is_active: true,
      };
      storedProviders = [
        ...storedProviders.filter(p => p.id !== saved.id).map(p => ({ ...p, is_active: false })),
        saved,
      ];
      return response({ provider: saved });
    }
    if (url === '/api/config' && method === 'POST') {
      const body = JSON.parse(options.body);
      requests.push({ kind: 'save_config', body });
      if (typeof body.allow_private_llm_endpoints === 'boolean') {
        allowPrivateSaved = body.allow_private_llm_endpoints;
      }
      return response({ status: 'ok', settings: { allow_private_llm_endpoints: allowPrivateSaved } });
    }
    if (url === '/api/providers') {
      return response({
        providers: storedProviders,
        presets: [],
      });
    }
    if (url === '/api/config') {
      return response({ status: 'ok', settings: { allow_private_llm_endpoints: allowPrivateSaved } });
    }
    return response({});
  };

  const { dom } = fixture(t);
  openModal(dom.globalModal);
  await loadProviders();
  await loadGlobalConfig();

  // 1. Public custom endpoint works even when gAllowPrivateEndpoints is unchecked
  dom.gProvider.value = 'custom';
  onProviderChange();
  dom.gBaseUrl.value = 'https://openrouter.ai/api/v1';
  await dom.gBaseUrl.dispatch('input');
  dom.gChatModel.value = 'deepseek/deepseek-chat';
  await dom.gChatModel.dispatch('input');

  assert.equal(dom.gAllowPrivateEndpoints.checked, false);
  await handleProviderTest();
  assert.equal(requests.filter(r => r.kind === 'test').length, 1, 'public custom URL test is allowed');

  const publicActivated = await handleActivateProvider();
  assert.equal(publicActivated, true, 'public custom URL save/activate is allowed');
  assert.equal(requests.filter(r => r.kind === 'save_provider').length, 1);

  // 2. Local URL is first intercepted, then user checks gAllowPrivateEndpoints -> hides error box & allows test/save
  requests.length = 0;
  dom.gBaseUrl.value = 'http://127.0.0.1:11434/v1';
  await dom.gBaseUrl.dispatch('input');
  dom.gChatModel.value = 'qwen3:latest';
  await dom.gChatModel.dispatch('input');

  await handleProviderTest();
  assert.equal(requests.length, 0, 'blocked while unchecked');
  assert.equal(dom.gProviderTestResult.classList.contains('hidden'), false, 'error box visible after block');

  // User checks #gAllowPrivateEndpoints
  dom.gAllowPrivateEndpoints.checked = true;
  await dom.gAllowPrivateEndpoints.dispatch('change');

  assert.equal(dom.gProviderTestResult.classList.contains('hidden'), true, 'checking toggle hides old interception error box');
  assert.equal(requests.filter(r => r.kind === 'save_config').length, 1, 'coordinator autosaves allow_private_llm_endpoints');
  assert.match(dom.gPrivateEndpointsStatus.textContent, /已保存/, 'coordinator status text is preserved');

  await handleProviderTest();
  assert.equal(requests.filter(r => r.kind === 'test').length, 1, 'test proceeds after user enables private endpoints');
  assert.equal(dom.gTestResultTitle.textContent, '连通性测试通过');

  const localActivated = await handleActivateProvider();
  assert.equal(localActivated, true, 'activate proceeds after user enables private endpoints');
  assert.equal(requests.filter(r => r.kind === 'save_provider').length, 1);
});
