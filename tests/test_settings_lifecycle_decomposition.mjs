import assert from 'node:assert/strict';
import test from 'node:test';
import { initGlobalSettings, loadGlobalConfig } from '../frontend/src/controllers/global_settings.js';
import { waitForEngine } from '../frontend/src/controllers/engine_recovery.js';
import { createEngineSettingsController } from '../frontend/src/controllers/engine_settings.js';
import { initEngineDirectory } from '../frontend/src/controllers/engine_directory.js';

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
    style: {},
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
      if (tag === 'select' && children.length > 0) {
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
    dispatch: (name = 'input', extra = {}) => Promise.all(
      (listeners[name] || []).map(fn => fn({ target: el, ...extra }))
    ),
    click: () => el.dispatch('click'),
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
    setAttribute: () => {},
    getAttribute: () => null,
    scrollIntoView: () => {},
    closest: () => null,
  };
  return el;
}

function createDom() {
  const tabs = element('div');
  const inferenceTab = element('button');
  inferenceTab.dataset.gtab = 'inference';
  const modelsTab = element('button');
  modelsTab.dataset.gtab = 'models';
  tabs.querySelectorAll = () => [inferenceTab, modelsTab];
  tabs.querySelector = (sel) => {
    if (sel.includes('inference')) return inferenceTab;
    if (sel.includes('models')) return modelsTab;
    return null;
  };

  return {
    globalModal: element('div'),
    gTabs: tabs,
    gBtnRefreshStatus: element('button'),
    gParamSovitsUrl: element('input', 'http://127.0.0.1:9880'),
    gPrecision: element('select', 'fp16'),
    gBtnRestartSovits: element('button'),
    gBtnTogglePrecision: element('button'),
    gBtnTogglePrecisionText: element('span'),
    gDashPrecisionBadge: element('span'),
    gDashPrecisionVal: element('span'),
    gDashDeviceVal: element('span'),
    gDashHardwareVal: element('span'),
    gDashUptimeVal: element('span'),
    gDashCacheVal: element('span'),
    gDashSovitsBadge: element('span'),
    gDashSovitsStatus: element('span'),
    gDashSovitsUrl: element('span'),
    gBtnClearCache: element('button'),
    gBtnGoPrecision: element('button'),
    gBtnGoSovitsSetup: element('button'),
    gSovitsDirectory: element('input', ''),
    gSovitsDirectoryStatus: element('span'),
    gBrowseSovitsDirectory: element('button'),
    gSaveSovitsDirectory: element('button'),
    gTgSave: element('button'),
  };
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
    createElement: tag => element(tag),
    activeElement: null,
  };
  globalThis.localStorage = { getItem: () => null, setItem: () => {} };

  t.after(() => {
    delete globalThis.document;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.confirm;
  });

  return { toasts };
}

test('Candidate 3: reinit same controls does not stack coordinator or controller listeners', async t => {
  fixture(t);
  const dom = createDom();

  let telemetryReads = 0;
  let configReads = 0;
  let tabSelectCount = 0;

  globalThis.fetch = async (url) => {
    if (url.endsWith('/engine-status') || url.endsWith('/system/status') || url.endsWith('/cache/stats')) {
      telemetryReads++;
      return response({});
    }
    if (url === '/api/config') {
      configReads++;
      return response({ status: 'ok', settings: {} });
    }
    if (url === '/api/providers') {
      return response({ providers: [], presets: [] });
    }
    return response({});
  };

  initGlobalSettings(dom, {
    onSettingsTabChange: () => { tabSelectCount++; },
  });

  initGlobalSettings(dom, {
    onSettingsTabChange: () => { tabSelectCount++; },
  });

  telemetryReads = 0;
  configReads = 0;
  await dom.gBtnRefreshStatus.click();
  await settle();

  assert.equal(configReads, 1, 'loadGlobalConfig triggered exactly once per click');

  tabSelectCount = 0;
  await dom.gTabs.querySelectorAll()[0].click();
  await settle();

  assert.equal(tabSelectCount, 1, 'Tab change callback triggered exactly once per click');
});

