import assert from 'node:assert/strict';
import test from 'node:test';
import * as api from '../frontend/src/api.js';
import { initApp } from '../frontend/src/main.js';
import { playAiVoice, stopCurrentVoice } from '../frontend/src/controllers/audio_player_ui.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

test('api.js does not export the unused fetchProvider wrapper', () => {
  assert.equal('fetchProvider' in api, false);
  assert.equal(typeof api.fetchProviders, 'function');
  assert.equal(typeof api.activateProvider, 'function');
});

test('the app does not give audio playback a callback that translates replies lacking Japanese', async t => {
  const doc = fakeDocument(); globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div'); root.id = 'advRoot'; doc.body.appendChild(root);
  const toasts = doc.createElement('div'); toasts.id = 'toastRoot'; doc.body.appendChild(toasts);
  const storage = new Map();
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  const requested = [];
  globalThis.window = { innerWidth: 1280, location: { search: '', hash: '' },
    sessionStorage: { getItem: () => '', setItem() {} },
    fetch: async url => {
      requested.push(String(url));
      return Response.json({ sessions: [], has_more: false, providers: [], profiles: [], backgrounds: [] });
    } };
  globalThis.fetch = (...args) => window.fetch(...args);
  const app = initApp({ dom: { advRoot: root }, renderSessionList() {}, showTitle() {} });
  t.after(() => {
    stopCurrentVoice(); app.conversation.stop(); app.adv.unmount();
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document; delete globalThis.window; delete globalThis.localStorage;
    delete globalThis.fetch; delete globalThis.location;
  });
  await app.ready;

  await playAiVoice({ id: 'legacy_no_japanese', role: 'assistant', content: '你好', japanese: '', audioUrls: [] },
    { setLoading() {}, setPlaying() {}, setProgress() {} });

  assert.deepEqual(requested.filter(url => url.includes('/api/chat/japanese')), []);
});
