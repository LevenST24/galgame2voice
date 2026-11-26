import assert from 'node:assert/strict';
import { fakeDocument } from './helpers/adv_dom.mjs';
import { requestJson, getErrorMessage } from '../frontend/src/request.js';
import { streamChat } from '../frontend/src/ai.js';
import { waitForEngine } from '../frontend/src/controllers/engine_recovery.js';
import { saveConfirmedConfig } from '../frontend/src/controllers/config_recovery.js';
import { ensureSessionVoice, initVoiceSettings } from '../frontend/src/controllers/voice_settings.js';
import { initGlobalSettings, loadGlobalConfig, saveGlobalConfig, handleRestartSovits, fetchSystemTelemetry, savePrivateEndpointPreference, handleProviderTest, handleActivateProvider } from '../frontend/src/controllers/global_settings.js';
import { initUpdateManager, loadSystemVersionInfo, handleCheckUpdate } from '../frontend/src/controllers/update_manager.js';

let passed = 0;
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

for (const [data, fallback, expected] of [
  [{ detail: { code: 'BUSY', message: '引擎正在启动，请等待' } }, 'HTTP 409', '引擎正在启动'],
  [{ detail: [{ loc: ['body', 'temperature'], msg: 'invalid' }] }, 'HTTP 422', '配置不完整'],
  [{ detail: 'sqlite3.OperationalError' }, 'HTTP 500', '运行状态'],
]) {
  assert.ok(getErrorMessage(data, fallback).includes(expected)); passed++;
}
// Technical errors containing localized Chinese text must not leak raw internals to users.
assert.equal(
  getErrorMessage({ detail: '[WinError 10061] 由于目标计算机积极拒绝，无法连接。' }, '无法连接对话模型，请检查模型设置。'),
  '无法连接对话模型，请检查模型设置。',
);
assert.equal(
  getErrorMessage(
    { detail: 'Traceback (most recent call last):\n  File "C:\\用户\\语音\\app.py", line 12\nRuntimeError: boom' },
    'HTTP 500',
  ),
  '此次操作未完成，请稍后重试；若仍失败，请打开「运行状态」检查服务。',
);
assert.equal(
  getErrorMessage({ detail: 'TTS synthesis failed with status 500: 内部错误' }, '语音合成暂时失败，请稍后重试。'),
  '语音合成暂时失败，请稍后重试。',
);
assert.equal(
  getErrorMessage({ detail: '磁盘空间不足，请清理后重试：[Errno 28] No space left on device' }, '请求失败'),
  '磁盘空间不足，请清理后重试',
);
assert.equal(
  getErrorMessage({ detail: '模型密钥无效，请在设置中重新填写。' }, '请求失败'),
  '模型密钥无效，请在设置中重新填写。',
);
passed += 5;
{
  let count = 0;
  globalThis.fetch = async () => { count++; throw new TypeError('Failed to fetch'); };
  await assert.rejects(requestJson('/api/config'), /启动.bat/);
  assert.equal(count, 1); passed++;
}
{
  let signal;
  globalThis.fetch = async (_url, options) => { signal = options.signal; return new Promise(() => {}); };
  await assert.rejects(requestJson('/api/config', { method: 'POST' }, { timeoutMs: 5 }), /保存结果可能尚未确认/);
  assert.equal(signal.aborted, true); passed++;
}
{
  globalThis.fetch = async () => ({ ok: true, json: () => new Promise(() => {}) });
  await assert.rejects(requestJson('/api/config', {}, { timeoutMs: 5 }), /超时/); passed++;
}
{
  globalThis.fetch = async () => ({ ok: true, json: async () => { throw new SyntaxError(); } });
  await assert.rejects(requestJson('/api/config'), /无法读取/); passed++;
}
{
  const controller = new AbortController(); controller.abort();
  let count = 0;
  globalThis.fetch = async () => { count++; };
  await assert.rejects(requestJson('/api/config', { signal: controller.signal }), { name: 'AbortError' });
  assert.equal(count, 0); passed++;
}

async function chatScenario(chunks, { hang = false, cancel = false, status = 200 } = {}) {
  let cancelled = 0, released = 0, endings = 0;
  const reader = {
    read: async () => chunks.length ? { done: false, value: new TextEncoder().encode(chunks.shift()) }
      : hang ? new Promise(() => {}) : { done: true },
    cancel: async () => { cancelled++; }, releaseLock() { released++; },
  };
  globalThis.fetch = async () => ({ ok: status < 400, status, body: { getReader: () => reader },
    json: async () => ({ detail: { message: '请填写模型密钥' } }) });
  let end;
  const finished = new Promise(resolve => { end = resolve; });
  const stop = streamChat({ prompt: 'test', sessionId: 'test', idleTimeoutMs: 10, onChunk() {},
    onEnd(text, meta) { endings++; end({ text, meta }); } });
  if (cancel) { await tick(); stop(); }
  const result = await finished; await tick();
  assert.equal(endings, 1);
  if (status < 400) { assert.equal(cancelled, 1); assert.equal(released, 1); }
  return result;
}
{
  const result = await chatScenario(['event: text\ndata: {"delta_chinese":"已收到的内容"}\n\n']);
  assert.equal(result.text, '已收到的内容');
  assert.match(result.meta.error, /提前结束/); passed++;
}
{
  const result = await chatScenario(['event: done\ndata: {"chinese":"完整回复"}\n\n']);
  assert.equal(result.text, '完整回复'); assert.equal(result.meta.error, null); passed++;
}
{
  const result = await chatScenario([], { hang: true, cancel: true });
  assert.equal(result.meta.cancelled, true); assert.equal(result.meta.error, null); passed++;
}
{
  const result = await chatScenario([], { status: 400 });
  assert.equal(result.meta.error, '请填写模型密钥'); passed++;
}
{
  const result = await chatScenario(['event: audio_chunk_error\ndata: {"error":"connection refused"}\n\n',
    'event: done\ndata: {"chinese":"文字正常"}\n\n']);
  assert.equal(result.meta.error, null); assert.match(result.meta.audioError, /设置中心 → 语音服务 → 计算方式」改选「CPU 模式/); passed++;
}
{
  // No text, line, or message arrived. The stage must not claim that content was kept.
  const timeout = await chatScenario([], { hang: true });
  assert.equal(timeout.text, '');
  assert.equal(timeout.meta.cancelled, false);
  assert.equal(timeout.meta.errorCode, 'stream_idle_timeout');
  assert.equal(timeout.meta.error, '回复等待超时，没有收到回复。请在「设置中心」检查网络、模型与语音服务，稍后重新发送上一条消息。');
  const closed = await chatScenario([]);
  assert.equal(closed.text, '');
  assert.equal(closed.meta.cancelled, false);
  assert.equal(closed.meta.error, '回复连接提前结束，没有收到回复。请检查服务与网络，稍后重新发送上一条消息。');
  const kept = await chatScenario(['event: text\ndata: {"delta_chinese":"半句"}\n\n'], { hang: true });
  assert.equal(kept.text, '半句');
  assert.equal(kept.meta.error, '回复等待超时，已收到的台词保留在「对话记录」里。请在「设置中心」检查网络、模型与语音服务。');
  passed++;
}
{
  const states = [{ state: 'starting', ready: false }, { state: 'ready', ready: true }];
  globalThis.fetch = async () => response(states.shift());
  assert.equal((await waitForEngine({ intervalMs: 1 })).ready, true); passed++;
}
{
  globalThis.fetch = async () => response({ state: 'starting', ready: false });
  const result = await waitForEngine({ timeoutMs: 3, intervalMs: 5 });
  assert.equal(result.ready, false); assert.match(result.message, /无需反复重启/); passed++;
}

