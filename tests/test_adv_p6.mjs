// P6b: stage background view, 背景 panel (per-work packs) and the adv_app wiring.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fakeDocument, fakeClock, Element, microtasks } from './helpers/adv_dom.mjs';
import {
  BG_TIME_CHOICES,
  createBackgroundView,
  effectiveBackgroundKey,
  normalizeBackgroundSelection,
  resolveVariant,
  timeOfDay,
} from '../frontend/src/adv/background.js';
import { createBgPanel } from '../frontend/src/adv/bg_panel.js';
import { mountAdv } from '../frontend/src/adv/adv_app.js';
import { pickSettings } from '../frontend/src/session_data.js';
import { DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';

// A background is addressed by its global "<work>/<id>" key; the panel filters by
// the active character's work and can reveal every work.
const LIST = [
  { key: 'cafe/stella', work: 'cafe', id: 'stella', name: '咖啡店', variants: {
    day: '/bg/cafe/day.png', evening: '/bg/cafe/evening.png', night: '/bg/cafe/night.png' } },
  { key: 'cafe/beach', work: 'cafe', id: 'beach', name: '海边', variants: {
    day: '/bg/beach/day.png', night: '/bg/beach/night.png' } },
  { key: 'onsen/hot', work: 'onsen', id: 'hot', name: '温泉', variants: { night: '/bg/onsen/night.png' } },
];
const WORKS = [{ id: 'cafe', name: '喫茶ステラ' }, { id: 'onsen', name: '温泉街' }];

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
    getWorks: () => (options.works === undefined ? WORKS : options.works),
    getWork: () => options.characterWork ?? null,
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
  const groups = () => panel.dialog.querySelectorAll('.adv-bg-group-title').map(node => node.textContent);
  const toggle = () => panel.dialog.querySelector('.adv-bg-show-others');
  const keys = () => tiles().map(tile => tile.dataset.background);
  const nameOf = tile => tile.querySelector('.adv-bg-name').textContent;
  const thumbOf = tile => tile.querySelector('.adv-bg-thumb');
  return { doc, container, panel, saves, previews, opens, tiles, times, groups, toggle, keys, nameOf, thumbOf };
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
    getCharacter: () => (options.characterWork ? { id: 'c', name: '角色', work: options.characterWork } : null),
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
      return { works: WORKS, backgrounds: LIST };
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
  assert.deepEqual(normalizeBackgroundSelection({ id: 'cafe/stella', time: 'midnight' }), { id: 'cafe/stella', time: 'auto' });
  assert.deepEqual(normalizeBackgroundSelection({ id: 7, time: 'night' }), { id: '', time: 'night' });
  assert.deepEqual(BG_TIME_CHOICES, ['auto', 'day', 'evening', 'night']);
});

// ---------------------------------------------------------------------------
// 2. Background view (keyed by <work>/<id>)
// ---------------------------------------------------------------------------

test('effectiveBackgroundKey: auto follows the work default, none is the sky, keys pass through', () => {
  const works = [{ id: 'cafe', name: '喫茶ステラ', default: 'cafe/beach' }, { id: 'onsen', name: '温泉街', default: null }];
  const at = (stored, work, list = LIST, workList = works) => effectiveBackgroundKey(stored, { list, works: workList, work });
  assert.equal(at('', 'cafe'), 'cafe/beach', 'the declared default');
  assert.equal(at('', 'cafe', LIST, WORKS), 'cafe/stella', 'no default: the first of the work');
  assert.equal(at('', 'cafe', LIST, [{ id: 'cafe', default: 'cafe/gone' }]), 'cafe/stella', 'a stale default: the first');
  assert.equal(at('', 'onsen'), 'onsen/hot');
  assert.equal(at('', 'riddle'), '', 'a work without a pack keeps the sky');
  assert.equal(at('', null), '', 'no character work keeps the sky');
  assert.equal(at('', 'cafe', null, null), '', 'no list yet');
  assert.equal(at('none', 'cafe'), '', 'none is the plain sky');
  assert.equal(at('onsen/hot', 'cafe'), 'onsen/hot', 'an explicit key wins over the work');
  assert.equal(at('cafe_hall', 'cafe'), 'cafe/beach', 'a bare id from before per-work packs is auto');
  assert.equal(at(undefined, 'cafe'), 'cafe/beach');
});

