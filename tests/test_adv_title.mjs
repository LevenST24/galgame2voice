import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fakeDocument, fakeClock, microtasks } from './helpers/adv_dom.mjs';
import { createTitleScreen, formatHomeTime } from '../frontend/src/adv/title.js';
import { homeExcerpt, createTitleActions } from '../frontend/src/adv/actions.js';
import { showTitle } from '../frontend/src/adv/title_hook.js';
import { createPortraitCache } from '../frontend/src/adv/portrait_cache.js';

const NOW = new Date(2026, 9, 24, 22, 30);
const audio = { status: 'ready', chunks: [{ audio_url: '/fake.wav' }] };
const messages = [{ role: 'user', content: '之前的问题' }, { role: 'assistant', content: '旧回复' },
  { role: 'user', content: '想去哪里？' }, { id: 'm9', role: 'assistant', ts: NOW.getTime(),
    lines: [{ index: 0, zh: '第一句', ja: '一' }, { index: 1, zh: '去海边吧。', ja: '海へ。', audio }] },
  { role: 'user', content: '这条在她之后，不应显示' }, { role: 'assistant', content: '' }];
const snapshot = (id, n = 0) => ({ id, title: `对话${id}`, characterName: `角色${id}`, updatedAt: NOW.getTime() - n * 86400000,
  hasUserMessages: id === 'a', settings: { voiceProfileId: 7, advBackground: 'w/sea', advBgTime: 'night' },
  excerpt: id === 'a' ? homeExcerpt(messages) : { zh: `台词${id}` }, lastLine: `台词${id}`, background: '/fake.webp' });
function textTree(node) { return node.textContent + node.children.map(textTree).join(''); }
const escape = (node, extra = {}) => node.dispatchEvent({ type: 'keydown', key: 'Escape', ...extra });

test('loading providers keeps the excerpt and checking pill; onboarding history never controls the primary label', async t => {
  const h = await fixture(t, { status: { modelLoaded: false, hasModel: false, model: { state: 'error' } } });
  assert.ok(!h.q('.home-first-title'));
  assert.ok(h.q('.home-quote'));
  assert.equal(h.q('.home-dot').className, 'home-dot warn');
  assert.match(textTree(h.q('.home-pill')), /对话 检查中/);
  h.setStatus({ modelLoaded: true, hasModel: false, model: { state: 'error' } }); await h.ui.refresh();
  assert.ok(h.q('.home-first-title'));
  h.sessions[0].hasUserMessages = true;
  h.setStatus({ modelLoaded: true, hasModel: true, model: { state: 'ok' } }); await h.ui.refresh();
  assert.equal(textTree(h.q('.home-continue')), '接着聊 →');
  h.sessions[0].hasUserMessages = false; await h.ui.refresh();
  assert.equal(textTree(h.q('.home-continue')), '开始聊天');
});

test('opening reconciles the stage even if the selected id already matches; stale active id cannot replace preview target', async t => {
  const h = await fixture(t, { active: 'missing', actions: { previewSession: async () => {} } });
  assert.equal(h.q('.home-meta').children[0].textContent, '角色a');
  const same = await fixture(t);
  assert.ok(same.calls.some(c => Array.isArray(c) && c[0] === 'preview' && c[1] === 'a'));
});

test('late profiles update names and cropped avatars; late manifests retry without replacing focused recent buttons', async t => {
  let profiles = [], artReady = false;
  const sessions = ['a', 'b'].map(id => ({ id, title: `和${id}聊天`, messages: [], settings: { voiceProfileId: 7 } }));
  const actions = createTitleActions({ getSessions: () => sessions, getProfiles: () => profiles });
  const h = await fixture(t, { actions: { listSessions: actions.listSessions, getAvatar: async () => artReady ? {
    rect: { x: 175, y: 0, width: 362, height: 325 }, crop: { left: 540, top: 865 },
    layers: [{ file: '/api/media/portrait/7/layers/A/face.webp?sig=fake', rect: { x: 715, y: 865, width: 362, height: 325 } }],
  } : null } });
  profiles = [{ id: 7, name: '白雪乃爱' }]; h.clock.tick(5000); await microtasks();
  assert.equal(h.q('.home-meta').children[0].textContent, '白雪乃爱');
  const recent = h.q('.home-recent'); recent.focus();
  artReady = true; h.clock.tick(5000); await microtasks();
  assert.ok(h.q('.home-avatar').querySelector('img'));
  assert.equal(h.q('.home-avatar').querySelector('img').style.left, '0%');
  assert.equal(h.doc.activeElement, recent);
});