globalThis.document = { getElementById: () => null };
{
  initVoiceSettings({});
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return url.endsWith('/profiles') ? response({ profiles: [{ id: 1 }], active_profile_id: null })
      : response({ detail: '系统内存不足，请关闭占用内存的程序' }, 503);
  };
  await ensureSessionVoice({ settings: { voiceProfileId: 1 } });
  const switches = requests.filter(item => item.url.endsWith('/switch'));
  assert.equal(switches.length, 1);
  assert.equal(JSON.parse(switches[0].options.body).force, undefined); passed++;
}
{
  initVoiceSettings({});
  globalThis.localStorage = { setItem() {} };
  globalThis.fetch = async url => url.endsWith('/profiles')
    ? response({ profiles: [], active_profile_id: null }) : response({ detail: 'Profile not found' }, 404);
  const session = { settings: { voiceProfileId: 999 } };
  await ensureSessionVoice(session);
  assert.equal(session.settings.voiceProfileId, 999, 'a missing selection cannot silently become the global default'); passed++;
}
{
  initVoiceSettings({});
  globalThis.fetch = async url => {
    if (url.endsWith('/profiles')) throw new TypeError('Failed to fetch');
    return response({}, 404);
  };
  const session = { settings: { voiceProfileId: 999 } };
  await ensureSessionVoice(session);
  assert.equal(session.settings.voiceProfileId, 999); passed++;
}

function element(value = '') {
  const listeners = {};
  return { value, textContent: '', innerHTML: '', disabled: false, checked: false, hidden: false,
    addEventListener(name, handler) { (listeners[name] ||= []).push(handler); },
    change(name = 'input') { listeners[name]?.forEach(handler => handler({ target: this })); } };
}
const cfg = (speed) => ({ status: 'ok', settings: { speed_factor: speed } });
function initGlobalDom(defaults) {
  const dom = Object.fromEntries(Object.entries(defaults).map(([k, v]) => [k, element(v)]));
  initGlobalSettings(dom);
  return dom;
}
{
  // An unconfirmed mode must start from saved preferences instead of forcing CUDA/FP16.
  const dom = initGlobalDom({ gDashPrecisionBadge: '', gBtnTogglePrecision: '' });
  dom.gDashPrecisionBadge.textContent = '待确认';
  const bodies = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url.endsWith('/restart_sovits')) { bodies.push(JSON.parse(options.body)); return response({ status: 'starting' }); }
    if (url.endsWith('/engine-status')) return response({ state: 'ready', ready: true, message: '已就绪' });
    return response({});
  };
  await clickPrecisionToggle(dom);
  assert.deepEqual(bodies, [{}]); passed++;
}
for (const known of [true, false]) {
  const dom = initGlobalDom({
    gDashPrecisionBadge: '', gDashPrecisionVal: '', gDashDeviceVal: '', gBtnTogglePrecisionText: '',
  });
  globalThis.fetch = async url => response(url.endsWith('/system/status') ? { hardware: {
    inference_precision: known ? 'FP16' : null, precision_source: known ? 'managed_launch' : 'unknown',
    configured_precision: 'cpu', next_inference_precision: 'CPU', precision_pending_restart: known ? true : null,
  } } : {});
  await fetchSystemTelemetry();
  assert.equal(dom.gDashPrecisionBadge.textContent, known ? 'FP16' : '待确认');
  if (known) {
    assert.match(dom.gDashPrecisionVal.textContent, /FP16/);
    assert.match(dom.gDashDeviceVal.textContent, /CPU.*重启/);
  } else {
    assert.match(dom.gDashPrecisionVal.textContent, /待确认/);
    assert.match(dom.gDashDeviceVal.textContent, /外部引擎/);
    assert.match(dom.gBtnTogglePrecisionText.textContent, /已保存/);
  }
  passed++;
}

