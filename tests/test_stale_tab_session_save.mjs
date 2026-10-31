// Another page can change gal2voice.chat.v1 while this page still holds the old
// session in memory. That conflict must not post the memory to the server.
// A failed local write (storage full) still must: the server is the only copy
// that can keep the edit.
import assert from 'node:assert/strict';
import test from 'node:test';
import { initApp } from '../frontend/src/main.js';
import { openSessionSettings } from '../frontend/src/controllers/voice_settings.js';
import { state, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL } from '../frontend/src/store.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

const KEY = 'gal2voice.chat.v1';

function snapshot(session) {
  return { sessions: [session], activeId: session.id, global: { ...DEFAULT_GLOBAL } };
}

function installBrowser(t, raw) {
  const doc = fakeDocument();
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  const root = doc.createElement('div');
  root.id = 'advRoot';
  doc.body.appendChild(root);
  const toasts = doc.createElement('div');
  toasts.id = 'toastRoot';
  doc.body.appendChild(toasts);
  const storage = new Map([[KEY, raw]]);
  globalThis.document = doc;
  globalThis.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key),
  };
  globalThis.location = { origin: 'http://127.0.0.1:8000' };
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '', origin: 'http://127.0.0.1:8000' },
    sessionStorage: { getItem: () => '', setItem() {}, removeItem() {} },
    prompt: () => null,
  };
  let app;
  t.after(() => {
    try { app?.conversation.stop(); app?.adv?.unmount(); } catch { /* already gone */ }
    for (const toast of toasts.children) clearTimeout(toast._timer);
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.fetch;
    delete globalThis.location;
  });
  return {
    doc, root, storage, toasts,
    setApp(value) { app = value; },
  };
}

function jsonResponse(body, status = 200) {
  return Response.json(body, { status });
}

const SESSION_CONTROLS = [
  'sSystem', 'sVoice', 'sTemp', 'sTopP', 'sFreq', 'sPres', 'sMaxTokens', 'sCtx',
  'sAiAdaptiveVoice', 'sAdvReplyFormat', 'sTtsSpeed', 'sTtsTopK', 'sTtsTopP', 'sTtsTemp', 'sSave',
];

function installSessionControls(doc) {
  const controls = {};
  const modal = doc.createElement('div');
  modal.id = 'sessionModal';
  modal.className = 'overlay hidden';
  const dialog = doc.createElement('div');
  dialog.className = 'modal';
  modal.appendChild(dialog);
  doc.body.appendChild(modal);
  controls.sessionModal = modal;
  for (const id of SESSION_CONTROLS) {
    const tag = id === 'sSave' ? 'button' : (id === 'sVoice' || id === 'sAdvReplyFormat' || id === 'sAiAdaptiveVoice' ? 'select' : 'input');
    const el = doc.createElement(tag);
    el.id = id;
    doc.body.appendChild(el);
    controls[id] = el;
  }
  return controls;
}

async function boot(t, { session, server, profiles = [], sessionControls = false }) {
  const env = installBrowser(t, JSON.stringify(snapshot(session)));
  const controls = sessionControls ? installSessionControls(env.doc) : {};
  globalThis.window.fetch = async (url) => {
    const target = String(url);
    if (target.includes('/api/chat/sessions')) {
      return jsonResponse({ sessions: [server], has_more: false, count: 1 });
    }
    if (target.includes('/api/voice/profiles')) {
      return jsonResponse({ profiles, active_profile_id: profiles[0]?.id ?? null });
    }
    if (target.includes('/api/providers')) return jsonResponse({ providers: [], active_provider: null });
    if (target.includes('/api/backgrounds')) return jsonResponse({ backgrounds: [] });
    if (target.includes('/api/chat/stream')) return jsonResponse({ detail: '测试不进行生成' }, 500);
    return jsonResponse({});
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);
  const app = initApp({
    dom: { advRoot: env.root, ...controls },
    renderSessionList() {},
    showTitle() {},
  });
  env.setApp(app);
  await app.ready;
  return { ...env, app };
}

function staleSession() {
  const session = {
    id: 's_stale', title: '新对话', createdAt: 1_700_000_000_000, updatedAt: 1_700_000_000_000,
    settings: { ...DEFAULT_SESSION_SETTINGS, voiceProfileId: null, advPlayerName: '旧名', advWindowOpacity: 80 },
    messages: [{ id: 'm_greet', role: 'assistant', content: '欢迎', noVoice: true, ts: 1 }],
  };
  const server = {
    id: session.id, title: '新对话', voice_profile_id: null,
    created_at: '2024-06-01 00:00:00', updated_at: '2024-06-01 00:00:01',
    message_count: 1, last_message: '欢迎', settings: { ...session.settings },
  };
  return { session, server };
}

// Another page wrote the shared record. This page's memory stays as it was at startup.
function writeOtherPage(storage) {
  const otherTab = JSON.parse(storage.get(KEY));
  otherTab.sessions[0].settings.advPlayerName = '新名';
  otherTab.sessions[0].settings.advWindowOpacity = 30;
  storage.set(KEY, JSON.stringify(otherTab));
}