test('home hides and inerts every stage control and restores them on close', async t => {
  const h = await fixture(t);
  assert.equal(h.q('.adv-drawer'), null);
  assert.equal(h.root.querySelector('.adv-drawer').inert, true);
  for (const selector of ['.adv-layer-textbox', '.adv-layer-quickmenu', '.adv-app-tools', '.adv-art-status']) {
    assert.equal(h.stage.querySelector(selector).inert, true, selector);
    const css = readFileSync(new URL('../frontend/src/adv/title.css', import.meta.url), 'utf8');
    assert.ok(css.includes(`.adv-stage.is-home ${selector}`));
  }
  h.ui.close();
  assert.equal(h.root.querySelector('.adv-drawer').inert, false);
  for (const node of h.stage.querySelectorAll('.adv-layer-textbox, .adv-layer-quickmenu, .adv-app-tools, .adv-art-status')) assert.equal(node.inert, false);
});

test('all-session cards have title then optional character; background getter is re-read after lists arrive', async t => {
  let loaded = false;
  const a = snapshot('a'); Object.defineProperty(a, 'background', { get: () => loaded ? '/fake-late.webp' : '' });
  const h = await fixture(t, { sessions: [a, { ...snapshot('b'), characterName: '', background: '' }] });
  h.q('.home-all').click(); assert.ok(!h.q('.adv-title-thumb-image'));
  loaded = true; h.clock.tick(5000); await microtasks();
  assert.equal(h.q('.adv-title-thumb-image').src, '/fake-late.webp');
  assert.equal(h.q('.adv-title-slot-meta').children[0].textContent, a.title);
  assert.equal(h.q('.adv-title-slot-title').textContent, a.characterName);
  assert.ok(!h.ui.element.querySelectorAll('.adv-title-slot')[1].querySelector('.adv-title-slot-title'));
});
async function fixture(t, options = {}) {
  const doc = fakeDocument(), clock = fakeClock(), calls = [];
  const stage = doc.createElement('div'); stage.className = 'adv-stage'; doc.body.appendChild(stage);
  for (const cls of ['adv-layer-textbox', 'adv-layer-quickmenu', 'adv-app-tools', 'adv-art-status']) { const n = doc.createElement('div'); n.className = cls; stage.appendChild(n); }
  const root = doc.createElement('div'); root.className = 'adv-layer-overlay'; stage.appendChild(root);
  const drawer = doc.createElement('div'); drawer.className = 'adv-drawer'; root.appendChild(drawer);
  let active = options.active === undefined ? 'a' : options.active;
  let status = options.status || { model: { state: 'ok', label: '测试模型 · 假模型' }, modelLoaded: true, hasModel: true, engine: { state: 'ready', base_url: 'http://127.0.0.1:9880' } };
  const sessions = options.sessions || ['a','b','c','d','e','f','g','h'].map((id, n) => snapshot(id, n));
  let finish, notify;
  const actions = { listSessions: () => sessions, getActiveId: () => active, getStatus: async () => { calls.push('status'); return status; },
    loadSession: id => calls.push(['load', id]), startNew: () => calls.push('new'), startNewWith: data => calls.push(['newWith', data]),
    previewSession: async id => { active = id; calls.push(['preview', id]); },
    listCharacters: () => [{ id: 7, name: '角色甲' }, { id: 8, name: '角色乙' }],
    getBackgrounds: () => [{ key: 'w/sea', name: '海边' }, { key: 'w/room', name: '房间' }],
    openSettings: tab => calls.push(['settings', tab]), openCharacters: () => calls.push('characters'),
    voiceEnabled: () => true,
    playExcerpt: e => { calls.push(['play', e.messageId, e.line.index]); return new Promise(resolve => { finish = resolve; }); },
    stopExcerpt: () => calls.push('stop'), subscribeSettings: fn => { notify = fn; return () => { notify = null; }; },
    ...options.actions };
  const ui = createTitleScreen(root, { actions, documentObj: doc, clock, now: () => NOW });
  t.after(() => ui.destroy()); ui.open(); await microtasks();
  const q = selector => ui.element.querySelector(selector);
  return { doc, clock, calls, stage, root, ui, q, sessions, actions,
    setStatus(value) { status = value; }, finish: result => finish(result), notify: () => notify?.() };
}

