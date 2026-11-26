import assert from 'node:assert/strict';
import test from 'node:test';
// Import before installing a document: don't auto-boot authentication or devices.
import './helpers/adv_main.mjs';
import { tick } from './helpers/chat_stream.mjs';
import { state, loadState, deleteSession, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL } from '../frontend/src/store.js';
import { initApp } from '../frontend/src/main.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

function chat(id, updatedAt) {
  return { id, title: id, createdAt: 1, updatedAt, settings: { ...DEFAULT_SESSION_SETTINGS }, messages: [] };
}

function memoryStorage(snapshot) {
  const values = new Map(snapshot ? [['gal2voice.chat.v1', JSON.stringify(snapshot)]] : []);
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
}

test('deleting the active chat switches to the most recently used one', () => {
  globalThis.localStorage = memoryStorage({
    sessions: [chat('c', 3000), chat('b', 2000), chat('a', 4000)], activeId: 'c', global: { ...DEFAULT_GLOBAL },
  });
  try {
    loadState();
    deleteSession('c');
    assert.equal(state.activeId, 'a');
  } finally {
    delete globalThis.localStorage;
  }
});

test('the session drawer lists the most recently used chat first', async (t) => {
  const doc = fakeDocument();
  globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);
  globalThis.localStorage = memoryStorage({
    sessions: [chat('c', 3000), chat('b', 2000), chat('a', 4000)], activeId: 'c', global: { ...DEFAULT_GLOBAL },
  });
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
    fetch: async url => {
      const target = String(url);
      if (target.includes('/api/chat/sessions')) return Response.json({ sessions: [], has_more: false, count: 0 });
      if (target.includes('/api/voice/profiles')) return Response.json({ profiles: [], active_profile_id: null });
      if (target.includes('/api/providers')) return Response.json({ providers: [], active_provider: null });
      if (target.includes('/api/backgrounds')) return Response.json({ backgrounds: [] });
      return Response.json({});
    },
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);
  const drawn = [];
  const app = initApp({
    dom: { advRoot: root, sessionList: doc.createElement('div') },
    renderSessionList(_container, { sessions }) { drawn.push(sessions.map(session => session.id)); },
  });
  t.after(() => {
    try { app.conversation.stop(); app.adv?.unmount(); } catch {}
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });
  await app.ready;
  for (let i = 0; i < 5; i++) await tick();
  assert.ok(drawn.length > 0, 'the drawer was drawn');
  const lastDrawn = drawn.at(-1);
  assert.deepEqual(lastDrawn.filter(id => ['a', 'b', 'c'].includes(id)), ['a', 'c', 'b']);
});
