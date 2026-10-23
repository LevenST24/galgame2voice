// P7: 标题画面模块 title.js（占位方案 B）——菜单、读取存档、键盘导航、减弱动效。
// DOM 环境复用 tests/helpers/adv_dom.mjs（与 test_adv_p6.mjs 相同风格）。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fakeDocument, microtasks } from './helpers/adv_dom.mjs';
import { createTitleScreen } from '../frontend/src/adv/title.js';

const pad2 = value => String(value).padStart(2, '0');
const localStamp = date => `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
  + ` ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
const DATE_RE = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/;
const keyEvent = key => ({ type: 'keydown', key, bubbles: true });

// 升序给出 12 个存档：s01 最旧（epoch 数字时间），s12 最新（ISO 本地串）。
const SESSIONS = [
  { id: 's01', title: '对话一', characterName: '小春', lastLine: '第一句。',
    updatedAt: 1420000000000, background: '/bg/cafe.png' },
  { id: 's02', title: '对话二', characterName: '凛', lastLine: '第二句。',
    updatedAt: '2016-02-02T02:02', background: '/bg/library.png' },
  { id: 's03', title: '对话三', characterName: '白鸟诗音', lastLine: '第三句。',
    updatedAt: '2017-03-03T03:03', background: null },
  { id: 's04', title: '对话四', characterName: '夜羽', lastLine: '第四句。',
    updatedAt: '2018-04-04T04:04', background: '/bg/sea.png' },
  { id: 's05', title: '对话五', characterName: '结衣', lastLine: '第五句。',
    updatedAt: '2019-05-05T05:05', background: null },
  { id: 's06', title: '对话六', characterName: '空', lastLine: '第六句。',
    updatedAt: '2020-06-06T06:06', background: null },
  { id: 's07', title: '对话七', characterName: '雪乃', lastLine: '第七句。',
    updatedAt: '2021-07-07T07:07', background: null },
  { id: 's08', title: '对话八', characterName: '霞', lastLine: '第八句。',
    updatedAt: '2022-08-08T08:08', background: null },
  { id: 's09', title: '对话九', characterName: '千夏', lastLine: '第九句。',
    updatedAt: '2023-09-09T09:09', background: null },
  { id: 's10', title: '对话十', characterName: '柚罗', lastLine: '第十句。',
    updatedAt: '2024-10-10T10:10', background: null },
  { id: 's11', title: '对话十一', characterName: '真琴', lastLine: '第十一句。',
    updatedAt: '2025-11-11T11:11', background: null },
  { id: 's12', title: '对话十二', characterName: '小春', lastLine: '第十二句。',
    updatedAt: '2026-10-19T21:04', background: null },
];

function setup(t, options = {}) {
  const doc = fakeDocument();
  globalThis.document = doc;
  const mqlEvents = [];
  const mql = {
    matches: Boolean(options.reduced),
    addEventListener(type, fn) { mqlEvents.push({ type, fn }); },
    removeEventListener(type, fn) {
      const at = mqlEvents.findIndex(entry => entry.type === type && entry.fn === fn);
      if (at >= 0) mqlEvents.splice(at, 1);
    },
  };
  // 闭包写法：模块会解构 actions，方法脱离 this 也不能丢调用记录。
  const calls = [];
  const listCalls = { count: 0 };
  const actions = {
    calls,
    startNew: () => calls.push('startNew'),
    continueLatest: () => calls.push('continueLatest'),
    loadSession: id => calls.push(['loadSession', id]),
    openCharacters: () => calls.push('openCharacters'),
    openSettings: () => calls.push('openSettings'),
    listSessions: () => {
      listCalls.count += 1;
      if (options.fail) return Promise.reject(new Error('offline'));
      return Promise.resolve(options.sessions);
    },
  };
  const root = doc.createElement('div');
  root.className = 'adv-stage';
  doc.body.appendChild(root);
  const title = createTitleScreen(root, {
    actions,
    documentObj: doc,
    matchMedia: () => mql,
  });
  t.after(() => title.destroy());
  return {
    doc, root, title, mql, mqlEvents, actions, listCalls,
    screen: () => title.element,
    items: () => title.element.querySelectorAll('.adv-title-item'),
    itemBy: key => title.element.querySelectorAll('.adv-title-item')
      .find(node => node.dataset.key === key),
    load: () => title.loadScreen,
    slots: () => title.loadScreen.querySelectorAll('.adv-title-slot'),
    empty: () => title.loadScreen.querySelector('.adv-title-empty'),
    back: () => title.loadScreen.querySelector('.adv-title-back'),
    prev: () => title.loadScreen.querySelector('.adv-title-page-prev'),
    next: () => title.loadScreen.querySelector('.adv-title-page-next'),
    info: () => title.loadScreen.querySelector('.adv-title-page-info'),
  };
}

