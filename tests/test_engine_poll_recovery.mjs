import assert from 'node:assert/strict';
import test from 'node:test';
import { waitForEngine } from '../frontend/src/controllers/engine_recovery.js';
import { initGlobalSettings, handleRestartSovits, fetchSystemTelemetry } from '../frontend/src/controllers/global_settings.js';

const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
const ready = { state: 'ready', ready: true, message: '语音引擎已就绪。' };
const tick = () => new Promise(resolve => setImmediate(resolve));

for (const fault of ['disconnect', 503, 429]) test(`poll recovers from ${fault} without restarting`, async () => {
  let reads = 0;
  const progress = [];
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/system/engine-status');
    assert.ok(!options.method || options.method === 'GET');
    if (++reads === 1) {
      if (fault === 'disconnect') throw new TypeError('Failed to fetch');
      return response({ detail: '测试：状态暂时无法读取' }, fault);
    }
    return response(ready);
  };
  const result = await waitForEngine({ timeoutMs: 250, intervalMs: 1, onProgress: value => progress.push(value) });
  assert.equal(result.ready, true);
  assert.deepEqual(progress.map(value => value.state), ['checking', 'ready']);
  assert.equal(reads, 2);
});

test('an invalid status cannot be shown as startup progress', async () => {
  let reads = 0;
  const states = [];
  globalThis.fetch = async () => response(++reads === 1 ? {} : ready);
  assert.equal((await waitForEngine({ timeoutMs: 250, intervalMs: 1, onProgress: value => states.push(value.state) })).ready, true);
  assert.deepEqual(states, ['checking', 'ready']);
});

test('a server-side process change keeps its specific checking message', async () => {
  let reads = 0;
  const progress = [];
  globalThis.fetch = async () => response(++reads === 1 ? {
    state: 'checking', ready: false, message: '引擎进程已变更，正在重新核对服务状态。',
  } : ready);
  await waitForEngine({ timeoutMs: 250, intervalMs: 1, onProgress: value => progress.push(value) });
  assert.match(progress[0].message, /进程已变更/);
  assert.equal(progress[1].ready, true);
});

test('persistent status disconnection ends with an uncertain result, not a failed engine', async () => {
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  const result = await waitForEngine({ timeoutMs: 20, intervalMs: 1 });
  assert.equal(result.ready, false);
  assert.equal(result.state, 'checking');
  assert.match(result.message, /无法确认/);
  assert.match(result.message, /刷新状态/);
});

test('a hanging status read is bounded by the remaining overall deadline', async () => {
  let signal;
  globalThis.fetch = async (_url, options) => { signal = options.signal; return new Promise(() => {}); };
  const started = performance.now();
  const result = await waitForEngine({ timeoutMs: 25, intervalMs: 1 });
  assert.ok(performance.now() - started < 500, 'a short deadline must not wait for a full 10 second read');
  assert.equal(signal.aborted, true);
  assert.equal(result.state, 'checking');
});

for (const status of [400, 401, 403, 404]) test(`HTTP ${status} stops polling without retry`, async () => {
  let reads = 0;
  globalThis.fetch = async () => { reads++; return response({ detail: '测试：需要用户处理' }, status); };
  await assert.rejects(waitForEngine({ timeoutMs: 250, intervalMs: 1 }), error => error.status === status);
  assert.equal(reads, 1);
});

for (const state of ['failed', 'unavailable']) test(`confirmed ${state} remains actionable`, async () => {
  let reads = 0;
  globalThis.fetch = async () => { reads++; return response({ state, ready: false, message: '请选择完整引擎包' }); };
  await assert.rejects(waitForEngine({ timeoutMs: 250, intervalMs: 1 }), /完整引擎包/);
  assert.equal(reads, 1);
});

test('a transient read failure does not hide a subsequently confirmed engine failure', async () => {
  let reads = 0;
  globalThis.fetch = async () => {
    if (++reads === 1) throw new TypeError('Failed to fetch');
    return response({ state: 'failed', ready: false, message: '请重新选择完整引擎目录' });
  };
  await assert.rejects(waitForEngine({ timeoutMs: 250, intervalMs: 1 }), /完整引擎目录/);
  assert.equal(reads, 2);
});

test('explicit cancellation is not retried', async () => {
  let reads = 0;
  globalThis.fetch = async () => { reads++; throw new DOMException('操作已取消', 'AbortError'); };
  await assert.rejects(waitForEngine({ timeoutMs: 250, intervalMs: 1 }), { name: 'AbortError' });
  assert.equal(reads, 1);
});

