import assert from 'node:assert/strict';
import test from 'node:test';
import { initVoiceSettings, ensureSessionVoice } from '../frontend/src/controllers/voice_settings.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });

async function runCase(switchHandler) {
  const doc = fakeDocument();
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);
  globalThis.document = doc;
  globalThis.localStorage = { getItem: () => null, setItem() {} };
  const origErr = console.error;
  console.error = () => {};
  try {
    initVoiceSettings({}, { isVoiceEnabled: () => true, onOpenEngineStatus: () => {} });
    globalThis.fetch = async url => (String(url).includes('/switch')
      ? switchHandler()
      : response({ profiles: [{ id: 1, name: 'A' }], active_profile_id: null }));
    await ensureSessionVoice({ settings: { voiceProfileId: 1 } });
    return toasts.children.map(node => { clearTimeout(node._timer); return String(node.dataset.msg || ''); });
  } finally {
    console.error = origErr;
  }
}

test('specific 502 error and REQUEST_TIMEOUT are preserved while generic 502 keeps fallback', async () => {
  const oom = '语音引擎显存不足，无法加载模型。请在「设置中心 → 语音服务 → 计算方式」改选「CPU 模式」后到「运行状态」重启引擎。';
  assert.deepEqual(await runCase(() => response({ detail: oom }, 502)), [oom]);
  const timeoutMsgs = await runCase(() => { throw Object.assign(new Error('timeout'), { code: 'REQUEST_TIMEOUT' }); });
  assert.equal(timeoutMsgs.length, 1);
  assert.match(timeoutMsgs[0], /超时/);
  assert.doesNotMatch(timeoutMsgs[0], /^语音引擎还没准备好/);
  const genericMsgs = await runCase(() => response({ detail: '语音模型尚未加载。' }, 502));
  assert.equal(genericMsgs.length, 1);
  assert.match(genericMsgs[0], /^语音引擎还没准备好/);
});