// ---------------------------------------------------------------------------
// 1. 结构与设计稿要求
// ---------------------------------------------------------------------------

test('title: labels follow design B without the prototype note', t => {
  const h = setup(t);
  const items = h.items();
  assert.equal(items.length, 5);
  assert.deepEqual(items.map(item => item.tagName), ['BUTTON', 'BUTTON', 'BUTTON', 'BUTTON', 'BUTTON']);
  assert.deepEqual(items.map(item => item.querySelector('.adv-title-item-label').textContent),
    ['开始', '继续', '读取', '角色', '设置']);
  assert.deepEqual(items.map(item => item.querySelector('.adv-title-item-en').textContent),
    ['NEW', 'CONTINUE', 'LOAD', 'CAST', 'CONFIG']);
  assert.equal(h.screen().querySelector('.adv-title-logo').textContent, 'galgame2voice');
  assert.equal(h.screen().querySelector('.adv-title-sub').textContent, '与 AI 角色的对话剧场');
  const texts = [h.screen(), ...h.screen().querySelectorAll('*')].map(node => node.textContent);
  assert.equal(texts.some(text => /prototype/i.test(text)), false, 'no PROTOTYPE B leftover');
  assert.equal(texts.some(text => /原型/.test(text)), false, 'no 原型 leftover');
  assert.equal(h.back().textContent, '返回');
  assert.equal(h.prev().textContent, '上一页');
  assert.equal(h.next().textContent, '下一页');
  assert.equal(h.screen().querySelector('.adv-title-hint').textContent.includes('← →'), true);
  assert.equal(h.load().getAttribute('aria-label'), '读取存档');
  assert.equal(h.screen().querySelector('.adv-title-menu').getAttribute('aria-label'), '主菜单');
  const strips = h.screen().querySelectorAll('.adv-title-strip');
  assert.equal(strips.length, 2, 'two cloud layers');
  assert.equal(strips[0].querySelectorAll('.adv-title-blob').length, 10, 'one pattern repeated twice');
});

// ---------------------------------------------------------------------------
// 2. 键盘导航：方向键、Enter、Esc
// ---------------------------------------------------------------------------

test('title: arrow keys cycle the menu, Enter opens the load page, Esc returns', async t => {
  const h = setup(t, { sessions: SESSIONS });
  h.title.open();
  await microtasks();
  const items = h.items();
  assert.equal(h.doc.activeElement, items[0], 'open focuses the first menu item');

  const press = (node, key) => node.dispatchEvent(keyEvent(key));
  press(items[0], 'ArrowRight');
  assert.equal(h.doc.activeElement, items[1]);
  press(items[1], 'ArrowLeft');
  assert.equal(h.doc.activeElement, items[0]);
  press(items[0], 'ArrowDown');
  assert.equal(h.doc.activeElement, items[1]);
  press(items[1], 'ArrowUp');
  assert.equal(h.doc.activeElement, items[0]);
  press(items[0], 'ArrowLeft');
  assert.equal(h.doc.activeElement, items.at(-1), 'arrow navigation wraps');

  items[2].focus();
  press(items[2], 'Enter');
  assert.equal(h.load().hidden, false, 'Enter opens the load screen');
  assert.equal(h.title.isOpen, true);
  assert.equal(h.screen().classList.contains('is-load'), true);
  assert.equal(h.slots().length, 6);

  h.screen().dispatchEvent(keyEvent('Escape'));
  assert.equal(h.load().hidden, true, 'Esc returns to the menu');
  assert.equal(h.doc.activeElement, items[2]);

  h.screen().dispatchEvent(keyEvent('Escape'));
  assert.equal(h.title.isOpen, true, 'Esc alone never closes the title');
});

// ---------------------------------------------------------------------------
// 3. 菜单动作与 继续 的禁用
// ---------------------------------------------------------------------------

test('title: menu actions fire and close only when they should', async t => {
  const h = setup(t, { sessions: SESSIONS });
  h.title.open();
  await microtasks();
  const items = h.items();

  items[0].click();
  assert.deepEqual(h.actions.calls, ['startNew']);
  assert.equal(h.title.isOpen, false, '开始 closes the title');

  h.title.open();
  await microtasks();
  items[1].click();
  assert.deepEqual(h.actions.calls.at(-1), 'continueLatest');
  assert.equal(h.title.isOpen, false, '继续 closes the title');

  h.title.open();
  await microtasks();
  items[3].click();
  items[4].click();
  assert.deepEqual(h.actions.calls.slice(-2), ['openCharacters', 'openSettings']);
  assert.equal(h.title.isOpen, true, '角色 and 设置 keep the title open');
  assert.equal(h.load().hidden, true);
});

