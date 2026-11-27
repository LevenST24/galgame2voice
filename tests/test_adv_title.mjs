import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fakeDocument, fakeClock, microtasks } from './helpers/adv_dom.mjs';
import { createTitleScreen, formatHomeTime } from '../frontend/src/adv/title.js';
import { homeExcerpt, createTitleActions } from '../frontend/src/adv/actions.js';
import { showTitle } from '../frontend/src/adv/title_hook.js';
import { createPortraitCache } from '../frontend/src/adv/portrait_cache.js';
import { tick } from './helpers/chat_stream.mjs';
import { initApp } from '../frontend/src/main.js';
import { GREETING, LEGACY_GREETINGS, state as storedState } from '../frontend/src/store.js';

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
  let finish, notify, voiceOn = true;
  const actions = { listSessions: () => sessions, getActiveId: () => active, getStatus: async () => { calls.push('status'); return status; },
    loadSession: id => calls.push(['load', id]), startNew: () => calls.push('new'), startNewWith: data => calls.push(['newWith', data]),
    previewSession: async id => { active = id; calls.push(['preview', id]); },
    listCharacters: () => [{ id: 7, name: '角色甲' }, { id: 8, name: '角色乙' }],
    getBackgrounds: () => [{ key: 'w/sea', name: '海边' }, { key: 'w/room', name: '房间' }],
    openSettings: tab => calls.push(['settings', tab]), openCharacters: () => calls.push('characters'),
    voiceEnabled: () => voiceOn, setVoiceEnabled: v => { voiceOn = Boolean(v); calls.push(['voiceEnabled', voiceOn]); },
    playExcerpt: e => { calls.push(['play', e.messageId, e.line.index]); return new Promise(resolve => { finish = resolve; }); },
    stopExcerpt: () => calls.push('stop'), subscribeSettings: fn => { notify = fn; return () => { notify = null; }; },
    ...options.actions };
  const ui = createTitleScreen(root, { actions, documentObj: doc, clock, now: options.now || (() => NOW) });
  t.after(() => ui.destroy()); ui.open(); await microtasks();
  const q = selector => ui.element.querySelector(selector);
  return { doc, clock, calls, stage, root, ui, q, sessions, actions,
    setStatus(value) { status = value; }, finish: result => finish(result), notify: (...args) => notify?.(...args) };
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
test('a session with nothing said yet shows the story opener, not an empty quote', async t => {
  const empty = { ...snapshot('a'), hasUserMessages: false, excerpt: homeExcerpt([]), lastLine: '' };
  const h = await fixture(t, { sessions: [empty] });
  assert.equal(h.q('.home-quote').textContent, '「从这一刻，开始你们的故事。」');
  assert.equal(h.q('.home-you'), null); assert.equal(h.q('.home-ja'), null); assert.equal(h.q('.home-listen'), null);
  assert.equal(h.q('.home-continue').children[0].textContent, '开始聊天');
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
  h.q('.home-listen').click(); h.finish('failed'); await microtasks();
  assert.equal(h.q('.home-listen'), null);
  assert.equal(h.q('.home-listen-note').textContent, '这句语音已过期或不可用');
  assert.equal(h.doc.activeElement, h.q('.home-continue'));
});

