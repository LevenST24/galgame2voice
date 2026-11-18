// Two clicks on the session-list trash button delete a chat. The open chat's
// toast names the conversation that remains. Wiring is the real list renderer,
// initApp's delete handler, and DELETE /api/chat/sessions/<id>.
import assert from 'node:assert/strict';
import test from 'node:test';
import { initApp } from '../frontend/src/main.js';
import { state, DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { fakeDocument, Element } from './helpers/adv_dom.mjs';

const OPEN_TITLE = 'V5原来的海边';
const OTHER_TITLE = 'V5新开的灯塔';

let sessionMarkupInstalled = false;

function installSessionMarkup() {
  if (sessionMarkupInstalled) return;
  sessionMarkupInstalled = true;
  const originalAppend = Element.prototype.appendChild;
  Element.prototype.appendChild = function appendChild(child) {
    if (child?.nodeType === 11) {
      for (const nested of [...child.children]) this.appendChild(nested);
      return child;
    }
    return originalAppend.call(this, child);
  };
  Object.defineProperty(Element.prototype, 'innerHTML', {
    configurable: true,
    get() { return this.html || ''; },
    set(html) {
      const text = String(html ?? '');
      for (const child of [...this.children]) child.remove();
      this.text = '';
      this.html = text;
      if (!text.includes('si-main')) return;
      const doc = this.ownerDocument;
      const title = doc.createElement('div');
      title.className = 'si-title';
      const preview = doc.createElement('span');
      preview.className = 'si-preview';
      const del = doc.createElement('button');
      del.className = 'si-del';
      const label = text.match(/aria-label="([^"]*)"/)?.[1] || '';
      const tip = text.match(/title="([^"]*)"/)?.[1] || '';
      del.setAttribute('aria-label', label);
      del.setAttribute('title', tip);
      this.append(title, preview, del);
    },
  });
}

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => { values.set(key, String(value)); },
    removeItem: (key) => { values.delete(key); },
  };
}

function session(id, title) {
  return {
    id,
    title,
    createdAt: 1,
    updatedAt: 1_700_000_000_000,
    messages: [{ id: `m_${id}`, role: 'user', content: title, ts: 1 }],
    settings: { ...DEFAULT_SESSION_SETTINGS },
  };
}

function installApp({ failDelete = false } = {}) {
  installSessionMarkup();
  const doc = fakeDocument();
  doc.createDocumentFragment = () => {
    const frag = doc.createElement('fragment');
    frag.nodeType = 11;
    return frag;
  };
  const sessionList = doc.createElement('nav');
  const sessionCount = doc.createElement('span');
  const toastRoot = doc.createElement('div');
  toastRoot.id = 'toastRoot';
  doc.body.append(sessionList, sessionCount, toastRoot);
  globalThis.document = doc;
  globalThis.window = {
    innerWidth: 1280,
    location: { search: '', hash: '' },
    confirm: () => { throw new Error('删除不该弹出系统确认框'); },
  };
  globalThis.confirm = globalThis.window.confirm;
  globalThis.localStorage = memoryStorage();
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const path = String(url);
    calls.push({ url: path, method });
    if (method === 'DELETE') {
      if (failDelete) {
        return new Response(JSON.stringify({ detail: '数据库忙' }), {
          status: 500,
          headers: { 'content-type': 'application/json' },
        });
      }
      return Response.json({ status: 'deleted', session_id: decodeURIComponent(path.split('/').pop()), success: true });
    }
    return Response.json({ profiles: [], sessions: [], has_more: false, backgrounds: [] });
  };
  const scheduled = [];
  const origSetTimeout = globalThis.setTimeout;
  const origClearTimeout = globalThis.clearTimeout;
  globalThis.setTimeout = (fn, ms, ...args) => {
    const id = origSetTimeout(fn, ms, ...args);
    scheduled.push({ id, fn, ms });
    return id;
  };
  globalThis.clearTimeout = (id) => {
    const index = scheduled.findIndex((item) => item.id === id);
    if (index >= 0) scheduled.splice(index, 1);
    return origClearTimeout(id);
  };
  state.sessions = [session('s_lighthouse', OTHER_TITLE), session('s_seaside', OPEN_TITLE)];
  state.activeId = 's_seaside';
  state.global = { voiceMode: true };
  const app = initApp({ initialize: false, dom: { sessionList, sessionCount } });
  app.fullRender();
  return {
    app, doc, sessionList, toastRoot, calls, scheduled, origSetTimeout, origClearTimeout,
    cleanup() {
      for (const item of scheduled) origClearTimeout(item.id);
      scheduled.length = 0;
      for (const toast of [...toastRoot.children]) origClearTimeout(toast._timer);
      globalThis.setTimeout = origSetTimeout;
      globalThis.clearTimeout = origClearTimeout;
      delete globalThis.document;
      delete globalThis.window;
      delete globalThis.confirm;
    },
  };
}