test('title: a click on the label text of a menu item activates it', async t => {
  const h = setup(t, { sessions: SESSIONS });
  h.title.open();
  await microtasks();
  h.items()[0].querySelector('.adv-title-item-label').click();
  assert.deepEqual(h.actions.calls, ['startNew']);
});

test('title: 继续 is disabled while there are zero sessions', async t => {
  const h = setup(t, { sessions: [] });
  h.title.open();
  await microtasks();
  const cont = h.itemBy('continue');
  assert.equal(cont.disabled, true);
  assert.equal(cont.getAttribute('aria-disabled'), 'true');
  assert.equal(h.itemBy('load').getAttribute('aria-disabled'), 'false');

  // A forced click must not fire continueLatest.
  cont.dispatchEvent({ type: 'click', button: 0 });
  assert.deepEqual(h.actions.calls, []);
  assert.equal(h.title.isOpen, true, 'a disabled item cannot close the title');

  h.title.close();
  h.listCalls.count = 0;
  h.title.open();
  await microtasks();
  assert.equal(h.listCalls.count, 1, 'open() re-reads the sessions once');
});

test('title: a failed session list also disables 继续', async t => {
  const h = setup(t, { fail: true });
  h.title.open();
  await microtasks();
  assert.equal(h.itemBy('continue').disabled, true);
  assert.equal(h.itemBy('continue').getAttribute('aria-disabled'), 'true');
});

// ---------------------------------------------------------------------------
// 4. 读取界面：排序、字段、日期、天空缩略图
// ---------------------------------------------------------------------------

test('title: load slots show newest first with full fields', async t => {
  const h = setup(t, { sessions: SESSIONS });
  h.title.open();
  await microtasks();
  h.itemBy('load').click();
  const slots = h.slots();
  assert.equal(slots.length, 6, 'six slots per page');
  assert.deepEqual(slots.map(slot => slot.getAttribute('data-session')),
    ['s12', 's11', 's10', 's09', 's08', 's07'], 'newest first');

  const first = slots[0];
  assert.equal(first.querySelector('.adv-title-slot-character').textContent, '小春');
  assert.equal(first.querySelector('.adv-title-slot-title').textContent, '对话十二');
  assert.equal(first.querySelector('.adv-title-slot-line').textContent, '第十二句。');
  assert.equal(first.querySelector('.adv-title-slot-date').textContent, '2026-10-19 21:04');
  assert.equal(first.querySelector('.adv-title-thumb-image'), null, 'a null background keeps the CSS sky');

  const second = slots[1];
  assert.equal(second.querySelector('.adv-title-slot-character').textContent, '真琴');
  assert.equal(second.querySelector('.adv-title-slot-date').textContent, '2025-11-11 11:11');

  assert.equal(slots[5].getAttribute('data-session'), 's07');
  assert.equal(slots[5].querySelector('.adv-title-slot-date').textContent, '2021-07-07 07:07');
  assert.equal(localStamp(new Date(SESSIONS[0].updatedAt)).match(DATE_RE) !== null, true);

  assert.equal(h.empty().hidden, true, 'the empty note stays hidden with saves');
  assert.ok(slots.every(slot => slot.tagName === 'BUTTON'));
});

test('title: numeric and invalid timestamps format as YYYY-MM-DD HH:mm', async t => {
  const rows = [
    { id: 'n1', title: '数字', characterName: 'A', lastLine: 'ok', updatedAt: 1759999999999, background: null },
    { id: 'n2', title: '坏值', characterName: 'B', lastLine: 'ok', updatedAt: 'not-a-date', background: null },
    { id: 'n3', title: '缺失', characterName: 'C', lastLine: 'ok', updatedAt: undefined, background: null },
  ];
  const h = setup(t, { sessions: rows });
  h.title.open();
  await microtasks();
  h.itemBy('load').click();
  const slots = h.slots();
  assert.equal(slots.map(slot => slot.getAttribute('data-session')).join(','), 'n1,n2,n3');
  assert.equal(slots[0].querySelector('.adv-title-slot-date').textContent.match(DATE_RE) !== null, true);
  assert.equal(slots[1].querySelector('.adv-title-slot-date').textContent, '');
  assert.equal(slots[2].querySelector('.adv-title-slot-date').textContent, '');
});