// readHardware() runs on every status poll, so the caller can replace the object between polls.
function beginPrecisionToggle(readHardware, engineStatus = { state: 'ready', ready: true, message: '已就绪' }) {
  const dom = initGlobalDom({
    gDashPrecisionBadge: '', gDashPrecisionVal: '', gDashDeviceVal: '',
    gBtnTogglePrecision: '', gBtnTogglePrecisionText: '', gBtnRestartSovits: '',
  });
  const restartRequests = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url.endsWith('/system/status')) return response({ hardware: readHardware() });
    if (url.endsWith('/restart_sovits')) {
      restartRequests.push(options.body ? JSON.parse(options.body) : {});
      return response({ status: 'starting' });
    }
    if (url.endsWith('/engine-status')) return response({ ...engineStatus });
    return response({});
  };
  return { dom, restartRequests };
}
const managedHw = (inference_precision, gpu_available, gpu_name) => ({
  ...(gpu_available !== undefined ? { gpu_available } : {}),
  ...(gpu_name ? { gpu_name } : {}),
  inference_precision,
  precision_source: 'managed_launch',
});
async function startPrecisionToggle(readHardware, engineStatus) {
  const ctx = beginPrecisionToggle(typeof readHardware === 'function' ? readHardware : () => readHardware, engineStatus);
  await fetchSystemTelemetry();
  return ctx;
}
async function clickPrecisionToggle(dom) {
  dom.gBtnTogglePrecision.change('click');
  for (let i = 0; i < 3; i++) await tick();
}
{
  // CPU-only machine without discrete GPU: hide precision toggle button and block switching to CUDA/FP16
  const { dom, restartRequests } = await startPrecisionToggle(managedHw('CPU', false, 'Intel Iris Xe'));

  assert.equal(dom.gDashPrecisionBadge.textContent, 'CPU');
  assert.equal(dom.gBtnTogglePrecision.hidden, true, 'Precision toggle button should be hidden on CPU-only device');
  assert.equal(dom.gBtnTogglePrecisionText.textContent.includes('GPU(FP16)'), false, 'Text must not offer switching to GPU(FP16)');
  assert.equal(dom.gBtnTogglePrecisionText.textContent, '当前仅支持 CPU 模式');

  // Triggering click must be blocked and not request /restart_sovits
  await clickPrecisionToggle(dom);
  assert.equal(restartRequests.length, 0, 'Clicking precision toggle when gpu_available is false must not send restart request');
  passed++;
}
{
  // Machine with GPU running in CPU mode: toggle button remains visible and clicking requests FP16
  const { dom, restartRequests } = await startPrecisionToggle(managedHw('CPU', true, 'NVIDIA GeForce RTX 4070'));

  assert.equal(dom.gDashPrecisionBadge.textContent, 'CPU');
  assert.equal(dom.gBtnTogglePrecision.hidden, false, 'Precision toggle button should be visible when GPU is available');
  assert.equal(dom.gBtnTogglePrecisionText.textContent, '切为 GPU(FP16)');

  await clickPrecisionToggle(dom);
  assert.deepEqual(restartRequests, [{ precision: 'fp16' }], 'Clicking toggle when GPU is available sends fp16 restart request');
  passed++;
}
{
  // Transition: Poll 1 has gpu_available: false, then Poll 2 has gpu_available: undefined (legacy/external).
  // gpuAvailable must recover to default true, making toggle visible and clickable for FP16.
  let statusHardware = managedHw('CPU', false, 'Intel Iris Xe');
  const { dom, restartRequests } = await startPrecisionToggle(() => statusHardware);
  assert.equal(dom.gBtnTogglePrecision.hidden, true);

  // Poll 2: hardware present but gpu_available is undefined
  statusHardware = managedHw('CPU');
  await fetchSystemTelemetry();
  assert.equal(dom.gBtnTogglePrecision.hidden, false);
  assert.equal(dom.gBtnTogglePrecisionText.textContent, '切为 GPU(FP16)');

  await clickPrecisionToggle(dom);
  assert.deepEqual(restartRequests, [{ precision: 'fp16' }], 'Clicking toggle after undefined recovery sends fp16');
  passed++;
}
{
  // CPU-only device mistakenly set to FP16: button remains visible and offers direct switch to CPU mode
  const { dom, restartRequests } = await startPrecisionToggle(
    managedHw('FP16', false, 'Intel Iris Xe'),
    { state: 'failed', ready: false, message: 'CUDA 不可用' },
  );

  assert.equal(dom.gDashPrecisionBadge.textContent, 'FP16');
  assert.equal(dom.gBtnTogglePrecision.hidden, false, 'Precision toggle button should remain visible to recover from FP16');
  assert.equal(dom.gBtnTogglePrecisionText.textContent, '切为 CPU 模式');

  await clickPrecisionToggle(dom);
  assert.deepEqual(restartRequests, [{ precision: 'cpu' }], 'Clicking toggle on CPU device in FP16 mode directly restarts in CPU mode');
  passed++;
}
async function pollSovitsTelemetry(engineStatus, sovitsStatus = { status: 'unreachable' }) {
  const dom = initGlobalDom({ gDashSovitsBadge: '', gDashSovitsStatus: '', gDashSovitsUrl: '' });
  globalThis.fetch = async url => response(url.endsWith('/engine-status') ? engineStatus : { gpt_sovits: sovitsStatus });
  await fetchSystemTelemetry();
  return dom;
}
{
  // A reachable app with a still-loading engine must not show a red offline badge.
  const dom = await pollSovitsTelemetry(
    { state: 'starting', ready: false, message: '引擎正在加载模型' },
    { status: 'unreachable', base_url: 'http://127.0.0.1:9872' },
  );
  assert.equal(dom.gDashSovitsBadge.textContent, '启动中');
  assert.match(dom.gDashSovitsBadge.className, /yellow/);
  assert.equal(dom.gDashSovitsStatus.textContent, '引擎正在加载模型'); passed++;
}
{
  const dom = await pollSovitsTelemetry({ state: 'loading_slow', ready: false, message: '引擎加载时间较长，请查看引擎日志' });
  assert.equal(dom.gDashSovitsBadge.textContent, '加载较慢');
  assert.match(dom.gDashSovitsStatus.textContent, /时间较长/); passed++;
}
{
  // A periodic status request that started before a restart must not overwrite progress.
  const dom = initGlobalDom({
    gDashSovitsBadge: '', gDashSovitsStatus: '', gDashSovitsUrl: '',
    gBtnRestartSovits: '', gBtnTogglePrecision: '',
  });
  let finishOld, finishOldEngine, finishRestart, finishPoll;
  let polls = 0, telemetryRequests = 0;
  globalThis.fetch = url => {
    if (url.endsWith('/system/status')) return ++telemetryRequests === 1
      ? new Promise(resolve => { finishOld = resolve; }) : Promise.resolve(response({}));
    if (url.endsWith('/restart_sovits')) return new Promise(resolve => { finishRestart = resolve; });
    if (url.endsWith('/engine-status') && polls < 2) return new Promise(resolve => {
      if (++polls === 1) finishOldEngine = resolve;
      else finishPoll = resolve;
    });
    return Promise.resolve(response({}));
  };
  const refresh = fetchSystemTelemetry();
  const restart = handleRestartSovits();
  finishRestart(response({ status: 'starting' })); await tick();
  finishOld(response({ gpt_sovits: { status: 'unreachable' } }));
  finishOldEngine(response({ state: 'unavailable', ready: false })); await refresh;
  assert.equal(dom.gDashSovitsBadge.textContent, '启动中');
  finishPoll(response({ state: 'failed', ready: false, message: '请重新选择引擎目录' }));
  await restart; await tick(); passed++;
}
function lostConfigSave(readback, { getFails = false } = {}) {
  const methods = [];
  globalThis.fetch = async (_url, options) => {
    methods.push(options.method || 'GET');
    if (getFails || options.method === 'POST') throw new TypeError('Failed to fetch');
    return response(cfg(readback));
  };
  return methods;
}
{
  const methods = lostConfigSave(1.8);
  assert.equal((await saveConfirmedConfig({ speed_factor: 1.8 })).confirmed_after_disconnect, true);
  assert.deepEqual(methods, ['POST', 'GET']); passed++;
}
{
  const methods = lostConfigSave(1);
  await assert.rejects(saveConfirmedConfig({ speed_factor: 1.8 }), /启动.bat/);
  assert.deepEqual(methods, ['POST', 'GET']); passed++;
}
{
  const methods = lostConfigSave(undefined, { getFails: true });
  await assert.rejects(saveConfirmedConfig({ gpt_sovits_url: 'http://127.0.0.1:9880' }), /启动.bat/);
  assert.deepEqual(methods, ['POST', 'GET']); passed++;
}
{
  const dom = initGlobalDom({ gAudioRetention: '30', gTgSave: '', gStatus: '' });
  globalThis.fetch = async () => response({ status: 'ok', settings: { audio_retention_minutes: 30 } });
  await loadGlobalConfig();
  dom.gAudioRetention.value = '45'; dom.gAudioRetention.change();
  await loadGlobalConfig();
  assert.equal(dom.gAudioRetention.value, '45'); passed++;
  globalThis.fetch = async () => response({ detail: [{ msg: 'invalid' }] }, 422);
  await saveGlobalConfig();
  assert.equal(dom.gTgSave.disabled, false);
  assert.equal(dom.gAudioRetention.value, '45'); passed++;
}
{
  const dom = initGlobalDom({ gAudioRetention: '45', gTgSave: '' });
  let complete;
  globalThis.fetch = async (_url, options) => options.method === 'POST'
    ? new Promise(resolve => { complete = resolve; })
    : response({ status: 'ok', settings: { audio_retention_minutes: 45 } });
  await loadGlobalConfig();
  const pending = saveGlobalConfig();
  dom.gAudioRetention.value = '60'; dom.gAudioRetention.change();
  complete(response({ status: 'ok' })); await pending;
  await loadGlobalConfig();
  assert.equal(dom.gAudioRetention.value, '60');
  assert.equal(dom.gTgSave.disabled, false); passed++;
}
{
  // The config never loaded (backend restarting): saving would overwrite every setting with
  // the form's defaults, resetting the engine URL and precision.
  const dom = initGlobalDom({ gAudioRetention: '30', gTgSave: '' });
  const posts = [];
  globalThis.fetch = async (_url, options = {}) => {
    if (options.method === 'POST') { posts.push(JSON.parse(options.body)); return response({ status: 'ok' }); }
    return response({ detail: '后端正在重启' }, 503);
  };
  await loadGlobalConfig();
  dom.gAudioRetention.value = '60'; dom.gAudioRetention.change();
  await saveGlobalConfig();
  assert.deepEqual(posts, []); passed++;
}
{
  // An edit made while the first load is in flight keeps the server's values in every other field.
  const dom = initGlobalDom({ gAudioRetention: '30', gParamSovitsUrl: '', gTgSave: '' });
  let complete;
  globalThis.fetch = () => new Promise(resolve => { complete = resolve; });
  const pending = loadGlobalConfig();
  dom.gAudioRetention.value = '90'; dom.gAudioRetention.change();
  complete(response({ status: 'ok', settings: { audio_retention_minutes: 30, gpt_sovits_url: 'http://10.0.0.5:9880' } }));
  await pending;
  assert.equal(dom.gAudioRetention.value, '90');
  assert.equal(dom.gParamSovitsUrl.value, 'http://10.0.0.5:9880');
  const posts = [];
  globalThis.fetch = async (_url, options = {}) => {
    if (options.method === 'POST') posts.push(JSON.parse(options.body));
    return response({ status: 'ok', settings: {} });
  };
  await saveGlobalConfig();
  assert.equal(posts[0].audio_retention_minutes, 90);
  assert.equal(posts[0].gpt_sovits_url, 'http://10.0.0.5:9880'); passed++;
}
{
  // Restarting before the config loaded must not send the dropdown's default precision.
  initGlobalDom({ gBtnRestartSovits: '', gBtnTogglePrecision: '', gPrecision: 'auto' });
  const bodies = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url.endsWith('restart_sovits')) { bodies.push(JSON.parse(options.body)); return response({ status: 'starting' }); }
    if (url.endsWith('engine-status')) return response({ state: 'failed', ready: false, message: '请重新选择引擎目录' });
    return response({});
  };
  await handleRestartSovits();
  assert.deepEqual(bodies, [{}]); passed++;
}
{
  const dom = initGlobalDom({ gBtnRestartSovits: '', gBtnTogglePrecision: '', gPrecision: 'cpu' });
  let complete;
  let restarts = 0;
  globalThis.fetch = async (url) => {
    if (url.endsWith('restart_sovits')) { restarts++; return new Promise(resolve => { complete = resolve; }); }
    if (url.endsWith('engine-status')) return response({ state: 'failed', ready: false, message: '请重新选择引擎目录' });
    return response({});
  };
  const pending = handleRestartSovits();
  await tick();
  assert.equal(dom.gBtnRestartSovits.disabled, true);
  await handleRestartSovits();
  assert.equal(restarts, 1);
  complete(response({ status: 'starting' })); await pending;
  assert.equal(dom.gBtnRestartSovits.disabled, false);
  assert.equal(dom.gBtnTogglePrecision.disabled, false); passed++;
}
{
  const dom = { gStatus: element() };
  dom.gStatus.textContent = 'diagnostic chips stay mounted';
  const error = element(); const backend = element(); const classes = new Set(['hidden']);
  error.classList = { add: name => classes.add(name), remove: name => classes.delete(name) };
  const previousLookup = document.getElementById;
  document.getElementById = id => ({gConfigError:error, gDiagBackend:backend}[id] || null);
  try {
    initGlobalSettings(dom);
    globalThis.fetch = async () => response({detail:'QA configuration unavailable'}, 503);
    await loadGlobalConfig();
    assert.equal(dom.gStatus.textContent, 'diagnostic chips stay mounted');
    assert.equal(classes.has('hidden'), false);
    assert.ok(error.textContent.length > 0);
    globalThis.fetch = async () => response(cfg(1));
    await loadGlobalConfig();
    assert.equal(classes.has('hidden'), true); assert.equal(error.textContent, '');
    assert.equal(backend.textContent, '运行正常'); passed++;
  } finally { document.getElementById = previousLookup; }
}
function endpointDom() {
  return { gAllowPrivateEndpoints: element(), gPrivateEndpointsStatus: element(),
    gProvider: element('custom'), gProviderTest: element(), gProviderActivate: element() };
}
{
  const dom = endpointDom(); initGlobalSettings(dom);
  const methods = [];
  globalThis.fetch = async (_url, options) => { methods.push(options.method || 'GET'); return response({ settings: {} }); };
  await loadGlobalConfig();
  assert.equal(dom.gAllowPrivateEndpoints.checked, false);
  assert.deepEqual(methods, ['GET'], 'opening settings must not enable private endpoints'); passed++;
}
for (const allowed of [true, false]) {
  const dom = endpointDom(); initGlobalSettings(dom);
  globalThis.fetch = async () => response({ settings: { allow_private_llm_endpoints: !allowed } });
  await loadGlobalConfig();
  assert.equal(dom.gAllowPrivateEndpoints.checked, !allowed);
  const writes = [];
  globalThis.fetch = async (url, options) => { writes.push({ url, body: JSON.parse(options.body) }); return response({}); };
  dom.gAllowPrivateEndpoints.checked = allowed;
  dom.gAllowPrivateEndpoints.change('change'); await tick();
  assert.deepEqual(writes, [{ url: '/api/config', body: { allow_private_llm_endpoints: allowed } }]);
  assert.equal(dom.gAllowPrivateEndpoints.checked, allowed);
  assert.equal(dom.gAllowPrivateEndpoints.disabled, false);
  assert.match(dom.gPrivateEndpointsStatus.textContent, /已保存/); passed++;
}
{
  const dom = endpointDom(); initGlobalSettings(dom);
  globalThis.fetch = async () => response({ settings: { allow_private_llm_endpoints: true } });
  await loadGlobalConfig();
  globalThis.fetch = async () => response({ detail: '验收：配置目录不可写' }, 400);
  dom.gAllowPrivateEndpoints.checked = false;
  await savePrivateEndpointPreference();
  assert.equal(dom.gAllowPrivateEndpoints.checked, true, 'a rejected save must restore the confirmed setting');
  assert.equal(dom.gAllowPrivateEndpoints.disabled, false);
  assert.match(dom.gPrivateEndpointsStatus.textContent, /尚未确认保存/); passed++;
}
{
  const dom = endpointDom(); initGlobalSettings(dom);
  let complete;
  globalThis.fetch = () => new Promise(resolve => { complete = resolve; });
  const loading = loadGlobalConfig();
  globalThis.fetch = async () => response({});
  dom.gAllowPrivateEndpoints.checked = true;
  await savePrivateEndpointPreference();
  complete(response({ settings: { allow_private_llm_endpoints: false } })); await loading;
  assert.equal(dom.gAllowPrivateEndpoints.checked, true, 'an older config response must not undo the saved choice'); passed++;
}
{
  const dom = endpointDom(); initGlobalSettings(dom);
  let complete, writes = 0;
  globalThis.fetch = () => { writes++; return new Promise(resolve => { complete = resolve; }); };
  dom.gAllowPrivateEndpoints.checked = true;
  const pending = savePrivateEndpointPreference();
  assert.equal(dom.gAllowPrivateEndpoints.disabled, true);
  await savePrivateEndpointPreference(); await handleProviderTest(); await handleActivateProvider();
  assert.equal(writes, 1, 'test and activation must wait for the connection scope to be confirmed');
  complete(response({})); await pending;
  assert.equal(dom.gAllowPrivateEndpoints.disabled, false); passed++;
}
{
  const dom = endpointDom(); initGlobalSettings(dom);
  const methods = [];
  globalThis.fetch = async (_url, options) => {
    methods.push(options.method || 'GET');
    if (options.method === 'POST') throw new TypeError('Lost acknowledgement');
    return response({ settings: { allow_private_llm_endpoints: true } });
  };
  dom.gAllowPrivateEndpoints.checked = true;
  await savePrivateEndpointPreference();
  assert.equal(dom.gAllowPrivateEndpoints.checked, true);
  assert.match(dom.gPrivateEndpointsStatus.textContent, /已保存/);
  assert.deepEqual(methods, ['POST', 'GET']); passed++;
}
function clearToasts(toastRoot, replace = true) {
  for (const toast of [...toastRoot.children]) clearTimeout(toast._timer);
  if (replace) toastRoot.replaceChildren();
}
async function withFakeDocument(fn, { captureError = false, muteConsole = false } = {}) {
  const previousDocument = globalThis.document;
  const doc = fakeDocument();
  globalThis.document = doc;
  const toastRoot = doc.createElement('div');
  toastRoot.id = 'toastRoot';
  doc.body.appendChild(toastRoot);
  const control = (tag, value = '', checked = false) => {
    const el = doc.createElement(tag);
    el.value = value;
    el.checked = checked;
    return el;
  };
  const errors = [];
  const originalError = console.error;
  const originalWarn = console.warn;
  if (captureError) console.error = (...args) => errors.push(args);
  if (muteConsole) { console.error = () => {}; console.warn = () => {}; }
  try {
    return await fn({ doc, toastRoot, control, errors });
  } finally {
    clearToasts(toastRoot, false);
    console.error = originalError;
    console.warn = originalWarn;
    globalThis.document = previousDocument;
  }
}
function createElements(doc, tags) {
  return Object.fromEntries(Object.entries(tags).map(([k, tag]) => [k, doc.createElement(tag)]));
}
async function withUpdateManager(extraTags, { buttonText = '检查更新', extraDom, beforeInit, ...opts } = {}, fn) {
  return withFakeDocument(async ctx => {
    const dom = {
      ...createElements(ctx.doc, { gBtnCheckUpdate: 'button', gBtnCheckUpdateText: 'span', gRemoteStatusTag: 'span', ...extraTags }),
      ...(extraDom ? extraDom(ctx.doc) : {}),
    };
    dom.gBtnCheckUpdateText.textContent = buttonText;
    if (beforeInit) beforeInit(dom);
    initUpdateManager(dom);
    return fn({ ...ctx, dom });
  }, opts);
}
await withFakeDocument(async ({ toastRoot, control }) => {
  // Retired memory, nickname, and persona controls used to ride along on every global save.
  // GET /api/config returns SettingsResponse and never contains those keys, so a lost POST
  // could not confirm. saveGlobalConfig must ignore them, accept the readback, and clear the draft.
  const dom = {
    gParamSovitsUrl: control('input', 'http://127.0.0.1:9880'),
    gPrecision: control('select', 'auto'),
    gAudioRetention: control('input', '30'),
    gMemoryEnabled: control('input', '', true),
    gUserNickname: control('input', ''),
    gDefaultSystemPrompt: control('textarea', ''),
    gTgSave: control('button', ''),
  };
  const persisted = (retention) => ({
    active_provider_id: 'deepseek',
    active_voice_profile_id: null,
    gpt_sovits_url: 'http://127.0.0.1:9880',
    audio_output_dir: 'audio',
    audio_retention_minutes: retention,
    audio_cleanup_interval_sec: 600,
    speed_factor: 1,
    temperature: 1,
    top_k: 15,
    top_p: 1,
    seed: -1,
    batch_size: 1,
    text_split_method: 'cut5',
    fragment_interval: 0.3,
    allow_private_llm_endpoints: false,
    console_url: '',
    max_history_messages: 10,
    inference_precision: 'auto',
    stt_engine: 'browser',
    console_token: '',
    updated_at: null,
  });
  for (const kind of ['disconnect', 'timeout']) {
    initGlobalSettings(dom);
    const posts = [];
    let configGets = 0;
    clearToasts(toastRoot);
    globalThis.fetch = async (url, options = {}) => {
      const method = options.method || 'GET';
      if (String(url).includes('/api/config')) {
        if (method === 'POST') {
          posts.push(JSON.parse(options.body));
          if (kind === 'timeout') {
            const error = new Error('等待服务响应超时。请打开「运行状态」检查；保存结果可能尚未确认，请刷新配置后再决定是否重试。');
            error.code = 'REQUEST_TIMEOUT';
            throw error;
          }
          throw new TypeError('Failed to fetch');
        }
        configGets += 1;
        const retention = configGets === 1 ? 30 : configGets === 2 ? 45 : 30;
        return response({ status: 'ok', settings: persisted(retention), active_provider: null });
      }
      return response({});
    };
    await loadGlobalConfig();
    dom.gAudioRetention.value = '45';
    dom.gAudioRetention.dispatchEvent({ type: 'input' });
    await saveGlobalConfig();
    await tick();
    const messages = [...toastRoot.children].map(node => node.textContent);
    const detail = `${kind}: toasts=[${messages.join(' | ')}] gets=${configGets} posted=${JSON.stringify(posts[0])}`;
    assert.equal(posts.length, 1, detail);
    assert.ok(configGets >= 2, detail);
    assert.ok(messages.includes('全局配置已保存'), detail);
    assert.equal(messages.some(message => message.includes('保存失败')), false, detail);
    assert.equal(messages.some(message => message.includes('尚未保存')), false, detail);
    assert.equal(Number(dom.gAudioRetention.value), 30, detail);
    assert.equal(dom.gTgSave.disabled, false, detail);
    assert.equal(Object.hasOwn(posts[0], 'memory_enabled'), false, detail);
    assert.equal(Object.hasOwn(posts[0], 'user_nickname'), false, detail);
    assert.equal(Object.hasOwn(posts[0], 'system_prompt'), false, detail);
    assert.equal(posts[0].audio_retention_minutes, 45, detail);
    passed++;
  }
});
await withUpdateManager({ gUpdateNoticeBox: 'div' }, { captureError: true }, async ({ toastRoot, errors }) => {
  globalThis.fetch = async () => response({ error: 'fatal: unable to access github.com: connection timed out' });
  await handleCheckUpdate();
  assert.equal(toastRoot.children.length, 1);
  const toast = toastRoot.children[0];
  assert.equal(toast.dataset.msg, '检查更新失败：暂时无法获取最新版本信息，请检查网络连接后重试');
  assert.doesNotMatch(toast.dataset.msg, /fatal|unable to access|timed out/);
  assert.ok(errors.some(args => args.join(' ').includes('fatal: unable to access')));
  passed++;
});
await withUpdateManager({}, {
  captureError: true,
  extraDom: doc => {
    const throwingEl = doc.createElement('div');
    Object.defineProperty(throwingEl, 'textContent', {
      get() { throw new Error('DOM detached during update check'); },
      set() {},
    });
    return { gDashVersionCommit: throwingEl };
  },
}, async ({ toastRoot, errors }) => {
  await handleCheckUpdate();
  assert.equal(toastRoot.children.length, 1);
  const toast = toastRoot.children[0];
  assert.equal(toast.dataset.msg, '检查更新失败：暂时无法连接服务，请检查网络连接后重试');
  assert.doesNotMatch(toast.dataset.msg, /DOM detached/);
  assert.ok(errors.some(args => args.join(' ').includes('DOM detached')));
  passed++;
});
await withFakeDocument(async ({ toastRoot, control }) => {
  // Modifying or browsing the local engine directory in Voice Service settings and clicking
  // the footer "保存全局配置" button must save the engine directory before confirming global save,
  // and must abort without claiming "全局配置已保存" if directory validation fails (HTTP 400).
  const dom = {
    gParamSovitsUrl: control('input', 'http://127.0.0.1:9880'),
    gSovitsDirectory: control('input', ''),
    gSovitsDirectoryStatus: control('small', ''),
    gBrowseSovitsDirectory: control('button', ''),
    gSaveSovitsDirectory: control('button', ''),
    gPrecision: control('select', 'auto'),
    gAudioRetention: control('input', '30'),
    gTgSave: control('button', ''),
  };
  let savedDirectory = '';
  let rejectDirSave = false;
  const dirPuts = [];
  const configPosts = [];
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    if (url === '/api/system/sovits-directory' && method === 'PUT') {
      const body = JSON.parse(options.body);
      dirPuts.push(body.directory);
      if (rejectDirSave) {
        return response({ detail: '所选文件夹需包含 api_v2.py。请先完整解压 GPT-SoVITS，再选择引擎目录。' }, 400);
      }
      savedDirectory = body.directory;
      return response({ directory: savedDirectory, valid: true, message: '引擎目录有效，可在「运行状态」中启动或重启引擎。' });
    }
    if (url === '/api/system/sovits-directory') {
      return response({ directory: savedDirectory, valid: Boolean(savedDirectory), message: savedDirectory ? '引擎目录有效' : '尚未设置' });
    }
    if (url === '/api/voice/browse-file' && method === 'POST') {
      return response({ selected_path: 'E:/GPT-SoVITS-browse' });
    }
    if (String(url).includes('/api/config')) {
      if (method === 'POST') {
        configPosts.push(JSON.parse(options.body));
      }
      return response({ status: 'ok', settings: { gpt_sovits_url: 'http://127.0.0.1:9880', inference_precision: 'auto', audio_retention_minutes: 30 } });
    }
    return response({});
  };

  initGlobalSettings(dom);
  await loadGlobalConfig();
  await tick();

  // 1. Input edited directory -> click Save Global Config -> PUT /api/system/sovits-directory is called
  dom.gSovitsDirectory.value = 'D:/GPT-SoVITS-v2';
  dom.gSovitsDirectory.dispatchEvent({ type: 'input' });
  await saveGlobalConfig();
  await tick();

  let messages = [...toastRoot.children].map(node => node.textContent);
  assert.deepEqual(dirPuts, ['D:/GPT-SoVITS-v2']);
  assert.equal(configPosts.length, 1);
  assert.equal(dom.gSovitsDirectory.value, 'D:/GPT-SoVITS-v2');
  assert.ok(messages.includes('全局配置已保存'), `expected global save toast, got: ${messages.join(' | ')}`);
  passed++;

  // 2. Browse directory -> click Save Global Config -> PUT /api/system/sovits-directory is called
  clearToasts(toastRoot);
  dom.gBrowseSovitsDirectory.dispatchEvent({ type: 'click' });
  await tick();
  assert.equal(dom.gSovitsDirectory.value, 'E:/GPT-SoVITS-browse');
  await saveGlobalConfig();
  await tick();
  assert.deepEqual(dirPuts, ['D:/GPT-SoVITS-v2', 'E:/GPT-SoVITS-browse']);
  assert.equal(dom.gSovitsDirectory.value, 'E:/GPT-SoVITS-browse');
  passed++;

  // 3. Directory validation returns 400 -> aborts global save, preserves user input, does not show "全局配置已保存"
  clearToasts(toastRoot);
  rejectDirSave = true;
  const prevConfigPosts = configPosts.length;
  dom.gSovitsDirectory.value = 'D:/incomplete-engine';
  dom.gSovitsDirectory.dispatchEvent({ type: 'input' });
  await saveGlobalConfig();
  await tick();

  messages = [...toastRoot.children].map(node => node.textContent);
  assert.deepEqual(dirPuts, ['D:/GPT-SoVITS-v2', 'E:/GPT-SoVITS-browse', 'D:/incomplete-engine']);
  assert.equal(configPosts.length, prevConfigPosts, 'global config POST must not be sent when engine directory save fails');
  assert.equal(messages.includes('全局配置已保存'), false, `must not claim global config saved when directory save fails: ${messages.join(' | ')}`);
  assert.ok(messages.some(m => m.includes('保存目录失败') && m.includes('api_v2.py')), `expected directory save error toast, got: ${messages.join(' | ')}`);
  assert.equal(dom.gSovitsDirectory.value, 'D:/incomplete-engine', 'user input must be preserved on error');
  assert.equal(dom.gTgSave.disabled, false);
  passed++;
});
await withUpdateManager({
  gDashVersionBadge: 'span', gCurrentCommit: 'span', gCurrentBranch: 'span',
  gUpdateNoticeBox: 'div', gUpdateNoticeTitle: 'div', gUpdateNoticeDesc: 'div',
  gUpdateNoticeIcon: 'span', gReleaseDownloadLink: 'a',
}, {
  buttonText: '检查新版本',
  muteConsole: true,
  beforeInit: dom => {
    dom.gUpdateNoticeBox.classList.add('hidden');
    dom.gReleaseDownloadLink.href = 'https://github.com/LevenST24/galgame2voice/releases';
  },
}, async ({ toastRoot, dom }) => {
  globalThis.fetch = async (url) => String(url).includes('check_remote=true')
    ? response({
      current_version: '3.1.2',
      latest_version: '3.2.0',
      current_branch: 'source',
      release_url: 'https://github.com/LevenST24/galgame2voice/releases/tag/v3.2.0',
      has_update: true,
      error: null,
    })
    : response({
      current_version: '3.1.2',
      latest_version: null,
      current_branch: 'source',
      release_url: 'https://github.com/LevenST24/galgame2voice/releases',
      has_update: false,
      error: null,
    });
  await handleCheckUpdate();
  assert.equal(dom.gDashVersionBadge.textContent, '发现新版本 v3.2.0');
  assert.equal(dom.gReleaseDownloadLink.href, 'https://github.com/LevenST24/galgame2voice/releases/tag/v3.2.0');
  await loadSystemVersionInfo(false);
  assert.equal(dom.gDashVersionBadge.textContent, '发现新版本 v3.2.0');
  assert.equal(dom.gRemoteStatusTag.textContent, '发现新版本 v3.2.0');
  assert.equal(dom.gReleaseDownloadLink.href, 'https://github.com/LevenST24/galgame2voice/releases/tag/v3.2.0');

  globalThis.fetch = async () => response({
    current_version: '3.2.0',
    latest_version: '3.2.0',
    current_branch: 'source',
    release_url: 'https://github.com/LevenST24/galgame2voice/releases/tag/v3.2.0',
    has_update: false,
    error: null,
  });
  await handleCheckUpdate();
  assert.equal(dom.gUpdateNoticeBox.classList.contains('hidden'), false);
  assert.equal(dom.gUpdateNoticeTitle.textContent, '当前已是最新版本');
  clearToasts(toastRoot);

  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await handleCheckUpdate();
  assert.equal(dom.gUpdateNoticeBox.classList.contains('hidden'), true);
  assert.equal(toastRoot.children.length, 1);
  assert.equal(toastRoot.children[0].dataset.msg, '检查更新失败：暂时无法连接服务，请检查网络连接后重试');
  assert.match(toastRoot.children[0].className, /toast-error/);
  passed++;
});
{
  // (a) handleRestartSovits failure states (400/409/500/network error) on the status card
  // must not be overwritten to '离线' or '在线' by finally fetchSystemTelemetry.
  const cases = [
    {
      name: '400',
      restart: () => response({ detail: '尚未选择语音引擎目录。请在「设置中心 → 语音服务」点击「选择文件夹」。' }, 400),
      engineStatus: { state: 'unavailable', ready: false, message: '语音引擎尚未就绪。' },
      systemStatus: { gpt_sovits: { status: 'unreachable', base_url: 'http://127.0.0.1:9880' } },
      expectedBadge: '启动失败',
      expectedMessage: /尚未选择语音引擎目录/,
    },
    {
      name: '409',
      restart: () => response({ detail: '当前连接的是远端或外部语音引擎，无法由此处重启。' }, 409),
      engineStatus: { state: 'ready', ready: true, message: '语音引擎已就绪。' },
      systemStatus: { gpt_sovits: { status: 'reachable', base_url: 'http://127.0.0.1:9880' } },
      expectedBadge: '待确认',
      expectedMessage: /远端或外部语音引擎/,
    },
    {
      name: '500',
      restart: () => response({ detail: '启动语音引擎失败：进程退出' }, 500),
      engineStatus: { state: 'unavailable', ready: false, message: '语音引擎尚未就绪。' },
      systemStatus: { gpt_sovits: { status: 'unreachable', base_url: 'http://127.0.0.1:9880' } },
      expectedBadge: '启动失败',
      expectedMessage: /启动语音引擎失败/,
    },
    {
      name: 'network',
      restart: () => { throw new TypeError('Failed to fetch'); },
      engineStatus: { state: 'unavailable', ready: false, message: '语音引擎尚未就绪。' },
      systemStatus: { gpt_sovits: { status: 'unreachable', base_url: 'http://127.0.0.1:9880' } },
      expectedBadge: '待确认',
      expectedMessage: /无法连接本机服务/,
    },
  ];

  for (const tc of cases) {
    const dom = initGlobalDom({
      gBtnRestartSovits: '', gBtnTogglePrecision: '', gPrecision: 'fp16',
      gDashSovitsBadge: '', gDashSovitsStatus: '', gDashSovitsUrl: '',
    });
    globalThis.fetch = async (url, options = {}) => {
      if (url.endsWith('/restart_sovits') && options.method === 'POST') return tc.restart();
      if (url.endsWith('/engine-status')) return response(tc.engineStatus);
      if (url.endsWith('/system/status')) return response(tc.systemStatus);
      return response({});
    };
    await handleRestartSovits();
    await tick();
    assert.equal(dom.gDashSovitsBadge.textContent, tc.expectedBadge, `case ${tc.name} badge`);
    assert.match(dom.gDashSovitsStatus.textContent, tc.expectedMessage, `case ${tc.name} status`);
    passed++;
  }
}
{
  // (b) When /api/system/status resolves before /api/system/engine-status during polling,
  // a 'starting' state must not be flashed to '离线' ('unavailable').
  const dom = await pollSovitsTelemetry(
    { state: 'starting', ready: false, message: '语音引擎正在启动，模型加载中…' },
    { status: 'unreachable', base_url: 'http://127.0.0.1:9880' },
  );
  assert.equal(dom.gDashSovitsBadge.textContent, '启动中');

  let finishEngineStatus;
  globalThis.fetch = url => {
    if (url.endsWith('/system/status')) {
      return Promise.resolve(response({ gpt_sovits: { status: 'unreachable', base_url: 'http://127.0.0.1:9880' } }));
    }
    if (url.endsWith('/engine-status')) {
      return new Promise(resolve => {
        finishEngineStatus = () => resolve(response({
          state: 'starting',
          ready: false,
          base_url: 'http://127.0.0.1:9880',
          message: '语音引擎正在启动，模型加载通常需要 10–60 秒；文字聊天可继续使用。',
        }));
      });
    }
    return Promise.resolve(response({}));
  };

  const pollPromise = fetchSystemTelemetry();
  await tick();
  assert.equal(dom.gDashSovitsBadge.textContent, '启动中', 'status resolving first must not flash badge to 离线');
  assert.doesNotMatch(dom.gDashSovitsStatus.textContent, /服务未响应/);
  finishEngineStatus();
  await pollPromise;
  assert.equal(dom.gDashSovitsBadge.textContent, '启动中');
  assert.match(dom.gDashSovitsStatus.textContent, /语音引擎正在启动/);
  passed++;
}
console.log(`Runtime recovery: ${passed} scenarios passed`);
