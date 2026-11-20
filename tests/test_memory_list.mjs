import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { createMemorySettingsController } from '../frontend/src/controllers/memory_settings.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

const settle = () => new Promise(resolve => setImmediate(resolve));
const okJson = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
const html = fs.readFileSync(new URL('../frontend/index.html', import.meta.url), 'utf8');
const memories = (...rows) => ({ character_id: 2, character_name: '芳乃', memories: rows });

function setup(t, fetchImpl) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const saved = { document: globalThis.document, fetch: globalThis.fetch };
  globalThis.document = fakeDocument();
  globalThis.fetch = fetchImpl;
  const dom = {};
  for (const id of ['gMemoryTitle', 'gMemoryNote', 'gMemoryError', 'gMemoryList']) {
    assert.match(html, new RegExp(`id="${id}"`));
    dom[id] = document.createElement('div');
  }
  const toasts = [];
  const ctl = createMemorySettingsController(dom, {
    getActiveSession: () => ({ id: 's1', settings: { voiceProfileId: 2 } }),
    showToast: msg => toasts.push(msg),
  });
  t.after(() => { ctl.dispose(); Object.assign(globalThis, saved); });
  const rows = () => dom.gMemoryList.querySelectorAll('.memory-item');
  const button = () => dom.gMemoryList.querySelector('.memory-del');
  return { dom, ctl, toasts, rows, button };
}

test('the memory tab lists what the chat character remembers, or how memories are made', async t => {
  const urls = [];
  let data = memories(
    { id: 1, category: 'nickname', fact_value: '小路' },
    { id: 2, category: 'preference', fact_value: '红茶' },
    { id: 3, category: 'taboo', fact_value: '香菜' },
  );
  const { dom, ctl, rows } = setup(t, async url => { urls.push(url); return okJson(data); });
  ctl.onTabChange('memory');
  await settle();
  assert.deepEqual(urls, ['/api/characters/memories?session_id=s1&character_id=2']);
  assert.equal(dom.gMemoryTitle.textContent, '芳乃记住的事');
  assert.match(dom.gMemoryNote.textContent, /芳乃在所有对话里记住的关于你的事。删掉一条后，之后的回复不会再用到它。/);
  assert.deepEqual(rows().map(r => r.children[0].children.map(c => c.textContent).join('')),
    ['称呼：小路', '喜欢：红茶', '讨厌：香菜']);

  data = memories();
  ctl.onTabChange('memory');
  await settle();
  assert.equal(rows().length, 0);
  assert.equal(dom.gMemoryList.querySelector('.memory-empty').textContent,
    '还没有记住什么。聊天时说「我叫……」「我喜欢……」「我讨厌……」，角色会记下来。');
});

test('deleting a memory takes a second click within 5 s; a failed delete keeps the row', async t => {
  const deletes = [];
  let deleteStatus = 200;
  const { dom, ctl, toasts, rows, button } = setup(t, async (url, options = {}) => {
    if (options.method !== 'DELETE') {
      return okJson(memories({ id: 10, category: 'nickname', fact_value: '小路' }, { id: 11, category: 'preference', fact_value: '红茶' }));
    }
    deletes.push(url);
    return deleteStatus === 200 ? okJson({ status: 'deleted' }) : okJson({ detail: '数据库忙，请稍后重试' }, deleteStatus);
  });
  ctl.onTabChange('memory');
  await settle();

  button().click();
  assert.equal(button().textContent, '确认删除');
  t.mock.timers.tick(5000);
  assert.equal(button().textContent, '删除');
  assert.equal(deletes.length, 0);

  button().click();
  button().click();
  await settle();
  assert.deepEqual(deletes, ['/api/characters/2/memories/10?session_id=s1']);
  assert.deepEqual(rows().map(r => r.dataset.id), ['11']);
  assert.deepEqual(toasts, ['已删除这条记忆']);

  deleteStatus = 500;
  button().click();
  button().click();
  await settle();
  assert.deepEqual(rows().map(r => r.dataset.id), ['11']);
  assert.equal(button().textContent, '删除');
  assert.equal(dom.gMemoryError.textContent, '数据库忙，请稍后重试');
});