test('background view: waits for decode, then crossfades between two buffers', async t => {
  const h = setupView(t);
  h.view.setList(LIST);
  h.view.setSelection({ id: 'cafe/stella', time: 'day' });
  assert.equal(h.loads.length, 1);
  assert.equal(h.shown().length, 0, 'nothing fades in before decode settles');
  await flush(h.loads);
  assert.equal(h.shown().length, 1);
  assert.equal(h.shown()[0].src, '/bg/cafe/day.png');
  assert.equal(h.sky().dataset.time, 'day');
  assert.equal(h.layer.children.length, 3, 'one sky plus two image buffers');

  h.view.setSelection({ id: 'cafe/stella', time: 'evening' });
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

test('background view: looks a background up by its global key, not by id alone', async t => {
  const h = setupView(t);
  h.view.setList(LIST);
  // "beach" is a real id, but the map is keyed by the full "<work>/<id>".
  h.view.setSelection({ id: 'beach', time: 'day' });
  await microtasks();
  assert.equal(h.loads.length, 0, 'a bare id is not a key and never matches');
  h.view.setSelection({ id: 'cafe/beach', time: 'night' });
  await flush(h.loads);
  assert.equal(h.shown()[0].src, '/bg/beach/night.png');
});

test('background view: auto re-checks every 60s and swaps the variant after 16:00', async t => {
  const h = setupView(t, { hour: 15 });
  h.view.setList(LIST);
  h.view.setSelection({ id: 'cafe/stella', time: 'auto' });
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

test('background view: no selection, unknown key and empty list all keep the sky', async t => {
  const h = setupView(t);
  assert.equal(h.sky().dataset.time, 'day', 'the sky follows the clock from the first paint');
  h.view.setList(LIST);
  assert.equal(h.shown().length, 0, 'no selection means artwork never loads');
  assert.equal(h.loads.length, 0);

  h.view.setSelection({ id: 'cafe/stella', time: 'night' });
  await flush(h.loads);
  assert.equal(h.shown()[0].src, '/bg/cafe/night.png');
  assert.equal(h.sky().dataset.time, 'night');

  h.view.setSelection({ id: 'cafe/unknown', time: 'night' });
  assert.equal(h.shown().length, 0, 'an unknown key drops back to the sky');
  assert.equal(h.sky().dataset.time, 'night');

  h.view.setSelection({ id: 'cafe/stella', time: 'midnight' });
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
  h.view.setSelection({ id: 'cafe/stella', time: 'day' });
  await flush(h.loads);
  const first = h.shown()[0];

  h.view.setSelection({ id: 'cafe/beach', time: 'day' });
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
  h.view.setSelection({ id: 'cafe/stella', time: 'day' });
  await flush(h.loads);
  assert.equal(h.shown()[0].classList.contains('is-instant'), true);
  assert.equal(h.shown()[0].classList.contains('is-visible'), true);

  h.reduced.matches = false;
  h.view.setSelection({ id: 'cafe/stella', time: 'night' });
  await flush(h.loads);
  assert.equal(h.shown()[0].classList.contains('is-instant'), false, 'restored motion fades again');

  h.reduced.matches = true;
  h.view.setSelection({ id: 'cafe/stella', time: 'day' });
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
// 3. 背景 panel (global keys, per-work filtering)
// ---------------------------------------------------------------------------

test('bg panel: a tile click previews at once and saves both settings', t => {
  const h = setupPanel(t);
  h.panel.open();
  assert.equal(h.panel.isOpen, true);
  assert.equal(h.container.querySelector('.adv-bg-overlay'), h.panel.element);
  assert.equal(h.panel.element.getAttribute('hidden'), null);

  const tiles = h.tiles();
  assert.equal(tiles.length, LIST.length + 2, '自动 and 无（天空） tiles plus every background');
  assert.ok(tiles.every(tile => tile.tagName === 'BUTTON'));
  assert.equal(tiles[0].dataset.background, '');
  assert.equal(h.nameOf(tiles[0]), '自动（本作）');
  assert.equal(tiles[0].getAttribute('aria-pressed'), 'true', 'an unset session is on 自动');
  assert.equal(tiles[1].dataset.background, 'none');
  assert.equal(h.nameOf(tiles[1]), '无（天空）');
  assert.equal(tiles[2].dataset.background, 'cafe/stella');
  assert.equal(h.nameOf(tiles[2]), '咖啡店');

  tiles[2].click();
  assert.deepEqual(h.saves.at(-1), { advBackground: 'cafe/stella', advBgTime: 'auto' });
  assert.deepEqual(h.previews.at(-1), { id: 'cafe/stella', time: 'auto' });
  assert.equal(tiles[2].getAttribute('aria-pressed'), 'true');
  assert.equal(tiles[0].getAttribute('aria-pressed'), 'false');
  assert.equal(tiles[3].getAttribute('aria-pressed'), 'false');
  assert.equal(h.thumbOf(tiles[2]).src, '/bg/cafe/day.png', 'the thumb uses the live variant');

  const evening = h.times().find(button => button.dataset.time === 'evening');
  evening.click();
  assert.deepEqual(h.saves.at(-1), { advBackground: 'cafe/stella', advBgTime: 'evening' });
  assert.equal(evening.getAttribute('aria-pressed'), 'true');
  assert.equal(h.times().find(button => button.dataset.time === 'auto').getAttribute('aria-pressed'), 'false');
  assert.equal(h.thumbOf(h.tiles()[2]).src, '/bg/cafe/evening.png');
  assert.equal(h.tiles()[2].getAttribute('aria-pressed'), 'true', 'the tile stays selected');

  const onsen = h.tiles()[4];
  assert.equal(onsen.dataset.background, 'onsen/hot');
  assert.equal(h.thumbOf(onsen).src, '/bg/onsen/night.png', 'the only variant wins after fallback');
  onsen.click();
  assert.deepEqual(h.saves.at(-1), { advBackground: 'onsen/hot', advBgTime: 'evening' });

  h.tiles()[1].click();
  assert.deepEqual(h.saves.at(-1), { advBackground: 'none', advBgTime: 'evening' });
  assert.equal(h.tiles()[1].getAttribute('aria-pressed'), 'true');
  assert.equal(h.panel.selection.id, 'none');

  h.tiles()[0].click();
  assert.deepEqual(h.saves.at(-1), { advBackground: '', advBgTime: 'evening' });
  assert.equal(h.tiles()[0].getAttribute('aria-pressed'), 'true');
  assert.equal(h.panel.selection.id, '');
});

test('bg panel: labels, time choices and the stored key when it opens', t => {
  const h = setupPanel(t, { selection: { id: 'cafe/beach', time: 'night' } });
  h.panel.open();
  assert.deepEqual(h.times().map(button => button.textContent), ['自动', '白天', '傍晚', '夜晚']);
  assert.deepEqual(h.times().map(button => button.dataset.time), BG_TIME_CHOICES);
  assert.equal(h.times()[3].getAttribute('aria-pressed'), 'true');
  assert.equal(h.tiles()[3].getAttribute('aria-pressed'), 'true');
  assert.equal(h.thumbOf(h.tiles()[3]).src, '/bg/beach/night.png');
  assert.equal(h.tiles()[1].querySelector('.adv-bg-swatch').dataset.time, 'night');
  assert.equal(h.tiles()[0].querySelector('.adv-bg-swatch').dataset.time, 'night');
  assert.equal(h.panel.element.getAttribute('aria-label'), '舞台背景');
  assert.equal(h.panel.dialog.querySelector('.adv-bg-title').textContent, '背景');
  assert.equal(h.panel.dialog.querySelector('.adv-bg-close').getAttribute('aria-label'), '关闭背景设置');
  assert.equal(h.doc.activeElement, h.tiles()[3], 'focus starts on the selected tile');
});

test('bg panel: filters to the active work and 显示其他作品 reveals every work grouped', t => {
  const h = setupPanel(t, { characterWork: 'cafe' });
  h.panel.open();
  assert.deepEqual(h.keys(), ['', 'none', 'cafe/stella', 'cafe/beach'], 'only the character work');
  const autoSwatch = h.tiles()[0].querySelector('.adv-bg-swatch');
  assert.equal(autoSwatch.style.backgroundImage, 'url("/bg/cafe/day.png")', '自动 previews the work default');
  assert.equal(autoSwatch.classList.contains('has-image'), true);
  assert.equal(h.tiles()[1].querySelector('.adv-bg-swatch').style.backgroundImage ?? '', '', 'the sky has no image');
  assert.equal(h.groups().length, 0, 'a single work needs no headings');
  const toggle = h.toggle();
  assert.equal(toggle.textContent, '显示其他作品');
  assert.equal(toggle.getAttribute('aria-pressed'), 'false');
  assert.equal(h.saves.length, 0);

  toggle.click();
  assert.equal(toggle.getAttribute('aria-pressed'), 'true');
  assert.deepEqual(h.keys(), ['', 'none', 'cafe/stella', 'cafe/beach', 'onsen/hot']);
  assert.deepEqual(h.groups(), ['喫茶ステラ', '温泉街'], 'each work is titled');
  assert.equal(h.saves.length, 0, 'revealing other works never saves');
});

test('bg panel: a character with no work lists every background grouped under its work', t => {
  const h = setupPanel(t);
  h.panel.open();
  assert.deepEqual(h.keys(), ['', 'none', 'cafe/stella', 'cafe/beach', 'onsen/hot']);
  assert.deepEqual(h.groups(), ['喫茶ステラ', '温泉街']);
  assert.equal(h.toggle().hidden, true, 'nothing else to reveal');
  assert.equal(h.tiles()[0].querySelector('.adv-bg-swatch').classList.contains('has-image'), false,
    '自动 without a character work is the sky');
});

test('bg panel: a work with no pack yet lists every work instead of an empty grid', t => {
  const h = setupPanel(t, { characterWork: 'riddle' });
  h.panel.open();
  assert.deepEqual(h.keys(), ['', 'none', 'cafe/stella', 'cafe/beach', 'onsen/hot']);
  assert.equal(h.toggle().hidden, true);
});

test('bg panel: a stored background of another work opens with every work shown', t => {
  const h = setupPanel(t, { characterWork: 'cafe', selection: { id: 'onsen/hot', time: 'auto' } });
  h.panel.open();
  assert.equal(h.toggle().getAttribute('aria-pressed'), 'true');
  assert.equal(h.toggle().hidden, false);
  const onsen = h.tiles().find(tile => tile.dataset.background === 'onsen/hot');
  assert.equal(onsen.getAttribute('aria-pressed'), 'true');
});

test('bg panel: one work in the list never shows group headings', t => {
  const h = setupPanel(t, { list: [LIST[0]], works: [{ id: 'cafe', name: '喫茶ステラ' }] });
  h.panel.open();
  assert.deepEqual(h.keys(), ['', 'none', 'cafe/stella']);
  assert.equal(h.groups().length, 0, 'a lone work needs no title');
});

test('bg panel: reopening resets 显示其他作品 to the character work', t => {
  const h = setupPanel(t, { characterWork: 'cafe' });
  h.panel.open();
  h.toggle().click();
  assert.equal(h.tiles().length, LIST.length + 2);
  h.panel.close();
  h.panel.open();
  assert.equal(h.toggle().getAttribute('aria-pressed'), 'false');
  assert.deepEqual(h.keys(), ['', 'none', 'cafe/stella', 'cafe/beach']);
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

test('bg panel: an empty or failed list still offers 自动 and the sky', t => {
  const h = setupPanel(t, { list: [] });
  h.panel.open();
  assert.deepEqual(h.keys(), ['', 'none']);
  h.tiles()[1].click();
  assert.deepEqual(h.saves.at(-1), { advBackground: 'none', advBgTime: 'auto' });
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
  assert.equal(tiles.length, LIST.length + 2);
  tiles[2].click();
  await flush(h.loads);
  assert.deepEqual(h.saves.at(-1), { advBackground: 'cafe/stella', advBgTime: 'auto' });
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
  assert.equal(h.app.bgPanel.dialog.querySelectorAll('.adv-bg-tile').length, LIST.length + 2);
  assert.equal(h.app.bgPanel.isOpen, true);
});

test('adv wiring: the 背景 panel follows the active character work', async t => {
  const h = setupApp(t, { characterWork: 'cafe' });
  await microtasks();
  h.app.action('background');
  const panelTiles = () => h.app.bgPanel.dialog.querySelectorAll('.adv-bg-tile');
  assert.deepEqual(panelTiles().map(tile => tile.dataset.background), ['', 'none', 'cafe/stella', 'cafe/beach']);
  h.app.bgPanel.dialog.querySelector('.adv-bg-show-others').click();
  assert.equal(panelTiles().length, LIST.length + 2, 'the toggle reveals the other work too');
});

test('adv wiring: an unset background shows the work default once the list arrives', async t => {
  const works = [{ ...WORKS[0], default: 'cafe/beach' }, WORKS[1]];
  const h = setupApp(t, { characterWork: 'cafe', fetchBackgrounds: async () => ({ works, backgrounds: LIST }) });
  await microtasks();
  await flush(h.loads);
  assert.equal(h.shown().length, 1);
  assert.equal(h.shown()[0].src, resolveVariant(LIST[1], timeOfDay(new Date())).url);
  assert.equal(h.saves.length, 0, 'auto never writes a background into the session');
  assert.equal(h.app.backgroundUrl('', 'night', 'cafe'), '/bg/beach/night.png', 'title thumbnails resolve auto too');
  assert.equal(h.app.backgroundUrl('none', 'night', 'cafe'), '');
  assert.equal(h.app.backgroundUrl('onsen/hot', 'night', 'cafe'), '/bg/onsen/night.png');

  h.app.action('background');
  const tiles = h.app.bgPanel.dialog.querySelectorAll('.adv-bg-tile');
  assert.equal(tiles[0].getAttribute('aria-pressed'), 'true', '自动 is the stored choice');
  tiles[1].click();
  assert.deepEqual(h.saves.at(-1), { advBackground: 'none', advBgTime: 'auto' });
  assert.equal(h.shown().length, 0, '无（天空） hides the artwork');
  tiles[0].click();
  await flush(h.loads);
  assert.equal(h.shown()[0].src, resolveVariant(LIST[1], timeOfDay(new Date())).url, '自动 brings the default back');
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
  assert.equal(tiles.length, 2);
  tiles[1].click();
  assert.deepEqual(h.saves.at(-1), { advBackground: 'none', advBgTime: 'auto' });
  assert.equal(h.shown().length, 0);
});

test('adv wiring: switching sessions closes the panel and re-applies per session', async t => {
  const h = setupApp(t);
  await microtasks();
  h.app.action('background');
  h.app.bgPanel.dialog.querySelectorAll('.adv-bg-tile')[2].click();
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
  assert.equal(h.app.bgPanel.dialog.querySelectorAll('.adv-bg-tile')[2].getAttribute('aria-pressed'), 'false');
  h.app.bgPanel.close();

  h.app.unmount();
  assert.equal(h.dom.root.children.length, 0, 'unmount removes the background layer');
  assert.equal(h.doc.body.querySelector('.adv-bg-overlay'), null);
});