test('title: no sessions shows 还没有存档', async t => {
  const h = setup(t, { sessions: [] });
  h.title.open();
  await microtasks();
  h.itemBy('load').click();
  assert.equal(h.slots().length, 0);
  assert.equal(h.empty().hidden, false);
  assert.equal(h.empty().textContent, '还没有存档');
  assert.equal(h.doc.activeElement, h.back(), 'focus falls back to the back button');
});

// ---------------------------------------------------------------------------
// 5. 分页与点击读取
// ---------------------------------------------------------------------------

test('title: pagination walks pages newest first and a slot loads and closes', async t => {
  const h = setup(t, { sessions: SESSIONS });
  h.title.open();
  await microtasks();
  h.itemBy('load').click();

  assert.equal(h.prev().disabled, true, 'first page has no previous');
  assert.equal(h.prev().getAttribute('aria-disabled'), 'true');
  assert.equal(h.next().disabled, false);
  assert.equal(h.info().textContent, '第 1 / 2 页 · 共 12 个存档');

  h.next().click();
  assert.equal(h.slots().length, 6, 'still six per page');
  assert.deepEqual(h.slots().map(slot => slot.getAttribute('data-session')),
    ['s06', 's05', 's04', 's03', 's02', 's01']);
  assert.equal(h.info().textContent, '第 2 / 2 页 · 共 12 个存档');
  assert.equal(h.next().disabled, true, 'last page has no next');
  assert.equal(h.prev().disabled, false);

  h.slots()[0].click();
  assert.deepEqual(h.actions.calls.at(-1), ['loadSession', 's06']);
  assert.equal(h.title.isOpen, false, 'a slot click closes the title');
  assert.equal(h.load().hidden, true, 'the load screen resets for the next visit');

  h.title.open();
  await microtasks();
  h.itemBy('load').click();
  assert.equal(h.info().textContent, '第 1 / 2 页 · 共 12 个存档', 'a new visit starts on the newest page');
  h.next().click();
  const thumbs = [];
  for (const slot of h.slots()) {
    const image = slot.querySelector('.adv-title-thumb-image');
    if (image) thumbs.push([slot.getAttribute('data-session'), image.src]);
  }
  assert.deepEqual(thumbs,
    [['s04', '/bg/sea.png'], ['s02', '/bg/library.png'], ['s01', '/bg/cafe.png']],
    'backgrounds arrive as image thumbs');
});

// ---------------------------------------------------------------------------
// 6. 返回按钮
// ---------------------------------------------------------------------------

test('title: the back button returns to the menu', async t => {
  const h = setup(t, { sessions: SESSIONS });
  h.title.open();
  await microtasks();
  h.itemBy('load').click();
  assert.equal(h.load().hidden, false);
  h.back().click();
  assert.equal(h.load().hidden, true);
  assert.equal(h.doc.activeElement, h.itemBy('load'));
  assert.equal(h.title.isOpen, true);
  assert.equal(h.screen().classList.contains('is-load'), false);
});

// ---------------------------------------------------------------------------
// 7. 减弱动效
// ---------------------------------------------------------------------------

test('title: reduced motion freezes the clouds and can change live', t => {
  const h = setup(t, { reduced: false });
  assert.equal(h.mqlEvents.length, 1, 'one change listener subscribed');
  assert.equal(h.screen().classList.contains('is-reduced'), false, 'full motion by default');

  h.mql.matches = true;
  h.mqlEvents.forEach(entry => entry.fn({ type: 'change', matches: true }));
  assert.equal(h.screen().classList.contains('is-reduced'), true, 'the clouds stop drifting');

  h.mql.matches = false;
  h.mqlEvents.forEach(entry => entry.fn({ type: 'change', matches: false }));
  assert.equal(h.screen().classList.contains('is-reduced'), false, 'motion comes back');

  h.mql.matches = true;
  h.mqlEvents.forEach(entry => entry.fn({ type: 'change', matches: true }));
  h.title.destroy();
  assert.equal(h.mqlEvents.length, 0, 'destroy releases the media listener');
});

// ---------------------------------------------------------------------------
// 8. open/close/isOpen
// ---------------------------------------------------------------------------

test('title: open covers the stage and close hides it', async t => {
  const h = setup(t, { sessions: SESSIONS });
  assert.equal(h.title.isOpen, false);
  assert.equal(h.screen().hidden, true);
  assert.equal(h.screen().getAttribute('hidden'), '');

  h.title.open();
  await microtasks();
  assert.equal(h.title.isOpen, true);
  assert.equal(h.screen().hidden, false);
  assert.equal(h.screen().getAttribute('hidden'), null);
  assert.equal(h.root.querySelector('.adv-title-screen'), h.screen(), 'mounted inside the stage');

  h.title.close();
  assert.equal(h.title.isOpen, false);
  assert.equal(h.screen().hidden, true);
  assert.equal(h.screen().getAttribute('hidden'), '');
});

