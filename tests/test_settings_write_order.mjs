import assert from 'node:assert/strict';
import test from 'node:test';
import { initGlobalSettings, loadGlobalConfig, saveGlobalConfig, handleRestartSovits } from '../frontend/src/controllers/global_settings.js';
import { saveConfirmedConfig } from '../frontend/src/controllers/config_recovery.js';

const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
function element(value = '') {
  const listeners = {};
  return { value, textContent: '', innerHTML: '', disabled: false, children: [],
    addEventListener(name, handler) { (listeners[name] ||= []).push(handler); },
    dispatch(name = 'change') { return Promise.all((listeners[name] || []).map(handler => handler({ target: this }))); },
    appendChild(child) { this.children.push(child); },
  };
}
globalThis.document = { getElementById: () => null, createElement: () => element() };
globalThis.localStorage = { getItem: () => null, setItem() {} };

function fixture(t, write, extraDom = {}) {
  const dom = { gPrecision: element('fp16'), gBtnRestartSovits: element(), gBtnTogglePrecision: element(), ...extraDom };
  let stored = 'fp16';
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    if (options.method === 'POST') {
      const payload = JSON.parse(options.body);
      calls.push({ url, payload });
      return write(url, payload, value => { stored = value; });
    }
    if (url === '/api/config') return response({ status: 'ok', settings: { inference_precision: stored } });
    if (url.endsWith('/engine-status')) return response({ state: 'ready', ready: true, message: '测试引擎已就绪' });
    if (url === '/api/providers') return response({ providers: [], presets: [] });
    return response({});
  };
  initGlobalSettings(dom);
  t.after(tick);
  return { dom, calls, stored: () => stored };
}

test('a late autosave cannot overwrite the final precision selection', async t => {
  let finishFirst;
  let writes = 0;
  const state = fixture(t, (_url, payload, persist) => {
    if (++writes === 1) return new Promise(resolve => {
      finishFirst = () => { persist(payload.inference_precision); resolve(response({})); };
    });
    persist(payload.inference_precision);
    return response({});
  });
  state.dom.gPrecision.value = 'cpu';
  const first = state.dom.gPrecision.dispatch(); await tick();
  state.dom.gPrecision.value = 'fp32';
  const second = state.dom.gPrecision.dispatch(); await tick();
  finishFirst();
  await Promise.all([first, second]);
  assert.equal(state.stored(), 'fp32');
  assert.equal(state.dom.gPrecision.value, 'fp32');
});

test('restart waits for prior autosaves rather than being overwritten by them', async t => {
  let finishSave;
  const order = [];
  const state = fixture(t, (url, payload, persist) => {
    if (url === '/api/config') return new Promise(resolve => {
      finishSave = () => { order.push('save'); persist(payload.inference_precision); resolve(response({})); };
    });
    order.push('restart');
    persist(payload.precision);
    return response({ status: 'starting' });
  });
  await loadGlobalConfig();
  state.dom.gPrecision.value = 'cpu';
  const saving = state.dom.gPrecision.dispatch(); await tick();
  const restarting = handleRestartSovits('fp32'); await tick();
  finishSave();
  await Promise.all([saving, restarting]);
  assert.deepEqual(order, ['save', 'restart']);
  assert.equal(state.stored(), 'fp32');
});

test('a selection made while restart is pending remains the next saved preference', async t => {
  let finishRestart;
  const state = fixture(t, (url, payload, persist) => {
    if (url.endsWith('/restart_sovits')) return new Promise(resolve => {
      finishRestart = () => { persist(payload.precision); resolve(response({ status: 'starting' })); };
    });
    persist(payload.inference_precision);
    return response({});
  });
  await loadGlobalConfig();
  const restarting = handleRestartSovits('fp32'); await tick();
  state.dom.gPrecision.value = 'cpu';
  const saving = state.dom.gPrecision.dispatch(); await tick();
  finishRestart();
  await Promise.all([restarting, saving]);
  assert.equal(state.dom.gPrecision.value, 'cpu');
  assert.equal(state.stored(), 'cpu');
});