test('excerpt selects last textual assistant and its final sentence, earlier user, Chinese/Japanese and time', async t => {
  const h = await fixture(t);
  assert.equal(h.q('.home-meta').children[0].textContent, '角色a');
  assert.equal(h.q('.home-time').textContent, ' · 今天 22:30');
  assert.equal(h.q('.home-you').textContent, '你：想去哪里？');
  assert.equal(h.q('.home-quote').textContent, '「去海边吧。」'); assert.equal(h.q('.home-ja').textContent, '海へ。');
  assert.equal(h.doc.activeElement, h.q('.home-continue'));
  assert.equal(h.ui.element.getAttribute('aria-label'), '首页');
});
test('legacy content and full portable greeting remain in the DOM; missing user/Japanese hidden', async t => {
  const greeting = '你好！我是你的 AI 助手，支持语音输入与朗读回复。\n请随时和我聊天。';
  const excerpt = homeExcerpt([{ role: 'assistant', content: greeting }]);
  const h = await fixture(t, { sessions: [{ ...snapshot('a'), characterName: '', excerpt }] });
  assert.equal(h.q('.home-quote').textContent, `「${greeting}」`); assert.match(textTree(h.ui.element), /支持语音输入与朗读回复/);
  assert.equal(h.q('.home-meta').children[0].textContent, '对话a');
  assert.equal(h.q('.home-you'), null); assert.equal(h.q('.home-ja'), null); assert.equal(h.q('.home-recents').hidden, true);
  assert.equal(h.q('.home-listen'), null);
});
test('continue uses current session even when another is newer; stage home class and input inert restored', async t => {
  const h = await fixture(t, { active: 'c' });
  assert.equal(h.stage.classList.contains('is-home'), true);
  assert.equal(h.stage.querySelector('.adv-layer-textbox').inert, true);
  h.q('.home-continue').children[0].click();
  assert.deepEqual(h.calls.at(-1), ['load', 'c']); assert.equal(h.ui.isOpen, false);
  assert.equal(h.stage.classList.contains('is-home'), false); assert.equal(h.stage.querySelector('.adv-layer-textbox').inert, false);
});
test('no active session previews the latest so the stage and excerpt agree', async t => {
  const h = await fixture(t, { active: null });
  assert.ok(h.calls.some(c => Array.isArray(c) && c[0] === 'preview' && c[1] === 'a'));
  assert.equal(h.q('.home-meta').children[0].textContent, '角色a');
});
test('recents exclude main, cap at three, preview without closing/fade and put previous main back', async t => {
  const h = await fixture(t);
  const recents = () => h.ui.element.querySelectorAll('.home-recent');
  assert.deepEqual(recents().map(n => n.dataset.session), ['b','c','d']);
  recents()[0].children[1].children[0].click(); await microtasks();
  assert.deepEqual(h.calls.at(-1), ['preview', 'b']); assert.equal(h.ui.isOpen, true);
  assert.equal(h.q('.home-quote').textContent, '「台词b」'); assert.equal(h.stage.classList.contains('is-home'), true);
  assert.deepEqual(recents().map(n => n.dataset.session), ['a','c','d']);
  assert.equal(h.doc.activeElement, h.q('.home-continue'));
});
test('audio availability, stop toggle, natural completion and playback failure', async t => {
  const h = await fixture(t); h.q('.home-listen').children[0].click();
  assert.equal(textTree(h.q('.home-listen')), '■ 停止'); assert.deepEqual(h.calls.at(-1), ['play','m9',1]);
  h.q('.home-listen').click(); assert.equal(h.calls.at(-1), 'stop'); assert.equal(textTree(h.q('.home-listen')), '▶ 试听这句');
  h.finish('ended'); await microtasks();
  h.q('.home-listen').click(); h.finish('ended'); await microtasks(); assert.equal(textTree(h.q('.home-listen')), '▶ 试听这句');
  h.q('.home-listen').click(); h.finish('failed'); await microtasks(); assert.equal(h.q('.home-listen').hidden, true);
});
for (const scenario of ['empty', 'expired', 'voiceOff']) test(`unplayable audio: ${scenario}`, async t => {
  const s = snapshot('a'); s.excerpt = structuredClone(s.excerpt);
  if (scenario === 'empty') s.excerpt.line.audio.chunks = [];
  if (scenario === 'expired') s.excerpt.ts = NOW.getTime() - 7 * 86400000 - 1;
  const h = await fixture(t, { sessions: [s], actions: { voiceEnabled: () => scenario !== 'voiceOff' } });
  assert.equal(h.q('.home-listen'), null);
});
test('draft lists/defaults, selection arguments and cancel without mutation', async t => {
  const h = await fixture(t); h.q('.home-new').click();
  const open = key => { h.q(`.home-pick-${key}`).click(); return h.ui.element.querySelectorAll('.home-option'); };
  let opts = open('voiceProfileId'); assert.deepEqual(opts.map(textTree), ['角色甲','角色乙']); assert.equal(opts[0].getAttribute('aria-pressed'), 'true');
  opts[1].children[0].click();
  opts = open('advBackground'); assert.deepEqual(opts.map(textTree), ['默认背景','海边','房间']); assert.equal(opts[1].getAttribute('aria-pressed'), 'true'); opts[2].click();
  opts = open('advBgTime'); assert.deepEqual(opts.map(textTree), ['跟随时间','白天','傍晚','夜晚']); assert.equal(opts[3].getAttribute('aria-pressed'), 'true'); opts[1].click();
  h.q('.home-start').click(); assert.deepEqual(h.calls.at(-1), ['newWith', { voiceProfileId: 8, advBackground: 'w/room', advBgTime: 'day' }]);
  assert.equal(h.sessions[0].settings.voiceProfileId, 7);
  h.ui.open(); await microtasks(); h.q('.home-new').click(); h.q('.home-cancel').click(); assert.ok(h.q('.home-continue'));
});
test('draft filters places by character work and resets a place from the previous work', async t => {
  const h = await fixture(t, { actions: {
    listCharacters: () => [{ id: 7, name: '角色甲', work: 'a' }, { id: 8, name: '角色乙', work: 'b' }],
    getBackgrounds: () => [{ key: 'w/sea', name: '甲海边', work: 'a' }, { key: 'b/sea', name: '乙海边', work: 'b' }],
  } });
  h.q('.home-new').click(); h.q('.home-pick-advBackground').click();
  let opts = h.ui.element.querySelectorAll('.home-option');
  assert.deepEqual(opts.map(textTree), ['默认背景', '甲海边']); assert.equal(opts[1].getAttribute('aria-pressed'), 'true');
  opts[1].click(); h.q('.home-pick-voiceProfileId').click(); h.ui.element.querySelectorAll('.home-option')[1].click();
  h.q('.home-pick-advBackground').click(); opts = h.ui.element.querySelectorAll('.home-option');
  assert.deepEqual(opts.map(textTree), ['默认背景', '乙海边']); assert.equal(opts[0].getAttribute('aria-pressed'), 'true');
  h.q('.home-start').click();
  assert.deepEqual(h.calls.at(-1), ['newWith', { voiceProfileId: 8, advBackground: '', advBgTime: 'night' }]);
  assert.equal(h.sessions[0].settings.advBackground, 'w/sea');
});