test('Engine: precision change auto-saves with revision guard', async t => {
  const { toasts } = fixture(t);
  const dom = createDom();
  const savedPrecision = [];

  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/config' && options.method === 'POST') {
      const payload = JSON.parse(options.body);
      savedPrecision.push(payload.inference_precision);
      return response({});
    }
    if (url === '/api/config') return response({ status: 'ok', settings: {} });
    if (url === '/api/providers') return response({ providers: [], presets: [] });
    return response({});
  };

  initGlobalSettings(dom);
  dom.gPrecision.value = 'cpu';
  await dom.gPrecision.dispatch('change');
  await settle();

  assert.deepEqual(savedPrecision, ['cpu']);
  assert.ok(toasts.some(msg => msg.includes('CPU 稳定模式')), 'Auto-save toast displayed');
});

test('Engine: clear cache confirms, clears local caches, and calls server endpoint', async t => {
  const { toasts } = fixture(t);
  const dom = createDom();
  let clearEndpointCalled = false;
  let confirmAsked = false;

  globalThis.confirm = () => {
    confirmAsked = true;
    return true;
  };

  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/cache/clear' && options.method === 'POST') {
      clearEndpointCalled = true;
      return response({ deleted_files: 42, freed_mb: 128.5 });
    }
    if (url.endsWith('/engine-status')) return response({ state: 'ready', ready: true });
    return response({});
  };

  initGlobalSettings(dom);
  await dom.gBtnClearCache.click();
  await settle();

  assert.equal(confirmAsked, true, 'Confirmation dialog prompted');
  assert.equal(clearEndpointCalled, true, 'Clear endpoint POST issued');
  assert.ok(toasts.some(msg => msg.includes('删除 42 个文件')), 'Success toast reported freed space');
});

test('initEngineDirectory: dispose cleans up input and button listeners', async () => {
  const dom = {
    gSovitsDirectory: element('input', ''),
    gSovitsDirectoryStatus: element('span'),
    gBrowseSovitsDirectory: element('button'),
    gSaveSovitsDirectory: element('button'),
  };

  let notifications = 0;
  const controller = initEngineDirectory(dom, () => { notifications++; });

  dom.gSovitsDirectory.value = '   ';
  await dom.gSaveSovitsDirectory.click();
  assert.equal(notifications, 1, 'Empty directory notified before dispose');

  controller.dispose();

  notifications = 0;
  await dom.gSaveSovitsDirectory.click();
  assert.equal(notifications, 0, 'No notification after dispose');
});

test('Config load scope: retired config GET does not overwrite newer coordinator view', async t => {
  fixture(t);
  const replies = [];
  globalThis.fetch = async (url) => {
    if (url === '/api/config') {
      return new Promise(resolve => replies.push(resolve));
    }
    return response({});
  };

  const oldDom = createDom();
  initGlobalSettings(oldDom);
  const first = loadGlobalConfig();
  await settle();

  const newDom = createDom();
  initGlobalSettings(newDom);
  const second = loadGlobalConfig();
  await settle();

  assert.equal(replies.length, 2, 'Both config GETs started');

  replies[1](response({ settings: { gpt_sovits_url: 'http://new.synthetic.invalid' } }));
  await second;
  await settle();

  replies[0](response({ settings: { gpt_sovits_url: 'http://old.synthetic.invalid' } }));
  await first;
  await settle();

  assert.equal(newDom.gParamSovitsUrl.value, 'http://new.synthetic.invalid', 'Retired GET must not overwrite current coordinator view');
});

test('Engine abort handling: aborting during progress cancels readiness wait immediately with AbortError', async () => {
  globalThis.fetch = async () => response({ state: 'ready', ready: true, message: 'synthetic' });
  const controller = new AbortController();
  await assert.rejects(
    waitForEngine({ signal: controller.signal, onProgress: () => controller.abort() }),
    { name: 'AbortError' },
    'Aborting in progress must reject with AbortError rather than resolving ready'
  );

  // Also test already aborted signal at entry
  const abortedController = new AbortController();
  abortedController.abort();
  await assert.rejects(
    waitForEngine({ signal: abortedController.signal }),
    { name: 'AbortError' },
    'Pre-aborted signal rejects immediately'
  );
});

