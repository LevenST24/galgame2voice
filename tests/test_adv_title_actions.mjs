import assert from 'node:assert/strict';
import test from 'node:test';
import { createTitleActions } from '../frontend/src/adv/actions.js';
import { showTitle } from '../frontend/src/adv/title_hook.js';
import { prepareSessionImport, applySessionImport, state, loadState, getActive, getSession } from '../frontend/src/store.js';
import { setup } from './helpers/adv_main.mjs';
import { initApp, advActions } from '../frontend/src/main.js';
import { fakeDocument } from './helpers/adv_dom.mjs';
import { tick } from './helpers/chat_stream.mjs';

function actions(sessions) {
  const calls = [];
  const api = createTitleActions({ getSessions: () => sessions, getProfiles: () => [{ id: 7, name: '角色甲' }],
    startNew: () => calls.push('new'), loadSession: id => calls.push(id),
    openCharacters: () => calls.push('characters'), openSettings: () => calls.push('settings') });
  return { api, calls };
}

test('listSessions returns all six fields newest first, with bound character, last line and background', () => {
  const sessions = [
    { id: 'old', title: '旧会话', updatedAt: 10, settings: {}, messages: [{ content: '旧消息' }] },
    { id: 'new', title: '新会话', updatedAt: 30, settings: { voiceProfileId: 7, advBackground: 'cafe' },
      messages: [{ content: '完整回复', lines: [{ zh: '第一句' }, { zh: '', ja: '最後の台詞' }] }] },
    { id: 'unloaded', title: '未加载历史', updatedAt: 20, settings: { voiceProfileId: 999 }, messages: [], lastLine: '后端摘要' },
  ];
  const { api } = actions(sessions);
  assert.deepEqual(api.listSessions(), [
    { id: 'new', title: '新会话', characterName: '角色甲', lastLine: '最後の台詞', updatedAt: 30, background: 'cafe' },
    { id: 'unloaded', title: '未加载历史', characterName: '', lastLine: '后端摘要', updatedAt: 20, background: '' },
    { id: 'old', title: '旧会话', characterName: '', lastLine: '旧消息', updatedAt: 10, background: '' },
  ]);
  assert.deepEqual(sessions.map(s => s.id), ['old', 'new', 'unloaded']);
  api.listSessions()[0].title = 'changed'; assert.equal(sessions[1].title, '新会话');
  sessions[0].updatedAt = 40; assert.equal(api.listSessions()[0].id, 'old');
});

test('empty summaries have predictable fields and same timestamps preserve store order', () => {
  const { api } = actions([{ id: 'a' }, { id: 'b', updatedAt: 0 }]);
  assert.deepEqual(api.listSessions()[0], { id: 'a', title: '新对话', characterName: '', lastLine: '', updatedAt: 0, background: '' });
  assert.deepEqual(api.listSessions().map(s => s.id), ['a', 'b']);
});

test('title actions delegate; continue uses newest or creates a first session; hook defaults to stage', () => {
  const sessions = [{ id: 'old', updatedAt: 1 }, { id: 'latest', updatedAt: 2 }];
  const { api, calls } = actions(sessions);
  assert.deepEqual(Object.keys(api).sort(), ['continueLatest', 'listSessions', 'loadSession', 'openCharacters', 'openSettings', 'startNew']);
  assert.equal(showTitle(api, { documentObj: { querySelector: () => null } }), null); assert.deepEqual(calls, []);
  api.startNew(); api.continueLatest(); api.loadSession('old'); api.openCharacters(); api.openSettings();
  assert.deepEqual(calls, ['new', 'latest', 'old', 'characters', 'settings']);
  sessions.length = 0; api.continueLatest(); assert.equal(calls.at(-1), 'new');
});

for (const value of [false, true, 'obsolete']) test(`old export with advEnabled=${value} imports and opens the only stage`, t => {
  const storage = new Map();
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
  const old = { format: 'galgame2voice.chat', version: 1, sessions: [{ id: 'legacy', title: '旧会话', settings: { advEnabled: value, advBackground: 'cafe' },
    messages: [{ id: 'm_1', role: 'user', content: '保留文字' }], updatedAt: 8 }], activeId: 'legacy' };
  storage.set('gal2voice.chat.v1', JSON.stringify(old)); loadState();
  assert.equal(getActive().settings.advEnabled, undefined);
  assert.equal(getActive().messages[0].content, '保留文字');
  const imported = prepareSessionImport(JSON.stringify(old)); applySessionImport(imported);
  assert.equal(getActive().settings.advEnabled, undefined);
  const h = setup(t, { callbacks: { state, getActive, getSession } });
  assert.ok(h.app.adv); assert.equal(h.app.adv.player.state, 'input');
  assert.equal(h.app.actions.listSessions()[0].lastLine, '保留文字');
});

test('startup calls showTitle once after authentication completes, with the public action object', async t => {
  const doc = fakeDocument(); globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div'); root.id = 'advRoot'; doc.body.appendChild(root);
  const storage = new Map();
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
  const events = []; let finishAuth, titles = 0;
  globalThis.location = { origin: 'http://p7.test' };
  globalThis.window = { innerWidth: 1280, location: { search: '', hash: '' },
    sessionStorage: { getItem: () => '', setItem() {} },
    prompt: () => { events.push('prompt'); return 'synthetic-test-token'; },
    fetch: async (url, options = {}) => {
      if (url.startsWith('/api/chat/sessions?')) {
        if (!options.headers?.get('Authorization')) { events.push('401'); return new Response('', { status: 401 }); }
        events.push('authenticated'); return new Promise(resolve => { finishAuth = () => resolve(Response.json({ sessions: [], has_more: false })); });
      }
      return Response.json(url === '/api/voice/profiles' ? { profiles: [], active_profile_id: null } : { providers: [], backgrounds: [] });
    },
  };
  globalThis.fetch = (...args) => window.fetch(...args);
  const app = initApp({ dom: { advRoot: root }, showTitle: actions => {
    titles++; events.push('title');
    assert.deepEqual(Object.keys(actions).sort(), Object.keys(advActions).sort());
    assert.equal(typeof actions.continueLatest, 'function');
  } });
  t.after(() => { app.conversation.stop(); app.adv.unmount(); delete globalThis.document; delete globalThis.location; });
  await tick(); assert.equal(titles, 0); assert.ok(finishAuth); assert.ok(app.adv);
  finishAuth(); await app.ready;
  assert.equal(titles, 1); assert.deepEqual(events, ['401', 'prompt', 'authenticated', 'title']);
});

test('showTitle mounts the title screen on the stage overlay and opens it', () => {
  const doc = fakeDocument();
  const overlay = doc.createElement('div');
  doc.body.appendChild(overlay);
  doc.querySelector = selector => (selector === '.adv-layer-overlay' ? overlay : null);
  const { api, calls } = actions([]);
  const screen = showTitle(api, { documentObj: doc });
  assert.ok(screen && screen.isOpen);
  assert.ok(overlay.children.length > 0);
  assert.deepEqual(calls, []);
  const again = showTitle(api, { documentObj: doc });
  assert.notEqual(again, screen);
  assert.equal(screen.isOpen, false);
  again.destroy();
});
