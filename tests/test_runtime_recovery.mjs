import assert from 'node:assert/strict';
import { requestJson, getErrorMessage } from '../frontend/src/request.js';
import { streamChat } from '../frontend/src/ai.js';
import { waitForEngine } from '../frontend/src/controllers/engine_recovery.js';
import { saveConfirmedConfig } from '../frontend/src/controllers/config_recovery.js';
import { ensureSessionVoice, initVoiceSettings } from '../frontend/src/controllers/voice_settings.js';
import { initGlobalSettings, loadGlobalConfig, saveGlobalConfig, handleRestartSovits, savePrivateEndpointPreference, handleProviderTest, handleActivateProvider } from '../frontend/src/controllers/global_settings.js';
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
  assert.equal(session.settings.voiceProfileId, null); passed++;
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
console.log(`Runtime recovery: ${passed} scenarios passed`);
