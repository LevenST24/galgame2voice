import assert from 'node:assert/strict';
import test from 'node:test';
import {
  initGlobalSettings,
  openGlobalSettings,
  testTelegram,
  handleRestartSovits,
  fetchSystemTelemetry,
  loadGlobalConfig,
  saveGlobalConfig,
  updateTelegramFieldsVisibility,
} from '../frontend/src/controllers/global_settings.js';
import { saveConfirmedConfig } from '../frontend/src/controllers/config_recovery.js';
import { waitForEngine } from '../frontend/src/controllers/engine_recovery.js';
import { createTelegramSettingsController } from '../frontend/src/controllers/telegram_settings.js';
import { createEngineSettingsController } from '../frontend/src/controllers/engine_settings.js';
import { createTelemetrySettingsController } from '../frontend/src/controllers/telemetry_settings.js';
import { initSettingsTabs } from '../frontend/src/settings_ui.js';
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
    gTgTest: element('button'),
    gTgSave: element('button'),
    gTgEnabled: element('input', 'false'),
    gTgChatId: element('input', '12345'),
    gTgToken: element('input', 'test-token'),
    gTgProxyEnabled: element('input', 'false'),
    gTgProxyHost: element('input', '127.0.0.1'),
    gTgProxyPort: element('input', '10809'),
    gTgFieldsGroup: element('div'),
    gParamSpeed: element('input', '1.0'),
    gParamSpeedVal: element('span'),
    gParamTopK: element('input', '15'),
    gParamTopKVal: element('span'),
    gParamTopP: element('input', '1.0'),
    gParamTopPVal: element('span'),
    gParamTemp: element('input', '1.0'),
    gParamTempVal: element('span'),
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

test('Candidate 1: old Telegram test pending -> reinit -> new test pending -> resolve old response: old response cannot leak toast or clear new button busy state', async t => {
  const { toasts } = fixture(t);
  const dom1 = createDom();
  const dom2 = createDom();

  let finishFirstPost;
  let finishSecondPost;
  let postCount = 0;

  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/telegram/test' && options.method === 'POST') {
      postCount++;
      if (postCount === 1) {
        return new Promise(resolve => {
          finishFirstPost = () => resolve(response({ success: true, message: '旧测试成功' }));
        });
      }
      return new Promise(resolve => {
        finishSecondPost = () => resolve(response({ success: true, message: '新测试成功' }));
      });
    }
    return response({});
  };

  initGlobalSettings(dom1);
  const test1Promise = testTelegram();
  await settle();

  assert.equal(postCount, 1, 'First test POST sent');
  assert.equal(dom1.gTgTest.disabled, true, 'First test button disabled while pending');

  initGlobalSettings(dom2);
  const test2Promise = testTelegram();
  await settle();

  assert.equal(postCount, 2, 'Second test POST sent');
  assert.equal(dom2.gTgTest.disabled, true, 'Second test button disabled while pending');

  finishFirstPost();
  await test1Promise;
  await settle();

  assert.equal(dom2.gTgTest.disabled, true, 'Second test button must remain disabled while still pending');
  assert.ok(!toasts.some(msg => msg.includes('旧测试成功')), 'Old test response must not leak toast');

  finishSecondPost();
  await test2Promise;
  await settle();

  assert.equal(dom2.gTgTest.disabled, false, 'Second test button re-enabled after its own completion');
  assert.ok(toasts.some(msg => msg.includes('新测试成功')), 'New test shows its success toast');
});

