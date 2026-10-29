import assert from 'node:assert/strict';
import test from 'node:test';
import { fetchSystemTelemetry, initGlobalSettings, openGlobalSettings } from '../frontend/src/controllers/global_settings.js';
import { openModal, closeModal } from '../frontend/src/settings_ui.js';

const settle = () => new Promise(resolve => setImmediate(resolve));
const response = data => ({ ok: true, status: 200, json: async () => data });
function element() {
  const classes = new Set(['hidden']);
  return { textContent: '', hidden: true, inert: false,
    classList: { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x) },
    querySelectorAll: () => [], querySelector: () => null,
  };
}
globalThis.document = { getElementById: () => null, activeElement: null };
globalThis.localStorage = { getItem: () => null, setItem() {} };

test('an open settings page follows engine startup and stops refreshing when closed', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const dom = { globalModal: element(), gDashSovitsBadge: element(), gDashSovitsStatus: element() };
  let ready = false;
  let reads = 0;
  globalThis.fetch = async (url, options = {}) => {
    assert.ok(!options.method || options.method === 'GET', 'status refresh must not write configuration');
    if (url.endsWith('/engine-status')) {
      reads++;
      return response({ state: ready ? 'ready' : 'starting', ready, message: ready ? '引擎已就绪' : '正在加载模型' });
    }
    return response(url.endsWith('/system/status') ? { gpt_sovits: { status: ready ? 'reachable' : 'unreachable' } } : {});
  };
  initGlobalSettings(dom, { openModal, closeModal });
  t.after(() => closeModal(dom.globalModal));
  openGlobalSettings();
  await settle();
  assert.equal(dom.gDashSovitsBadge.textContent, '启动中');
  assert.equal(reads, 1);
  ready = true;
  t.mock.timers.tick(5000);
  await settle();
  assert.equal(dom.gDashSovitsBadge.textContent, '在线');
  assert.equal(dom.gDashSovitsStatus.textContent, '引擎已就绪');
  assert.equal(reads, 2);
  closeModal(dom.globalModal);
  t.mock.timers.tick(20000);
  await settle();
  assert.equal(reads, 2, 'closed settings must not poll');
  openGlobalSettings();
  await settle();
  assert.equal(reads, 3, 'reopening refreshes immediately');
});

test('closing during a pending refresh cannot restart polling or overwrite a reopened page', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const dom = { globalModal: element(), gDashSovitsBadge: element(), gDashSovitsStatus: element() };
  let finishOld;
  let reads = 0;
  globalThis.fetch = async url => {
    if (url.endsWith('/engine-status')) {
      if (++reads === 1) return new Promise(resolve => { finishOld = () => resolve(response({ state: 'starting', ready: false, message: '旧状态' })); });
      return response({ state: 'ready', ready: true, message: '引擎已就绪' });
    }
    return response(url.endsWith('/system/status') ? { gpt_sovits: { status: 'reachable' } } : {});
  };
  initGlobalSettings(dom, { openModal, closeModal });
  t.after(() => closeModal(dom.globalModal));
  openGlobalSettings(); await settle();
  closeModal(dom.globalModal);
  openGlobalSettings(); await settle();
  finishOld(); await settle();
  assert.equal(dom.gDashSovitsBadge.textContent, '在线');
  t.mock.timers.tick(5000); await settle();
  assert.equal(reads, 3, 'only the current page owns a refresh timer');
});

test('closing settings aborts every pending telemetry read', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const dom = { globalModal: element() }, pending = [];
  globalThis.fetch = async (url, options = {}) => {
    if (['/api/system/status', '/api/cache/stats', '/api/system/engine-status'].includes(url)) {
      return new Promise(resolve => pending.push({ signal: options.signal, finish: () => resolve(response({})) }));
    }
    return response({});
  };
  initGlobalSettings(dom, { openModal, closeModal });
  t.after(async () => { closeModal(dom.globalModal); pending.forEach(item => item.finish()); await settle(); });
  openGlobalSettings(); await settle();
  assert.equal(pending.length, 3);
  closeModal(dom.globalModal); await settle();
  assert.ok(pending.every(item => item.signal.aborted), 'closing must cancel reads, not just the next timer');
});