// ---------------------------------------------------------------------------
// 9. destroy 清理
// ---------------------------------------------------------------------------

test('title: destroy removes the DOM and every listener', async t => {
  const h = setup(t, { sessions: SESSIONS });
  h.title.open();
  await microtasks();
  h.itemBy('load').click();
  const screen = h.screen();
  const nodes = [screen, ...screen.querySelectorAll('button')];
  assert.ok(nodes.length > 10);
  h.title.destroy();

  assert.equal(h.root.querySelector('.adv-title-screen'), null, 'the module node is gone');
  assert.equal(screen.parentNode, null);
  assert.equal(h.title.isOpen, false);
  for (const node of nodes) {
    assert.ok([...node.listeners.values()].every(set => set.size === 0), `${node.className} leaked listeners`);
    assert.equal(node.onclick ?? null, null, `${node.className} leaked onclick`);
  }
  assert.equal(h.mqlEvents.length, 0, 'the media listener is released');

  h.title.open();
  assert.equal(h.title.isOpen, false, 'a destroyed module never re-mounts');
});

// ---------------------------------------------------------------------------
// 10. 默认参数容忍
// ---------------------------------------------------------------------------

test('title: works with an empty actions object', async t => {
  const doc = fakeDocument();
  globalThis.document = doc;
  const root = doc.createElement('div');
  doc.body.appendChild(root);
  const title = createTitleScreen(root, { documentObj: doc, matchMedia: () => ({ matches: false }) });
  t.after(() => title.destroy());
  title.open();
  await microtasks();
  const items = title.element.querySelectorAll('.adv-title-item');
  items[0].click();
  title.open();
  items[2].click();
  assert.equal(title.loadScreen.hidden, false);
  assert.equal(title.isOpen, true);
});

// ---------------------------------------------------------------------------
// 11. title.css 结构检查（同 P6 对 background.css 的做法）
// ---------------------------------------------------------------------------

test('title.css: unit geometry, drifting clouds, sky thumbs, reduced motion', () => {
  const css = readFileSync(new URL('../frontend/src/adv/title.css', import.meta.url), 'utf8');
  const unitSized = css.match(/calc\(-?\d+(?:\.\d+)? \* var\(--u\)\)/g) || [];
  assert.ok(unitSized.length >= 25, `sizes use the --u stage unit (found ${unitSized.length})`);

  assert.match(css, /\.adv-title-screen\s*\{[^}]*position:\s*absolute/);
  assert.match(css, /\.adv-title-screen\s*\{[^}]*inset:\s*0/);
  // The overlay layer lets clicks through to the stage; the title must take them back.
  assert.match(css, /\.adv-title-screen\s*\{[^}]*pointer-events:\s*auto/);
  assert.equal(/transform:\s*scale\(/.test(css), false, 'no self scaling');
  assert.equal(/100vw|100dvh/.test(css), false, 'no own letterbox viewport math');

  assert.match(css, /@keyframes\s+adv-title-drift\s*\{[\s\S]*?-1920 \* var\(--u\)/);
  assert.match(css, /\.adv-title-strip\s*\{[^}]*animation:\s*adv-title-drift[^}]*infinite/);
  assert.match(css, /\.adv-title-strip-far\s*\{[^}]*150s/);
  assert.match(css, /\.adv-title-strip-near\s*\{[^}]*84s/);

  assert.match(css, /\.adv-title-slot-line\s*\{[^}]*white-space:\s*nowrap/);
  assert.match(css, /\.adv-title-slot-line\s*\{[^}]*text-overflow:\s*ellipsis/);
  assert.match(css, /\.adv-title-thumb-image[^{]*\{[^}]*object-fit:\s*cover/);
  assert.match(css, /:has\(\.adv-title-thumb-image\)/);
  assert.match(css, /radial-gradient\(/);

  assert.match(css, /\.adv-title-screen\.is-reduced\s+\.adv-title-strip\s*\{[^}]*animation:\s*none/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)[\s\S]*\.adv-title-strip[\s\S]*animation:\s*none/);
  assert.match(css, /:focus-visible\s*\{[^}]*outline:\s*calc/);
  assert.match(css, /\.adv-title-menu\s*\{[^}]*bottom:\s*calc\(\d+ \* var\(--u\)\)/);
  assert.equal(/backdrop-filter/.test(css), false, 'no backdrop-filter anywhere');
});