test('Candidate 2: old restart POST pending -> reinit -> new explicit restart: old restart does not block new restart, nor does old progress write into new DOM', async t => {
  const { toasts } = fixture(t);
  const dom1 = createDom();
  const dom2 = createDom();

  let finishFirstRestart;
  let finishSecondRestart;
  let restartCount = 0;

  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/system/restart_sovits' && options.method === 'POST') {
      restartCount++;
      if (restartCount === 1) {
        return new Promise(resolve => {
          finishFirstRestart = () => resolve(response({ message: '旧引擎正在启动' }));
        });
      }
      return new Promise(resolve => {
        finishSecondRestart = () => resolve(response({ message: '新引擎正在启动' }));
      });
    }
    if (url.endsWith('/engine-status')) {
      return response({ state: 'ready', ready: true, message: '引擎已就绪' });
    }
    if (url === '/api/config') {
      return response({ status: 'ok', settings: { inference_precision: 'fp16' } });
    }
    return response({});
  };

  initGlobalSettings(dom1);
  const restart1Promise = handleRestartSovits();
  await settle();

  assert.equal(restartCount, 1, 'First restart POST sent');
  assert.equal(dom1.gBtnRestartSovits.disabled, true, 'First restart button disabled');

  initGlobalSettings(dom2);
  const restart2Promise = handleRestartSovits();
  await settle();

  finishFirstRestart();
  await restart1Promise;
  await settle();

  assert.equal(restartCount, 2, 'Second restart POST sent and not blocked by old restart');
  assert.equal(dom2.gBtnRestartSovits.disabled, true, 'Second restart button disabled while pending');

  finishSecondRestart();
  await restart2Promise;
  await settle();

  assert.equal(dom2.gBtnRestartSovits.disabled, false, 'Second restart button restored on completion');
});

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

test('Telegram: editing form fields during test invalidates test and prevents toast/button pollution', async t => {
  const { toasts } = fixture(t);
  const dom = createDom();

  let finishTest;
  globalThis.fetch = async (url) => {
    if (url === '/api/telegram/test') {
      return new Promise(resolve => {
        finishTest = () => resolve(response({ success: true, message: '迟到的成功提示' }));
      });
    }
    return response({});
  };

  initGlobalSettings(dom);
  const testPromise = testTelegram();
  await settle();

  assert.equal(dom.gTgTest.disabled, true);

  // Edit token field while test is in flight
  dom.gTgToken.value = 'new-token';
  await dom.gTgToken.dispatch('input');
  await settle();

  // Resolve old test
  finishTest();
  await testPromise;
  await settle();

  assert.ok(!toasts.some(msg => msg.includes('迟到的成功提示')), 'Invalidated test must not show toast');
});

