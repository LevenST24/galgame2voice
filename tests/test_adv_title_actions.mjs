import assert from 'node:assert/strict';
import test from 'node:test';
import { createTitleActions } from '../frontend/src/adv/actions.js';
import { showTitle } from '../frontend/src/adv/title_hook.js';
import { prepareSessionImport, applySessionImport, state, loadState, getActive, getSession } from '../frontend/src/store.js';
import { setup } from './helpers/adv_main.mjs';
import { initApp, advActions } from '../frontend/src/main.js';
import { fakeDocument, fakeClock, microtasks } from './helpers/adv_dom.mjs';
import { createTitleScreen } from '../frontend/src/adv/title.js';
import { tick } from './helpers/chat_stream.mjs';
import { fetchVoiceProfiles } from '../frontend/src/controllers/voice_settings.js';

test('model status is sampled coherently after a slow engine response and refreshes late character data on stage', async t => {
  let loaded = false, finishEngine;
  const h = setup(t, { callbacks: { haveProvidersLoaded: () => loaded,
    getCurrentActiveProvider: () => loaded ? { id: 'custom', name: '本机模型' } : null } });
  let updates = 0;
  const update = h.app.adv.update;
  h.app.adv.update = () => { updates++; update(); };
  globalThis.fetch = async url => url === '/api/system/engine-status'
    ? new Promise(resolve => { finishEngine = () => resolve(Response.json({ state: 'ready' })); })
    : Response.json({});
  const pending = h.app.actions.getStatus();
  loaded = true; finishEngine(); const status = await pending;
  assert.equal(status.modelLoaded, true); assert.equal(status.hasModel, true); assert.equal(status.model.state, 'ok');
  assert.ok(updates > 0, 'late profiles/work are reconciled with the actual stage');
});

test('previewing the same active id still refreshes stage settings and late profiles without focusing input', async t => {
  const h = setup(t);
  let updates = 0;
  const update = h.app.adv.update;
  h.app.adv.update = () => { updates++; update(); };
  h.sessions[0].messages = [{ role: 'assistant', content: '开场白' }];
  h.sessions[0].settings.voiceProfileId = 7;
  const focus = document.activeElement;
  await h.app.actions.previewSession(h.state.activeId);
  assert.ok(updates > 0);
  assert.ok(document.activeElement === focus);
});

test('session snapshots distinguish a greeting from any user turn independently of excerpt selection', () => {
  const sessions = [{ id: 'a', messages: [{ role: 'assistant', content: '开场白' }] }];
  const { api } = actions(sessions);
  assert.equal(api.listSessions()[0].hasUserMessages, false);
  sessions[0].messages.push({ role: 'user', content: '你好' });
  assert.equal(api.listSessions()[0].hasUserMessages, true);
});

