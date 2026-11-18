import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {
  initGlobalSettings,
  openGlobalSettings,
} from '../frontend/src/controllers/global_settings.js';
import * as settingsUi from '../frontend/src/settings_ui.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

const { openModal, closeModal } = settingsUi;
const settle = () => new Promise(resolve => setImmediate(resolve));
const okJson = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });

function mountMemoryIndexDom() {
  const html = fs.readFileSync(new URL('../frontend/index.html', import.meta.url), 'utf8');
  const bodyMatch = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
  assert.ok(bodyMatch, 'Expected <body> in frontend/index.html');
  const cleaned = bodyMatch[1]
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<svg[\s\S]*?<\/svg>/gi, '')
    .replace(/<script[\s\S]*?<\/script>/gi, '');

  const doc = fakeDocument();
  const voidTags = new Set([
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
  ]);
  const stack = [doc.body];
  const tagRe = /<(\/?)([a-zA-Z0-9-]+)((?:"[^"]*"|'[^']*'|[^>"'])*)>/g;
  const attrRe = /([a-zA-Z0-9_:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;

  for (const match of cleaned.matchAll(tagRe)) {
    const [, closing, rawTag, rawAttrs] = match;
    const tag = rawTag.toLowerCase();
    if (closing) {
      while (stack.length > 1) {
        const popped = stack.pop();
        if (popped.tagName.toLowerCase() === tag) break;
      }
      continue;
    }
    const selfClosing = /\/\s*$/.test(rawAttrs) || voidTags.has(tag);
    const attrSource = rawAttrs.replace(/\/\s*$/, '');
    const el = doc.createElement(tag);
    el.scrollIntoView = () => {};
    el.getClientRects = () => [{}];

    for (const attrMatch of attrSource.matchAll(attrRe)) {
      const name = attrMatch[1];
      const val = attrMatch[2] ?? attrMatch[3] ?? attrMatch[4];
      if (name === 'id') {
        el.id = val ?? '';
        el.setAttribute('id', el.id);
      } else if (name === 'class') {
        el.className = val ?? '';
        el.setAttribute('class', val ?? '');
      } else if (name === 'hidden') {
        el.hidden = true;
        el.setAttribute('hidden', val ?? '');
      } else if (name.startsWith('data-')) {
        const camel = name.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
        el.dataset[camel] = val ?? '';
        el.setAttribute(name, val ?? '');
      } else {
        el.setAttribute(name, val ?? '');
      }
    }
    stack.at(-1).appendChild(el);
    if (!selfClosing) stack.push(el);
  }

  const dom = {};
  for (const id of [
    'globalModal', 'gTabs', 'gMemoryTitle', 'gMemoryNote', 'gMemoryError', 'gMemoryList',
    'gOpenSessionSettings', 'gResetBtn', 'gTgSave',
  ]) {
    dom[id] = doc.getElementById(id);
  }
  return { doc, dom };
}

function setupFixture(t, fetchImpl, session = { id: 's_yoshino', settings: { voiceProfileId: 2 } }) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const origDocument = globalThis.document;
  const origFetch = globalThis.fetch;
  const origLocalStorage = globalThis.localStorage;

  const { doc, dom } = mountMemoryIndexDom();
  globalThis.document = doc;
  globalThis.localStorage = { getItem: () => null, setItem: () => {} };
  globalThis.fetch = fetchImpl;

  const toastRoot = doc.getElementById('toastRoot');

  t.after(async () => {
    for (const toast of [...toastRoot.children]) clearTimeout(toast._timer);
    settingsUi.closeAllModals();
    closeModal(dom.globalModal);
    initGlobalSettings({});
    if (origDocument !== undefined) globalThis.document = origDocument; else delete globalThis.document;
    if (origFetch !== undefined) globalThis.fetch = origFetch; else delete globalThis.fetch;
    if (origLocalStorage !== undefined) globalThis.localStorage = origLocalStorage; else delete globalThis.localStorage;
    await settle();
  });

  initGlobalSettings(dom, {
    openModal,
    closeModal,
    getActiveSession: () => session,
  });

  return { doc, dom, toastRoot };
}

test('memory tab renders Chinese category labels without internal fields and shows empty copy when empty', async t => {
  let empty = false;
  const { dom } = setupFixture(t, async (url) => {
    if (String(url).startsWith('/api/characters/memories')) {
      return okJson({
        character_id: 2,
        character_name: '芳乃',
        memories: empty ? [] : [
          { id: 101, category: 'nickname', fact_key: 'player_name', fact_value: '小路', confidence: 1.0, updated_at: '2026-11-18 10:00:00' },
          { id: 102, category: 'preference', fact_key: 'like_红茶', fact_value: '红茶', confidence: 0.95, updated_at: '2026-11-18 09:00:00' },
          { id: 103, category: 'taboo', fact_key: 'dislike_香菜', fact_value: '香菜', confidence: 0.95, updated_at: '2026-11-18 08:00:00' },
          { id: 104, category: 'promise', fact_key: 'promise_散步', fact_value: '周末去散步', confidence: 0.9, updated_at: '2026-11-18 07:00:00' },
          { id: 105, category: 'identity', fact_key: 'occupation', fact_value: '学生', confidence: 1.0, updated_at: '2026-11-18 06:00:00' },
        ],
      });
    }
    return okJson({});
  });

  openGlobalSettings('memory');
  await settle();

  assert.equal(dom.gMemoryTitle.textContent, '芳乃记住的事');
  assert.match(dom.gMemoryNote.textContent, /芳乃在所有对话里记住的关于你的事/);
  assert.match(dom.gMemoryNote.textContent, /删掉一条后，之后的回复不会再用到它/);

  const rowTexts = dom.gMemoryList.querySelectorAll('.memory-item-main').map(el =>
    el.children.map(c => c.textContent).join('')
  );
  assert.deepEqual(rowTexts, [
    '称呼：小路',
    '喜欢：红茶',
    '讨厌：香菜',
    '约定：周末去散步',
    '身份：学生',
  ]);
  for (const text of rowTexts) {
    assert.equal(text.includes('player_name'), false);
    assert.equal(text.includes('like_'), false);
    assert.equal(text.includes('0.95'), false);
    assert.equal(text.includes('101'), false);
  }

  empty = true;
  dom.gTabs.querySelector('[data-gtab="dashboard"]').click();
  await settle();
  dom.gTabs.querySelector('[data-gtab="memory"]').click();
  await settle();

  assert.equal(dom.gMemoryList.querySelectorAll('.memory-item').length, 0);
  assert.equal(
    dom.gMemoryList.querySelector('.memory-empty').textContent,
    '还没有记住什么。聊天时说「我叫……」「我喜欢……」「我讨厌……」，角色会记下来。',
  );
});

test('memory delete requires two clicks within 5s, disarms after 5s, and shows toast on delete', async t => {
  const deletes = [];
  const { dom, toastRoot } = setupFixture(t, async (url, options = {}) => {
    if (options.method === 'DELETE') {
      deletes.push(String(url));
      return okJson({ status: 'deleted' });
    }
    if (String(url).startsWith('/api/characters/memories')) {
      return okJson({
        character_id: 2,
        character_name: '芳乃',
        memories: [
          { id: 10, category: 'nickname', fact_value: '小路', updated_at: '2026-11-18 10:00:00' },
          { id: 11, category: 'preference', fact_value: '红茶', updated_at: '2026-11-18 09:00:00' },
        ],
      });
    }
    return okJson({});
  });

  openGlobalSettings('memory');
  await settle();

  const firstBtn = () => dom.gMemoryList.querySelectorAll('.memory-del')[0];
  assert.equal(firstBtn().textContent, '删除');

  // First click arms only
  firstBtn().click();
  await settle();
  assert.equal(deletes.length, 0);
  assert.equal(firstBtn().textContent, '确认删除');
  assert.equal(firstBtn().classList.contains('si-del-confirm'), true);

  // After 5 seconds without second click, button reverts
  t.mock.timers.tick(5000);
  await settle();
  assert.equal(deletes.length, 0);
  assert.equal(firstBtn().textContent, '删除');
  assert.equal(firstBtn().classList.contains('si-del-confirm'), false);

  // Two clicks delete the first row and show toast
  firstBtn().click();
  await settle();
  firstBtn().click();
  await settle();

  assert.deepEqual(deletes, ['/api/characters/2/memories/10?session_id=s_yoshino']);
  const remaining = dom.gMemoryList.querySelectorAll('.memory-item');
  assert.equal(remaining.length, 1);
  assert.equal(remaining[0].dataset.id, '11');
  const toasts = toastRoot.querySelectorAll('.toast').map(el => el.dataset.msg);
  assert.ok(toasts.includes('已删除这条记忆'));
});

test('failed delete keeps the memory row and shows Chinese error reason', async t => {
  let deleteAttempts = 0;
  const { dom } = setupFixture(t, async (url, options = {}) => {
    if (options.method === 'DELETE') {
      deleteAttempts += 1;
      return okJson({ detail: '数据库忙，请稍后重试' }, 500);
    }
    if (String(url).startsWith('/api/characters/memories')) {
      return okJson({
        character_id: 2,
        character_name: '芳乃',
        memories: [{ id: 10, category: 'preference', fact_value: '红茶', updated_at: '2026-11-18 10:00:00' }],
      });
    }
    return okJson({});
  });

  openGlobalSettings('memory');
  await settle();

  const btn = () => dom.gMemoryList.querySelector('.memory-del');
  btn().click();
  await settle();
  btn().click();
  await settle();

  assert.equal(deleteAttempts, 1);
  assert.equal(dom.gMemoryList.querySelectorAll('.memory-item').length, 1);
  assert.equal(btn().disabled, false);
  assert.equal(btn().textContent, '删除');
  assert.equal(dom.gMemoryError.classList.contains('hidden'), false);
  assert.equal(dom.gMemoryError.textContent, '数据库忙，请稍后重试');
});

test('switching tabs aborts pending read and prevents stale overwrite', async t => {
  let resolveStaleRead;
  let aborted = false;
  let reads = 0;
  const { dom } = setupFixture(t, async (url, options = {}) => {
    if (String(url).startsWith('/api/characters/memories')) {
      reads += 1;
      if (reads === 1) {
        options.signal?.addEventListener('abort', () => { aborted = true; });
        return new Promise((resolve) => { resolveStaleRead = resolve; });
      }
      return okJson({
        character_id: 3,
        character_name: '夏目',
        memories: [{ id: 20, category: 'preference', fact_value: '抹茶', updated_at: '2026-11-18 11:00:00' }],
      });
    }
    return okJson({});
  });

  openGlobalSettings('memory');
  await settle();
  assert.equal(reads, 1);

  // Switch away before first read finishes -> aborted
  dom.gTabs.querySelector('[data-gtab="dashboard"]').click();
  await settle();
  assert.equal(aborted, true);

  // Reopen memory tab -> starts fresh read and shows 夏目
  dom.gTabs.querySelector('[data-gtab="memory"]').click();
  await settle();
  assert.equal(dom.gMemoryTitle.textContent, '夏目记住的事');

  // Late response from first read cannot overwrite 夏目
  resolveStaleRead(okJson({
    character_id: 2,
    character_name: '芳乃',
    memories: [{ id: 10, category: 'nickname', fact_value: '旧数据', updated_at: '2026-11-18 09:00:00' }],
  }));
  await settle();
  assert.equal(dom.gMemoryTitle.textContent, '夏目记住的事');
  assert.equal(dom.gMemoryList.querySelectorAll('.memory-item').length, 1);
  assert.equal(dom.gMemoryList.querySelector('.memory-item-val').textContent, '抹茶');
});