for (const work of [undefined, 'missing']) test(`draft lists all places when character work is ${work}`, async t => {
  const h = await fixture(t, { actions: {
    listCharacters: () => [{ id: 7, name: '角色甲', work }],
    getBackgrounds: () => [{ key: 'w/sea', name: '甲海边', work: 'a' }, { key: 'b/sea', name: '乙海边', work: 'b' }],
  } });
  h.q('.home-new').click(); h.q('.home-pick-advBackground').click();
  assert.deepEqual(h.ui.element.querySelectorAll('.home-option').map(textTree), ['默认背景', '甲海边', '乙海边']);
});

test('draft starts from 默认背景 when the main session uses a place the list cannot show', async t => {
  // 'none' is the plain sky from the ADV background panel; the draft list has no such entry.
  const session = snapshot('a'); session.settings.advBackground = 'none';
  const h = await fixture(t, { sessions: [session], actions: { listCharacters: () => [{ id: 7, name: '角色甲' }] } });
  h.q('.home-new').click();
  assert.equal(textTree(h.q('.home-pick-advBackground')), '默认背景 ▾');
  h.q('.home-start').click();
  assert.deepEqual(h.calls.at(-1), ['newWith', { voiceProfileId: 7, advBackground: '', advBgTime: 'night' }]);
});