test('fresh browser drops its unused local starter after sync and home previews the latest server session', async t => {
  const doc = fakeDocument(); globalThis.document = doc;
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div'); doc.body.appendChild(root);
  const storage = new Map();
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
  globalThis.location = { origin: 'http://home.test' };
  let releaseSessions, home;
  const profiles = [{ id: 7, name: '主角', work: 'fake' }, { id: 8, name: '其他角色', work: 'fake' }];
  globalThis.window = { innerWidth: 1280, location: { search: '', hash: '' },
    sessionStorage: { getItem: () => '', setItem() {} }, fetch: async url => {
      // The server sends SQLite times; anything else falls back to Date.now() per session.
      if (url.startsWith('/api/chat/sessions?')) return new Promise(resolve => { releaseSessions = () => resolve(Response.json({ sessions: [
        { id: 'latest', title: '主角会话', updated_at: '2026-10-20 08:30:00', voice_profile_id: 7 },
        { id: 'older', title: '其他会话', updated_at: '2026-10-20 08:10:00', voice_profile_id: 8 },
      ], has_more: false })); });
      if (url === '/api/voice/profiles') return Response.json({ profiles, active_profile_id: 8 });
      return Response.json({ providers: [], backgrounds: [] });
    } };
  globalThis.fetch = (...args) => window.fetch(...args);
  const app = initApp({ dom: { advRoot: root }, getVoiceProfiles: () => profiles,
    haveProvidersLoaded: () => true, getCurrentActiveProvider: () => ({ id: 'custom', name: '测试模型' }),
    fetchSessionHistoryPage: async () => ({ messages: [{ id: 1, role: 'user', content_chinese: '你好' },
      { id: 2, role: 'assistant', content_chinese: '欢迎回来' }], has_more: false }),
    showTitle: actions => {
      home = createTitleScreen(doc.querySelector('.adv-layer-overlay'), { actions, documentObj: doc, clock: fakeClock() });
      home.open();
    } });
  t.after(() => { home?.destroy(); app.conversation.stop(); app.adv.unmount(); delete globalThis.document; delete globalThis.location; });
  await tick();
  const starterId = state.activeId;
  assert.ok(starterId); assert.equal(state.sessions.length, 1);
  releaseSessions(); await app.ready; await microtasks();
  assert.equal(state.activeId, 'latest');
  assert.equal(state.sessions.some(s => s.id === starterId), false);
  assert.deepEqual(app.actions.listSessions().map(s => s.id), ['latest', 'older']);
  assert.equal(home.element.querySelector('.home-meta').children[0].textContent, '主角');
  assert.deepEqual(home.element.querySelectorAll('.home-recent').map(n => n.dataset.session), ['older']);
  assert.equal(home.element.querySelector('.home-quote').textContent, '「欢迎回来」');
  assert.equal(home.element.querySelector('.home-continue').children[0].textContent, '接着聊 →');
});

test('an unbound home session never borrows the engine active character; binding it later loads its own portrait', async t => {
  const requested = [];
  const h = setup(t, { callbacks: { getVoiceProfiles: () => [{ id: 99, name: '后端当前角色' }, { id: 7, name: '会话角色' }] } });
  globalThis.fetch = async url => {
    if (url === '/api/voice/profiles') return Response.json({ profiles: [{ id: 99, name: '后端当前角色' }], active_profile_id: 99 });
    if (url.startsWith('/api/characters/')) requested.push(url);
    return Response.json({ enabled: false });
  };
  await fetchVoiceProfiles();
  const stage = h.app.adv.stage.stage;
  stage.classList.add('is-home');
  await h.app.actions.previewSession(h.state.activeId); await microtasks();
  assert.equal(requested.some(url => url.includes('/99/')), false);
  const snapshots = h.app.actions.listSessions();
  assert.equal(snapshots[0].characterName, '');
  assert.equal(await h.app.actions.getAvatar(snapshots[0]), null);
  h.sessions[0].settings.voiceProfileId = 7;
  await h.app.actions.previewSession(h.state.activeId); await microtasks();
  assert.ok(requested.some(url => url.includes('/7/')));
});