test('opening settings center stops excerpt playback immediately via gear, capsule popover, or modal subscriber', async t => {
  const h = await fixture(t);

  // 1. Click .home-gear while playing
  h.q('.home-listen').children[0].click();
  assert.equal(textTree(h.q('.home-listen')), '■ 停止');
  assert.equal(h.q('.home-listen').getAttribute('aria-pressed'), 'true');
  assert.deepEqual(h.calls.at(-1), ['play', 'm9', 1]);

  h.q('.home-gear').click();
  assert.equal(h.calls.includes('stop'), true, 'clicking gear button must call stopExcerpt');
  assert.equal(textTree(h.q('.home-listen')), '▶ 试听这句');
  assert.equal(h.q('.home-listen').getAttribute('aria-pressed'), 'false');
  assert.deepEqual(h.calls.at(-1), ['settings', undefined]);

  // 1b. Late completion or failure after stopping does not mark failure or hide listen button
  h.finish('failed');
  await microtasks();
  assert.equal(h.q('.home-listen').hidden, false);
  assert.equal(textTree(h.q('.home-listen')), '▶ 试听这句');

  // 2. Click capsule popover "去设置" while playing (chat pill)
  h.calls.length = 0;
  h.q('.home-listen').children[0].click();
  assert.equal(textTree(h.q('.home-listen')), '■ 停止');
  assert.equal(h.q('.home-listen').getAttribute('aria-pressed'), 'true');

  const pills = h.ui.element.querySelectorAll('.home-pill');
  pills[0].click();
  assert.ok(h.q('.home-popover'));
  h.q('.home-popover').querySelector('button').click();
  assert.equal(h.calls.includes('stop'), true, 'clicking capsule popover settings must call stopExcerpt');
  assert.equal(textTree(h.q('.home-listen')), '▶ 试听这句');
  assert.equal(h.q('.home-listen').getAttribute('aria-pressed'), 'false');
  assert.deepEqual(h.calls.at(-1), ['settings', 'models']);

  // 2b. Click voice capsule popover "去设置" while playing
  h.calls.length = 0;
  h.q('.home-listen').children[0].click();
  assert.equal(textTree(h.q('.home-listen')), '■ 停止');
  assert.equal(h.q('.home-listen').getAttribute('aria-pressed'), 'true');

  pills[1].click();
  assert.ok(h.q('.home-popover'));
  h.q('.home-popover').querySelector('button').click();
  assert.equal(h.calls.includes('stop'), true, 'clicking voice capsule popover settings must call stopExcerpt');
  assert.equal(textTree(h.q('.home-listen')), '▶ 试听这句');
  assert.equal(h.q('.home-listen').getAttribute('aria-pressed'), 'false');
  assert.deepEqual(h.calls.at(-1), ['settings', 'inference']);

  // 3. Trigger subscribeSettings(true) while playing
  h.calls.length = 0;
  h.q('.home-listen').children[0].click();
  assert.equal(textTree(h.q('.home-listen')), '■ 停止');
  assert.equal(h.q('.home-listen').getAttribute('aria-pressed'), 'true');

  h.notify(true);
  assert.equal(h.calls.includes('stop'), true, 'opening modal via subscribeSettings(true) must call stopExcerpt');
  assert.equal(textTree(h.q('.home-listen')), '▶ 试听这句');
  assert.equal(h.q('.home-listen').getAttribute('aria-pressed'), 'false');

  // 4. Clicking gear when not playing does not issue stopExcerpt
  h.calls.length = 0;
  h.q('.home-gear').click();
  assert.equal(h.calls.includes('stop'), false);
  assert.deepEqual(h.calls.at(-1), ['settings', undefined]);

  // 5. Triggering subscribeSettings(false) triggers refresh, not stop
  h.calls.length = 0;
  h.notify(false);
  await microtasks();
  assert.equal(h.calls.includes('stop'), false);
  assert.equal(h.calls.includes('status'), true);

  // 6. First-use step buttons call stopListen before openSettings / openCharacters
  const firstH = await fixture(t, {
    status: { modelLoaded: true, hasModel: false, model: { state: 'error' }, engine: { state: 'unavailable' } },
    sessions: [{ ...snapshot('a'), settings: { voiceProfileId: null } }],
  });
  const stepButtons = firstH.ui.element.querySelectorAll('.home-step-button');
  assert.equal(stepButtons.length, 3);
  stepButtons[0].click();
  assert.deepEqual(firstH.calls.at(-1), ['settings', 'models']);
  stepButtons[1].click();
  assert.deepEqual(firstH.calls.at(-1), ['settings', 'inference']);
  stepButtons[2].click();
  assert.deepEqual(firstH.calls.at(-1), 'characters');
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
  opts = open('advBgTime'); assert.deepEqual(opts.map(textTree), ['自动（跟随时间）','白天','傍晚','夜晚']); assert.equal(opts[3].getAttribute('aria-pressed'), 'true'); opts[1].click();
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

test('first use checks model/voice/character, text-only persists via setVoiceEnabled, updates after settings close', async t => {
  const status = { modelLoaded: true, hasModel: false, model: { state: 'error', label: '未配置对话模型' }, engine: { state: 'unavailable' } };
  const session = snapshot('a'); session.settings.voiceProfileId = null;
  const h = await fixture(t, { status, sessions: [session] });
  assert.ok(h.q('.home-first-title')); assert.equal(h.ui.element.querySelectorAll('.home-step.is-done').length, 0);
  assert.equal(h.q('.home-continue'), null); assert.equal(h.doc.activeElement, h.q('.home-step-pending'));
  assert.equal(h.ui.element.querySelectorAll('button').some(button => textTree(button) === '开始聊天'), false);
  h.q('.home-step-pending').click(); assert.deepEqual(h.calls.at(-1), ['settings','models']);
  h.q('.home-text-only').click(); assert.equal(h.ui.element.querySelectorAll('.home-step.is-done').length, 1);
  assert.deepEqual(h.calls.at(-1), ['voiceEnabled', false]);
  session.settings.voiceProfileId = 7; h.notify(); await microtasks(); assert.equal(h.ui.element.querySelectorAll('.home-step.is-done').length, 2);
  h.ui.close(); h.ui.open(); await microtasks(); assert.equal(h.ui.element.querySelectorAll('.home-step.is-done').length, 2);
  h.setStatus({ ...status, hasModel: true, model: { state: 'ok' } }); h.notify(); await microtasks();
  assert.equal(h.q('.home-first-title'), null); assert.ok(h.q('.home-continue')); assert.match(textTree(h.q('.home-notice')), /先用文字/);
  assert.equal(textTree(h.q('.home-continue')), '接着聊 →');
});
test('the text-only choice on the first-use page can be undone, also once the engine is ready', async t => {
  const status = { modelLoaded: true, hasModel: false, model: { state: 'error', label: '未配置对话模型' }, engine: { state: 'unavailable' } };
  const session = snapshot('a'); session.settings.voiceProfileId = null;
  const h = await fixture(t, { status, sessions: [session] });
  h.q('.home-text-only').click(); assert.deepEqual(h.calls.at(-1), ['voiceEnabled', false]);
  h.setStatus({ ...status, engine: { state: 'ready' } }); h.notify(); await microtasks();
  assert.equal(textTree(h.q('.home-text-only')), '✓ 暂时只用文字');
  h.q('.home-text-only').click(); assert.deepEqual(h.calls.at(-1), ['voiceEnabled', true]);
  assert.equal(h.q('.home-text-only'), null);
});
test('first use sends a user with no characters to import one, and offers a choice once one is installed', async t => {
  const status = { modelLoaded: true, hasModel: false, model: { state: 'error', label: '未配置对话模型' }, engine: { state: 'unavailable' } };
  const session = snapshot('a'); session.settings.voiceProfileId = null;
  const characters = [];
  const h = await fixture(t, { status, sessions: [session], actions: { listCharacters: () => characters } });
  const row = () => textTree(h.ui.element.querySelectorAll('.home-step')[2]);
  const button = () => textTree(h.ui.element.querySelectorAll('.home-step-button')[2]);
  assert.match(row(), /还没有角色/); assert.equal(button(), '去导入');
  characters.push({ id: 7, name: '角色甲' }); h.notify(); await microtasks();
  assert.match(row(), /从已安装的角色里挑一位/); assert.equal(button(), '选择');
});
test('first use does not tick the character step for a character id kept from an earlier install', async t => {
  const status = { modelLoaded: true, hasModel: false, model: { state: 'error', label: '未配置对话模型' }, engine: { state: 'unavailable' } };
  const session = snapshot('a'); session.settings.voiceProfileId = 7;
  const characters = [];
  const h = await fixture(t, { status, sessions: [session], actions: { listCharacters: () => characters } });
  const step = () => h.ui.element.querySelectorAll('.home-step')[2];
  assert.equal(step().classList.contains('is-done'), false); assert.match(textTree(step()), /还没有角色/);
  characters.push({ id: 9, name: '角色丙' }); h.notify(); await microtasks();
  assert.equal(step().classList.contains('is-done'), false);
  characters.push({ id: 7, name: '角色甲' }); h.notify(); await microtasks();
  assert.equal(step().classList.contains('is-done'), true);
});

test('status colors/text, explanations, settings tabs, polling and disposal with fake clock', async t => {
  const h = await fixture(t);
  assert.deepEqual(h.ui.element.querySelectorAll('.home-dot').map(n => n.className), ['home-dot ok','home-dot ok']);
  const pills = h.ui.element.querySelectorAll('.home-pill');
  pills[0].children[1].click(); assert.match(textTree(h.q('.home-popover')), /测试模型/);
  h.q('.home-popover').querySelector('button').click(); assert.deepEqual(h.calls.at(-1), ['settings','models']);
  pills[1].click(); assert.match(textTree(h.q('.home-popover')), /语音引擎已连接.*127.0.0.1:9880/);
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

test('a hidden tab stops reading status on the home screen and refreshes once it is shown', async t => {
  const h = await fixture(t);
  const reads = () => h.calls.filter(c => c === 'status').length;
  assert.equal(reads(), 1);
  h.doc.hidden = true;
  h.clock.tick(30000); await microtasks();
  assert.equal(reads(), 1, 'a hidden tab must not keep polling');
  h.doc.hidden = false;
  h.doc.dispatchEvent({ type: 'visibilitychange' }); await microtasks();
  assert.equal(reads(), 2, 'showing the tab refreshes at once');
  h.clock.tick(5000); await microtasks();
  assert.equal(reads(), 3);
});

const SLOW_ENGINE = '引擎加载时间较长，尚未确认就绪。无需反复重启；请点击「刷新状态」稍后检查。文字聊天可继续使用。';
const CHECKING_ENGINE = '引擎进程已变更，正在重新核对服务状态。请稍候，无需再次重启。';
const readyModel = { model: { state: 'ok', label: '测试模型' }, modelLoaded: true, hasModel: true };
function voicePill(h) { return h.ui.element.querySelectorAll('.home-pill')[1]; }

test('a slow managed load stays a loading label on the home pill', async t => {
  const h = await fixture(t, { status: { ...readyModel, engine: { state: 'loading_slow', message: SLOW_ENGINE, base_url: 'http://127.0.0.1:9880' } } });
  const voice = voicePill(h);
  voice.click();
  const dot = voice.querySelector('.home-dot').className;
  const label = textTree(voice);
  const notice = h.q('.home-notice')?.textContent || '';
  assert.ok(
    dot === 'home-dot warn' && /语音 加载较慢/.test(label) && !/未连接/.test(label)
      && /加载较慢/.test(notice) && !/未连接/.test(notice)
      && /无需反复重启/.test(textTree(h.q('.home-popover'))),
    `home treats loading_slow as disconnected\npill=${dot} ${label}\nnotice=${notice}\npopover=${textTree(h.q('.home-popover') || h.ui.element)}`,
  );
});

test('leaving home open flips a slow load off the starting pill into a warning', async t => {
  const h = await fixture(t, { status: { ...readyModel, engine: { state: 'starting', message: '引擎正在加载模型，请稍候；文字聊天可继续使用。' } } });
  const voice = voicePill(h);
  assert.match(textTree(voice), /语音 启动中/);
  const startingNotice = h.q('.home-notice')?.textContent || '';
  h.setStatus({ ...readyModel, engine: { state: 'loading_slow', message: SLOW_ENGINE, base_url: 'http://127.0.0.1:9880' } });
  h.clock.tick(5000);
  await microtasks();
  const label = textTree(voice);
  const dot = voice.querySelector('.home-dot').className;
  const notice = h.q('.home-notice')?.textContent || '';
  assert.ok(
    dot === 'home-dot warn' && /语音 加载较慢/.test(label) && !/未连接/.test(label)
      && /加载较慢/.test(notice) && !/未连接/.test(notice)
      && /启动/.test(startingNotice) && !/未连接/.test(startingNotice),
    `poll replaced 启动中 with a failure\nstartingNotice=${startingNotice}\npill=${dot} ${label}\nnotice=${notice}`,
  );
});

test('home shows an in-flight launch change as unconfirmed, not disconnected', async t => {
  const h = await fixture(t, { status: { ...readyModel, engine: { state: 'checking', message: CHECKING_ENGINE, base_url: 'http://127.0.0.1:9880' } } });
  const voice = voicePill(h);
  voice.click();
  const dot = voice.querySelector('.home-dot').className;
  const label = textTree(voice);
  const notice = h.q('.home-notice')?.textContent || '';
  assert.ok(
    dot === 'home-dot warn' && /语音 待确认/.test(label) && !/未连接/.test(label)
      && /核对/.test(notice) && !/未连接/.test(notice)
      && /无需再次重启/.test(textTree(h.q('.home-popover'))),
    `home treats checking as disconnected\npill=${dot} ${label}\nnotice=${notice}\npopover=${textTree(h.q('.home-popover') || h.ui.element)}`,
  );
});

test('opening home shows voice pill as checking and unconfirmed before first engine status response arrives', async t => {
  let resolveStatus;
  const statusPromise = new Promise(resolve => { resolveStatus = resolve; });
  const h = await fixture(t, {
    actions: {
      getStatus: () => statusPromise,
    },
  });

  const voice = voicePill(h);
  const dot = voice.querySelector('.home-dot');
  const label = textTree(voice);

  assert.equal(dot.className, 'home-dot warn');
  assert.match(label, /语音 待确认/);
  assert.doesNotMatch(label, /未连接/);
  assert.equal(h.q('.home-notice').textContent, '正在核对语音引擎，可以先用文字聊天');

  voice.click();
  const popover = textTree(h.q('.home-popover'));
  assert.match(popover, /正在核对语音引擎，可以先用文字聊天/);
  assert.doesNotMatch(popover, /语音引擎未连接/);

  resolveStatus({
    ...readyModel,
    engine: { state: 'unavailable' },
  });
  await microtasks();

  assert.equal(dot.className, 'home-dot bad');
  assert.match(textTree(voice), /语音 未连接/);
  assert.equal(h.q('.home-notice').textContent, '语音引擎未连接，可以先用文字聊天');
  assert.match(textTree(h.q('.home-popover')), /语音引擎未连接，可以先用文字聊天/);
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
test('all sessions grid preserves DOM slot buttons and keyboard focus across polling refresh when sessions are unchanged', async t => {
  const h = await fixture(t);
  h.q('.home-all').click();
  const firstSlotBefore = h.q('.adv-title-slot');
  assert.ok(firstSlotBefore, 'First slot button should exist');
  firstSlotBefore.focus();
  assert.equal(h.doc.activeElement, firstSlotBefore, 'First slot should have focus before poll');

  // Advance 5000ms to trigger refresh() poll while mode === 'all'
  h.clock.tick(5000);
  await microtasks();

  const firstSlotAfter = h.q('.adv-title-slot');
  assert.equal(firstSlotBefore.isConnected, true, 'Focused slot button should not be detached from DOM during polling');
  assert.equal(firstSlotAfter, firstSlotBefore, 'Slot button reference should be preserved when sessions have not changed');
  assert.equal(h.doc.activeElement, firstSlotBefore, 'Focus should remain on the same slot button after polling');
});

test('all sessions grid restores focus to corresponding slot when sessions change and grid is rebuilt', async t => {
  const h = await fixture(t);
  h.q('.home-all').click();
  const firstSlotBefore = h.q('.adv-title-slot');
  firstSlotBefore.focus();
  assert.equal(h.doc.activeElement, firstSlotBefore);

  // Mutate session data so gridKey changes
  h.sessions[0].title = '更新后的标题';
  h.clock.tick(5000);
  await microtasks();

  const firstSlotAfter = h.q('.adv-title-slot');
  assert.equal(firstSlotBefore.isConnected, false, 'Old slot should be detached on rebuild');
  assert.notEqual(firstSlotAfter, firstSlotBefore, 'Grid slot should be reconstructed');
  assert.equal(h.doc.activeElement, firstSlotAfter, 'Focus should be restored to slot with same session id');
  assert.match(textTree(firstSlotAfter), /更新后的标题/);
});

test('all sessions grid updates relative time in-place during poll without replacing elements', async t => {
  let currentTime = new Date(NOW.getTime());
  const h = await fixture(t, { now: () => currentTime });
  h.q('.home-all').click();
  const slot = h.q('.adv-title-slot');
  const dateEl = slot.querySelector('.adv-title-slot-date');
  assert.match(dateEl.textContent, /今天/);

  // Advance current time by 1 day so formatHomeTime produces '昨天'
  currentTime = new Date(NOW.getTime() + 86400000);
  h.clock.tick(5000);
  await microtasks();

  assert.equal(slot.isConnected, true, 'Slot should remain connected');
  assert.match(dateEl.textContent, /昨天/, 'Date text should update in place');
});

test('all sessions grid preserves focus on non-slot controls and on later pages across polling refresh', async t => {
  const h = await fixture(t);
  h.q('.home-all').click();

  // Focus pager next button
  const nextBtn = h.q('.adv-title-page-next');
  nextBtn.focus();
  assert.equal(h.doc.activeElement, nextBtn);
  h.clock.tick(5000);
  await microtasks();
  assert.equal(h.doc.activeElement, nextBtn, 'Pager next button retains focus during poll');

  // Navigate to page 2 and focus a slot
  nextBtn.click();
  const page2Slots = h.ui.element.querySelectorAll('.adv-title-slot');
  assert.equal(page2Slots.length, 2);
  const slotOnPage2 = page2Slots[0];
  slotOnPage2.focus();
  assert.equal(h.doc.activeElement, slotOnPage2);

  h.clock.tick(5000);
  await microtasks();
  assert.equal(slotOnPage2.isConnected, true);
  assert.equal(h.doc.activeElement, slotOnPage2, 'Slot on page 2 retains focus during poll');
});

test('all sessions grid safely handles rebuild when focused session is removed', async t => {
  const h = await fixture(t);
  h.q('.home-all').click();
  const firstSlot = h.q('.adv-title-slot');
  firstSlot.focus();
  assert.equal(h.doc.activeElement, firstSlot);

  // Remove the focused session from sessions list
  const removedId = h.sessions[0].id;
  h.sessions.splice(0, 1);
  h.clock.tick(5000);
  await microtasks();

  const slotsAfter = h.ui.element.querySelectorAll('.adv-title-slot');
  assert.ok(![...slotsAfter].some(s => (s.getAttribute('data-session') || s.dataset?.session) === removedId));
  assert.equal(firstSlot.isConnected, false, 'Old slot should be disconnected');
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
  assert.match(css, /-webkit-line-clamp:\s*2/); assert.match(css, /prefers-reduced-motion/);
  assert.doesNotMatch(css, /adv-title-menu|adv-title-cloud/);
});
test('reduced motion leaves nothing in the textbox transitioning, so entering chat can focus the input', () => {
  const css = readFileSync(new URL('../frontend/src/adv/title.css', import.meta.url), 'utf8');
  const reduced = css.match(/@media \(prefers-reduced-motion: reduce\) \{([\s\S]*?)\r?\n\}/)?.[1] || '';
  assert.match(reduced, /\.adv-stage \.adv-layer-textbox \* \{ transition: none !important; \}/);
});

test('home layout anchors the excerpt', () => {
  const css = readFileSync(new URL('../frontend/src/adv/title.css', import.meta.url), 'utf8');
  assert.match(css, /\.home-excerpt\.is-reading\s*\{[^}]*top:\s*calc\(330 \* var\(--u\)\)/);
  assert.match(css, /\.home-notice\s*\{[^}]*bottom:\s*100%/);
  assert.match(css, /\.home-step-mark\s*\{[^}]*border-radius:\s*50%/);
  assert.match(css, /\.home-step\s*\{[^}]*border-top:/);
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

test('unavailable model does not prevent opening an existing conversation', async t => {
  const session = snapshot('a');
  const h = await fixture(t, { sessions: [session], status: {
    modelLoaded: true, hasModel: false, model: { state: 'error', label: '未配置对话模型' },
    engine: { state: 'unavailable' },
  } });
  assert.ok(h.q('.home-first-title'), 'model configuration guidance remains available');
  assert.ok(h.q('.home-existing'), 'existing conversation remains accessible');
  h.q('.home-existing').click();
  assert.deepEqual(h.calls.at(-1), ['load', 'a']);
  assert.equal(h.ui.isOpen, false);
});

test('model read failure is uncertain and does not replace existing chat with onboarding', async t => {
  const h = await fixture(t, { sessions: [snapshot('a')], status: {
    modelLoaded: true, hasModel: false,
    model: { state: 'error', readFailed: true, label: '模型配置暂时无法读取' },
    engine: { state: 'unavailable' },
  } });
  assert.ok(!h.q('.home-first-title'), 'a read error must not be treated as a first-time setup');
  assert.match(textTree(h.q('.home-pill')), /状态未确认/);
  assert.equal(h.q('.home-dot').className, 'home-dot warn');
  assert.ok(h.q('.home-continue'));
});

test('first use without a user conversation does not offer an existing chat', async t => {
  const session = { ...snapshot('a'), hasUserMessages: false };
  const h = await fixture(t, { sessions: [session], status: {
    modelLoaded: true, hasModel: false, model: { state: 'error' }, engine: { state: 'unavailable' },
  } });
  assert.equal(h.q('.home-existing'), null);
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

test('reopening home starts a fresh status request while the old one is pending', async t => {
  const h = await fixture(t);
  const pending = [];
  h.actions.getStatus = () => new Promise(resolve => pending.push(resolve));
  h.ui.refresh();
  h.ui.close();
  h.ui.open();
  assert.equal(pending.length, 2, 'a previous opening must not block the new status check');
  pending[1]({ modelLoaded: true, hasModel: true, model: { state: 'ok' }, engine: { state: 'ready' } });
  await microtasks();
  assert.match(textTree(h.q('.home-pill')), /对话 可用/);
  pending[0]({ modelLoaded: true, hasModel: false, model: { state: 'error' }, engine: { state: 'failed' } });
  await microtasks();
  assert.match(textTree(h.q('.home-pill')), /对话 可用/, 'old results must not replace current status');
});

test('an old status request cannot clear the new opening\'s in-flight check', async t => {
  const h = await fixture(t);
  const pending = [];
  h.actions.getStatus = () => new Promise(resolve => pending.push(resolve));
  h.ui.refresh();
  h.ui.close();
  h.ui.open();
  assert.equal(pending.length, 2);
  pending[0]({ modelLoaded: true, hasModel: false, model: { state: 'error' } });
  await microtasks();
  h.ui.refresh();
  assert.equal(pending.length, 2, 'the current request remains the only in-flight check');
  pending[1]({ modelLoaded: true, hasModel: true, model: { state: 'ok' } });
  await microtasks();
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

test('title screen presents latest Japanese-only reply with audio preview and without duplicate ja line', async t => {
  const audio = { status: 'ready', chunks: [{ audio_url: '/tadaima.wav' }] };
  const sessionMessages = [
    { role: 'user', content: '第一轮中文提问' },
    { id: 'm1', role: 'assistant', ts: NOW.getTime() - 10000, lines: [{ index: 0, zh: '第一轮中文', ja: '一' }] },
    { role: 'user', content: '第二轮提问：回家了吗？' },
    { id: 'm2', role: 'assistant', ts: NOW.getTime(), lines: [{ index: 0, zh: '', ja: 'ただいま。', audio }] },
  ];
  const s = {
    ...snapshot('ja_test'),
    messages: sessionMessages,
    excerpt: homeExcerpt(sessionMessages),
    hasUserMessages: true,
  };
  const h = await fixture(t, { sessions: [s], active: 'ja_test' });
  assert.equal(h.q('.home-you').textContent, '你：第二轮提问：回家了吗？');
  assert.equal(h.q('.home-quote').textContent, '「ただいま。」');
  assert.equal(h.q('.home-ja'), null, 'Japanese fallback quote must not be duplicated in p.home-ja');
  assert.ok(h.q('.home-listen'), 'play excerpt button must be visible for Japanese-only reply');
  assert.equal(h.q('.home-continue').children[0].textContent, '接着聊 →');

  h.q('.home-listen').click();
  assert.deepEqual(h.calls.find(c => Array.isArray(c) && c[0] === 'play'), ['play', 'm2', 0]);
});

test('onboarding preserves Japanese-only greeting in hidden home-greeting DOM', async t => {
  const greetingExcerpt = { zh: '', ja: 'おかえりなさいませ。' };
  const session = { ...snapshot('first_ja'), excerpt: greetingExcerpt };
  const h = await fixture(t, { sessions: [session], status: { modelLoaded: true, hasModel: false, model: { state: 'error' } } });
  assert.ok(h.q('.home-first-title'));
  assert.ok(h.q('.home-greeting'));
  assert.equal(h.q('.home-greeting').textContent, 'おかえりなさいませ。');
});

test('title holds 开始聊天 and checklist until /api/providers settles, then renders each state', async t => {
  const doc = fakeDocument();
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  globalThis.document = doc;
  const root = doc.body.appendChild(doc.createElement('div'));
  root.id = 'advRoot';
  const storage = new Map();
  globalThis.localStorage = { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v) };
  const providerReplies = [];
  const fetchMock = async url => {
    const path = String(url);
    if (path === '/api/providers') return new Promise(resolve => providerReplies.push(resolve));
    if (path.startsWith('/api/chat/sessions')) return Response.json({ sessions: [], has_more: false });
    if (path === '/api/system/engine-status') return Response.json({ state: 'unavailable' });
    if (path === '/api/voice/profiles') return Response.json({ profiles: [], active_profile_id: null });
    return Response.json({ backgrounds: [], settings: {} });
  };
  const loc = { search: '', hash: '', origin: 'http://home.test' };
  globalThis.location = loc;
  const sessionStorage = { getItem: () => '', setItem() {} };
  globalThis.window = { innerWidth: 1280, location: loc, sessionStorage, fetch: fetchMock };
  globalThis.fetch = fetchMock;
  const app = initApp({ dom: { advRoot: root }, renderSessionList() {} });
  let title;
  t.after(() => {
    while (providerReplies.length) providerReplies.shift()(Response.json({ providers: [] }));
    title?.destroy();
    app.conversation.stop();
    app.adv?.unmount();
    for (const key of ['document', 'window', 'localStorage', 'fetch', 'location']) delete globalThis[key];
  });
  title = await app.ready;
  await tick();
  const label = sel => title.element.querySelector(sel) ? textTree(title.element.querySelector(sel)) : null;
  assert.equal(providerReplies.length, 1);
  assert.equal(label('.home-continue'), null);
  assert.equal(label('.home-first-title'), null);
  assert.doesNotMatch(textTree(title.element), /开始聊天|先做三件小事/);
  assert.match(textTree(title.element), /正在检查设置…/);
  providerReplies.shift()(new Response('boom', { status: 500 }));
  await tick();
  assert.match(label('.home-pill'), /对话 状态未确认/);
  assert.equal(label('.home-continue'), '开始聊天');
  title.element.querySelector('.home-gear').click();
  providerReplies.shift()(Response.json({ providers: [], presets: [] }));
  await tick();
  assert.equal(label('.home-first-title'), '先做三件小事，就能开始聊天');
  assert.equal(label('.home-continue'), null);
  title.element.querySelector('.home-step-button').click();
  const active = { id: 'deepseek', name: 'DeepSeek', chat_model: 'deepseek-chat', api_key: 'sk-****', is_active: true };
  providerReplies.shift()(Response.json({ providers: [active], presets: [] }));
  await tick();
  // Step 1 is done, so 开始聊天 is offered while the checklist stays visible for unfinished steps 2 & 3.
  assert.equal(label('.home-first-title'), '先做三件小事，就能开始聊天');
  assert.equal(title.element.querySelectorAll('.home-step.is-done').length, 1);
  assert.equal(label('.home-continue'), '开始聊天');
});

test('first-use checklist hides text-only choice when voice engine is ready', async t => {
  const unconfigured = { modelLoaded: true, hasModel: false, model: { state: 'error' } };
  const h = await fixture(t, { status: { ...unconfigured, engine: { state: 'ready' } } });
  assert.ok(h.q('.home-first-title'));
  assert.ok(!h.q('.home-text-only'), 'unexpected .home-text-only when engine is ready');
  h.setStatus({ ...unconfigured, engine: { state: 'unavailable' } });
  await h.ui.refresh();
  assert.ok(h.q('.home-text-only'));
  h.q('.home-text-only').click();
  assert.equal(textTree(h.q('.home-text-only')), '✓ 暂时只用文字');
  h.setStatus({ ...unconfigured, engine: { state: 'ready' } });
  await h.ui.refresh();
  // The choice persists as voiceMode=false, so it stays visible (and can be undone) once the engine is ready.
  assert.equal(textTree(h.q('.home-text-only')), '✓ 暂时只用文字');
});


test('home voice settings jumps to dashboard when engine directory is already configured', async t => {
  const h = await fixture(t, {
    status: {
      modelLoaded: true,
      hasModel: false,
      model: { state: 'error', label: '未配置对话模型' },
      engine: { state: 'unavailable', directory_configured: true },
    },
    sessions: [{ ...snapshot('a'), settings: { voiceProfileId: null } }],
  });
  const stepButtons = h.ui.element.querySelectorAll('.home-step-button');
  stepButtons[1].click();
  assert.deepEqual(h.calls.at(-1), ['settings', 'dashboard']);

  const pills = h.ui.element.querySelectorAll('.home-pill');
  pills[1].click();
  h.q('.home-popover').querySelector('button').click();
  assert.deepEqual(h.calls.at(-1), ['settings', 'dashboard']);
});


test('onboarding checklist stays visible after step 1 until all 3 steps are done or any session has messages', async t => {
  const status = { modelLoaded: true, hasModel: false, model: { state: 'error', label: '未配置对话模型' }, engine: { state: 'unavailable' } };
  const emptySession = { ...snapshot('empty'), hasUserMessages: false, settings: { voiceProfileId: null, advBackground: '', advBgTime: 'auto' } };
  const h = await fixture(t, { status, sessions: [emptySession], active: 'empty' });
  assert.ok(h.q('.home-first-title'));
  assert.equal(h.ui.element.querySelectorAll('.home-step.is-done').length, 0);
  assert.equal(h.q('.home-continue'), null);

  // Finishing step 1 (chat model) keeps the 3-step checklist visible because steps 2 & 3 are incomplete and no session has sent messages.
  h.setStatus({ ...status, hasModel: true, model: { state: 'ok', label: 'DeepSeek · deepseek-chat' } });
  h.notify(); await microtasks();
  assert.ok(h.q('.home-first-title'), 'checklist must stay visible after step 1 when steps 2 & 3 are pending');
  assert.equal(h.ui.element.querySelectorAll('.home-step.is-done').length, 1);
  assert.equal(textTree(h.q('.home-continue')), '开始聊天');

  // Finishing step 2 (via text-only) checks step 2; checklist remains because step 3 is still pending.
  h.q('.home-text-only').click();
  assert.ok(h.q('.home-first-title'), 'checklist must stay visible while step 3 is pending');
  assert.equal(h.ui.element.querySelectorAll('.home-step.is-done').length, 2);
  assert.equal(textTree(h.q('.home-continue')), '开始聊天');

  // Finishing step 3 (choosing an installed character) completes all 3 steps and reveals the continue pane.
  emptySession.settings.voiceProfileId = 7;
  h.notify(); await microtasks();
  assert.equal(h.q('.home-first-title'), null);
  assert.equal(textTree(h.q('.home-continue')), '开始聊天');

  // Returning user who creates a brand-new empty session must NOT see the checklist again because another session already has messages.
  const olderSent = { ...snapshot('a'), hasUserMessages: true };
  const brandNew = { ...snapshot('fresh'), hasUserMessages: false, settings: { voiceProfileId: null, advBackground: '', advBgTime: 'auto' } };
  const hReturning = await fixture(t, {
    status: { modelLoaded: true, hasModel: true, model: { state: 'ok' }, engine: { state: 'unavailable' } },
    sessions: [brandNew, olderSent],
    active: 'fresh',
  });
  assert.equal(hReturning.q('.home-first-title'), null, 'returning user with a new empty session must not see onboarding checklist');
  assert.equal(textTree(hReturning.q('.home-continue')), '开始聊天');
});



test('syncSessionsFromBackend marks server messages on unloaded/evicted sessions while ignoring greeting-only sessions', async t => {
  const doc = fakeDocument();
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  globalThis.document = doc;
  const root = doc.body.appendChild(doc.createElement('div'));
  root.id = 'advRoot';
  const storage = new Map();
  // Seed an existing local session whose messages were evicted (messages: []) by storage quota.
  storage.set('gal2voice.chat.v1', JSON.stringify({
    sessions: [
      { id: 's_fresh', title: '新对话', createdAt: 3000, updatedAt: 3000, messages: [], settings: { voiceProfileId: null } },
      { id: 's_evicted', title: '已淘汰老会话', createdAt: 2000, updatedAt: 2000, messages: [], settings: { voiceProfileId: null } },
    ],
    activeId: 's_fresh',
    global: { voiceMode: true, ttsPreset: '' },
  }));
  globalThis.localStorage = { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) };
  const activeProvider = { id: 'deepseek', name: 'DeepSeek', chat_model: 'deepseek-chat', api_key: 'sk-****', is_active: true };
  const fetchMock = async url => {
    const path = String(url);
    if (path === '/api/providers') return Response.json({ providers: [activeProvider], presets: [] });
    if (path.startsWith('/api/chat/sessions/')) return Response.json({ messages: [], has_more: false });
    if (path.startsWith('/api/chat/sessions')) {
      return Response.json({
        sessions: [
          { id: 's_fresh', title: '新对话', created_at: '2026-06-01 00:00:05', updated_at: '2026-06-01 00:00:05', message_count: 0, last_message: '', settings: {} },
          { id: 's_evicted', title: '已淘汰老会话', created_at: '2026-06-01 00:00:04', updated_at: '2026-06-01 00:00:04', message_count: 2, last_message: '之前聊过的内容', settings: {} },
          { id: 's_remote_only', title: '换端口同步来的老会话', created_at: '2026-06-01 00:00:03', updated_at: '2026-06-01 00:00:03', message_count: 2, last_message: '服务端历史消息', settings: {} },
          { id: 's_greet_only', title: '仅开场白', created_at: '2026-06-01 00:00:02', updated_at: '2026-06-01 00:00:02', message_count: 1, last_message: GREETING, settings: {} },
          { id: 's_legacy_greet', title: '旧版开场白', created_at: '2026-06-01 00:00:01', updated_at: '2026-06-01 00:00:01', message_count: 1, last_message: LEGACY_GREETINGS[0], settings: {} },
        ],
        has_more: false,
      });
    }
    if (path === '/api/system/engine-status') return Response.json({ state: 'unavailable' });
    if (path === '/api/voice/profiles') return Response.json({ profiles: [], active_profile_id: null });
    return Response.json({ backgrounds: [], settings: {} });
  };
  const loc = { search: '', hash: '', origin: 'http://home.test' };
  globalThis.location = loc;
  globalThis.window = { innerWidth: 1280, location: loc, sessionStorage: { getItem: () => '', setItem() {} }, fetch: fetchMock };
  globalThis.fetch = fetchMock;
  const app = initApp({ dom: { advRoot: root }, renderSessionList() {} });
  let title;
  t.after(() => {
    title?.destroy();
    app.conversation.stop();
    app.adv?.unmount();
    for (const key of ['document', 'window', 'localStorage', 'fetch', 'location']) delete globalThis[key];
  });
  title = await app.ready;
  await tick();
  const byId = Object.fromEntries(app.actions.listSessions().map(s => [s.id, s]));
  assert.equal(byId.s_fresh.hasUserMessages, false);
  assert.equal(byId.s_evicted.hasUserMessages, true, 'evicted local session with server message_count > 0 must count as having messages');
  assert.equal(byId.s_remote_only.hasUserMessages, true, 'remote-synced unloaded session with server message_count > 0 must count as having messages');
  assert.equal(byId.s_greet_only.hasUserMessages, false, 'session with only GREETING must not count as having user messages');
  assert.equal(byId.s_legacy_greet.hasUserMessages, false, 'session with only LEGACY_GREETINGS must not count as having user messages');
  assert.equal(Boolean(title.element.querySelector('.home-first-title')), false, 'returning user with unloaded server history must not see onboarding checklist');
});

test('opening home waits on checking instead of flashing checklist before engine status returns', async t => {
  let resolveStatus;
  const emptySession = { ...snapshot('empty'), hasUserMessages: false, settings: { voiceProfileId: 7, advBackground: '', advBgTime: 'auto' } };
  const h = await fixture(t, {
    sessions: [emptySession],
    active: 'empty',
    actions: {
      listCharacters: () => [{ id: 7, name: '星野' }],
      getStatus: (onModel) => {
        onModel?.({ modelLoaded: true, hasModel: true, model: { state: 'ok', label: 'DeepSeek' } });
        return new Promise(resolve => { resolveStatus = resolve; });
      },
    },
  });
  t.after(() => {
    resolveStatus?.({ modelLoaded: true, hasModel: true, model: { state: 'ok' }, engine: { state: 'ready' } });
  });
  // Model is already loaded and OK, but engine status has not returned yet: must show checking, NOT flash checklist.
  assert.equal(Boolean(h.q('.home-first-title')), false, 'must not flash onboarding checklist while engine status is still pending');
  assert.match(textTree(h.ui.element), /正在检查设置…/);
  assert.equal(h.q('.home-continue'), null);

  resolveStatus({ modelLoaded: true, hasModel: true, model: { state: 'ok', label: 'DeepSeek' }, engine: { state: 'ready' } });
  await microtasks();
  assert.equal(Boolean(h.q('.home-first-title')), false, 'must stay in continue view when engine returns ready and character is chosen');
  assert.equal(textTree(h.q('.home-continue')), '开始聊天');

  // When user chose text-only (voiceEnabled === false), do NOT wait on engine status before showing checklist for unfinished step 3.
  let resolveTextOnlyStatus;
  const hTextOnly = await fixture(t, {
    sessions: [{ ...emptySession, settings: { voiceProfileId: null, advBackground: '', advBgTime: 'auto' } }],
    active: 'empty',
    actions: {
      voiceEnabled: () => false,
      listCharacters: () => [{ id: 7, name: '星野' }],
      getStatus: (onModel) => {
        onModel?.({ modelLoaded: true, hasModel: true, model: { state: 'ok', label: 'DeepSeek' } });
        return new Promise(resolve => { resolveTextOnlyStatus = resolve; });
      },
    },
  });
  t.after(() => {
    resolveTextOnlyStatus?.({ modelLoaded: true, hasModel: true, model: { state: 'ok' }, engine: { state: 'unavailable' } });
  });
  assert.ok(hTextOnly.q('.home-first-title'), 'text-only user should not block on pending engine status');
});

test('late-arriving character list refreshes open home screen immediately', async t => {
  const doc = fakeDocument();
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  globalThis.document = doc;
  const root = doc.body.appendChild(doc.createElement('div'));
  root.id = 'advRoot';
  const storage = new Map();
  globalThis.localStorage = { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) };
  const activeProvider = { id: 'deepseek', name: 'DeepSeek', chat_model: 'deepseek-chat', api_key: 'sk-****', is_active: true };
  const profileReplies = [];
  const fetchMock = async url => {
    const path = String(url);
    if (path === '/api/providers') return Response.json({ providers: [activeProvider], presets: [] });
    if (path.startsWith('/api/chat/sessions/')) return Response.json({ messages: [], has_more: false });
    if (path.startsWith('/api/chat/sessions')) {
      return Response.json({
        sessions: [
          { id: 's_bound', title: '新对话', voice_profile_id: 7, created_at: '2026-06-01 00:00:01', updated_at: '2026-06-01 00:00:01', message_count: 0, last_message: '', settings: {} },
        ],
        has_more: false,
      });
    }
    if (path === '/api/system/engine-status') return Response.json({ state: 'ready', base_url: 'http://127.0.0.1:9880' });
    if (path === '/api/voice/profiles') return new Promise(resolve => profileReplies.push(resolve));
    if (path === '/api/voice/switch') return Response.json({ profile: '星野' });
    return Response.json({ backgrounds: [], settings: {} });
  };
  const loc = { search: '', hash: '', origin: 'http://home.test' };
  globalThis.location = loc;
  globalThis.window = { innerWidth: 1280, location: loc, sessionStorage: { getItem: () => '', setItem() {} }, fetch: fetchMock };
  globalThis.fetch = fetchMock;
  const app = initApp({ dom: { advRoot: root }, renderSessionList() {} });
  let title;
  t.after(() => {
    while (profileReplies.length) profileReplies.shift()(Response.json({ profiles: [], active_profile_id: null }));
    title?.destroy();
    app.conversation.stop();
    app.adv?.unmount();
    for (const key of ['document', 'window', 'localStorage', 'fetch', 'location']) delete globalThis[key];
  });
  title = await app.ready;
  await tick();
  // Profiles haven't loaded yet, so step 3 is not yet satisfied.
  assert.ok(title.element.querySelector('.home-first-title'), 'checklist shows while character list has not arrived yet');
  // Now character list arrives late -> renderHeader refreshes the open home screen without waiting for poll.
  while (profileReplies.length) {
    profileReplies.shift()(Response.json({ profiles: [{ id: 7, name: '星野' }], active_profile_id: 7 }));
    await tick();
  }
  await tick();
  assert.equal(Boolean(title.element.querySelector('.home-first-title')), false, 'home screen must refresh when late character list arrives');
  assert.equal(textTree(title.element.querySelector('.home-continue')), '开始聊天');
});

function bootTitleStartupEnv(t, { storageEntries = [], fetchImpl, renderSessionList = () => {} } = {}) {
  const doc = fakeDocument();
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  globalThis.document = doc;
  const root = doc.body.appendChild(doc.createElement('div'));
  root.id = 'advRoot';
  const sessionList = doc.body.appendChild(doc.createElement('div'));
  sessionList.id = 'sessionList';
  const toastRoot = doc.body.appendChild(doc.createElement('div'));
  toastRoot.id = 'toastRoot';
  const storage = new Map(storageEntries);
  globalThis.localStorage = {
    getItem: k => storage.get(k) ?? null,
    setItem: (k, v) => storage.set(k, v),
    removeItem: k => storage.delete(k),
  };
  const loc = { search: '', hash: '', origin: 'http://home.test' };
  globalThis.location = loc;
  globalThis.window = {
    innerWidth: 1280,
    location: loc,
    sessionStorage: { getItem: () => '', setItem() {} },
    fetch: fetchImpl,
  };
  globalThis.fetch = (...args) => globalThis.window.fetch(...args);
  const app = initApp({ dom: { advRoot: root, sessionList }, renderSessionList });
  let bootedTitle = null;
  app.ready?.then?.(s => { bootedTitle = s; });
  let cleaned = false;
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    bootedTitle?.destroy?.();
    for (const toast of toastRoot.children) clearTimeout(toast._timer);
    app.conversation.stop();
    app.adv?.unmount();
    for (const key of ['document', 'window', 'localStorage', 'fetch', 'location']) delete globalThis[key];
  };
  t.after(cleanup);
  return { doc, root, storage, app, cleanup };
}

test('sending a message while startup /api/chat/sessions is pending loads providers and clears 正在检查设置… on return home', async t => {
  const activeProvider = { id: 'deepseek', name: 'DeepSeek', chat_model: 'deepseek-chat', api_key: 'sk-****', is_active: true };
  const sseReply = [
    'event: line\ndata: {"index":0,"zh":"你好呀。","ja":"こんにちは。"}\n\n',
    'event: line_audio\ndata: {"line":0,"status":"skipped","chunks":[]}\n\n',
    'event: done\ndata: {"full_chinese":"你好呀。"}\n\n',
  ].join('');

  // Case 1: /api/chat/sessions finishes after user sends a message, then user returns home.
  let releaseSessions;
  const sessionsGate = new Promise(resolve => { releaseSessions = resolve; });
  let providerCalls = 0;
  const env1 = bootTitleStartupEnv(t, {
    fetchImpl: async (url, options = {}) => {
      const path = String(url);
      const method = (options.method || 'GET').toUpperCase();
      if (path === '/api/providers') {
        providerCalls += 1;
        return Response.json({ providers: [activeProvider], presets: [] });
      }
      if (path.startsWith('/api/chat/sessions') && !path.startsWith('/api/chat/sessions/') && method === 'GET') {
        await sessionsGate;
        return Response.json({ sessions: [], has_more: false });
      }
      if (path === '/api/chat/stream') {
        return new Response(sseReply, { headers: { 'Content-Type': 'text/event-stream' } });
      }
      if (path === '/api/system/engine-status') return Response.json({ state: 'ready', base_url: 'http://127.0.0.1:9880' });
      if (path === '/api/voice/profiles') return Response.json({ profiles: [{ id: 7, name: '星野' }], active_profile_id: 7 });
      return Response.json({ status: 'ok', backgrounds: [], settings: {} });
    },
  });

  await tick();
  const input = env1.app.adv.textbox.inputElement;
  input.value = '在同步完成前发消息';
  input.dispatchEvent({ type: 'keydown', key: 'Enter' });
  for (let i = 0; i < 5; i++) await tick();

  releaseSessions();
  const startupTitle = await env1.app.ready;
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(startupTitle === null, true, 'stale startup sync must not open home over active chat');
  assert.equal(env1.app.adv.stage.stage.classList.contains('is-home'), false);

  const homeScreen1 = env1.app.returnHome();
  t.after(() => homeScreen1?.destroy());
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(homeScreen1.isOpen, true);
  assert.doesNotMatch(textTree(homeScreen1.element), /正在检查设置…/);
  assert.equal(textTree(homeScreen1.element.querySelector('.home-continue')), '接着聊 →');
  assert.equal(providerCalls, 1, 'stale startup sync and home entry must load providers once');
  homeScreen1.destroy();
  env1.cleanup();

  // Case 2: user returns home while /api/chat/sessions is still pending -> getStatus() loads providers once without duplicating on late sync.
  let releaseSessions2;
  const sessionsGate2 = new Promise(resolve => { releaseSessions2 = resolve; });
  let providerCalls2 = 0;
  const env2 = bootTitleStartupEnv(t, {
    fetchImpl: async (url, options = {}) => {
      const path = String(url);
      const method = (options.method || 'GET').toUpperCase();
      if (path === '/api/providers') {
        providerCalls2 += 1;
        return Response.json({ providers: [activeProvider], presets: [] });
      }
      if (path.startsWith('/api/chat/sessions') && !path.startsWith('/api/chat/sessions/') && method === 'GET') {
        await sessionsGate2;
        return Response.json({ sessions: [], has_more: false });
      }
      if (path === '/api/chat/stream') {
        return new Response(sseReply, { headers: { 'Content-Type': 'text/event-stream' } });
      }
      if (path === '/api/system/engine-status') return Response.json({ state: 'ready', base_url: 'http://127.0.0.1:9880' });
      if (path === '/api/voice/profiles') return Response.json({ profiles: [{ id: 7, name: '星野' }], active_profile_id: 7 });
      return Response.json({ status: 'ok', backgrounds: [], settings: {} });
    },
  });

  await tick();
  const input2 = env2.app.adv.textbox.inputElement;
  input2.value = '还没返回就回首页';
  input2.dispatchEvent({ type: 'keydown', key: 'Enter' });
  for (let i = 0; i < 5; i++) await tick();

  const homeScreen2 = env2.app.returnHome();
  t.after(() => homeScreen2?.destroy());
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(providerCalls2, 1, 'getStatus must fetch /api/providers when not yet loaded');
  assert.doesNotMatch(textTree(homeScreen2.element), /正在检查设置…/);
  assert.equal(textTree(homeScreen2.element.querySelector('.home-continue')), '接着聊 →');

  releaseSessions2();
  await env2.app.ready;
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(providerCalls2, 1, 'late stale startup sync must not fetch /api/providers a second time');
  homeScreen2.destroy();
  env2.cleanup();
});

test('normal startup without user interaction fetches /api/providers once and opens home', async t => {
  const activeProvider = { id: 'deepseek', name: 'DeepSeek', chat_model: 'deepseek-chat', api_key: 'sk-****', is_active: true };
  let providerCalls = 0;
  const env = bootTitleStartupEnv(t, {
    fetchImpl: async url => {
      const path = String(url);
      if (path === '/api/providers') {
        providerCalls += 1;
        return Response.json({ providers: [activeProvider], presets: [] });
      }
      if (path.startsWith('/api/chat/sessions')) {
        return Response.json({
          sessions: [{ id: 's_1', title: '新对话', voice_profile_id: 7, created_at: '2026-06-01 00:00:01', updated_at: '2026-06-01 00:00:01', message_count: 0, last_message: '', settings: {} }],
          has_more: false,
        });
      }
      if (path === '/api/system/engine-status') return Response.json({ state: 'ready', base_url: 'http://127.0.0.1:9880' });
      if (path === '/api/voice/profiles') return Response.json({ profiles: [{ id: 7, name: '星野' }], active_profile_id: 7 });
      return Response.json({ status: 'ok', backgrounds: [], settings: {} });
    },
  });

  const title = await env.app.ready;
  t.after(() => title?.destroy());
  for (let i = 0; i < 5; i++) await tick();
  assert.ok(title?.isOpen, 'normal startup must open home');
  assert.equal(env.app.adv.stage.stage.classList.contains('is-home'), true);
  assert.equal(providerCalls, 1, 'normal startup must fetch /api/providers only once');
  assert.doesNotMatch(textTree(title.element), /正在检查设置…/);
  assert.equal(textTree(title.element.querySelector('.home-continue')), '开始聊天');
  title.destroy();
});

test('typing in input or switching session during startup sync keeps chat open and preserves draft', async t => {
  const activeProvider = { id: 'deepseek', name: 'DeepSeek', chat_model: 'deepseek-chat', api_key: 'sk-****', is_active: true };

  // 1. Typing during startup sync: home does not open and typed draft remains in input.
  let releaseTypingSync;
  const typingGate = new Promise(resolve => { releaseTypingSync = resolve; });
  let providerCalls1 = 0;
  const envTyping = bootTitleStartupEnv(t, {
    fetchImpl: async (url, options = {}) => {
      const path = String(url);
      const method = (options.method || 'GET').toUpperCase();
      if (path === '/api/providers') {
        providerCalls1 += 1;
        return Response.json({ providers: [activeProvider], presets: [] });
      }
      if (path.startsWith('/api/chat/sessions') && !path.startsWith('/api/chat/sessions/') && method === 'GET') {
        await typingGate;
        return Response.json({
          sessions: [{ id: 's_server', title: '已有对话', voice_profile_id: 7, created_at: '2026-06-01 00:00:01', updated_at: '2026-06-01 00:00:01', message_count: 1, last_message: '欢迎回来', settings: {} }],
          has_more: false,
        });
      }
      if (path.startsWith('/api/chat/history')) {
        return Response.json({
          session_id: 's_server',
          messages: [{ id: 1, role: 'assistant', content_chinese: '欢迎回来', created_at: '2026-06-01 00:00:01' }],
          has_more: false,
          oldest_id: 1,
        });
      }
      if (path === '/api/system/engine-status') return Response.json({ state: 'ready', base_url: 'http://127.0.0.1:9880' });
      if (path === '/api/voice/profiles') return Response.json({ profiles: [{ id: 7, name: '星野' }], active_profile_id: 7 });
      return Response.json({ status: 'ok', backgrounds: [], settings: {} });
    },
  });

  await tick();
  const input = envTyping.app.adv.textbox.inputElement;
  input.value = '正在输入的半句话';
  input.dispatchEvent({ type: 'input' });

  releaseTypingSync();
  const titleAfterTyping = await envTyping.app.ready;
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(titleAfterTyping === null, true, 'home must not pop over active typing');
  assert.equal(envTyping.app.adv.stage.stage.classList.contains('is-home'), false);
  assert.equal(envTyping.app.adv.stage.textbox.inert, false);
  assert.equal(envTyping.app.adv.textbox.draft, '正在输入的半句话');
  assert.equal(envTyping.app.adv.textbox.inputElement.value, '正在输入的半句话');
  assert.equal(providerCalls1, 1, 'providers still load once in background');
  envTyping.cleanup();

  // 2. Switching session during startup sync: home does not open and stays on chosen session.
  let releaseSwitchSync;
  const switchGate = new Promise(resolve => { releaseSwitchSync = resolve; });
  let selectSession = null;
  let providerCalls2 = 0;
  const seeded = JSON.stringify({
    sessions: [
      { id: 's_one', title: '会话一', createdAt: 2000, updatedAt: 2000, messages: [{ id: 'm_1', serverMessageId: 1, role: 'user', content: '一' }], settings: { voiceProfileId: 7 } },
      { id: 's_two', title: '会话二', createdAt: 1000, updatedAt: 1000, messages: [{ id: 'm_2', serverMessageId: 2, role: 'user', content: '二' }], settings: { voiceProfileId: 7 } },
    ],
    activeId: 's_one',
    global: { voiceMode: true, ttsPreset: '' },
  });
  const envSwitch = bootTitleStartupEnv(t, {
    storageEntries: [['gal2voice.chat.v1', seeded]],
    renderSessionList(_el, { onSelect }) { selectSession = onSelect; },
    fetchImpl: async (url, options = {}) => {
      const path = String(url);
      const method = (options.method || 'GET').toUpperCase();
      if (path === '/api/providers') {
        providerCalls2 += 1;
        return Response.json({ providers: [activeProvider], presets: [] });
      }
      if (path.startsWith('/api/chat/sessions') && !path.startsWith('/api/chat/sessions/') && method === 'GET') {
        await switchGate;
        return Response.json({
          sessions: [
            { id: 's_one', title: '会话一', voice_profile_id: 7, created_at: '2026-06-01 00:00:02', updated_at: '2026-06-01 00:00:02', message_count: 1, last_message: '一', settings: {} },
            { id: 's_two', title: '会话二', voice_profile_id: 7, created_at: '2026-06-01 00:00:01', updated_at: '2026-06-01 00:00:01', message_count: 1, last_message: '二', settings: {} },
          ],
          has_more: false,
        });
      }
      if (path === '/api/system/engine-status') return Response.json({ state: 'ready', base_url: 'http://127.0.0.1:9880' });
      if (path === '/api/voice/profiles') return Response.json({ profiles: [{ id: 7, name: '星野' }], active_profile_id: 7 });
      return Response.json({ status: 'ok', backgrounds: [], settings: {} });
    },
  });

  await tick();
  await selectSession('s_two');
  releaseSwitchSync();
  const titleAfterSwitch = await envSwitch.app.ready;
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(titleAfterSwitch === null, true, 'home must not pop open after user switched session');
  assert.equal(envSwitch.app.adv.stage.stage.classList.contains('is-home'), false);
  assert.equal(envSwitch.app.actions.getActiveId(), 's_two');
  assert.equal(providerCalls2, 1);
  envSwitch.cleanup();
});

async function pendingPreviewFixture(t, result = true) {
  let finish, active = 'a';
  const sessions = ['a', 'b', 'c', 'd'].map((id, n) => ({ ...snapshot(id, n), hasUserMessages: true }));
  const h = await fixture(t, {
    sessions,
    actions: {
      getActiveId: () => active,
      previewSession: id => {
        if (id !== 'b') { active = id; return; }
        if (result !== false) active = id;
        return new Promise(resolve => {
          finish = (out = result) => {
            if (out !== false) sessions[1].excerpt = { zh: '加载完的台词b', ja: '読み込み完了。' };
            resolve(out);
          };
        });
      },
    },
  });
  return { ...h, finish: out => finish?.(out) };
}

test('clicking continue while a recent session preview is still loading enters the clicked session', async t => {
  const h = await pendingPreviewFixture(t);
  h.q('.home-recent').click();
  const titleWhilePending = h.q('.home-meta').children[0].textContent;
  const busyWhilePending = h.ui.element.getAttribute('aria-busy');
  h.q('.home-continue').click();
  assert.deepEqual(h.calls.at(-1), ['load', 'b']);
  assert.equal(titleWhilePending, '角色b');
  assert.equal(busyWhilePending, 'true');
  assert.equal(h.ui.isOpen, false);
  assert.equal(h.ui.element.getAttribute('aria-busy'), null);
  h.finish();
  await microtasks();
  assert.equal(h.ui.isOpen, false);
});

test('opening new-chat draft while a recent preview is pending keeps the draft when preview settles', async t => {
  const h = await pendingPreviewFixture(t);
  h.q('.home-recent').click();
  h.q('.home-new').click();
  const busyAfterNew = h.ui.element.getAttribute('aria-busy');
  h.q('.home-pick-voiceProfileId').click();
  h.ui.element.querySelectorAll('.home-option')[1].click();
  h.q('.home-pick-advBackground').click();
  h.ui.element.querySelectorAll('.home-option')[2].click();
  h.finish();
  await microtasks();
  assert.ok(h.q('.home-start'), 'new-chat draft view must stay open after stale preview resolves');
  assert.equal(h.q('.home-continue'), null);
  assert.equal(busyAfterNew, null);
  assert.equal(textTree(h.q('.home-pick-voiceProfileId')), '角色乙 ▾');
  assert.equal(textTree(h.q('.home-pick-advBackground')), '房间 ▾');
  h.q('.home-start').click();
  assert.deepEqual(h.calls.at(-1), ['newWith', { voiceProfileId: 8, advBackground: 'w/room', advBgTime: 'night' }]);
});

test('opening all sessions while a recent preview is pending keeps all-sessions view and Escape returns home', async t => {
  const h = await pendingPreviewFixture(t);
  h.q('.home-recent').click();
  h.q('.home-all').click();
  const busyAfterAll = h.ui.element.getAttribute('aria-busy');
  h.finish();
  await microtasks();
  assert.equal(h.ui.loadScreen.hidden, false);
  assert.equal(h.q('.home-excerpt').hidden, true);
  assert.equal(h.q('.home-recents').hidden, true, 'recents must not reappear over all-sessions view');
  assert.equal(busyAfterAll, null);
  escape(h.q('.adv-title-back'));
  assert.equal(h.ui.loadScreen.hidden, true, 'Escape must exit all-sessions view back to home');
  assert.equal(h.q('.home-excerpt').hidden, false);
  assert.equal(h.q('.home-recents').hidden, false);
  assert.equal(h.q('.home-quote').textContent, '「加载完的台词b」');
});

test('failed recent session preview rolls back main session and recent order', async t => {
  const h = await pendingPreviewFixture(t, false);
  const recents = () => h.ui.element.querySelectorAll('.home-recent').map(n => n.dataset.session);
  assert.deepEqual(recents(), ['b', 'c', 'd']);
  h.q('.home-recent').click();
  assert.equal(h.q('.home-meta').children[0].textContent, '角色b');
  assert.deepEqual(recents(), ['a', 'c', 'd']);
  h.finish(false);
  await microtasks();
  assert.equal(h.ui.element.getAttribute('aria-busy'), null);
  assert.equal(h.q('.home-meta').children[0].textContent, '角色a');
  assert.deepEqual(recents(), ['b', 'c', 'd']);
  h.q('.home-continue').click();
  assert.deepEqual(h.calls.at(-1), ['load', 'a']);
});

function bootProviderRetry(t, replyProviders) {
  return bootTitleStartupEnv(t, {
    fetchImpl: async url => {
      const path = String(url);
      if (path === '/api/providers') return replyProviders();
      if (path.startsWith('/api/chat/sessions')) return Response.json({ sessions: [], has_more: false });
      if (path === '/api/system/engine-status') return Response.json({ state: 'ready', base_url: 'http://127.0.0.1:9880' });
      return Response.json({ status: 'ok', profiles: [{ id: 7, name: '星野' }], active_profile_id: 7, backgrounds: [], settings: {} });
    },
  });
}

test('home refresh retries after initial /api/providers failure, recovers home and stage status, and stops refetching', async t => {
  let calls = 0;
  const active = { id: 'deepseek', name: 'DeepSeek', chat_model: 'deepseek-chat', api_key: 'sk-****', is_active: true };
  const env = bootProviderRetry(t, () => (++calls === 1
    ? Response.json({ detail: 'down' }, { status: 503 }) : Response.json({ providers: [active], presets: [] })));
  const title = await env.app.ready; for (let i = 0; i < 5; i++) await tick();
  assert.equal(title.element.querySelector('.home-pill-text').textContent, '对话 状态未确认');
  assert.equal(env.root.querySelector('.adv-qm-status-label').textContent, '模型配置暂时无法读取');
  await title.refresh(); for (let i = 0; i < 5; i++) await tick();
  assert.equal(calls, 2);
  assert.equal(title.element.querySelector('.home-pill-text').textContent, '对话 可用');
  assert.equal(env.root.querySelector('.adv-qm-status-label').textContent, 'deepseek-chat');
  await title.refresh(); for (let i = 0; i < 5; i++) await tick();
  assert.equal(calls, 2);
});

test('background /api/providers retry failure stays silent while opening settings center still shows error toast', async t => {
  let calls = 0;
  const env = bootProviderRetry(t, () => (++calls, Response.json({ detail: 'down' }, { status: 503 })));
  const title = await env.app.ready; for (let i = 0; i < 5; i++) await tick();
  const toasts = env.doc.getElementById('toastRoot');
  for (const n of [...toasts.children]) { clearTimeout(n._timer); n.remove(); }
  await title.refresh(); for (let i = 0; i < 5; i++) await tick();
  assert.equal(calls, 2); assert.equal(toasts.children.length, 0);
  env.app.actions.openSettings('models'); for (let i = 0; i < 5; i++) await tick();
  assert.equal(toasts.children.length, 1);
});

test('home excerpt keeps inner quotes balanced at either end', async t => {
  for (const [zh, expected] of [
    ['我最讨厌的词就是「笨蛋」', '「我最讨厌的词就是「笨蛋」」'],
    ['「笨蛋」什么的，我才没说过', '「「笨蛋」什么的，我才没说过」'],
  ]) {
    const h = await fixture(t, { sessions: [{ ...snapshot('a'), excerpt: homeExcerpt([{ role: 'assistant', content: zh }]) }] });
    assert.equal(h.q('.home-quote').textContent, expected);
  }
});

test('home refresh reads the session list again after the first /api/chat/sessions read failed, then stops refetching', async t => {
  let listCalls = 0;
  const active = { id: 'deepseek', name: 'DeepSeek', chat_model: 'deepseek-chat', api_key: 'sk-****', is_active: true };
  const serverSession = { id: 's_server', title: '星夜的约定', last_message: '晚安。', message_count: 0,
    created_at: '2026-11-20T10:00:00Z', updated_at: '2026-11-20T10:00:00Z', voice_profile_id: 7, settings: {} };
  const env = bootTitleStartupEnv(t, {
    fetchImpl: async url => {
      const path = String(url);
      if (path === '/api/chat/sessions' || path.startsWith('/api/chat/sessions?')) {
        return ++listCalls === 1 ? Response.json({ detail: 'down' }, { status: 503 })
          : Response.json({ sessions: [serverSession], has_more: false });
      }
      if (path.startsWith('/api/chat/sessions/')) return Response.json({ messages: [], has_more: false });
      if (path === '/api/providers') return Response.json({ providers: [active], presets: [] });
      if (path === '/api/system/engine-status') return Response.json({ state: 'ready', base_url: 'http://127.0.0.1:9880' });
      return Response.json({ status: 'ok', profiles: [{ id: 7, name: '星野' }], active_profile_id: 7, backgrounds: [], settings: {} });
    },
  });
  await env.app.ready; t.mock.method(console, 'debug', () => {});
  assert.equal(listCalls, 1);
  const home = env.app.returnHome();
  t.after(() => home?.destroy());
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(listCalls, 2);
  assert.equal(textTree(home.element.querySelector('.home-meta')), `星野 · ${formatHomeTime(serverSession.updated_at)}`, 'home shows the session read from the server');
  await home.refresh(); for (let i = 0; i < 5; i++) await tick();
  assert.equal(listCalls, 2);
});

test('home refresh reads the session list again when the first read was dropped because the user acted during it', async t => {
  let listCalls = 0;
  let releaseFirstRead;
  const active = { id: 'deepseek', name: 'DeepSeek', chat_model: 'deepseek-chat', api_key: 'sk-****', is_active: true };
  const serverSession = { id: 's_server', title: '星夜的约定', last_message: '晚安。', message_count: 0,
    created_at: '2026-11-20T10:00:00Z', updated_at: '2026-11-20T10:00:00Z', voice_profile_id: 7, settings: {} };
  const list = () => Response.json({ sessions: [serverSession], has_more: false });
  const env = bootTitleStartupEnv(t, {
    fetchImpl: async (url, options = {}) => {
      const path = String(url);
      if ((options.method || 'GET') !== 'GET' && path === '/api/chat/sessions') return Response.json({ status: 'ok' });
      if (path === '/api/chat/sessions' || path.startsWith('/api/chat/sessions?')) {
        if (++listCalls === 1) return new Promise(resolve => { releaseFirstRead = () => resolve(list()); });
        return list();
      }
      if (path.startsWith('/api/chat/sessions/')) return Response.json({ messages: [], has_more: false });
      if (path === '/api/providers') return Response.json({ providers: [active], presets: [] });
      if (path === '/api/system/engine-status') return Response.json({ state: 'ready', base_url: 'http://127.0.0.1:9880' });
      return Response.json({ status: 'ok', profiles: [{ id: 7, name: '星野' }], active_profile_id: 7, backgrounds: [], settings: {} });
    },
  });
  for (let i = 0; i < 5 && !releaseFirstRead; i++) await tick();
  assert.equal(listCalls, 1);
  const created = env.app.actions.startNew(null);
  releaseFirstRead();
  await env.app.ready; t.mock.method(console, 'debug', () => {});
  assert.ok(!storedState.sessions.some(session => session.id === 's_server'), 'the dropped read is not applied');
  const home = env.app.returnHome();
  t.after(() => home?.destroy());
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(listCalls, 2, 'home reads the list again');
  assert.ok(storedState.sessions.some(session => session.id === 's_server'), 'the server session is listed after all');
  assert.ok(storedState.sessions.some(session => session.id === created.id), 'the newly created session is kept');
  assert.equal(storedState.activeId, created.id, 'the newly created session remains active');
});

function bootRetryEnv(t, { storageEntries = [], onSessions, onHistory } = {}) {
  if (!console.debug.mock) t.mock.method(console, 'debug', () => {});
  const ts = new Date(Date.now() - 60000).toISOString();
  const s = { id: 's_server', title: '星夜的约定', last_message: '晚安。', message_count: 2, created_at: ts, updated_at: ts, voice_profile_id: 7, settings: {} };
  const hist = { session_id: 's_server', has_more: false, oldest_id: 1, messages: [{ id: 1, role: 'user', content_chinese: '睡了吗？', created_at: ts }, { id: 2, role: 'assistant', content_chinese: '晚安。', created_at: ts }] };
  return bootTitleStartupEnv(t, { storageEntries, fetchImpl: async (url, opt = {}) => {
    const p = String(url), m = (opt.method || 'GET').toUpperCase();
    if (p === '/api/chat/sessions' || p.startsWith('/api/chat/sessions?')) return onSessions(m, opt, s);
    if (p.startsWith('/api/chat/history')) return onHistory ? onHistory(hist) : Response.json(hist);
    if (p === '/api/providers') return Response.json({ providers: [{ id: 'deepseek', name: 'DeepSeek', chat_model: 'deepseek-chat', api_key: 'sk-****', is_active: true }], presets: [] });
    return Response.json({ state: 'ready', base_url: 'http://127.0.0.1:9880', profiles: [{ id: 7, name: '星野' }], active_profile_id: 7, backgrounds: [], settings: {} });
  } });
}
test('home retry deduplicates in-flight reads, renders server history, and carries draft if chat opens early', async t => {
  let calls = 0, releaseList, releaseHist;
  const onSessions = async (_m, _o, s) => (++calls === 1 ? Response.json({ detail: 'down' }, { status: 503 }) : (await new Promise(r => { releaseList = r; }), Response.json({ sessions: [s], has_more: false })));
  const env1 = bootRetryEnv(t, { onSessions }); await env1.app.ready;
  const home1 = env1.app.returnHome(); t.after(() => home1?.destroy());
  for (let i = 0; i < 10; i++) await tick(); await home1.refresh(); for (let i = 0; i < 5; i++) await tick();
  assert.equal(calls, 2); releaseList(); for (let i = 0; i < 10; i++) await tick();
  assert.equal(home1.element.querySelector('.home-meta').children[0].textContent, '星野'); assert.equal(home1.element.querySelector('.home-quote').textContent, '「晚安。」');
  await home1.refresh(); for (let i = 0; i < 5; i++) await tick(); assert.equal(calls, 2); home1.destroy(); env1.cleanup();
  calls = 0; const env2 = bootRetryEnv(t, { onSessions, onHistory: async h => (await new Promise(r => { releaseHist = r; }), Response.json(h)) });
  await env2.app.ready; const home2 = env2.app.returnHome(); t.after(() => home2?.destroy());
  for (let i = 0; i < 5; i++) await tick(); releaseList(); for (let i = 0; i < 5; i++) await tick(); home2.element.querySelector('.home-continue').click();
  const input = env2.app.adv.textbox.inputElement; input.value = '刚写的草稿'; input.dispatchEvent({ type: 'input' });
  releaseHist(); for (let i = 0; i < 10; i++) await tick(); assert.equal(env2.app.adv.textbox.draft, '刚写的草稿'); assert.equal(env2.doc.activeElement, input);
});
test('home retry keeps an unconfirmed new chat and ranks a newer server session first in recents', async t => {
  let up = false; const posted = [], nowMs = Date.now(), iso = ms => new Date(ms).toISOString();
  const cached = [1, 2, 3, 4].map(n => ({ id: `s_c${n}`, title: `旧${n}`, createdAt: nowMs - (6 - n) * 60000, updatedAt: nowMs - (6 - n) * 60000, messages: [{ id: `m_${n}`, serverMessageId: n, role: 'user', content: 'hi' }], settings: {} }));
  const env = bootRetryEnv(t, {
    storageEntries: [['gal2voice.chat.v1', JSON.stringify({ sessions: cached, activeId: 's_c4', global: { voiceMode: true, ttsPreset: '' } })]],
    onSessions: (m, opt, s) => !up ? Response.json({ detail: 'down' }, { status: 503 }) : m === 'POST' ? (posted.push(JSON.parse(opt.body).id), Response.json({ status: 'ok' }))
      : Response.json({ sessions: [s, ...cached.map(c => ({ id: c.id, title: c.title, last_message: 'hi', message_count: 1, created_at: iso(c.createdAt), updated_at: iso(c.updatedAt), voice_profile_id: null, settings: {} }))], has_more: false }),
    onHistory: () => Response.json({ messages: [], has_more: false }) });
  await env.app.ready; const created = env.app.actions.startNewWith({ voiceProfileId: 7, advBackground: 'w/sea', advBgTime: 'night' });
  for (let i = 0; i < 5; i++) await tick(); up = true; const home = env.app.returnHome(); t.after(() => home?.destroy());
  for (let i = 0; i < 10; i++) await tick(); assert.equal(env.app.actions.getActiveId(), created.id); assert.deepEqual(posted, [created.id]);
  assert.deepEqual(home.element.querySelectorAll('.home-recent').map(n => n.dataset.session), ['s_server', 's_c4', 's_c3']); });
test('startup 401 after cancelling token prompt shows action toast to re-enter token and retry sync', async t => {
  let listCalls = 0, tok = '';
  const env = bootTitleStartupEnv(t, {
    fetchImpl: async (url, init = {}) => String(url).startsWith('/api/chat/sessions')
      ? (++listCalls, init.headers?.get?.('Authorization') === 'Bearer ok-tok'
        ? Response.json({ sessions: [], has_more: false })
        : Response.json({ detail: 'unauthorized' }, { status: 401 }))
      : Response.json({ providers: [], presets: [], profiles: [], backgrounds: [], state: 'ready', settings: {} }),
  });
  globalThis.window.sessionStorage = { getItem: () => tok, setItem: (_k, v) => { tok = String(v); }, removeItem: () => { tok = ''; } };
  globalThis.window.prompt = () => null;
  t.mock.method(console, 'debug', () => {});
  await env.app.ready;
  const btn = env.doc.getElementById('toastRoot').querySelector('.toast-action-btn');
  assert.equal(btn?.textContent, '输入访问令牌');
  globalThis.window.prompt = () => 'ok-tok';
  btn.click();
  for (let i = 0; i < 6; i++) await tick();
  assert.equal(listCalls, 2);
});

test('unreadable saved API key updates home onboarding prompt while keeping existing chat link', async t => {
  const ts = '2026-11-20T10:00:00Z';
  const env = bootTitleStartupEnv(t, { fetchImpl: async url => {
    const p = String(url);
    if (p === '/api/providers') return Response.json({ providers: [{ id: 'deepseek', name: 'DeepSeek', chat_model: 'deepseek-chat', api_key: '', api_key_unreadable: true, is_active: true }], presets: [] });
    if (p === '/api/chat/sessions' || p.startsWith('/api/chat/sessions?')) return Response.json({ sessions: [{ id: 's1', title: '旧对话', last_message: '你好', message_count: 2, created_at: ts, updated_at: ts, voice_profile_id: 7, settings: {} }], has_more: false });
    if (p.startsWith('/api/chat/history')) return Response.json({ session_id: 's1', has_more: false, oldest_id: 1, messages: [{ id: 1, role: 'user', content_chinese: '在吗', created_at: ts }, { id: 2, role: 'assistant', content_chinese: '你好', created_at: ts }] });
    return Response.json({ state: 'ready', base_url: 'http://127.0.0.1:9880', profiles: [{ id: 7, name: '星野' }], active_profile_id: 7, backgrounds: [], settings: {} });
  } });
  const home = await env.app.ready;
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(home.element.querySelector('.home-first-title')?.textContent, '换了电脑或账户，之前保存的 API 密钥需要重新填写');
  assert.doesNotMatch(home.element.querySelector('.home-label')?.textContent || '', /第一次使用/);
  assert.equal(textTree(home.element.querySelector('.home-existing')), '查看已有对话 →');
});

test('title screen releases hidden grid and avatar images on returnHome and after entering chat', async t => {
  const sessions = [
    { ...snapshot('a'), background: '/bg/a.png' },
    { ...snapshot('b', 1), background: '/bg/b.png' },
  ];
  const h = await fixture(t, { sessions, actions: {
    getAvatar: async () => ({ rect: { x: 0, y: 0, width: 100, height: 100 }, layers: [{ file: '/face/b.webp', width: 100, height: 100 }] }),
  } });
  h.ui.destroy();
  let finishFade;
  const ui = showTitle(h.actions, { documentObj: h.doc, matchMedia: () => ({ matches: false }) });
  t.after(() => ui.destroy());
  await microtasks();
  assert.equal(ui.element.querySelector('.home-avatar').querySelectorAll('img').length, 1);
  ui.element.querySelector('.home-all').click();
  const gridImgs = ui.element.querySelectorAll('.adv-title-thumb-image');
  assert.equal(gridImgs.length, 2);
  ui.element.querySelector('.adv-title-back').click();
  assert.equal(ui.element.querySelectorAll('.adv-title-thumb-image').length, 0);
  assert.ok(gridImgs.every(img => !img.src), 'leaving all-sessions grid must clear thumb img src');
  await microtasks();
  const avatarImgs = ui.element.querySelector('.home-avatar').querySelectorAll('img');
  assert.equal(avatarImgs.length, 1);
  ui.element.animate = () => ({ finished: new Promise(r => { finishFade = r; }), cancel() {} });
  ui.element.querySelector('.home-continue').click();
  assert.equal(ui.element.querySelectorAll('img').length, 1, 'fade keeps avatars visible until done');
  finishFade();
  await microtasks();
  assert.equal(ui.element.querySelectorAll('img').length, 0);
  assert.ok(avatarImgs.every(img => !img.src), 'avatar img src must be cleared once fade finishes');
});