test('changing to a work without backgrounds resets the previous work place while offering all places', async t => {
  const h = await fixture(t, { actions: {
    listCharacters: () => [{ id: 7, name: '角色甲', work: 'a' }, { id: 8, name: '角色乙', work: 'missing' }],
    getBackgrounds: () => [{ key: 'w/sea', name: '甲海边', work: 'a' }],
  } });
  h.q('.home-new').click(); h.q('.home-pick-voiceProfileId').click(); h.ui.element.querySelectorAll('.home-option')[1].click();
  h.q('.home-pick-advBackground').click();
  const opts = h.ui.element.querySelectorAll('.home-option');
  assert.deepEqual(opts.map(textTree), ['默认背景', '甲海边']); assert.equal(opts[0].getAttribute('aria-pressed'), 'true');
});

test('first use checks model/voice/character, text-only lasts only this opening, updates after settings close', async t => {
  const status = { modelLoaded: true, hasModel: false, model: { state: 'error', label: '未配置对话模型' }, engine: { state: 'unavailable' } };
  const session = snapshot('a'); session.settings.voiceProfileId = null;
  const h = await fixture(t, { status, sessions: [session] });
  assert.ok(h.q('.home-first-title')); assert.equal(h.ui.element.querySelectorAll('.home-step.is-done').length, 0);
  assert.equal(h.q('.home-continue'), null); assert.equal(h.doc.activeElement, h.q('.home-step-pending'));
  assert.equal(h.ui.element.querySelectorAll('button').some(button => textTree(button) === '开始聊天'), false);
  h.q('.home-step-pending').click(); assert.deepEqual(h.calls.at(-1), ['settings','models']);
  h.q('.home-text-only').click(); assert.equal(h.ui.element.querySelectorAll('.home-step.is-done').length, 1);
  session.settings.voiceProfileId = 7; h.notify(); await microtasks(); assert.equal(h.ui.element.querySelectorAll('.home-step.is-done').length, 2);
  h.ui.close(); h.ui.open(); await microtasks(); assert.equal(h.ui.element.querySelectorAll('.home-step.is-done').length, 1);
  h.setStatus({ ...status, hasModel: true, model: { state: 'ok' } }); h.notify(); await microtasks();
  assert.equal(h.q('.home-first-title'), null); assert.ok(h.q('.home-continue')); assert.match(textTree(h.q('.home-notice')), /先用文字/);
  assert.equal(textTree(h.q('.home-continue')), '接着聊 →');
});
test('status colors/text, explanations, settings tabs, polling and disposal with fake clock', async t => {
  const h = await fixture(t);
  assert.deepEqual(h.ui.element.querySelectorAll('.home-dot').map(n => n.className), ['home-dot ok','home-dot ok']);
  const pills = h.ui.element.querySelectorAll('.home-pill');
  pills[0].children[1].click(); assert.match(textTree(h.q('.home-popover')), /测试模型/);
  h.q('.home-popover').querySelector('button').click(); assert.deepEqual(h.calls.at(-1), ['settings','models']);
  pills[1].click(); assert.match(textTree(h.q('.home-popover')), /语音服务已连接.*127.0.0.1:9880/);
  h.q('.home-popover').querySelector('button').click(); assert.deepEqual(h.calls.at(-1), ['settings','inference']);
  h.setStatus({ modelLoaded: false, model: { state: 'error' }, engine: { state: 'starting' } });
  h.clock.tick(4999); await microtasks(); assert.equal(h.calls.filter(c => c === 'status').length, 1);
  h.clock.tick(1); await microtasks(); assert.equal(h.calls.filter(c => c === 'status').length, 2);
  assert.deepEqual(h.ui.element.querySelectorAll('.home-dot').map(n => n.className), ['home-dot warn','home-dot warn']);
  assert.match(textTree(pills[0]), /对话 检查中/); assert.match(textTree(pills[1]), /语音 启动中/);
  h.setStatus({ modelLoaded: true, hasModel: false, model: { state: 'error', label: '未填写 API Key' }, engine: { state: 'failed', message: '假引擎失败原因' } });
  h.clock.tick(5000); await microtasks();
  assert.deepEqual(h.ui.element.querySelectorAll('.home-dot').map(n => n.className), ['home-dot bad','home-dot bad']);
  assert.match(textTree(pills[0]), /对话 未设置/); assert.match(textTree(pills[1]), /语音 未连接/);
  pills[1].click(); assert.match(textTree(h.q('.home-popover')), /假引擎失败原因/);
  h.ui.close(); const count = h.calls.length; h.clock.tick(20000); await microtasks(); h.notify();
  assert.equal(h.calls.length, count); assert.equal(h.clock.timers.size, 0);
});
test('all sessions paginate, show image/title/last line/time, return and load via nested spans', async t => {
  const h = await fixture(t); h.q('.home-all').click(); assert.equal(h.ui.loadScreen.hidden, false);
  assert.equal(h.ui.element.querySelectorAll('.adv-title-slot').length, 6); assert.equal(h.q('.adv-title-thumb-image').src, '/fake.webp');
  assert.match(textTree(h.q('.adv-title-slot')), /对话a.*角色a.*台词a.*今天/);
  h.q('.adv-title-page-next').click(); assert.equal(h.ui.element.querySelectorAll('.adv-title-slot').length, 2);
  assert.match(h.q('.adv-title-page-info').textContent, /第 2 \/ 2 页/);
  h.q('.adv-title-page-prev').click(); h.q('.adv-title-back').click(); assert.equal(h.ui.loadScreen.hidden, true);
  h.q('.home-all').click(); escape(h.q('.adv-title-back')); assert.equal(h.ui.loadScreen.hidden, true);
  h.q('.home-all').click(); h.q('.adv-title-slot-meta').children[0].click(); assert.deepEqual(h.calls.at(-1), ['load','a']); assert.equal(h.ui.isOpen, false);
});
test('Escape closes popover then dropdown then draft/list; composing keys ignored; outside click closes', async t => {
  const h = await fixture(t); h.q('.home-new').click();
  h.q('.home-pick-advBgTime').click(); escape(h.q('.home-option'), { isComposing: true }); assert.ok(h.q('.home-dropdown'));
  escape(h.q('.home-option'), { keyCode: 229 }); assert.ok(h.q('.home-dropdown'));
  escape(h.q('.home-option')); assert.equal(h.q('.home-dropdown'), null); assert.ok(h.q('.home-start'));
  h.q('.home-pill').click(); escape(h.q('.home-popover').querySelector('button')); assert.equal(h.q('.home-popover'), null); assert.ok(h.q('.home-start'));
  escape(h.q('.home-start')); assert.ok(h.q('.home-continue')); escape(h.q('.home-continue')); assert.equal(h.ui.isOpen, true);
  h.q('.home-pill').click(); h.ui.element.click(); assert.equal(h.q('.home-popover'), null);
  h.q('.home-new').click(); h.q('.home-pick-advBgTime').click(); h.ui.element.click(); assert.equal(h.q('.home-dropdown'), null);
});
test('home CSS uses transparent interactive overlay, framing-based movement, clamping and reduced motion', () => {
  const css = readFileSync(new URL('../frontend/src/adv/title.css', import.meta.url), 'utf8');
  assert.match(css, /pointer-events:\s*auto/); assert.match(css, /1380 - var\(--sprite-cx/);
  assert.match(css, /-webkit-line-clamp:\s*3/); assert.match(css, /prefers-reduced-motion/);
  assert.doesNotMatch(css, /adv-title-menu|adv-title-cloud/);
});
test('reduced motion leaves nothing in the textbox transitioning, so entering chat can focus the input', () => {
  const css = readFileSync(new URL('../frontend/src/adv/title.css', import.meta.url), 'utf8');
  const reduced = css.match(/@media \(prefers-reduced-motion: reduce\) \{([\s\S]*?)\r?\n\}/)?.[1] || '';
  assert.match(reduced, /\.adv-stage \.adv-layer-textbox \* \{ transition: none !important; \}/);
});

test('home layout anchors the excerpt and contains mobile pills, full-width primary and scrollable recents', () => {
  const css = readFileSync(new URL('../frontend/src/adv/title.css', import.meta.url), 'utf8');
  assert.match(css, /\.home-excerpt\.is-reading\s*\{[^}]*top:\s*calc\(330 \* var\(--u\)\)/);
  assert.match(css, /\.home-notice\s*\{[^}]*bottom:\s*100%/);
  assert.match(css, /\.home-step-mark\s*\{[^}]*border-radius:\s*50%/);
  assert.match(css, /\.home-step\s*\{[^}]*border-top:/);
  assert.match(css, /\.adv-stage\.is-portrait \.home-pill-text\s*\{\s*display:\s*none/);
  assert.match(css, /\.adv-stage\.is-portrait \.home-pill-short\s*\{\s*display:\s*inline/);
  assert.match(css, /\.adv-stage\.is-portrait \.home-primary\s*\{[^}]*min-width:\s*0/);
  assert.match(css, /\.adv-stage\.is-portrait \.home-recents\s*\{[^}]*overflow-x:\s*auto/);
  assert.match(css, /\.adv-stage\.is-portrait \.home-recents\s*\{[^}]*scrollbar-width:\s*thin;[^}]*scrollbar-color:\s*#ffffff26 #0d0f18/);
  assert.match(css, /\.adv-stage\.is-portrait \.adv-title-screen::before\s*\{[^}]*flex:\s*1 0 30dvh/);
});
test('formatHomeTime covers calendar days including year boundary and invalid dates', () => {
  const fmt = d => formatHomeTime(d, NOW);
  assert.equal(fmt(new Date(2026,9,24,22,14)), '今天 22:14'); assert.equal(fmt(new Date(2026,9,23,22,14)), '昨天 22:14');
  assert.equal(fmt(new Date(2026,9,20)), '周二'); assert.equal(fmt(new Date(2026,9,18)), '周日');
  assert.equal(fmt(new Date(2026,9,17)), '10月17日'); assert.equal(fmt(new Date(2026,9,3)), '10月3日');
  assert.equal(fmt(new Date(2025,9,3)), '2025年10月3日'); assert.equal(fmt('invalid'), '');
  assert.equal(formatHomeTime(new Date(2025,11,31,23), new Date(2026,0,1)), '昨天 23:00');
});
test('hook fades continue and newWith only; preview remains open with no animation', async t => {
  const h = await fixture(t); h.ui.destroy();
  const ui = showTitle(h.actions, { documentObj: h.doc, matchMedia: () => ({ matches: false }) }); t.after(() => ui.destroy());
  await microtasks(); const animations = [];
  ui.element.animate = (frames, options) => { animations.push({ frames, options }); return { finished: Promise.resolve(), cancel() {} }; };
  ui.element.querySelector('.home-recent').click(); await microtasks(); assert.equal(animations.length, 0); assert.equal(ui.isOpen, true);
  ui.element.querySelector('.home-continue').click(); assert.equal(animations[0].options.duration, 300); assert.deepEqual(h.calls.at(-1), ['load','b']);
  ui.open(); await microtasks(); ui.element.querySelector('.home-new').click(); ui.element.querySelector('.home-start').click();
  assert.equal(animations.length, 2); assert.equal(h.calls.at(-1)[0], 'newWith');
});
test('portrait cache shares concurrent manifests and expires/refreshes/fails without retaining rejection', async () => {
  let count = 0, time = 0;
  const cache = createPortraitCache(async () => { count++; return { enabled: false }; }, () => time);
  await Promise.all([cache.get(7), cache.get(7)]); assert.equal(count, 1);
  await cache.get(7, { refresh: true }); assert.equal(count, 2); time = 61000; await cache.get(7); assert.equal(count, 3);
  const fail = createPortraitCache(async () => { count++; throw Error('fake'); });
  await assert.rejects(fail.get(8)); await assert.rejects(fail.get(8)); assert.equal(count, 5);
});

test('onboarding keeps the actual new-session greeting in the DOM for portable smoke', async t => {
  const greeting = '你好，支持语音输入与朗读回复。这里是完整开场白。';
  const session = { ...snapshot('a'), excerpt: homeExcerpt([{ role: 'assistant', content: greeting }]) };
  const h = await fixture(t, { sessions: [session], status: { modelLoaded: true, hasModel: false, model: { state: 'error' } } });
  assert.ok(h.q('.home-first-title')); assert.match(textTree(h.ui.element), /支持语音输入与朗读回复/);
  assert.equal(h.q('.home-greeting').textContent, greeting);
});

test('preview of an older active session puts it back into the clicked recent slot', async t => {
  const h = await fixture(t, { active: 'h' });
  assert.deepEqual(h.ui.element.querySelectorAll('.home-recent').map(n => n.dataset.session), ['a','b','c']);
  h.q('.home-recent').click(); await microtasks();
  assert.deepEqual(h.ui.element.querySelectorAll('.home-recent').map(n => n.dataset.session), ['h','b','c']);
});
test('ready engine and a bound character complete only their respective onboarding steps', async t => {
  const h = await fixture(t, { status: { modelLoaded: true, model: { state: 'error' }, hasModel: false, engine: { state: 'ready' } } });
  const steps = h.ui.element.querySelectorAll('.home-step');
  assert.equal(steps[0].classList.contains('is-done'), false);
  assert.equal(steps[1].classList.contains('is-done'), true); assert.equal(steps[2].classList.contains('is-done'), true);
  assert.equal(h.q('.home-continue'), null);
});
test('closing home stops active replay; late playback/status completions cannot reopen it', async t => {
  let resolveStatus;
  const h = await fixture(t);
  h.q('.home-listen').click();
  h.actions.getStatus = () => new Promise(resolve => { resolveStatus = resolve; });
  h.ui.refresh(); h.ui.close(); assert.equal(h.calls.at(-1), 'stop');
  h.finish('ended'); resolveStatus({ modelLoaded: true, model: { state: 'error' } }); await microtasks();
  assert.equal(h.ui.isOpen, false); assert.equal(h.ui.element.hidden, true); assert.equal(h.clock.timers.size, 0);
});
test('avatar crop uses manifest faceRect with fallback and no real artwork', async t => {
  const h = await fixture(t, { actions: { getAvatar: async () => ({ rect: { x: 200, y: 100, width: 400, height: 360 },
    crop: { left: 100, top: 50 }, layers: [{ file: '/fake-face.webp', rect: { x: 300, y: 150, width: 400, height: 300 } }] }) } });
  const avatar = h.q('.home-avatar'); const img = avatar.querySelector('img');
  assert.equal(img.src, '/fake-face.webp'); assert.equal(img.style.left, '0%'); assert.equal(img.style.top, '0%'); assert.equal(img.style.width, '100%');
  img.onerror(); assert.equal(avatar.textContent, '角'); assert.equal(avatar.querySelector('img'), null);
});

test('manifest-backed avatars reuse stage v2/v3 normalization and layer planning', async () => {
  const rect = { x: 200, y: 100, width: 400, height: 360 };
  const whole = { enabled: true, defaultCostume: 'uniform', costumes: [{ id: 'uniform', faceRect: rect }],
    sprites: { uniform: { smile: { file: '/fake-whole.webp', width: 800, height: 1600 } } },
    faces: { smile: { primary: 'gentle' } }, expressionSets: { gentle: [{ id: 'smile' }] } };
  const crop = { left: 100, top: 50, width: 800, height: 1600 };
  const layered = { enabled: true, version: 3, defaultOutfit: 'uniform', defaultPose: 'a',
    canvases: { A: { width: 1000, height: 2000 } }, outfits: { uniform: { name: '校服' } }, poses: { a: { name: '站姿', canvas: 'A' } },
    faces: { smile: { emotion: 'gentle' } }, expression_sets: { gentle: [{ id: 'smile' }] },
    looks: { 'a/uniform': { pose: 'a', outfit: 'uniform', crop, faceRect: rect, headAnchor: { x: 400, y: 80 },
      defaultFace: 'smile', body: ['body'], faces: { smile: { layers: ['face'] } } } },
    layers: { body: { canvas: 'A', file: '/fake-body.webp', z: 0, rect: { x: 100, y: 50, width: 800, height: 1600 } },
      face: { canvas: 'A', file: '/fake-face.webp', z: 2, rect: { x: 300, y: 150, width: 400, height: 300 } } } };
  let calls = 0;
  const cache = createPortraitCache(async id => { calls++; return id === 7 ? whole : layered; });
  await cache.get(7);
  const avatar = await cache.avatar({ settings: { voiceProfileId: 7 } });
  assert.equal(calls, 1); assert.deepEqual(avatar.rect, rect); assert.equal(avatar.layers[0].file, '/fake-whole.webp');
  const layeredAvatar = await cache.avatar({ settings: { voiceProfileId: 8 } });
  assert.deepEqual(layeredAvatar.crop, crop); assert.deepEqual(layeredAvatar.rect, rect);
  assert.deepEqual(layeredAvatar.layers.map(l => l.file), ['/fake-body.webp', '/fake-face.webp']);
});