function element() {
  return { value: 'fp16', textContent: '', innerHTML: '', disabled: false, dataset: {}, addEventListener() {} };
}

globalThis.localStorage = { getItem: () => null, setItem() {} };
for (const fault of ['disconnect', 500]) test(`lost restart ${fault} response checks status without resending or claiming restart success`, async t => {
  const toasts = [];
  const root = { querySelectorAll: () => [], appendChild(el) {
    toasts.push(el.textContent);
    queueMicrotask(() => clearTimeout(el._timer));
  } };
  globalThis.document = { getElementById: id => id === 'toastRoot' ? root : null, createElement: element };
  const dom = { gBtnRestartSovits: element(), gBtnTogglePrecision: element(), gPrecision: element(),
    gDashSovitsBadge: element(), gDashSovitsStatus: element() };
  initGlobalSettings(dom);
  let restarts = 0;
  let reads = 0;
  globalThis.fetch = async (url, options = {}) => {
    if (url.endsWith('/restart_sovits')) {
      assert.equal(options.method, 'POST');
      restarts++;
      if (fault === 'disconnect') throw new TypeError('Failed to fetch');
      return response({ detail: '测试：重启响应丢失' }, fault);
    }
    if (url.endsWith('/engine-status')) { reads++; return response(ready); }
    return response({});
  };
  await handleRestartSovits('fp32'); await tick();
  assert.equal(restarts, 1);
  assert.ok(reads >= 1, 'read actual readiness after a lost POST response');
  assert.equal(dom.gDashSovitsBadge.textContent, '在线');
  assert.ok(toasts.some(value => /重启.*未确认/.test(value) && /当前引擎.*在线/.test(value)), toasts.join('\n'));
  assert.equal(toasts.length, 1, 'status card and busy button already report progress; avoid stacked recovery toasts');
  assert.ok(toasts.every(value => !value.includes('启动.bat')), 'a lost POST response alone does not require restarting G2V');
  assert.equal(dom.gPrecision.value, 'fp16', 'an unconfirmed restart must not publish the requested precision');
  assert.equal(dom.gBtnRestartSovits.disabled, false);
  t.after(tick);
});

for (const status of [401, 403]) test(`a rejected status read with HTTP ${status} does not claim engine failure`, async t => {
  globalThis.document = { getElementById: () => null };
  const dom = { gBtnRestartSovits: element(), gDashSovitsBadge: element(), gDashSovitsStatus: element() };
  initGlobalSettings(dom);
  globalThis.fetch = async url => url.endsWith('/restart_sovits') ? response({ status: 'starting' })
    : url.endsWith('/engine-status') ? response({ detail: '需要访问令牌' }, status) : response({});
  await handleRestartSovits(); await tick();
  assert.equal(dom.gDashSovitsBadge.textContent, '待确认');
  assert.match(dom.gDashSovitsStatus.textContent, /访问令牌/);
  assert.equal(dom.gBtnRestartSovits.disabled, false);
  t.after(tick);
});

test('the status card displays the address attached to its engine result', async () => {
  globalThis.document = { getElementById: () => null };
  const dom = { gDashSovitsBadge: element(), gDashSovitsStatus: element(), gDashSovitsUrl: element() };
  initGlobalSettings(dom);
  globalThis.fetch = async url => response(url.endsWith('/engine-status')
    ? { ...ready, base_url: 'http://127.0.0.1:19231' }
    : url.endsWith('/system/status') ? { gpt_sovits: { status: 'unreachable', base_url: 'http://127.0.0.1:19230' } } : {});
  await fetchSystemTelemetry();
  assert.equal(dom.gDashSovitsBadge.textContent, '在线');
  assert.equal(dom.gDashSovitsUrl.textContent, 'http://127.0.0.1:19231');
});

test('engine polling updates its displayed address even without the full telemetry response', async t => {
  globalThis.document = { getElementById: () => null };
  const dom = { gBtnRestartSovits: element(), gDashSovitsBadge: element(),
    gDashSovitsStatus: element(), gDashSovitsUrl: element() };
  initGlobalSettings(dom);
  dom.gDashSovitsUrl.textContent = 'http://127.0.0.1:19230';
  globalThis.fetch = async url => response(url.endsWith('/engine-status')
    ? { ...ready, base_url: 'http://127.0.0.1:19231' } : {});
  await handleRestartSovits(); await tick();
  assert.equal(dom.gDashSovitsUrl.textContent, 'http://127.0.0.1:19231');
  t.after(tick);
});
