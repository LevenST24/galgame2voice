import assert from 'node:assert/strict';
import { initVoiceSettings, loadSessionVoiceSelect, populateScanOptions, invalidateScannedModels } from '../frontend/src/controllers/voice_settings.js';

const makeSelect = () => ({
  value: '', innerHTML: '', options: [],
  addEventListener() {},
  appendChild(option) { this.options.push(option); },
});
globalThis.document = { createElement: () => ({ value: '', textContent: '' }) };

for (const scenario of [
  { profiles: [], bound: null, expected: '__custom__', setup: true },
  { profiles: [], bound: 99, expected: '__custom__', setup: true },
  { profiles: [{ id: 1, name: 'User voice' }], bound: null, expected: '', setup: false },
  { profiles: [{ id: 1, name: 'User voice' }], bound: 1, expected: '1', setup: false },
]) {
  const hidden = new Set(['hidden']);
  const dom = {
    sVoice: makeSelect(), sVoiceActive: { textContent: '' },
    sCustomVoiceBox: {
      classList: { toggle(name, value) { value ? hidden.add(name) : hidden.delete(name); } },
    },
  };
  globalThis.fetch = async (url) => ({
    ok: true,
    json: async () => url.endsWith('/profiles')
      ? { profiles: scenario.profiles, active_profile_id: null }
      : { gpt_weights: [], sovits_weights: [], audio_files: [] },
  });
  initVoiceSettings(dom);
  await loadSessionVoiceSelect({ settings: { voiceProfileId: scenario.bound } });
  assert.equal(dom.sVoice.value, scenario.expected);
  assert.equal(!hidden.has('hidden'), scenario.setup);
  assert.equal(dom.sVoiceActive.textContent.includes('首次使用'), scenario.setup);
}
console.log('Voice first setup: 4 scenarios passed');

// Changing engines while a scan is pending must not repopulate old weights.
{
  const dom = { sCvGpt: makeSelect(), sCvSovits: makeSelect(), sCvRef: makeSelect() };
  initVoiceSettings(dom);
  invalidateScannedModels();
  let complete;
  globalThis.fetch = () => new Promise(resolve => { complete = resolve; });
  const pending = populateScanOptions();
  invalidateScannedModels();
  complete({ ok: true, json: async () => ({ gpt_weights: [{ name: 'old', path: '/old.ckpt' }] }) });
  await pending;
  assert.equal(dom.sCvGpt.options.length, 0);
  let requests = 0;
  globalThis.fetch = async () => {
    requests++;
    return { ok: true, json: async () => ({ gpt_weights: [{ name: 'new', path: '/new.ckpt' }] }) };
  };
  await populateScanOptions();
  assert.equal(dom.sCvGpt.options[0].value, '/new.ckpt');
  await populateScanOptions();
  assert.equal(requests, 1);
}
console.log('Model scan invalidation: 1 scenario passed');
