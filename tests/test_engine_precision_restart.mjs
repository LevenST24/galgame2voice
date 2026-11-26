// A restart that fails after the server saved the requested precision must not
// leave the 计算方式 dropdown on the old value, or the next start reverts it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createEngineSettingsController } from '../frontend/src/controllers/engine_settings.js';

const response = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'Content-Type': 'application/json' },
});
const el = (props = {}) => ({ disabled: false, innerHTML: '', textContent: '', value: '', addEventListener() {}, removeEventListener() {}, ...props });

function harness(failure) {
  const dom = { gBtnRestartSovits: el(), gBtnTogglePrecision: el(), gPrecision: el({ value: 'fp16' }),
    gDashPrecisionBadge: el({ textContent: 'FP16' }) };
  const sent = [];
  let saved = 'fp16';
  let failNext = true;
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  globalThis.document = { getElementById: () => null };
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/system/restart_sovits') {
      const body = JSON.parse(options.body);
      sent.push(body.precision);
      if (body.precision && failure.saves !== false) saved = body.precision;
      if (failNext) { failNext = false; return response({ detail: failure.detail }, failure.status); }
      return response({ status: 'starting', message: 'ok' });
    }
    if (url === '/api/system/engine-status') {
      return response(sent.length > 1 ? { state: 'ready', ready: true, message: 'ok' } : { state: 'unavailable', ready: false, message: '语音引擎未运行' });
    }
    if (url === '/api/config') return response({ settings: { inference_precision: saved } });
    return response({});
  };
  const ctl = createEngineSettingsController(dom, { isConfigLoaded: () => true, isGpuAvailable: () => true,
    showToast() {}, renderEngineStatus() {}, fetchSystemTelemetry() {}, loadGlobalConfig() {} });
  const restore = () => {
    ctl.dispose();
    globalThis.fetch = originalFetch;
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
  };
  return { ctl, dom, sent, restore };
}

for (const [name, failure] of [
  ['old engine still holds the port', { status: 409, detail: { code: 'SOVITS_PORT_STILL_IN_USE', message: '旧 GPT-SoVITS 进程未在 5 秒内释放端口 9880，请等待片刻再试。' } }],
  ['new engine exits at once', { status: 502, detail: { code: 'SOVITS_EXITED', message: '语音引擎启动后立即退出。' } }],
]) {
  test(`after a failed precision switch (${name}) the next start keeps the saved precision`, async () => {
    const { ctl, dom, sent, restore } = harness(failure);
    try {
      await ctl.handleTogglePrecision();
      assert.ok(dom.gPrecision.value === 'fp32', 'dropdown shows ' + dom.gPrecision.value);
      await ctl.handleRestartSovits();
      assert.ok(sent[0] === 'fp32');
      assert.ok(sent[1] === 'fp32', 'second start sent ' + sent[1]);
    } finally {
      restore();
    }
  });
}

test('a start refused before anything is saved leaves the dropdown alone', async () => {
  const { ctl, dom, sent, restore } = harness({ status: 400, saves: false,
    detail: '所选文件夹缺少 api_v2.py。请完整解压引擎包，在「语音服务」重新选择引擎文件夹。' });
  try {
    dom.gPrecision.value = 'cpu';
    await ctl.handleRestartSovits();
    assert.ok(sent[0] === 'cpu');
    assert.ok(dom.gPrecision.value === 'cpu', 'dropdown shows ' + dom.gPrecision.value);
  } finally {
    restore();
  }
});