test('an unconfirmed save stops queued writes and restart; a new explicit save can recover', async t => {
  const failures = [];
  let unavailable = true;
  const state = fixture(t, (_url, payload, persist) => {
    if (unavailable) return new Promise((_, reject) => { failures.push(() => reject(new TypeError('Failed to fetch'))); });
    persist(payload.inference_precision);
    return response({});
  });
  await loadGlobalConfig();
  state.dom.gPrecision.value = 'cpu';
  const first = state.dom.gPrecision.dispatch(); await tick();
  state.dom.gPrecision.value = 'fp32';
  const second = state.dom.gPrecision.dispatch();
  const restarting = handleRestartSovits(); await tick();
  // The pre-fix implementation issued three concurrent writes; release all of
  // them without leaving timers behind before checking the actual request count.
  unavailable = false;
  failures.forEach(fail => fail());
  await Promise.all([first, second, restarting]);
  assert.equal(state.calls.length, 1);
  assert.equal(state.dom.gPrecision.value, 'fp32');
  await saveGlobalConfig();
  assert.equal(state.stored(), 'fp32');
  assert.equal(state.calls.length, 2);
});

test('a committed config write with an HTTP 500 response is checked once without replay', async () => {
  const methods = [];
  globalThis.fetch = async (_url, options = {}) => {
    methods.push(options.method || 'GET');
    return options.method === 'POST' ? response({ detail: '测试：响应阶段出错' }, 500)
      : response({ settings: { inference_precision: 'cpu' } });
  };
  const saved = await saveConfirmedConfig({ inference_precision: 'cpu' });
  assert.equal(saved.settings.inference_precision, 'cpu');
  assert.deepEqual(methods, ['POST', 'GET']);
});

test('footer save reserves its config order while the model saves, preserving newer drafts', async t => {
  let finishProvider;
  let submittedModel;
  const state = fixture(t, (url, payload, persist) => {
    if (url === '/api/providers') {
      submittedModel = payload.chat_model;
      return new Promise(resolve => {
        finishProvider = () => resolve(response({ provider: { ...payload, is_active: true } }));
      });
    }
    persist(payload.inference_precision);
    return response({});
  }, { gProvider: element('custom'), gProviderActivate: element(),
    gBaseUrl: element('https://synthetic.invalid/v1'), gChatModel: element('model-at-click'), gApiKey: element() });
  await loadGlobalConfig();
  await state.dom.gChatModel.dispatch('input');
  const footer = saveGlobalConfig(); await tick();
  state.dom.gChatModel.value = 'newer-unsaved-model';
  await state.dom.gChatModel.dispatch('input');
  state.dom.gPrecision.value = 'cpu';
  const autosave = state.dom.gPrecision.dispatch(); await tick();
  finishProvider();
  await Promise.all([footer, autosave]);
  assert.equal(submittedModel, 'model-at-click');
  assert.equal(state.stored(), 'cpu');
  assert.equal(state.dom.gPrecision.value, 'cpu');
  assert.equal(state.dom.gChatModel.value, 'newer-unsaved-model');
});

test('a skipped global save still reports an already started model save accurately', async t => {
  let failFirst, finishProvider;
  const toasts = [];
  const state = fixture(t, (url, payload) => {
    if (url === '/api/providers') return new Promise(resolve => {
      finishProvider = () => resolve(response({ provider: { ...payload, is_active: true } }));
    });
    return new Promise((_, reject) => { failFirst = () => reject(new TypeError('Failed to fetch')); });
  }, { gProvider: element('custom'), gProviderActivate: element(),
    gBaseUrl: element('https://synthetic.invalid/v1'), gChatModel: element('synthetic-model'), gApiKey: element() });
  const root = { querySelectorAll: () => [], appendChild(el) {
    toasts.push(el.textContent);
    // Observe real toast text without leaving expiry timers running after the test.
    queueMicrotask(() => clearTimeout(el._timer));
  } };
  globalThis.document.getElementById = id => id === 'toastRoot' ? root : null;
  globalThis.document.createElement = () => ({ ...element(), dataset: {} });
  t.after(() => {
    globalThis.document.getElementById = () => null;
    globalThis.document.createElement = () => element();
  });
  await loadGlobalConfig();
  state.dom.gPrecision.value = 'cpu';
  const autosave = state.dom.gPrecision.dispatch(); await tick();
  await state.dom.gChatModel.dispatch('input');
  const footer = saveGlobalConfig(); await tick();
  state.dom.gChatModel.value = 'newer-unsaved-draft';
  await state.dom.gChatModel.dispatch('input');
  failFirst(); await autosave; await tick();
  assert.equal(state.dom.gProviderActivate.disabled, true, 'wait for the model outcome before declaring completion');
  finishProvider(); await footer;
  assert.equal(state.calls.filter(call => call.url === '/api/config').length, 1);
  assert.ok(toasts.some(message => /模型设置已保存并启用；全局配置保存失败/.test(message)), toasts.join('\n'));
  assert.equal(state.dom.gChatModel.value, 'newer-unsaved-draft');
});
