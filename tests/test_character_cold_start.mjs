import assert from 'node:assert/strict';
import { fetchVoiceProfiles, getVoiceProfiles, initVoiceSettings } from '../frontend/src/controllers/voice_settings.js';

const toasts = [];
const root = { querySelectorAll: () => toasts, appendChild: item => toasts.push(item) };
globalThis.document = {
  getElementById: id => id === 'toastRoot' ? root : null,
  createElement: () => ({ dataset: {}, classList: { contains: () => false, add() {} }, remove() {} }),
};
const originalTimeout = globalThis.setTimeout;
globalThis.setTimeout = () => 0;
try {
  initVoiceSettings({});
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({
    profiles: [{ id: 1, name: 'Saved' }], active_profile_id: 1,
    catalog_warning: '角色包 ID 重复：ann；请修改 manifest.json 中的 id。',
  }) });
  await fetchVoiceProfiles();
  assert.equal(getVoiceProfiles()[0].name, 'Saved');
  assert.match(toasts[0]?.textContent || '', /角色包 ID 重复.*ann/);
} finally { globalThis.setTimeout = originalTimeout; }
console.log('character cold-start warning: passed');