test('Telegram: validation blocks save and shows guidance when enabled without chat ID', async t => {
  const { toasts } = fixture(t);
  const dom = createDom();

  globalThis.fetch = async (url) => {
    if (url === '/api/config') {
      return response({ status: 'ok', settings: { telegram_enabled: false } });
    }
    if (url === '/api/providers') return response({ providers: [], presets: [] });
    return response({});
  };

  initGlobalSettings(dom);
  await loadGlobalConfig();
  await settle();

  dom.gTgEnabled.checked = true;
  dom.gTgChatId.value = '   ';

  await saveGlobalConfig();
  await settle();

  assert.ok(toasts.some(msg => msg.includes('填写允许访问的用户ID')), 'Validation error toast shown');
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

test('initSettingsTabs: cleanup function removes event listeners from tabs', async t => {
  fixture(t);
  const root = element('div');
  const tab1 = element('button');
  tab1.dataset.gtab = 't1';
  const tab2 = element('button');
  tab2.dataset.gtab = 't2';
  root.querySelectorAll = () => [tab1, tab2];

  let selections = 0;
  const cleanup = initSettingsTabs(root, () => { selections++; });

  await tab1.click();
  assert.equal(selections, 1);

  // Invoke cleanup
  cleanup();

  await tab1.click();
  await tab2.click();
  assert.equal(selections, 1, 'Listeners removed after cleanup');
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

test('Telegram edit invalidation: editing input enables immediate retry and does not permanently lock button', async t => {
  fixture(t);
  const dom = createDom();
  const releases = [];
  globalThis.fetch = async (url) => {
    if (url === '/api/telegram/test') {
      return new Promise(resolve => releases.push(resolve));
    }
    return response({});
  };

  initGlobalSettings(dom);
  const oldTask = testTelegram();
  await settle();

  assert.equal(releases.length, 1);
  assert.equal(dom.gTgTest.disabled, true);

  dom.gTgToken.value = 'synthetic-edited';
  await dom.gTgToken.dispatch('input');
  await settle();

  assert.equal(dom.gTgTest.disabled, false, 'Button re-enabled after input edit invalidation');

  releases[0](response({ success: true, message: 'old' }));
  await oldTask;
  await settle();

  assert.equal(dom.gTgTest.disabled, false, 'Button remains enabled after old task settles');

  const newTask = testTelegram();
  await settle();

  assert.equal(releases.length, 2, 'Second test request dispatched');
  assert.equal(dom.gTgTest.disabled, true, 'Second test button busy');

  releases[1](response({ success: true, message: 'new' }));
  await newTask;
  await settle();

  assert.equal(dom.gTgTest.disabled, false, 'Second test button restored on success');
});

test('Engine restart queue: disposing unsent restart skips POST without poisoning subsequent config mutations', async t => {
  fixture(t);
  let releaseConfig;
  let restartPosts = 0;
  let configPosts = 0;

  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/config' && options.method === 'POST') {
      configPosts++;
      if (configPosts === 1) return new Promise(resolve => {
        releaseConfig = () => resolve(response({ settings: { inference_precision: 'cpu' } }));
      });
      return response({ settings: JSON.parse(options.body) });
    }
    if (url === '/api/system/restart_sovits' && options.method === 'POST') {
      restartPosts++;
      return response({ message: 'synthetic acknowledgement' });
    }
    return response({});
  };

  const dom = createDom();
  initGlobalSettings(dom);

  const config = saveConfirmedConfig({ inference_precision: 'cpu' });
  await settle();

  const queued = handleRestartSovits('cpu');
  await settle();

  assert.equal(restartPosts, 0, 'Restart initially queued behind config POST');

  // Reinit/dispose the engine controller while restart is still unsent
  const newDom = createDom();
  initGlobalSettings(newDom);

  const currentConfig = saveConfirmedConfig({ inference_precision: 'fp32' });
  releaseConfig();

  const results = await Promise.allSettled([config, queued, currentConfig]);

  assert.equal(restartPosts, 0, 'Disposed controller must not send still-unsent queued restart POST');
  assert.equal(results[2].status, 'fulfilled', 'Skipping retired unsent operation must not poison current-owner write queue');
  assert.equal(configPosts, 2, 'Current owner config POST must still be sent');
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

test('Same-controls Telegram busy disposal/reinit: synchronous release on disposal enables retry; old completion does not unlock new pending test', async t => {
  const { toasts } = fixture(t);
  const releases = [];
  globalThis.fetch = async (url) => {
    if (url === '/api/telegram/test') {
      return new Promise(resolve => releases.push(resolve));
    }
    return response({});
  };

  const dom = createDom();
  initGlobalSettings(dom);

  const oldTask = testTelegram();
  await settle();
  assert.equal(releases.length, 1, 'First Telegram test started');
  assert.equal(dom.gTgTest.disabled, true, 'First test disables button');

  // Reinit using the exact SAME dom controls
  initGlobalSettings(dom);
  assert.equal(dom.gTgTest.disabled, false, 'Disposing old controller synchronously restores button for new owner');

  const newTask = testTelegram();
  await settle();
  assert.equal(releases.length, 2, 'Second test started on same controls');
  assert.equal(dom.gTgTest.disabled, true, 'Second test re-disables button');

  // Resolve old test
  releases[0](response({ success: true, message: 'OLD test completed' }));
  await oldTask;
  await settle();

  // Late old finally must not unlock new pending operation or leak toast
  assert.equal(dom.gTgTest.disabled, true, 'Button remains disabled while new test is still pending');
  assert.equal(toasts.length, 0, 'Old completion does not leak toast to current view');

  // Resolve new test
  releases[1](response({ success: true, message: 'NEW test completed' }));
  await newTask;
  await settle();

  assert.equal(dom.gTgTest.disabled, false, 'Button re-enabled after new test completes');
  assert.ok(toasts.some(m => m.includes('NEW test completed')), 'New test result delivered');
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
