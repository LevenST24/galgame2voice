import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.document ??= { getElementById: () => null, hidden: false };
const { createTelemetrySettingsController } = await import('../frontend/src/controllers/telemetry_settings.js');

const button = () => ({ disabled: false, innerHTML: '<span>启动 SoVITS 引擎</span>', hidden: false });

test('the start button is locked while another launch is still loading, and unlocked after', () => {
  let restarting = false;
  const dom = { gBtnRestartSovits: button(), gBtnTogglePrecision: button(), gDashSovitsBadge: {}, gDashSovitsStatus: {} };
  const telemetry = createTelemetrySettingsController(dom, { isRestarting: () => restarting });

  // Opened after a reload (or from another tab) while the engine loads: pressing 启动 would kill that load.
  telemetry.renderEngineStatus({ state: 'starting', ready: false, message: '引擎正在加载模型，请稍候。' });
  assert.equal(dom.gBtnRestartSovits.disabled, true);
  assert.equal(dom.gBtnTogglePrecision.disabled, true);
  assert.match(dom.gBtnRestartSovits.innerHTML, /正在加载模型/);

  // A slow load must stay restartable.
  telemetry.renderEngineStatus({ state: 'loading_slow', ready: false, message: '引擎加载时间较长。' });
  assert.equal(dom.gBtnRestartSovits.disabled, false);
  assert.equal(dom.gBtnTogglePrecision.disabled, false);
  assert.match(dom.gBtnRestartSovits.innerHTML, /重启 SoVITS 引擎/);

  telemetry.renderEngineStatus({ state: 'starting', ready: false, message: '引擎正在加载模型。' });
  telemetry.renderEngineStatus({ state: 'ready', ready: true, message: '语音引擎已就绪。' });
  assert.equal(dom.gBtnRestartSovits.disabled, false);
  assert.match(dom.gBtnRestartSovits.innerHTML, /重启 SoVITS 引擎/);

  // This tab's own restart manages the button itself.
  restarting = true;
  dom.gBtnRestartSovits.disabled = true;
  telemetry.renderEngineStatus({ state: 'starting', ready: false, message: '引擎正在加载模型。' });
  restarting = false;
  dom.gBtnRestartSovits.disabled = false;
  telemetry.renderEngineStatus({ state: 'ready', ready: true, message: '语音引擎已就绪。' });
  assert.equal(dom.gBtnRestartSovits.disabled, false);
});