test('Same-controls Engine & Directory busy disposal/reinit: synchronous release leaves controls usable; old completion preserves new pending operation busy state', async t => {
  fixture(t);
  const dom = createDom();

  // 1. Engine restart on same controls
  {
    let restartRelease;
    globalThis.fetch = async (url, options = {}) => {
      if (url === '/api/system/restart_sovits' && options.method === 'POST') {
        return new Promise(resolve => { restartRelease = resolve; });
      }
      return response({ ready: true, state: 'ready' });
    };

    const oldEngine = createEngineSettingsController(dom);
    const oldRestart = oldEngine.handleRestartSovits('cpu');
    await settle();
    assert.equal(dom.gBtnRestartSovits.disabled, true);
    assert.equal(dom.gBtnTogglePrecision.disabled, true);

    oldEngine.dispose();
    assert.equal(dom.gBtnRestartSovits.disabled, false, 'Disposal releases restart button');
    assert.equal(dom.gBtnTogglePrecision.disabled, false, 'Disposal releases toggle precision button');
    assert.ok(dom.gBtnRestartSovits.innerHTML.includes('重启 SoVITS 引擎'), 'Disposal resets restart button label');

    // New controller starts a new restart on same controls before old drains
    const newEngine = createEngineSettingsController(dom);
    let newRestartRelease;
    globalThis.fetch = async (url, options = {}) => {
      if (url === '/api/system/restart_sovits' && options.method === 'POST') {
        return new Promise(resolve => { newRestartRelease = resolve; });
      }
      return response({ ready: true, state: 'ready' });
    };

    const newRestart = newEngine.handleRestartSovits('fp16');
    await settle();
    assert.equal(dom.gBtnRestartSovits.disabled, true);

    // Resolve old restart
    restartRelease(response({ message: 'old restart ack' }));
    await oldRestart;
    await settle();

    // Late old finally must not unlock new pending restart
    assert.equal(dom.gBtnRestartSovits.disabled, true, 'New restart remains busy after old drains');

    newRestartRelease(response({ message: 'new restart ack' }));
    await newRestart;
    await settle();
    assert.equal(dom.gBtnRestartSovits.disabled, false);
    newEngine.dispose();
  }

  // 2. Cache clear on same controls
  {
    let cacheRelease;
    globalThis.fetch = async (url, options = {}) => {
      if (url === '/api/cache/clear' && options.method === 'POST') {
        return new Promise(resolve => { cacheRelease = resolve; });
      }
      return response({});
    };
    globalThis.confirm = () => true;

    const oldEngine = createEngineSettingsController(dom);
    const oldCache = oldEngine.handleClearCache();
    await settle();
    assert.equal(dom.gBtnClearCache.disabled, true);

    oldEngine.dispose();
    assert.equal(dom.gBtnClearCache.disabled, false, 'Disposal releases cache clear button');

    const newEngine = createEngineSettingsController(dom);
    cacheRelease(response({ deleted_files: 0, freed_mb: 0 }));
    await oldCache;
    await settle();
    assert.equal(dom.gBtnClearCache.disabled, false, 'Button remains idle and usable');
    newEngine.dispose();
  }

  // 3. Directory browse on same controls
  {
    let browseRelease;
    globalThis.fetch = async (url, options = {}) => {
      if (url === '/api/voice/browse-file' && options.method === 'POST') {
        return new Promise(resolve => { browseRelease = resolve; });
      }
      return response({});
    };

    const oldDir = initEngineDirectory(dom, () => {});
    const oldBrowse = oldDir.browse();
    await settle();
    assert.equal(dom.gBrowseSovitsDirectory.disabled, true);

    oldDir.dispose();
    assert.equal(dom.gBrowseSovitsDirectory.disabled, false, 'Disposal releases browse button');

    const newDir = initEngineDirectory(dom, () => {});
    browseRelease(response({ selected_path: null }));
    await oldBrowse;
    await settle();
    assert.equal(dom.gBrowseSovitsDirectory.disabled, false, 'Browse button remains idle after old settles');
    newDir.dispose();
  }

  // 4. Directory save on same controls
  {
    let saveRelease;
    globalThis.fetch = async (url, options = {}) => {
      if (url === '/api/system/sovits-directory' && options.method === 'PUT') {
        return new Promise(resolve => { saveRelease = resolve; });
      }
      return response({});
    };

    dom.gSovitsDirectory.value = 'D:/GPT-SoVITS';
    const oldDir = initEngineDirectory(dom, () => {});
    const oldSave = oldDir.save();
    await settle();
    assert.equal(dom.gSaveSovitsDirectory.disabled, true);

    oldDir.dispose();
    assert.equal(dom.gSaveSovitsDirectory.disabled, false, 'Disposal releases save button');

    const newDir = initEngineDirectory(dom, () => {});
    saveRelease(response({ directory: 'D:/GPT-SoVITS' }));
    await oldSave;
    await settle();
    assert.equal(dom.gSaveSovitsDirectory.disabled, false, 'Save button remains idle after old settles');
    newDir.dispose();
  }
});
