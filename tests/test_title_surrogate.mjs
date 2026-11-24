import assert from 'node:assert/strict';
import test from 'node:test';
import { autoTitle } from '../frontend/src/store.js';
import { persistSession } from '../frontend/src/session_api.js';
import { createTitleScreen } from '../frontend/src/adv/title.js';
import { fakeDocument, fakeClock, microtasks } from './helpers/adv_dom.mjs';

const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

test('an automatic title never ends in half an emoji', () => {
  // The emoji sits at UTF-16 units 19-20, so a 20-unit cut would keep only its first half.
  const session = { title: '新对话', messages: [{ role: 'user', content: '今天终于考完试了好开心我们出去玩吧走呀😊还想去吃火锅' }] };
  assert.equal(autoTitle(session), true);
  assert.equal(LONE_SURROGATE.test(session.title), false, JSON.stringify(session.title));
  assert.equal(session.title, '今天终于考完试了好开心我们出去玩吧走呀😊…');
});

test('a short first message becomes the title unchanged', () => {
  const session = { title: '新对话', messages: [{ role: 'user', content: '晚上好🌙' }] };
  autoTitle(session);
  assert.equal(session.title, '晚上好🌙');
});

test('a title broken by an older version is repaired before it is saved', async t => {
  let body = null;
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (_url, opts) => {
    body = opts.body;
    return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => ({}), text: async () => '{}' };
  };
  t.after(() => { globalThis.fetch = realFetch; });
  await persistSession({ id: 's_broken_title', title: '走呀\uD83D…', settings: {} });
  const sent = JSON.parse(body).title;
  assert.equal(LONE_SURROGATE.test(sent), false, JSON.stringify(sent));
  assert.equal(sent, '走呀�…');
});

test('a recent conversation whose title starts with an emoji gets the whole emoji as its avatar', async t => {
  const doc = fakeDocument(), clock = fakeClock();
  const stage = doc.createElement('div'); stage.className = 'adv-stage'; doc.body.appendChild(stage);
  const root = doc.createElement('div'); root.className = 'adv-layer-overlay'; stage.appendChild(root);
  const now = new Date(2026, 9, 24, 22, 30);
  const sessions = [
    { id: 'a', title: '对话a', characterName: '', updatedAt: now.getTime(), hasUserMessages: true, settings: {}, excerpt: { zh: '嗯' }, lastLine: '嗯' },
    { id: 'b', title: '🌙夜聊', characterName: '', updatedAt: now.getTime() - 1000, hasUserMessages: true, settings: {}, excerpt: { zh: '晚安' }, lastLine: '晚安' },
  ];
  const ui = createTitleScreen(root, { documentObj: doc, clock, now: () => now, actions: {
    listSessions: () => sessions, getActiveId: () => 'a', previewSession: async () => {}, listCharacters: () => [],
    getStatus: async () => ({ model: { state: 'ok' }, modelLoaded: true, hasModel: true, engine: { state: 'ready' } }),
  } });
  t.after(() => ui.destroy()); ui.open(); await microtasks();
  assert.equal(ui.element.querySelector('.home-avatar').textContent, '🌙');
});