function actions(sessions, getBackgroundUrl) {
  const calls = [];
  const api = createTitleActions({ getSessions: () => sessions, getProfiles: () => [{ id: 7, name: '角色甲', work: 'cafe_stella' }], getBackgroundUrl,
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
  const { api } = actions(sessions, (id, time) => (id ? `/signed/${id}/${time ?? 'auto'}.webp` : ''));
  assert.deepEqual(api.listSessions().map(({ excerpt, settings, hasUserMessages, ...summary }) => summary), [
    { id: 'new', title: '新会话', characterName: '角色甲', lastLine: '最後の台詞', updatedAt: 30, background: '/signed/cafe/auto.webp' },
    { id: 'unloaded', title: '未加载历史', characterName: '', lastLine: '后端摘要', updatedAt: 20, background: '' },
    { id: 'old', title: '旧会话', characterName: '', lastLine: '旧消息', updatedAt: 10, background: '' },
  ]);
  assert.deepEqual(sessions.map(s => s.id), ['old', 'new', 'unloaded']);
  api.listSessions()[0].title = 'changed'; assert.equal(sessions[1].title, '新会话');
  sessions[0].updatedAt = 40; assert.equal(api.listSessions()[0].id, 'old');
});

test('a stored background id never reaches the thumbnail as an image URL', () => {
  const sessions = [{ id: 'a', settings: { advBackground: 'cafe_hall', advBgTime: 'night' } }];
  assert.equal(actions(sessions).api.listSessions()[0].background, '', 'no resolver yet: no thumbnail');
  assert.equal(actions(sessions, () => '').api.listSessions()[0].background, '', 'unknown background: no thumbnail');
  const seen = [];
  const url = actions(sessions, (...args) => { seen.push(args); return '/api/media/background/cafe_hall/night?sig=x'; })
    .api.listSessions()[0].background;
  assert.equal(url, '/api/media/background/cafe_hall/night?sig=x');
  assert.deepEqual(seen, [['cafe_hall', 'night', null]], 'no bound character: no work');
  let loaded = false;
  const late = actions(sessions, () => (loaded ? '/api/media/background/cafe_hall/night?sig=y' : '')).api.listSessions()[0];
  assert.equal(late.background, '', 'before the background list arrives');
  loaded = true;
  assert.equal(late.background, '/api/media/background/cafe_hall/night?sig=y', 'resolved again when the slot renders');
});

test('an unset background asks for the bound character work default', () => {
  const sessions = [{ id: 'a', settings: { voiceProfileId: 7 } }, { id: 'b', settings: { voiceProfileId: 7, advBackground: 'none' } }];
  const seen = [];
  const { api } = actions(sessions, (...args) => { seen.push(args); return args[0] === '' ? '/signed/auto.webp' : ''; });
  assert.deepEqual(api.listSessions().map(s => s.background), ['/signed/auto.webp', '']);
  assert.deepEqual(seen, [['', undefined, 'cafe_stella'], ['none', undefined, 'cafe_stella']]);
});

test('empty summaries have predictable fields and same timestamps preserve store order', () => {
  const { api } = actions([{ id: 'a' }, { id: 'b', updatedAt: 0 }]);
  assert.deepEqual((({ excerpt, settings, hasUserMessages, ...summary }) => summary)(api.listSessions()[0]), { id: 'a', title: '新对话', characterName: '', lastLine: '', updatedAt: 0, background: '' });
  assert.deepEqual(api.listSessions().map(s => s.id), ['a', 'b']);
});

test('title actions delegate; continue uses newest or creates a first session; hook defaults to stage', () => {
  const sessions = [{ id: 'old', updatedAt: 1 }, { id: 'latest', updatedAt: 2 }];
  const { api, calls } = actions(sessions);
  assert.deepEqual(api.listCharacters(), [{ id: 7, name: '角色甲', work: 'cafe_stella' }]);
  assert.deepEqual(Object.keys(api).sort(), ['continueLatest', 'listSessions', 'loadSession', 'openCharacters', 'openSettings', 'startNew', 'startNewWith', 'previewSession', 'getActiveId', 'getStatus', 'getBackgrounds', 'listCharacters', 'getAvatar', 'playExcerpt', 'stopExcerpt', 'subscribeSettings', 'voiceEnabled'].sort());
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


test('new home actions forward only chosen settings, preserve settings snapshots and pass settings page', () => {
  const sessions = [{ id: 'a', settings: { voiceProfileId: 7 }, messages: [{ role: 'assistant', lines: [{ zh: '原句', audio: { chunks: ['/fake.wav'] } }] }] }];
  const calls = [];
  const api = createTitleActions({ getSessions: () => sessions, getProfiles: () => [], getActiveId: () => 'a',
    startNew: settings => calls.push(['new', settings]), loadSession: id => calls.push(['load', id]),
    previewSession: id => calls.push(['preview', id]), openCharacters() {}, openSettings: page => calls.push(['settings', page]) });
  const snap = api.listSessions()[0]; snap.settings.voiceProfileId = 8; snap.excerpt.line.audio.chunks.push('changed');
  assert.equal(sessions[0].settings.voiceProfileId, 7); assert.equal(sessions[0].messages[0].lines[0].audio.chunks.length, 1);
  api.previewSession('a'); api.startNewWith({ voiceProfileId: 8, advBackground: 'w/sea', advBgTime: 'day', ttsSpeed: 999 }); api.openSettings('models');
  assert.deepEqual(calls, [['preview','a'], ['new', { voiceProfileId: 8, advBackground: 'w/sea', advBgTime: 'day' }], ['settings','models']]);
});
