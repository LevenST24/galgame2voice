import assert from 'node:assert/strict';
import { fakeDocument } from './helpers/adv_dom.mjs';
import { requestJson, getErrorMessage } from '../frontend/src/request.js';
import { streamChat } from '../frontend/src/ai.js';
import { waitForEngine } from '../frontend/src/controllers/engine_recovery.js';
import { saveConfirmedConfig } from '../frontend/src/controllers/config_recovery.js';
import { ensureSessionVoice, initVoiceSettings } from '../frontend/src/controllers/voice_settings.js';
import { initGlobalSettings, loadGlobalConfig, saveGlobalConfig, handleRestartSovits, fetchSystemTelemetry, savePrivateEndpointPreference, handleProviderTest, handleActivateProvider } from '../frontend/src/controllers/global_settings.js';
import { renderSystemVersionUI, initUpdateManager, handleCheckUpdate, handleApplyUpdate } from '../frontend/src/controllers/update_manager.js';
import { formatUpdateStatus } from '../frontend/src/settings.js';

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
  const result = await chatScenario([], { hang: true });
  assert.match(result.meta.error, /超时/); assert.equal(result.meta.cancelled, false); passed++;
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
  assert.equal(result.meta.error, null); assert.match(result.meta.audioError, /启动引擎/); passed++;
}
{
  const states = [{ state: 'starting', ready: false }, { state: 'ready', ready: true }];
  globalThis.fetch = async () => response(states.shift());
  assert.equal((await waitForEngine({ intervalMs: 1 })).ready, true); passed++;
}
{
  globalThis.fetch = async () => response({ state: 'failed', ready: false, message: '请选择完整引擎包' });
  await assert.rejects(waitForEngine(), /完整引擎包/); passed++;
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
  return { value, textContent: '', innerHTML: '', disabled: false, checked: false,
    addEventListener(name, handler) { (listeners[name] ||= []).push(handler); },
    change(name = 'input') { listeners[name]?.forEach(handler => handler({ target: this })); } };
}
const cfg = (speed) => ({ status: 'ok', settings: { speed_factor: speed } });
{
  // An unconfirmed mode must start from saved preferences instead of forcing CUDA/FP16.
  const dom = { gDashPrecisionBadge: element(), gBtnTogglePrecision: element() };
  dom.gDashPrecisionBadge.textContent = '待确认';
  initGlobalSettings(dom);
  const bodies = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url.endsWith('/restart_sovits')) { bodies.push(JSON.parse(options.body)); return response({ status: 'starting' }); }
    if (url.endsWith('/engine-status')) return response({ state: 'ready', ready: true, message: '已就绪' });
    return response({});
  };
  dom.gBtnTogglePrecision.change('click');
  for (let i = 0; i < 3; i++) await tick();
  assert.deepEqual(bodies, [{}]); passed++;
}
for (const known of [true, false]) {
  const dom = { gDashPrecisionBadge: element(), gDashPrecisionVal: element(), gDashDeviceVal: element(),
    gBtnTogglePrecisionText: element() };
  initGlobalSettings(dom);
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
{
  // A reachable app with a still-loading engine must not show a red offline badge.
  const dom = { gDashSovitsBadge: element(), gDashSovitsStatus: element(), gDashSovitsUrl: element() };
  initGlobalSettings(dom);
  globalThis.fetch = async url => response(url.endsWith('/engine-status')
    ? { state: 'starting', ready: false, message: '引擎正在加载模型' }
    : { gpt_sovits: { status: 'unreachable', base_url: 'http://127.0.0.1:9872' } });
  await fetchSystemTelemetry();
  assert.equal(dom.gDashSovitsBadge.textContent, '启动中');
  assert.match(dom.gDashSovitsBadge.className, /yellow/);
  assert.equal(dom.gDashSovitsStatus.textContent, '引擎正在加载模型'); passed++;
}
{
  const dom = { gDashSovitsBadge: element(), gDashSovitsStatus: element(), gDashSovitsUrl: element() };
  initGlobalSettings(dom);
  globalThis.fetch = async url => response(url.endsWith('/engine-status')
    ? { state: 'loading_slow', ready: false, message: '引擎加载时间较长，请查看引擎日志' }
    : { gpt_sovits: { status: 'unreachable' } });
  await fetchSystemTelemetry();
  assert.equal(dom.gDashSovitsBadge.textContent, '加载较慢');
  assert.match(dom.gDashSovitsStatus.textContent, /时间较长/); passed++;
}
{
  // A periodic status request that started before a restart must not overwrite progress.
  const dom = { gDashSovitsBadge: element(), gDashSovitsStatus: element(), gDashSovitsUrl: element(),
    gBtnRestartSovits: element(), gBtnTogglePrecision: element() };
  initGlobalSettings(dom);
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
{
  const methods = [];
  globalThis.fetch = async (_url, options) => {
    methods.push(options.method || 'GET');
    if (options.method === 'POST') throw new TypeError('Failed to fetch');
    return response(cfg(1.8));
  };
  assert.equal((await saveConfirmedConfig({ speed_factor: 1.8 })).confirmed_after_disconnect, true);
  assert.deepEqual(methods, ['POST', 'GET']); passed++;
}
{
  const methods = [];
  globalThis.fetch = async (_url, options) => {
    methods.push(options.method || 'GET');
    if (options.method === 'POST') throw new TypeError('Failed to fetch');
    return response(cfg(1));
  };
  await assert.rejects(saveConfirmedConfig({ speed_factor: 1.8 }), /启动.bat/);
  assert.deepEqual(methods, ['POST', 'GET']); passed++;
}
{
  let count = 0;
  globalThis.fetch = async () => { count++; throw new TypeError('Failed to fetch'); };
  await assert.rejects(saveConfirmedConfig({ telegram_bot_token: 'private' }));
  assert.equal(count, 1); passed++;
}
{
  const dom = { gParamSpeed: element('1'), gTgSave: element(), gStatus: element() };
  initGlobalSettings(dom);
  globalThis.fetch = async () => response(cfg(1));
  await loadGlobalConfig();
  dom.gParamSpeed.value = '1.8'; dom.gParamSpeed.change();
  await loadGlobalConfig();
  assert.equal(dom.gParamSpeed.value, '1.8'); passed++;
  globalThis.fetch = async () => response({ detail: [{ msg: 'invalid' }] }, 422);
  await saveGlobalConfig();
  assert.equal(dom.gTgSave.disabled, false);
  assert.equal(dom.gParamSpeed.value, '1.8'); passed++;
}
{
  const dom = { gParamSpeed: element('1') };
  initGlobalSettings(dom);
  let complete;
  globalThis.fetch = () => new Promise(resolve => { complete = resolve; });
  const pending = loadGlobalConfig();
  dom.gParamSpeed.value = '1.7'; dom.gParamSpeed.change();
  complete(response(cfg(1))); await pending;
  assert.equal(dom.gParamSpeed.value, '1.7'); passed++;
}
{
  const dom = { gParamSpeed: element('1.8'), gTgSave: element() };
  initGlobalSettings(dom);
  let complete;
  globalThis.fetch = async (_url, options) => options.method === 'POST'
    ? new Promise(resolve => { complete = resolve; }) : response(cfg(1.8));
  await loadGlobalConfig();
  const pending = saveGlobalConfig();
  dom.gParamSpeed.value = '1.9'; dom.gParamSpeed.change();
  complete(response({ status: 'ok' })); await pending;
  await loadGlobalConfig();
  assert.equal(dom.gParamSpeed.value, '1.9');
  assert.equal(dom.gTgSave.disabled, false); passed++;
}
{
  // The config never loaded (backend restarting): saving would overwrite every setting with
  // the form's defaults, stopping Telegram and resetting the engine URL and precision.
  const dom = { gParamSpeed: element('1'), gTgEnabled: element(), gTgSave: element() };
  initGlobalSettings(dom);
  const posts = [];
  globalThis.fetch = async (_url, options = {}) => {
    if (options.method === 'POST') { posts.push(JSON.parse(options.body)); return response({ status: 'ok' }); }
    return response({ detail: '后端正在重启' }, 503);
  };
  await loadGlobalConfig();
  dom.gParamSpeed.value = '1.2'; dom.gParamSpeed.change();
  await saveGlobalConfig();
  assert.deepEqual(posts, []); passed++;
}
{
  // An edit made while the first load is in flight keeps the server's values in every other field.
  const dom = { gParamSpeed: element('1'), gTgEnabled: element(), gParamSovitsUrl: element(''), gTgSave: element() };
  initGlobalSettings(dom);
  let complete;
  globalThis.fetch = () => new Promise(resolve => { complete = resolve; });
  const pending = loadGlobalConfig();
  dom.gParamSpeed.value = '1.3'; dom.gParamSpeed.change();
  complete(response({ status: 'ok', settings: { speed_factor: 1, telegram_enabled: true, gpt_sovits_url: 'http://10.0.0.5:9880' } }));
  await pending;
  assert.equal(dom.gParamSpeed.value, '1.3');
  assert.equal(dom.gTgEnabled.checked, true);
  assert.equal(dom.gParamSovitsUrl.value, 'http://10.0.0.5:9880');
  const posts = [];
  globalThis.fetch = async (_url, options = {}) => {
    if (options.method === 'POST') posts.push(JSON.parse(options.body));
    return response({ status: 'ok', settings: {} });
  };
  await saveGlobalConfig();
  assert.equal(posts[0].speed_factor, 1.3);
  assert.equal(posts[0].telegram_enabled, true);
  assert.equal(posts[0].gpt_sovits_url, 'http://10.0.0.5:9880'); passed++;
}
{
  // Restarting before the config loaded must not send the dropdown's default precision.
  const dom = { gBtnRestartSovits: element(), gBtnTogglePrecision: element(), gPrecision: element('auto') };
  initGlobalSettings(dom);
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
  const dom = { gBtnRestartSovits: element(), gBtnTogglePrecision: element(), gPrecision: element('cpu') };
  initGlobalSettings(dom);
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
{
  const dom = Object.fromEntries(['gDashVersionCommit', 'gDashVersionBranch', 'gDashVersionBadge',
    'gRemoteStatusTag', 'gBtnApplyUpdate', 'gBtnApplyUpdateText', 'gBtnCheckUpdate', 'gBtnCheckUpdateText',
    'gUpdateNoticeBox', 'gUpdateNoticeTitle', 'gUpdateNoticeDesc', 'gUpdateNoticeIcon'].map(name => {
    const el = element(); el.classList = { add() {}, remove() {} }; return [name, el];
  }));
  initUpdateManager(dom);
  const portable = { current_version: '2.1.0', current_branch: 'portable', has_update: false,
    error: 'Windows 便携版请下载新版完整发布包更新' };
  assert.equal(formatUpdateStatus(portable).canUpdate, false);
  renderSystemVersionUI(portable);
  assert.equal(dom.gBtnApplyUpdate.disabled, true);
  assert.equal(dom.gRemoteStatusTag.textContent, 'Windows 便携版');
  assert.equal(dom.gDashVersionBadge.textContent, '便携版');
  assert.match(dom.gUpdateNoticeDesc.textContent, /data、audio、characters/); passed++;
  let requests = 0;
  globalThis.fetch = async () => { requests++; throw new Error('portable must not invoke Git updates'); };
  await handleCheckUpdate(); await handleApplyUpdate();
  assert.equal(requests, 0); passed++;
  renderSystemVersionUI({ current_branch: 'main', current_version: 'abc', has_update: true, behind_count: 2 });
  assert.equal(dom.gBtnApplyUpdate.disabled, false);
  assert.match(dom.gBtnApplyUpdateText.textContent, /2 个新提交/); passed++;
}
{
  // Retired memory, nickname, and persona controls used to ride along on every global save.
  // GET /api/config returns SettingsResponse and never contains those keys, so a lost POST
  // could not confirm. saveGlobalConfig must ignore them, accept the readback, and clear the draft.
  const previousDocument = globalThis.document;
  const doc = fakeDocument();
  globalThis.document = doc;
  const toastRoot = doc.createElement('div');
  toastRoot.id = 'toastRoot';
  doc.body.appendChild(toastRoot);
  const control = (tag, value, checked = false) => {
    const el = doc.createElement(tag);
    el.value = value;
    el.checked = checked;
    return el;
  };
  const dom = {
    gParamSovitsUrl: control('input', 'http://127.0.0.1:9880'),
    gPrecision: control('select', 'auto'),
    gParamSliceMethod: control('select', 'cut5'),
    gParamSpeed: control('input', '1'),
    gParamTopK: control('input', '15'),
    gParamTopP: control('input', '1'),
    gParamTemp: control('input', '1'),
    gParamFragmentInterval: control('input', '0.3'),
    gAudioRetention: control('input', '30'),
    gTgEnabled: control('input', '', false),
    gTgToken: control('input', ''),
    gTgChatId: control('input', ''),
    gTgProxyHost: control('input', '127.0.0.1'),
    gTgProxyPort: control('input', '10809'),
    gTgProxyEnabled: control('input', '', false),
    gMemoryEnabled: control('input', '', true),
    gUserNickname: control('input', ''),
    gDefaultSystemPrompt: control('textarea', ''),
    gTgSave: control('button', ''),
  };
  const persisted = (speed) => ({
    active_provider_id: 'deepseek',
    active_voice_profile_id: null,
    gpt_sovits_url: 'http://127.0.0.1:9880',
    audio_output_dir: 'audio',
    audio_retention_minutes: 30,
    audio_cleanup_interval_sec: 600,
    speed_factor: speed,
    temperature: 1,
    top_k: 15,
    top_p: 1,
    seed: -1,
    batch_size: 1,
    text_split_method: 'cut5',
    fragment_interval: 0.3,
    telegram_bot_username: 'galgame2voice_bot',
    telegram_proxy_host: '127.0.0.1',
    telegram_proxy_port: 10809,
    telegram_proxy_enabled: false,
    telegram_enabled: false,
    telegram_admin_ids: '',
    allow_private_llm_endpoints: false,
    console_url: '',
    max_history_messages: 10,
    inference_precision: 'auto',
    stt_engine: 'browser',
    telegram_chat_id: '',
    telegram_bot_token: '',
    console_token: '',
    updated_at: null,
  });
  try {
    for (const kind of ['disconnect', 'timeout']) {
      initGlobalSettings(dom);
      const posts = [];
      let configGets = 0;
      for (const toast of [...toastRoot.children]) clearTimeout(toast._timer);
      toastRoot.replaceChildren();
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
          const speed = configGets === 1 ? 1 : configGets === 2 ? 1.8 : 1;
          return response({ status: 'ok', settings: persisted(speed), active_provider: null });
        }
        return response({});
      };
      await loadGlobalConfig();
      dom.gParamSpeed.value = '1.8';
      dom.gParamSpeed.dispatchEvent({ type: 'input' });
      await saveGlobalConfig();
      await tick();
      const messages = [...toastRoot.children].map(node => node.textContent);
      const detail = `${kind}: toasts=[${messages.join(' | ')}] gets=${configGets} posted=${JSON.stringify(posts[0])}`;
      assert.equal(posts.length, 1, detail);
      assert.ok(configGets >= 2, detail);
      assert.ok(messages.includes('全局配置已保存'), detail);
      assert.equal(messages.some(message => message.includes('保存失败')), false, detail);
      assert.equal(messages.some(message => message.includes('尚未保存')), false, detail);
      assert.equal(Number(dom.gParamSpeed.value), 1, detail);
      assert.equal(dom.gTgSave.disabled, false, detail);
      assert.equal(Object.hasOwn(posts[0], 'memory_enabled'), false, detail);
      assert.equal(Object.hasOwn(posts[0], 'user_nickname'), false, detail);
      assert.equal(Object.hasOwn(posts[0], 'system_prompt'), false, detail);
      assert.equal(posts[0].speed_factor, 1.8, detail);
      passed++;
    }
  } finally {
    for (const toast of [...toastRoot.children]) clearTimeout(toast._timer);
    globalThis.document = previousDocument;
  }
}
console.log(`Runtime recovery: ${passed} scenarios passed`);
