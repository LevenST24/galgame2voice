import assert from 'node:assert/strict';
import { test, afterEach } from 'node:test';
import { createEngineSettingsController } from '../frontend/src/controllers/engine_settings.js';

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

async function savePrecision(value, gpuAvailable) {
  globalThis.fetch = async () => new Response(JSON.stringify({ settings: { inference_precision: value } }), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  });
  const toasts = [];
  const controller = createEngineSettingsController({ gPrecision: { value } }, {
    isGpuAvailable: () => gpuAvailable,
    showToast: (message, kind) => toasts.push({ message, kind }),
  });
  await controller.onPrecisionChange();
  controller.dispose?.();
  return toasts;
}

test('the saved-precision toast names the button the status page actually shows', async () => {
  const [toast] = await savePrecision('cpu', true);
  assert.match(toast.message, /已保存计算方式/);
  assert.doesNotMatch(toast.message, /重启语音引擎/);
  assert.match(toast.message, /SoVITS 引擎/);
  assert.equal(toast.kind, 'info');
});

test('choosing FP16 or FP32 without a detected NVIDIA card warns that the engine may not start', async () => {
  for (const value of ['fp16', 'fp32']) {
    const [toast] = await savePrecision(value, false);
    assert.equal(toast.kind, 'warning', value);
    assert.match(toast.message, /未检测到 NVIDIA 显卡/);
    assert.match(toast.message, /CPU 模式/);
  }
});

test('FP16 with a detected card and CPU without one keep the plain notice', async () => {
  assert.equal((await savePrecision('fp16', true))[0].kind, 'info');
  assert.equal((await savePrecision('cpu', false))[0].kind, 'info');
});