const deletes = (calls) => calls.filter((call) => call.method === 'DELETE');
const row = (list, id) => [...list.querySelectorAll('.session-item')].find((item) => item.dataset.id === id);
const trash = (list, id) => row(list, id).querySelector('.si-del');
const toastText = (root) => [...root.querySelectorAll('.toast')].map((item) => item.dataset.msg);

async function flush() {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

test('one trash click arms delete; the second click removes the open chat and names what remains', async (t) => {
  const h = installApp();
  t.after(() => h.cleanup());
  const open = () => trash(h.sessionList, 's_seaside');
  let selects = 0;
  row(h.sessionList, 's_seaside').addEventListener('click', () => { selects += 1; });

  open().click();
  await flush();
  assert.equal(deletes(h.calls).length, 0, '第一次点击就发出了 DELETE');
  assert.equal(selects, 0, '点垃圾桶同时触发了切换会话');
  assert.equal(state.activeId, 's_seaside');
  assert.equal(open().textContent, '确认删除');
  assert.equal(open().classList.contains('si-del-confirm'), true);
  assert.equal(open().getAttribute('aria-label'), `再点一次删除「${OPEN_TITLE}」`);
  assert.equal(open().getAttribute('title'), `再点一次删除「${OPEN_TITLE}」`);
  const confirmTimer = h.scheduled.find((item) => item.ms >= 3000 && item.ms <= 8000);
  assert.ok(confirmTimer, '准备删除没有在几秒后超时');

  h.origClearTimeout(confirmTimer.id);
  confirmTimer.fn();
  assert.equal(deletes(h.calls).length, 0);
  assert.equal(open().textContent, '');
  assert.equal(open().classList.contains('si-del-confirm'), false);
  assert.equal(open().getAttribute('aria-label'), '删除对话');
  assert.equal(state.sessions.map((item) => item.id).join(','), 's_lighthouse,s_seaside');

  open().click();
  await flush();
  assert.equal(deletes(h.calls).length, 0);
  assert.equal(open().textContent, '确认删除');
  h.app.fullRender();
  assert.equal(trash(h.sessionList, 's_seaside').textContent, '');
  assert.equal(trash(h.sessionList, 's_seaside').getAttribute('aria-label'), '删除对话');
  assert.equal(deletes(h.calls).length, 0);
  let selectsAfterRender = 0;
  row(h.sessionList, 's_seaside').addEventListener('click', () => { selectsAfterRender += 1; });

  trash(h.sessionList, 's_seaside').click();
  await flush();
  assert.equal(deletes(h.calls).length, 0);
  assert.equal(selectsAfterRender, 0);
  trash(h.sessionList, 's_seaside').click();
  await flush();

  assert.deepEqual(deletes(h.calls).map((call) => call.url), ['/api/chat/sessions/s_seaside']);
  assert.equal(state.activeId, 's_lighthouse');
  assert.deepEqual(state.sessions.map((item) => item.title), [OTHER_TITLE]);
  assert.equal(row(h.sessionList, 's_seaside'), undefined);
  assert.deepEqual(toastText(h.toastRoot), [`对话已删除，现在是「${OTHER_TITLE}」。`]);
});

test('deleting a chat that is not open still takes two clicks and keeps the plain toast', async (t) => {
  const h = installApp();
  t.after(() => h.cleanup());
  let selects = 0;
  row(h.sessionList, 's_lighthouse').addEventListener('click', () => { selects += 1; });
  const other = () => trash(h.sessionList, 's_lighthouse');

  other().click();
  await flush();
  assert.equal(deletes(h.calls).length, 0);
  assert.equal(selects, 0);
  assert.equal(state.activeId, 's_seaside');
  assert.equal(other().getAttribute('aria-label'), `再点一次删除「${OTHER_TITLE}」`);
  other().click();
  await flush();

  assert.deepEqual(deletes(h.calls).map((call) => call.url), ['/api/chat/sessions/s_lighthouse']);
  assert.equal(state.activeId, 's_seaside');
  assert.deepEqual(state.sessions.map((item) => item.title), [OPEN_TITLE]);
  assert.deepEqual(toastText(h.toastRoot), ['对话已删除']);
});

test('a failed delete still keeps the chat and the existing error toast', async (t) => {
  const h = installApp({ failDelete: true });
  t.after(() => h.cleanup());
  const open = () => trash(h.sessionList, 's_seaside');
  open().click();
  await flush();
  assert.equal(deletes(h.calls).length, 0);
  assert.equal(toastText(h.toastRoot).length, 0);
  open().click();
  await flush();
  assert.deepEqual(deletes(h.calls).map((call) => call.url), ['/api/chat/sessions/s_seaside']);
  assert.equal(state.activeId, 's_seaside');
  assert.deepEqual(state.sessions.map((item) => item.id), ['s_lighthouse', 's_seaside']);
  assert.equal(toastText(h.toastRoot).length, 1);
  assert.match(toastText(h.toastRoot)[0], /^尚未确认删除，对话保留在列表中。/);
  assert.doesNotMatch(toastText(h.toastRoot)[0], /现在是/);
});
