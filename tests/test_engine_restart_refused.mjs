import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.document ??= { getElementById: () => null, hidden: false };
const { createEngineSettingsController } = await import('../frontend/src/controllers/engine_settings.js');

const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });

// Each of these is a 409 from POST /api/system/restart_sovits: the restart was refused, not a failed start.
for (const [name, detail] of [
  ['another tab is already restarting', '语音引擎正在启动或重启，请等待当前操作完成，勿重复点击。'],
  ['the engine already runs in another window', '语音引擎已在其他窗口运行，可直接使用。若需重启，请先关闭该引擎窗口，再在「运行状态」页启动引擎。'],
  ['the configured engine is remote', {
    code: 'REMOTE_SOVITS_NOT_MANAGED', base_url: 'http://192.168.1.50:9880',
    message: '当前 GPT-SoVITS 地址为远端服务 (http://192.168.1.50:9880)，本机不会管理（拉起/重启）远端进程；请在远端主机上自行重启引擎。',
  }],
]) test(`a restart refused because ${name} is not shown as 启动失败`, async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async (url) => {
    assert.equal(url, '/api/system/restart_sovits');
    return response({ detail }, 409);
  };
  const rendered = [];
  const toasts = [];
  try {
    const engine = createEngineSettingsController({}, {
      renderEngineStatus: (status) => rendered.push(status),
      showToast: (message, type) => toasts.push({ message, type }),
      isConfigLoaded: () => true,
    });
    await engine.handleRestartSovits();
  } finally {
    globalThis.fetch = original;
  }
  const last = rendered.at(-1);
  // telemetry_settings maps state 'failed' to the red 「启动失败」 badge.
  assert.equal(last.state, 'checking', JSON.stringify(last));
  assert.match(last.message, /引擎|远端/);
  assert.deepEqual(toasts.map(toast => toast.type), ['info']);
});