test('leaving the tab drops a read that answers late', async t => {
  let answerLate;
  let aborted = false;
  let reads = 0;
  const { dom, ctl } = setup(t, async (url, options = {}) => {
    reads += 1;
    if (reads > 1) return okJson({ character_id: 3, character_name: '夏目', memories: [] });
    options.signal?.addEventListener('abort', () => { aborted = true; });
    return new Promise(resolve => { answerLate = resolve; });
  });
  ctl.onTabChange('memory');
  await settle();
  ctl.onTabChange('dashboard');
  await settle();
  assert.equal(aborted, true);
  ctl.onTabChange('memory');
  await settle();
  answerLate(okJson(memories({ id: 10, category: 'nickname', fact_value: '旧数据' })));
  await settle();
  assert.equal(dom.gMemoryTitle.textContent, '夏目记住的事');
  assert.equal(dom.gMemoryList.querySelectorAll('.memory-item').length, 0);
});

test('closing the settings modal cancels in-flight memory reads and drops late delete toasts', async t => {
  const { initGlobalSettings, openGlobalSettings } = await import('../frontend/src/controllers/global_settings.js');
  const { openModal, closeModal, closeAllModals } = await import('../frontend/src/settings_ui.js');
  const saved = { document: globalThis.document, fetch: globalThis.fetch };
  const doc = fakeDocument();
  globalThis.document = doc;

  const toastRoot = doc.createElement('div');
  toastRoot.id = 'toastRoot';
  doc.body.appendChild(toastRoot);

  const globalModal = doc.createElement('div');
  globalModal.id = 'globalModal';
  globalModal.classList.add('hidden');
  globalModal.hidden = true;
  const gTabs = doc.createElement('div');
  const memTab = doc.createElement('button');
  memTab.setAttribute('data-gtab', 'memory');
  memTab.dataset.gtab = 'memory';
  memTab.scrollIntoView = () => {};
  gTabs.appendChild(memTab);
  globalModal.appendChild(gTabs);
  doc.body.appendChild(globalModal);

  const dom = { globalModal, gTabs };
  for (const id of ['gMemoryTitle', 'gMemoryNote', 'gMemoryError', 'gMemoryList']) {
    dom[id] = doc.createElement('div');
    globalModal.appendChild(dom[id]);
  }

  let aborted = false;
  let resolveRead;
  let resolveDelete;
  let mode = 'slow-read';
  globalThis.fetch = async (url, options = {}) => {
    const target = String(url);
    if (target.startsWith('/api/characters/memories')) {
      if (mode === 'slow-read') {
        options.signal?.addEventListener('abort', () => { aborted = true; });
        return new Promise(resolve => { resolveRead = resolve; });
      }
      return okJson(memories({ id: 21, category: 'nickname', fact_value: '旧称呼' }));
    }
    if (target.includes('/memories/21') && options.method === 'DELETE') {
      return new Promise(resolve => { resolveDelete = resolve; });
    }
    return okJson({});
  };

  initGlobalSettings(dom, {
    openModal,
    closeModal,
    getActiveSession: () => ({ id: 's1', settings: { voiceProfileId: 2 } }),
  });
  t.after(() => {
    closeAllModals();
    initGlobalSettings({});
    for (const c of toastRoot.children) clearTimeout(c._timer);
    Object.assign(globalThis, saved);
  });

  // 1. Closing the modal while reading aborts the pending memory read and ignores a late answer.
  openGlobalSettings('memory');
  await settle();
  assert.equal(typeof resolveRead, 'function', '打开长期记忆页时应发起记忆读取请求');
  closeModal(globalModal);
  assert.equal(aborted, true, '关闭设置中心时应中止进行中的长期记忆读取');
  resolveRead(okJson(memories({ id: 99, category: 'nickname', fact_value: '迟到数据' })));
  await settle();
  assert.equal(dom.gMemoryList.querySelectorAll('.memory-item').length, 0, '关闭设置中心后迟到的读取结果不应渲染进列表');

  // 2. Closing the modal while deleting suppresses the late delete toast.
  mode = 'fast-read';
  openGlobalSettings('memory');
  await settle();
  const delBtn = dom.gMemoryList.querySelector('.memory-del');
  delBtn.click();
  delBtn.click();
  await settle();
  assert.equal(typeof resolveDelete, 'function', '两次点击删除后应发起删除请求');
  closeModal(globalModal);
  resolveDelete(okJson({ status: 'deleted' }));
  await settle();
  assert.equal(
    toastRoot.children.some(c => String(c.dataset?.msg || '').includes('已删除这条记忆')),
    false,
    '关闭设置中心后迟到的删除响应不应再弹成功提示',
  );
});
