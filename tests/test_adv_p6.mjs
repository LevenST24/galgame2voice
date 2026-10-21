// P6: stage background view, 背景 panel and the adv_app wiring.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fakeDocument, fakeClock, Element, microtasks } from './helpers/adv_dom.mjs';
import {
  BG_TIME_CHOICES,
  createBackgroundView,
  normalizeBackgroundSelection,
  resolveVariant,
  timeOfDay,
} from '../frontend/src/adv/background.js';
import { createBgPanel } from '../frontend/src/adv/bg_panel.js';
import { mountAdv } from '../frontend/src/adv/adv_app.js';
import { pickSettings } from '../frontend/src/session_data.js';
import { DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';

const LIST = [
  { id: 'cafe', name: '咖啡店', source: '喫茶ステラ', variants: {
    day: '/bg/cafe/day.png', evening: '/bg/cafe/evening.png', night: '/bg/cafe/night.png' } },
  { id: 'beach', name: '海边', variants: { day: '/bg/beach/day.png', night: '/bg/beach/night.png' } },
  { id: 'onsen', name: '温泉', variants: { night: '/bg/onsen/night.png' } },
];

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function imageFactory(doc, loads) {
  return class Image extends Element {
    constructor() { super('img', doc); }
    decode() {
      // These P6 assertions count display-buffer requests. P8's detached
      // variant preloads are checked independently in test_adv_p8.mjs.
      if (!this.classList.contains('adv-bg-image')) return Promise.resolve();
      const load = { image: this, url: this.src, settled: false };
      load.promise = new Promise((resolve, reject) => { load.resolve = resolve; load.reject = reject; });
      loads.push(load);
      return load.promise;
    }
  };
}

// Resolves every pending artwork request so a decode can finish and fade.
async function flush(loads, mode = 'resolve') {
  const pending = loads.filter(load => !load.settled && load.url.startsWith('/bg/'));
  pending.forEach(load => {
    load.settled = true;
    if (mode === 'reject') load.reject(new Error('decode failed'));
    else load.resolve();
  });
  await microtasks();
  return pending;
}

const skyOf = layer => layer.querySelector('.adv-bg-sky');
const shownOf = layer => layer.querySelectorAll('.adv-bg-image').filter(node => node.classList.contains('is-visible'));

function setupView(t, options = {}) {
  const doc = fakeDocument();
  globalThis.document = doc;
  const clock = fakeClock();
  const loads = [];
  let wall = new Date(2026, 9, 20, options.hour ?? 10, 0, 0);
  const reduced = { matches: Boolean(options.reducedMotion) };
  const layer = doc.createElement('div');
  layer.className = 'adv-layer adv-layer-bg';
  const view = createBackgroundView({
    layer,
    now: () => wall,
    setTimer: (fn, ms) => clock.setTimeout(fn, ms),
    clearTimer: id => clock.clearTimeout(id),
    matchMedia: () => reduced,
    ImageClass: imageFactory(doc, loads),
    documentObj: doc,
  });
  t.after(() => view.destroy());
  return {
    doc, layer, view, clock, loads, reduced,
    setHour: hour => { wall = new Date(2026, 9, 20, hour, 0, 0); },
    sky: () => skyOf(layer),
    shown: () => shownOf(layer),
  };
}

function setupPanel(t, options = {}) {
  const doc = fakeDocument();
  globalThis.document = doc;
  const container = doc.createElement('div');
  container.className = 'adv-layer adv-layer-overlay';
  doc.body.appendChild(container);
  const saves = [];
  const previews = [];
  const opens = [];
  let selection = options.selection || { id: '', time: 'auto' };
  const panel = createBgPanel(container, {
    getList: () => (options.list === undefined ? LIST : options.list),
    getSelection: () => selection,
    saveSettings: patch => saves.push(patch),
    onPreview: next => { selection = next; previews.push(next); },
    onOpen: () => opens.push('open'),
    onClose: () => opens.push('close'),
    now: () => new Date(2026, 9, 20, options.hour ?? 10, 0, 0),
    documentObj: doc,
  });
  t.after(() => panel.destroy());
  const tiles = () => panel.dialog.querySelectorAll('.adv-bg-tile');
  const times = () => panel.dialog.querySelectorAll('.adv-bg-time');
  const nameOf = tile => tile.querySelector('.adv-bg-name').textContent;
  const thumbOf = tile => tile.querySelector('.adv-bg-thumb');
  return { doc, container, panel, saves, previews, opens, tiles, times, nameOf, thumbOf };
}

function setupApp(t, options = {}) {
  const doc = fakeDocument();
  globalThis.document = doc;
  const clock = fakeClock();
  const loads = [];
  const listeners = new Map();
  const sessions = [0, 1].map(n => ({
    id: `s_${n}`,
    title: `对话 ${n}`,
    settings: { ...DEFAULT_SESSION_SETTINGS, advEnabled: true, ...options.settings },
    messages: [],
  }));
  let active = sessions[0];
  let subscription = null;
  const saves = [];
  const calls = { fetches: 0 };
  const mql = { matches: Boolean(options.reducedMotion), addEventListener: () => {}, removeEventListener: () => {} };
  const dom = Object.fromEntries(
    ['root', 'sidebar', 'messages', 'input', 'micBtn'].map(key => [key, doc.createElement(key === 'input' ? 'textarea' : 'div')])
  );
  for (const node of Object.values(dom)) doc.body.appendChild(node);

  const controller = {
    busy: false, sent: [],
    on(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
      return () => listeners.get(type)?.delete(fn);
    },
    send(text) { this.sent.push(text); return true; },
    canRetry() { return false; },
    retry() {},
    stop() {},
  };
  const audio = { played: [], playLine() { return new Promise(() => {}); }, stopLine() {} };

  const app = mountAdv(dom, {
    controller,
    audio,
    clock,
    getSession: () => active,
    voiceEnabled: () => false,
    getCharacter: () => null,
    loadPortrait: null,
    ImageClass: imageFactory(doc, loads),
    raf: () => 0,
    cancelRaf: () => {},
    matchMedia: () => mql,
    viewport: doc.createElement('div'),
    // Mirrors main.js: unknown adv* keys stay dropped until the settings
    // whitelist lands, so only the live preview is observable here.
    saveSettings: patch => {
      saves.push(patch);
      active.settings = pickSettings({ ...active.settings, ...patch }, DEFAULT_SESSION_SETTINGS);
    },
    fetchBackgrounds: async () => {
      calls.fetches++;
      if (options.fetchBackgrounds) return options.fetchBackgrounds();
      return { backgrounds: LIST };
    },
    subscribe: fn => { subscription = fn; return () => { subscription = null; }; },
    subscribeOverlay: () => () => {},
  });
  t.after(() => app.unmount());

  return {
    doc, dom, app, clock, loads, saves, sessions, controller, calls,
    get active() { return active; },
    switchSession: index => { active = sessions[index]; subscription?.(); },
    sky: () => skyOf(app.stage.background),
    shown: () => shownOf(app.stage.background),
  };
}

// ---------------------------------------------------------------------------
// 1. Time of day and variant fallback
// ---------------------------------------------------------------------------

test('timeOfDay: 06-15 day, 16-18 evening, 19-05 night', () => {
  const at = hour => timeOfDay(new Date(2026, 9, 20, hour, 0, 0));
  assert.deepEqual([0, 4, 5, 19, 23].map(at), ['night', 'night', 'night', 'night', 'night']);
  assert.deepEqual([6, 12, 15].map(at), ['day', 'day', 'day']);
  assert.deepEqual([16, 17, 18].map(at), ['evening', 'evening', 'evening']);
  assert.equal(timeOfDay(undefined), 'day', 'an unusable clock falls back to day');
});

test('resolveVariant: a missing variant uses the next closest light level', () => {
  const [cafe, beach, onsen] = LIST;
  assert.deepEqual(resolveVariant(cafe, 'day'), { time: 'day', url: '/bg/cafe/day.png' });
  // night -> evening -> day
  assert.deepEqual(resolveVariant(beach, 'night'), { time: 'night', url: '/bg/beach/night.png' });
  assert.deepEqual(resolveVariant({ id: 'x', variants: { day: '/d', evening: '/e' } }, 'night'),
    { time: 'evening', url: '/e' });
  // evening -> day -> night
  assert.deepEqual(resolveVariant(beach, 'evening'), { time: 'day', url: '/bg/beach/day.png' });
  assert.deepEqual(resolveVariant(onsen, 'evening'), { time: 'night', url: '/bg/onsen/night.png' });
  // day -> evening -> night
  assert.deepEqual(resolveVariant(onsen, 'day'), { time: 'night', url: '/bg/onsen/night.png' });
  assert.deepEqual(resolveVariant({ id: 'x', variants: { night: '/n' } }, 'day'), { time: 'night', url: '/n' });
  assert.equal(resolveVariant({ id: 'x', variants: {} }, 'day'), null);
  assert.equal(resolveVariant(null, 'day'), null);
});

test('normalizeBackgroundSelection: a missing stored value uses the defaults', () => {
  assert.deepEqual(normalizeBackgroundSelection(undefined), { id: '', time: 'auto' });
  assert.deepEqual(normalizeBackgroundSelection({}), { id: '', time: 'auto' });
  assert.deepEqual(normalizeBackgroundSelection({ id: 'cafe', time: 'midnight' }), { id: 'cafe', time: 'auto' });
  assert.deepEqual(normalizeBackgroundSelection({ id: 7, time: 'night' }), { id: '', time: 'night' });
  assert.deepEqual(BG_TIME_CHOICES, ['auto', 'day', 'evening', 'night']);
});

// ---------------------------------------------------------------------------
// 2. Background view
// ---------------------------------------------------------------------------

test('background view: waits for decode, then crossfades between two buffers', async t => {
  const h = setupView(t);
  h.view.setList(LIST);
  h.view.setSelection({ id: 'cafe', time: 'day' });
  assert.equal(h.loads.length, 1);
  assert.equal(h.shown().length, 0, 'nothing fades in before decode settles');
  await flush(h.loads);
  assert.equal(h.shown().length, 1);
  assert.equal(h.shown()[0].src, '/bg/cafe/day.png');
  assert.equal(h.sky().dataset.time, 'day');
  assert.equal(h.layer.children.length, 3, 'one sky plus two image buffers');

  h.view.setSelection({ id: 'cafe', time: 'evening' });
  assert.equal(h.loads.length, 2);
  assert.equal(h.loads[1].url, '/bg/cafe/evening.png');
  assert.equal(h.shown().length, 1, 'the previous artwork stays until the new one decodes');
  const incoming = h.loads[1].image;
  await flush(h.loads);
  assert.deepEqual(h.shown(), [incoming]);
  assert.equal(h.layer.children.at(-1), incoming, 'the incoming buffer paints on top');
  assert.equal(h.sky().dataset.time, 'evening');
  assert.equal(h.clock.timers.size, 0, 'a fixed time never arms the auto re-check');
});

test('background view: auto re-checks every 60s and swaps the variant after 16:00', async t => {
  const h = setupView(t, { hour: 15 });
  h.view.setList(LIST);
  h.view.setSelection({ id: 'cafe', time: 'auto' });
  assert.equal(h.clock.timers.size, 1);
  await flush(h.loads);
  assert.equal(h.shown()[0].src, '/bg/cafe/day.png');

  h.clock.tick(59999);
  assert.equal(h.loads.length, 1, 'the variant is stable inside the same hour band');
  h.setHour(16);
  h.clock.tick(1);
  assert.equal(h.loads.length, 2);
  assert.equal(h.sky().dataset.time, 'evening');
  await flush(h.loads);
  assert.equal(h.shown()[0].src, '/bg/cafe/evening.png');
  assert.equal(h.clock.timers.size, 1, 'the re-check arms itself again');

  h.view.destroy();
  assert.equal(h.clock.timers.size, 0, 'destroy clears the pending re-check');
  assert.equal(h.layer.children.length, 0, 'destroy removes every owned node');
});

test('background view: no selection, unknown id and empty list all keep the sky', async t => {
  const h = setupView(t);
  assert.equal(h.sky().dataset.time, 'day', 'the sky follows the clock from the first paint');
  h.view.setList(LIST);
  assert.equal(h.shown().length, 0, 'no selection means artwork never loads');
  assert.equal(h.loads.length, 0);

  h.view.setSelection({ id: 'cafe', time: 'night' });
  await flush(h.loads);
  assert.equal(h.shown()[0].src, '/bg/cafe/night.png');
  assert.equal(h.sky().dataset.time, 'night');

  h.view.setSelection({ id: 'not_in_list', time: 'night' });
  assert.equal(h.shown().length, 0, 'an unknown id drops back to the sky');
  assert.equal(h.sky().dataset.time, 'night');

  h.view.setSelection({ id: 'cafe', time: 'midnight' });
  assert.equal(h.loads.length, 2, 'an invalid time is treated as the auto default');
  await flush(h.loads);
  assert.equal(h.shown()[0].src, '/bg/cafe/day.png', 'auto is day at 10:00');

  h.view.setList([]);
  assert.equal(h.shown().length, 0, 'an empty list falls back to the sky');
  assert.equal(h.layer.querySelectorAll('.adv-bg-image').length, 2, 'the buffers stay for the next fade');
});

test('background view: a failed decode keeps the previous artwork', async t => {
  const h = setupView(t);
  h.view.setList(LIST);
  h.view.setSelection({ id: 'cafe', time: 'day' });
  await flush(h.loads);
  const first = h.shown()[0];

  h.view.setSelection({ id: 'beach', time: 'day' });
  assert.equal(h.loads.length, 2);
  await flush(h.loads, 'reject');
  assert.deepEqual(h.shown(), [first], 'the old image stays visible');

  // An explicit sky choice still takes over.
  h.view.setSelection({ id: '', time: 'day' });
  assert.equal(h.shown().length, 0);
});

test('background view: prefers-reduced-motion shows artwork without a crossfade', async t => {
  const h = setupView(t, { reducedMotion: true });
  h.view.setList(LIST);
  h.view.setSelection({ id: 'cafe', time: 'day' });
  await flush(h.loads);
  assert.equal(h.shown()[0].classList.contains('is-instant'), true);
  assert.equal(h.shown()[0].classList.contains('is-visible'), true);

  h.reduced.matches = false;
  h.view.setSelection({ id: 'cafe', time: 'night' });
  await flush(h.loads);
  assert.equal(h.shown()[0].classList.contains('is-instant'), false, 'restored motion fades again');

  h.reduced.matches = true;
  h.view.setSelection({ id: 'cafe', time: 'day' });
  await flush(h.loads);
  assert.equal(h.shown()[0].classList.contains('is-instant'), true);
  assert.equal(h.shown()[0].classList.contains('is-visible'), true);
});

test('background.css: cover fit, 400ms crossfade, sky gradient per time, no backdrop-filter', () => {
  const css = readFileSync(new URL('../frontend/src/adv/background.css', import.meta.url), 'utf8');
  assert.match(css, /object-fit:\s*cover/);
  assert.match(css, /transition:\s*opacity\s+400ms/);
  for (const time of ['day', 'evening', 'night']) {
    assert.match(css, new RegExp(`\\.adv-bg-sky\\[data-time="${time}"\\][^{]*\\{[^}]*linear-gradient`));
  }
  assert.match(css, /\.is-instant[^{]*\{[^}]*transition:\s*none/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
  assert.equal(/backdrop-filter\s*:/.test(css), false);
});

// ---------------------------------------------------------------------------
// 3. 背景 panel
// ---------------------------------------------------------------------------

test('bg panel: a tile click previews at once and saves both settings', t => {
  const h = setupPanel(t);
  h.panel.open();
  assert.equal(h.panel.isOpen, true);
  assert.equal(h.container.querySelector('.adv-bg-overlay'), h.panel.element);
  assert.equal(h.panel.element.getAttribute('hidden'), null);

  const tiles = h.tiles();
  assert.equal(tiles.length, LIST.length + 1, 'a 无（天空） tile plus every background');
  assert.ok(tiles.every(tile => tile.tagName === 'BUTTON'));
  assert.equal(tiles[0].dataset.background, '');
  assert.equal(h.nameOf(tiles[0]), '无（天空）');
  assert.equal(h.nameOf(tiles[1]), '咖啡店');

  tiles[1].click();
  assert.deepEqual(h.saves.at(-1), { advBackground: 'cafe', advBgTime: 'auto' });
  assert.deepEqual(h.previews.at(-1), { id: 'cafe', time: 'auto' });
  assert.equal(tiles[1].getAttribute('aria-pressed'), 'true');
  assert.equal(tiles[0].getAttribute('aria-pressed'), 'false');
  assert.equal(tiles[2].getAttribute('aria-pressed'), 'false');
  assert.equal(h.thumbOf(tiles[1]).src, '/bg/cafe/day.png', 'the thumb uses the live variant');

  const evening = h.times().find(button => button.dataset.time === 'evening');
  evening.click();
  assert.deepEqual(h.saves.at(-1), { advBackground: 'cafe', advBgTime: 'evening' });
  assert.equal(evening.getAttribute('aria-pressed'), 'true');
  assert.equal(h.times().find(button => button.dataset.time === 'auto').getAttribute('aria-pressed'), 'false');
  assert.equal(h.thumbOf(h.tiles()[1]).src, '/bg/cafe/evening.png');
  assert.equal(h.tiles()[1].getAttribute('aria-pressed'), 'true', 'the tile stays selected');

  const onsen = h.tiles()[3];
  assert.equal(onsen.dataset.background, 'onsen');
  assert.equal(h.thumbOf(onsen).src, '/bg/onsen/night.png', 'the only variant wins after fallback');
  onsen.click();
  assert.deepEqual(h.saves.at(-1), { advBackground: 'onsen', advBgTime: 'evening' });

  h.tiles()[0].click();
  assert.deepEqual(h.saves.at(-1), { advBackground: '', advBgTime: 'evening' });
  assert.equal(h.tiles()[0].getAttribute('aria-pressed'), 'true');
  assert.equal(h.panel.selection.id, '');
});

test('bg panel: labels, time choices and the stored selection when it opens', t => {
  const h = setupPanel(t, { selection: { id: 'beach', time: 'night' } });
  h.panel.open();
  assert.deepEqual(h.times().map(button => button.textContent), ['自动', '白天', '傍晚', '夜晚']);
  assert.deepEqual(h.times().map(button => button.dataset.time), BG_TIME_CHOICES);
  assert.equal(h.times()[3].getAttribute('aria-pressed'), 'true');
  assert.equal(h.tiles()[2].getAttribute('aria-pressed'), 'true');
  assert.equal(h.thumbOf(h.tiles()[2]).src, '/bg/beach/night.png');
  assert.equal(h.tiles()[0].querySelector('.adv-bg-swatch').dataset.time, 'night');
  assert.equal(h.panel.element.getAttribute('aria-label'), '舞台背景');
  assert.equal(h.panel.dialog.querySelector('.adv-bg-title').textContent, '背景');
  assert.equal(h.panel.dialog.querySelector('.adv-bg-close').getAttribute('aria-label'), '关闭背景设置');
  assert.equal(h.doc.activeElement, h.tiles()[2], 'focus starts on the selected tile');
});

test('bg panel: Esc, the close button and the backdrop all end the panel', t => {
  const h = setupPanel(t);
  h.panel.open();
  h.panel.dialog.dispatchEvent({ type: 'keydown', key: 'Escape' });
  assert.equal(h.panel.isOpen, false);
  assert.equal(h.panel.element.getAttribute('hidden'), '');
  assert.deepEqual(h.opens, ['open', 'close']);

  h.panel.open();
  h.panel.dialog.querySelector('.adv-bg-close').click();
  assert.equal(h.panel.isOpen, false);

  h.panel.open();
  h.panel.element.querySelector('.adv-bg-backdrop').click();
  assert.equal(h.panel.isOpen, false);
  assert.equal(h.saves.length, 0, 'closing without choosing saves nothing');
});

test('bg panel: an empty or failed list still offers the sky', t => {
  const h = setupPanel(t, { list: [] });
  h.panel.open();
  assert.equal(h.tiles().length, 1);
  assert.equal(h.tiles()[0].dataset.background, '');
  h.tiles()[0].click();
  assert.deepEqual(h.saves.at(-1), { advBackground: '', advBgTime: 'auto' });
});

test('bg panel: destroy drops its listeners and its node', t => {
  const h = setupPanel(t);
  h.panel.open();
  const tiles = h.tiles();
  h.panel.destroy();
  assert.equal(h.container.querySelector('.adv-bg-overlay'), null);
  assert.equal(h.panel.isOpen, false);
  assert.ok(tiles.every(tile => tile.onclick === null));
  for (const node of [h.panel.element, ...h.panel.element.querySelectorAll('button')]) {
    assert.ok([...node.listeners.values()].every(set => set.size === 0), `${node.className} leaked listeners`);
  }
});

// ---------------------------------------------------------------------------
// 4. adv_app wiring
// ---------------------------------------------------------------------------

test('adv wiring: quick menu 背景 opens the panel and paints the picked artwork', async t => {
  const h = setupApp(t);
  await microtasks();
  assert.equal(h.calls.fetches, 1, 'the list is requested once when ADV mode opens');
  const expected = resolveVariant(LIST[0], timeOfDay(new Date()));
  const button = h.app.stage.quickmenu.querySelector('.adv-qm-btn-background');
  assert.equal(button.querySelector('.adv-qm-text').textContent, '背景');
  assert.equal(button.dataset.action, 'background');

  assert.equal(h.shown().length, 0, 'no selection means the gradient sky');
  button.click();
  assert.equal(h.app.bgPanel.isOpen, true);
  assert.equal(h.app.stage.textbox.inert, true, 'the panel suspends the dialogue layer');

  const tiles = h.app.bgPanel.dialog.querySelectorAll('.adv-bg-tile');
  assert.equal(tiles.length, LIST.length + 1);
  tiles[1].click();
  await flush(h.loads);
  assert.deepEqual(h.saves.at(-1), { advBackground: 'cafe', advBgTime: 'auto' });
  assert.equal(h.shown().length, 1);
  assert.equal(h.shown()[0].src, expected.url);
  assert.equal(h.sky().dataset.time, expected.time);

  // Stage gestures stay dead while the panel is open.
  let advances = 0;
  const realAdvance = h.app.player.advance;
  h.app.player.advance = (...args) => { advances++; return realAdvance(...args); };
  h.app.stage.stage.dispatchEvent({ type: 'click', button: 0, target: h.app.stage.background });
  h.app.stage.stage.dispatchEvent({ type: 'keydown', key: 'Enter' });
  assert.deepEqual([advances, h.controller.sent.length], [0, 0], 'no advance from the panel');
  h.app.player.advance = realAdvance;

  h.app.stage.stage.dispatchEvent({ type: 'keydown', key: 'Escape' });
  assert.equal(h.app.bgPanel.isOpen, false);
  assert.equal(h.app.stage.textbox.inert, false);

  button.click();
  assert.equal(h.calls.fetches, 1, 'a later open reuses the cached list');
  assert.equal(h.app.bgPanel.dialog.querySelectorAll('.adv-bg-tile').length, LIST.length + 1);
  assert.equal(h.app.bgPanel.isOpen, true);
});

test('adv wiring: a failed background fetch leaves the sky and still offers the panel', async t => {
  const h = setupApp(t, { fetchBackgrounds: async () => { throw new Error('无法连接本机服务。'); } });
  await microtasks();
  assert.equal(h.calls.fetches, 1);
  assert.equal(h.sky().dataset.time, timeOfDay(new Date()));
  assert.equal(h.shown().length, 0);
  h.app.action('background');
  assert.equal(h.app.bgPanel.isOpen, true);
  const tiles = h.app.bgPanel.dialog.querySelectorAll('.adv-bg-tile');
  assert.equal(tiles.length, 1);
  tiles[0].click();
  assert.deepEqual(h.saves.at(-1), { advBackground: '', advBgTime: 'auto' });
  assert.equal(h.shown().length, 0);
});

test('adv wiring: switching sessions closes the panel and re-applies per session', async t => {
  const h = setupApp(t);
  await microtasks();
  h.app.action('background');
  h.app.bgPanel.dialog.querySelectorAll('.adv-bg-tile')[1].click();
  await flush(h.loads);
  assert.equal(h.shown().length, 1);

  h.app.action('settings');
  assert.equal(h.app.bgPanel.isOpen, false, 'settings takes the overlay slot');
  assert.equal(h.app.configPanel.isOpen, true);

  h.app.action('background');
  h.switchSession(1);
  assert.equal(h.app.bgPanel.isOpen, false, 'a session change ends the panel');
  assert.equal(h.shown().length, 0, 'the new session has no stored background');
  assert.equal(h.sky().dataset.time, timeOfDay(new Date()));

  h.app.action('background');
  assert.equal(h.app.bgPanel.dialog.querySelectorAll('.adv-bg-tile')[1].getAttribute('aria-pressed'), 'false');
  h.app.bgPanel.close();

  h.app.unmount();
  assert.equal(h.dom.root.children.length, 0, 'unmount removes the background layer');
  assert.equal(h.doc.body.querySelector('.adv-bg-overlay'), null);
});