function watchSessionPosts() {
  const posts = [];
  const previous = globalThis.fetch;
  globalThis.fetch = async (url, options = {}) => {
    const target = String(url);
    if (target.includes('/api/chat/sessions') && (options.method || 'GET') === 'POST') {
      posts.push(JSON.parse(options.body));
    }
    return previous(url, options);
  };
  return posts;
}

function storedSettings(storage) {
  return JSON.parse(storage.get(KEY)).sessions[0].settings;
}

// The primary record cannot grow. saveState evicts the backup and retries once;
// both writes throw, which is the "storage full" refusal.
function denyStorageWrites() {
  localStorage.setItem = () => {
    const error = new Error('quota');
    error.name = 'QuotaExceededError';
    throw error;
  };
}

function parameterSession() {
  const built = staleSession();
  built.session.settings.voiceProfileId = 7;
  built.server.voice_profile_id = 7;
  built.server.settings = { ...built.session.settings };
  built.profiles = [{ id: 7, name: '测试角色', system_prompt: '' }];
  return built;
}

async function until(predicate) {
  for (let i = 0; i < 30 && !predicate(); i++) await new Promise(resolve => setTimeout(resolve, 0));
}

test('a stale tab does not post old settings when the first message renames the chat', async (t) => {
  const { session, server } = staleSession();
  const { storage, app } = await boot(t, { session, server });
  const live = () => state.sessions.find(item => item.id === session.id);
  assert.equal(live().settings.advPlayerName, '旧名');
  assert.equal(live().settings.advWindowOpacity, 80);
  writeOtherPage(storage);
  assert.equal(live().settings.advPlayerName, '旧名');
  assert.equal(live().settings.advWindowOpacity, 80);
  const posts = watchSessionPosts();
  const started = app.conversation.send('你好');
  await new Promise(resolve => setTimeout(resolve, 40));
  app.conversation.stop();
  const stored = storedSettings(storage);
  assert.equal(started, true);
  assert.equal(posts.length, 0, '另一网页已改过存储时，不应把本页内存里的会话提交到服务端');
  assert.equal(stored.advPlayerName, '新名');
  assert.equal(stored.advWindowOpacity, 30);
});

test('a stale tab does not post old settings when chat display settings are saved', async (t) => {
  const { session, server } = staleSession();
  const { storage, app } = await boot(t, { session, server });
  writeOtherPage(storage);
  const posts = watchSessionPosts();
  app.adv.configPanel.open();
  const saved = await app.adv.configPanel.save();
  const stored = storedSettings(storage);
  assert.equal(posts.length, 0, '保存显示设置时本地保存被拒绝，不应提交内存里的旧设置');
  assert.equal(stored.advPlayerName, '新名');
  assert.equal(stored.advWindowOpacity, 30);
  assert.equal(saved, false);
});

test('a stale tab does not post a new chat after another page changed storage', async (t) => {
  const { session, server } = staleSession();
  const { storage, app } = await boot(t, { session, server });
  writeOtherPage(storage);
  const posts = watchSessionPosts();
  app.actions.startNew();
  await new Promise(resolve => setTimeout(resolve, 40));
  const stored = storedSettings(storage);
  assert.equal(posts.length, 0, '新建对话时本地保存被拒绝，不应把内存里的会话提交到服务端');
  assert.equal(stored.advPlayerName, '新名');
  assert.equal(stored.advWindowOpacity, 30);
});

test('a full browser storage still posts the session when the first message renames the chat', async (t) => {
  const { session, server } = staleSession();
  const { storage, app } = await boot(t, { session, server });
  denyStorageWrites();
  const posts = watchSessionPosts();
  const started = app.conversation.send('你好');
  await new Promise(resolve => setTimeout(resolve, 40));
  app.conversation.stop();
  const stored = storedSettings(storage);
  assert.equal(started, true);
  assert.equal(posts.length, 1, '浏览器存储写满时，首条消息自动命名仍应把会话提交到服务端');
  assert.equal(posts[0].id, session.id);
  assert.equal(posts[0].title, '你好');
  assert.equal(posts[0].settings.advPlayerName, '旧名');
  assert.equal(posts[0].settings.advWindowOpacity, 80);
  assert.equal(stored.advPlayerName, '旧名');
  assert.equal(JSON.parse(storage.get(KEY)).sessions[0].title, '新对话');
});

test('a full browser storage still posts the session when session parameters are saved', async (t) => {
  const { session, server, profiles } = parameterSession();
  const { doc, app } = await boot(t, { session, server, profiles, sessionControls: true });
  await openSessionSettings();
  const voice = doc.getElementById('sVoice');
  await until(() => voice.value === '7');
  assert.equal(voice.value, '7');
  doc.getElementById('sTemp').value = '1.25';
  denyStorageWrites();
  const posts = watchSessionPosts();
  doc.getElementById('sSave').click();
  await until(() => posts.length > 0);
  const live = state.sessions.find(item => item.id === session.id);
  assert.equal(live.settings.temperature, 1.25);
  assert.equal(posts.length, 1, '浏览器存储写满时，保存会话参数仍应提交到服务端');
  assert.equal(posts[0].id, session.id);
  assert.equal(posts[0].settings.temperature, 1.25);
  assert.equal(posts[0].voice_profile_id, 7);
  assert.equal(JSON.parse(localStorage.getItem(KEY)).sessions[0].settings.temperature, 0.7);
  app.conversation.stop();
});
