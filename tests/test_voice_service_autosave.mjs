import assert from 'node:assert/strict';
import test from 'node:test';
import { initGlobalSettings, loadGlobalConfig, autosaveConfig } from '../frontend/src/controllers/global_settings.js';
import { initEngineDirectory } from '../frontend/src/controllers/engine_directory.js';

const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const settle = async () => { for (let i = 0; i < 10; i += 1) await tick(); };
function element(value = '') {
  const listeners = {};
  return { value, textContent: '', innerHTML: '', disabled: false, children: [], classList: { add() {}, remove() {} },
    addEventListener(name, handler) { (listeners[name] ||= []).push(handler); },
    removeEventListener(name, handler) { listeners[name] = (listeners[name] || []).filter(h => h !== handler); },
    dispatch(name = 'change') { return Promise.all((listeners[name] || []).map(handler => handler({ target: this }))); },
    appendChild(child) { this.children.push(child); },
  };
}
globalThis.document = { getElementById: () => null, createElement: () => element() };
globalThis.localStorage = { getItem: () => null, setItem() {} };

function fixture(t, { saveGate = null } = {}) {
  const stored = { gpt_sovits_url: 'http://127.0.0.1:9880', audio_retention_minutes: 30, inference_precision: 'auto' };
  const writes = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/config' && options.method === 'POST') {
      const payload = JSON.parse(options.body);
      writes.push(payload);
      if (saveGate) await saveGate(payload);
      Object.assign(stored, payload);
      return response({ status: 'ok', settings: { ...stored } });
    }
    if (url === '/api/config') return response({ status: 'ok', settings: { ...stored } });
    if (url === '/api/providers') return response({ providers: [], presets: [] });
    return response({});
  };
  const dom = {
    gParamSovitsUrl: element(),
    gAudioRetention: element(),
    gPrecision: element('auto'),
    gResetBtn: element(),
    gTgSave: element(),
  };
  initGlobalSettings(dom);
  t.after(settle);
  return { dom, stored, writes };
}

test('a finished edit on the voice service page is stored without pressing save', async t => {
  const state = fixture(t);
  await loadGlobalConfig();
  state.dom.gParamSovitsUrl.value = 'http://127.0.0.1:9999';
  await state.dom.gParamSovitsUrl.dispatch('input');
  await state.dom.gParamSovitsUrl.dispatch('change');
  await settle();
  assert.equal(state.stored.gpt_sovits_url, 'http://127.0.0.1:9999');

  state.dom.gAudioRetention.value = '90';
  await state.dom.gAudioRetention.dispatch('input');
  await state.dom.gAudioRetention.dispatch('change');
  await settle();
  assert.equal(state.stored.audio_retention_minutes, 90);

  // Reopening reads the stored values back instead of keeping a pending draft.
  await loadGlobalConfig();
  assert.equal(state.dom.gParamSovitsUrl.value, 'http://127.0.0.1:9999');
  assert.equal(state.dom.gAudioRetention.value, 90);
});

test('an edit made while an autosave runs is saved right after it', async t => {
  let release;
  let gated = true;
  const state = fixture(t, {
    saveGate: () => (gated ? new Promise(resolve => { release = resolve; }) : undefined),
  });
  await loadGlobalConfig();
  state.dom.gAudioRetention.value = '45';
  await state.dom.gAudioRetention.dispatch('input');
  state.dom.gAudioRetention.dispatch('change');
  await settle();
  state.dom.gAudioRetention.value = '60';
  await state.dom.gAudioRetention.dispatch('input');
  state.dom.gAudioRetention.dispatch('change');
  gated = false;
  release();
  await settle();
  assert.equal(state.stored.audio_retention_minutes, 60);
  assert.deepEqual(state.writes.map(w => w.audio_retention_minutes), [45, 60]);
});

test('an edit made before the config was read is saved once the read finishes', async t => {
  const state = fixture(t);
  state.dom.gParamSovitsUrl.value = 'http://10.0.0.2:9880';
  await state.dom.gParamSovitsUrl.dispatch('input');
  await autosaveConfig();
  assert.equal(state.writes.length, 0, 'saving before the read would overwrite with defaults');
  await loadGlobalConfig();
  await settle();
  assert.equal(state.stored.gpt_sovits_url, 'http://10.0.0.2:9880');
});

test('restoring defaults stores them at once', async t => {
  const state = fixture(t);
  state.stored.audio_retention_minutes = 120;
  state.stored.inference_precision = 'fp32';
  await loadGlobalConfig();
  globalThis.confirm = () => true;
  t.after(() => { delete globalThis.confirm; });
  await state.dom.gResetBtn.dispatch('click');
  await settle();
  assert.equal(state.stored.audio_retention_minutes, 30);
  assert.equal(state.stored.inference_precision, 'auto');
});

test('the engine folder saves itself after typing or picking one', async () => {
  const puts = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/system/sovits-directory' && options.method === 'PUT') {
      const { directory } = JSON.parse(options.body);
      puts.push(directory);
      return response({ directory, message: '目录已保存。' });
    }
    if (url === '/api/system/sovits-directory') return response({ directory: 'D:/old' });
    if (url === '/api/voice/browse-file') return response({ selected_path: 'D:/picked' });
    return response({});
  };
  const dom = {
    gSovitsDirectory: element(),
    gSovitsDirectoryStatus: element(),
    gBrowseSovitsDirectory: element(),
    gSaveSovitsDirectory: element(),
  };
  const directory = initEngineDirectory(dom, () => {});
  await directory.load();
  dom.gSovitsDirectory.value = 'D:/typed';
  await dom.gSovitsDirectory.dispatch('input');
  await dom.gSovitsDirectory.dispatch('change');
  await settle();
  assert.deepEqual(puts, ['D:/typed']);
  assert.equal(directory.isDirty(), false);

  await directory.browse();
  await settle();
  assert.deepEqual(puts, ['D:/typed', 'D:/picked']);

  // Clearing the box is not a folder to save.
  dom.gSovitsDirectory.value = '';
  await dom.gSovitsDirectory.dispatch('input');
  await dom.gSovitsDirectory.dispatch('change');
  await settle();
  assert.deepEqual(puts, ['D:/typed', 'D:/picked']);
  directory.dispose();
});