test('old cache and uptime responses cannot overwrite a reopened settings page', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const dom = { globalModal: element(), gDashCacheVal: element(), gDashUptimeVal: element() };
  let old = true;
  const finish = [];
  globalThis.fetch = async url => {
    if (url === '/api/system/status' || url === '/api/cache/stats') {
      const data = url.endsWith('/stats') ? { total_entries: old ? 1 : 2, total_size_mb: old ? 1 : 2 }
        : { app: { pid: old ? 111 : 222, uptime_seconds: 20 } };
      if (old) return new Promise(resolve => finish.push(() => resolve(response(data))));
      return response(data);
    }
    return response({});
  };
  initGlobalSettings(dom, { openModal, closeModal });
  t.after(async () => { closeModal(dom.globalModal); finish.forEach(resolve => resolve()); await settle(); });
  openGlobalSettings(); await settle();
  closeModal(dom.globalModal); old = false;
  openGlobalSettings(); await settle();
  assert.match(dom.gDashCacheVal.textContent, /^2 个文件/);
  assert.match(dom.gDashUptimeVal.textContent, /PID: 222/);
  finish.forEach(resolve => resolve()); await settle();
  assert.match(dom.gDashCacheVal.textContent, /^2 个文件/);
  assert.match(dom.gDashUptimeVal.textContent, /PID: 222/);
});

test('engine readiness renders before a slow storage diagnostics response', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const dom = { globalModal: element(), gDashSovitsBadge: element(), gDashSovitsStatus: element() };
  let finish;
  globalThis.fetch = async url => {
    if (url === '/api/system/status') return new Promise(resolve => { finish = () => resolve(response({ gpt_sovits: { status: 'unreachable' } })); });
    if (url === '/api/system/engine-status') return response({ state: 'ready', ready: true, message: '已独立确认就绪' });
    return response({});
  };
  initGlobalSettings(dom, { openModal, closeModal });
  t.after(async () => { finish?.(); await settle(); closeModal(dom.globalModal); });
  openGlobalSettings(); await settle();
  assert.equal(dom.gDashSovitsBadge.textContent, '在线');
  assert.equal(dom.gDashSovitsStatus.textContent, '已独立确认就绪');
  finish(); await settle();
  assert.equal(dom.gDashSovitsBadge.textContent, '在线', 'late general diagnostics must not replace the engine result');
});

test('a refresh arriving after the modal closed does not start new telemetry reads', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const dom = { globalModal: element() };
  let reads = 0;
  globalThis.fetch = async url => { if (url.includes('/api/')) reads++; return response({}); };
  initGlobalSettings(dom, { openModal, closeModal });
  await fetchSystemTelemetry();
  assert.equal(reads, 0);
});

test('backgrounding the page aborts reads and foregrounding refreshes once', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const dom = { globalModal: element() }, listeners = new Map(), pending = [];
  const originalAdd = document.addEventListener, originalRemove = document.removeEventListener;
  document.hidden = false;
  document.addEventListener = (event, callback) => listeners.set(event, callback);
  document.removeEventListener = (event, callback) => { if (listeners.get(event) === callback) listeners.delete(event); };
  globalThis.fetch = async (url, options = {}) => {
    if (['/api/system/status', '/api/cache/stats', '/api/system/engine-status'].includes(url))
      return new Promise(resolve => pending.push({ signal: options.signal, finish: () => resolve(response({})) }));
    return response({});
  };
  initGlobalSettings(dom, { openModal, closeModal });
  t.after(async () => {
    closeModal(dom.globalModal); pending.forEach(item => item.finish()); await settle();
    document.hidden = false; initGlobalSettings({}, {});
    document.addEventListener = originalAdd; document.removeEventListener = originalRemove;
  });
  openGlobalSettings(); await settle();
  assert.equal(pending.length, 3);
  document.hidden = true; listeners.get('visibilitychange')?.(); await settle();
  assert.ok(pending.every(item => item.signal.aborted));
  t.mock.timers.tick(20000); await settle();
  assert.equal(pending.length, 3);
  document.hidden = false; listeners.get('visibilitychange')?.(); await settle();
  assert.equal(pending.length, 6, 'foregrounding should immediately start exactly one new batch');
});
