import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { createStage } from '../frontend/src/adv/stage.js';
import { createBackgroundView, selectionUrl } from '../frontend/src/adv/background.js';
import { createSpriteTransition } from '../frontend/src/adv/transitions.js';
import { createShortcuts } from '../frontend/src/adv/shortcuts.js';
import { createTextbox } from '../frontend/src/adv/textbox.js';
import { showTitle } from '../frontend/src/adv/title_hook.js';
import { mountAdv } from '../frontend/src/adv/adv_app.js';
import { createSpriteView } from '../frontend/src/adv/sprite.js';
import { DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { Element, fakeDocument, fakeClock, microtasks } from './helpers/adv_dom.mjs';

function media(matches = false) {
  const listeners = new Set();
  return { matches, listeners,
    addEventListener: (_, fn) => listeners.add(fn), removeEventListener: (_, fn) => listeners.delete(fn),
    rotate(value) { this.matches = value; for (const fn of listeners) fn({ matches: value }); },
  };
}
function documentFixture() { const doc = fakeDocument(); globalThis.document = doc; return doc; }
function animateFixture(element, clock) {
  const animations = [];
  element.animate = (frames, options) => {
    let resolve, reject;
    const finished = new Promise((yes, no) => { resolve = yes; reject = no; });
    const timer = clock.setTimeout(resolve, options.duration);
    const animation = { frames, options, finished, cancelled: false,
      cancel() { this.cancelled = true; clock.clearTimeout(timer); reject(new Error('cancelled')); } };
    animations.push(animation); return animation;
  };
  return animations;
}

test('P8 portrait mount and live rotation', () => {
  const doc = documentFixture(), mql = media(true), queries = [];
  const view = createStage(doc.body, { matchMedia: query => { queries.push(query); return mql; } });
  assert.deepEqual(queries, ['(orientation: portrait)']);
  assert.ok(view.stage.classList.contains('is-portrait'));
  mql.rotate(false); assert.ok(!view.stage.classList.contains('is-portrait'));
  mql.rotate(true); assert.ok(view.stage.classList.contains('is-portrait'));
  view.destroy(); assert.equal(mql.listeners.size, 0);
});

test('P8 landscape class and portrait units', () => {
  const doc = documentFixture();
  const view = createStage(doc.body, { matchMedia: () => media(false) });
  assert.equal(view.stage.className, 'adv-stage'); view.destroy();
  const css = readFileSync(new URL('../frontend/src/adv/portrait.css', import.meta.url), 'utf8');
  const adv = readFileSync(new URL('../frontend/src/adv/adv.css', import.meta.url), 'utf8');
  assert.match(adv, /@import '\.\/portrait\.css'/);
  const rule = css.match(/\.adv-stage\.is-portrait\s*\{([^}]+)\}/)[1];
  const units = rule.match(/--u:\s*min\(calc\(100vw \/ (\d+)\), calc\(100dvh \/ (\d+)\)\)/);
  assert.ok(units, 'parse the actual portrait --u declaration');
  const width = Number(units[1]), height = Number(units[2]);
  assert.deepEqual([width, height], [1080, 1920]);
  const u = Math.min(390 / width, 844 / height);
  assert.equal(u * 1080, 390, 'a 390x844 phone is width-bound');
  assert.ok(u * 1920 <= 844, 'the 1080x1920 safe area fits on the phone');
  assert.match(rule, /width: 100vw;/, 'the portrait stage fills the screen instead of letterboxing');
  assert.match(rule, /height: 100dvh;/);
  assert.match(adv, /--u: min\(calc\(100vw \/ 1920\), calc\(100dvh \/ 1080\)\)/);
});

test('P8 portrait wrapped pagination', () => {
  const doc = documentFixture(), stage = createStage(doc.body, { matchMedia: () => media(true) });
  const box = createTextbox(stage.textbox);
  const column = stage.textbox.querySelector('.adv-text-column');
  const measurer = stage.textbox.querySelector('.adv-text-measurer');
  const primary = stage.textbox.querySelector('.adv-primary-text');
  column.getBoundingClientRect = () => ({ width: 100 });
  primary.getBoundingClientRect = () => ({ height: 60 });
  measurer.getBoundingClientRect = () => ({ width: 100, height: Math.ceil(measurer.textContent.length / 5) * 20 });
  const pages = box.paginate({ mode: 'player' }, 'abcdefghijklmnopqrst');
  assert.ok(pages.length > 1); assert.equal(pages.join(''), 'abcdefghijklmnopqrst');
  stage.stage.classList.remove('is-portrait');
  assert.equal(box.paginate({ mode: 'player' }, 'abcdefghijklmnopqrst').length, 1);
  box.destroy(); stage.destroy();
});

function appFixture(t, reduced = false, costumeA = {}) {
  const doc = documentFixture(), clock = fakeClock(), events = new Map();
  const portraitMedia = media(false), motionMedia = media(reduced);
  let character = { id: 1, name: 'Synthetic' };
  const session = { id: 'p8', settings: { ...DEFAULT_SESSION_SETTINGS }, messages: [] };
  class Image extends Element { constructor() { super('img', doc); this.complete = true; } decode() { return Promise.resolve(); } }
  const pack = id => ({ enabled: true, defaultCostume: 'a',
    costumes: ['a', 'b'].map(id => ({ id, ...(id === 'a' ? costumeA : {}) })),
    faces: { smile: { primary: 'gentle' }, shy: { primary: 'shy' } },
    expressionSets: { gentle: [{ id: 'smile' }], shy: [{ id: 'shy' }] },
    sprites: Object.fromEntries(['a', 'b'].map(outfit => [outfit,
      Object.fromEntries(['smile', 'shy'].map(face => [face, { file: `/${id}/${outfit}/${face}.png`, width: 800, height: 1600 }]))])) });
  const app = mountAdv({ root: doc.body }, {
    clock, raf: fn => clock.setTimeout(fn, 0), cancelRaf: id => clock.clearTimeout(id),
    controller: { busy: false, stop() {}, canRetry() { return false; },
      on(type, fn) { events.set(type, fn); return () => events.delete(type); } },
    audio: { stopLine() {} }, getSession: () => session, getCharacter: () => character,
    voiceEnabled: () => false, ImageClass: Image, loadPortrait: async id => pack(id),
    fetchBackgrounds: async () => ({ backgrounds: [] }),
    matchMedia: query => query === '(orientation: portrait)' ? portraitMedia : motionMedia,
  });
  const animations = animateFixture(app.stage.sprite, clock);
  const flush = async () => { await microtasks(); clock.tick(0); await microtasks(); };
  t.after(() => app.unmount());
  return { app, clock, animations, flush, session, events, portraitMedia, motionMedia,
    character(id) { character = { id, name: 'Synthetic' }; app.update(); } };
}

test('P8 portrait sprite offset follows --sprite-cx', () => {
  const css = readFileSync(new URL('../frontend/src/adv/portrait.css', import.meta.url), 'utf8');
  const rule = css.match(/\.adv-stage\.is-portrait \.adv-layer-fx\s*\{([^}]+)\}/)[1];
  const left = rule.match(/left: calc\(50% - \((\d+) \+ ([\d.]+) \* \(var\(--sprite-cx, 960\) - 960\)\) \* var\(--u\)\)/);
  assert.ok(left, 'portrait left is computed from --sprite-cx');
  const scale = Number(rule.match(/scale: ([\d.]+);/)[1]);
  assert.equal(Number(left[1]), 960, 'the wrapper scales around left + 960u, which lands on 50%');
  assert.equal(Number(left[2]), scale, 'the offset grows with the wrapper scale');
});

test('P8 sprite publishes its landscape centre for the portrait layout', async t => {
  const h = appFixture(t); await h.flush();
  // Legacy framing of 800x1600 art: 540u wide from x=830, so the body centre is x=1100.
  assert.equal(h.app.stage.sprite.style['--sprite-cx'], '1100');
  assert.equal(h.app.stage.fx.style['--sprite-cx'], '1100');
});

test('P8 stage-override sprites centre on the head anchor', async t => {
  const h = appFixture(t, false, { stage: { scale: 0.5, offset: { x: 600, y: 0 } }, headAnchor: { x: 300, y: 100 } });
  await h.flush();
  assert.equal(h.app.stage.sprite.style['--sprite-cx'], '750');
  assert.equal(h.app.stage.fx.style['--sprite-cx'], '750');
});

test('P8 app orientation wiring', async t => {
  const h = appFixture(t); await h.flush();
  h.portraitMedia.rotate(true); assert.ok(h.app.stage.stage.classList.contains('is-portrait'));
});

for (const kind of ['character', 'outfit']) {
  test(`P8 ${kind} sprite fade 250ms`, async t => {
    const h = appFixture(t); await h.flush();
    assert.equal(h.animations.length, 0, 'initial art does not fade');
    const before = h.app.sprite.getLastCommitted().faceUrl;
    if (kind === 'character') h.character(2);
    else { h.session.settings.advOutfit = 'b'; h.app.update(); }
    assert.equal(h.animations.length, 1);
    assert.deepEqual(h.animations[0].frames, [{ opacity: 1 }, { opacity: 0 }]);
    assert.equal(h.animations[0].options.duration, 125);
    assert.equal(h.app.sprite.getLastCommitted().faceUrl, before);
    h.clock.tick(124); await h.flush(); assert.equal(h.animations.length, 1);
    h.clock.tick(1); await h.flush();
    assert.notEqual(h.app.sprite.getLastCommitted().faceUrl, before);
    assert.deepEqual(h.animations[1].frames, [{ opacity: 0 }, { opacity: 1 }]);
    assert.equal(h.animations[1].options.duration, 125);
    h.clock.tick(125); await h.flush();
    if (kind === 'outfit') {
      h.session.settings.advOutfit = 'a'; h.app.update();
      h.session.settings.advOutfit = 'b'; h.app.update();
      h.clock.tick(500); await h.flush();
      assert.equal(h.app.sprite.getLastCommitted().state.outfit, 'b', 'revert cancels an uncommitted outfit');
    }
  });
  test(`P8 ${kind} reduced-motion bypass`, async t => {
    const h = appFixture(t, true); await h.flush();
    const before = h.app.sprite.getLastCommitted().faceUrl;
    if (kind === 'character') h.character(2);
    else { h.session.settings.advOutfit = 'b'; h.app.update(); }
    await h.flush();
    assert.notEqual(h.app.sprite.getLastCommitted().faceUrl, before);
    assert.equal(h.animations.length, 0);
    h.motionMedia.rotate(false);
    if (kind === 'character') h.character(3);
    else { h.session.settings.advOutfit = 'a'; h.app.update(); }
    assert.equal(h.animations.length, 1, 'turning reduced motion off restores the fade');
  });
}

test('P8 transition cancellation', async () => {
  const doc = documentFixture(), clock = fakeClock(), changes = [];
  const animations = animateFixture(doc.body, clock), transition = createSpriteTransition(doc.body);
  const a = transition.change(() => changes.push('a'));
  const b = transition.change(() => changes.push('b'));
  assert.ok(animations[0].cancelled);
  clock.tick(125); await microtasks(); assert.deepEqual(changes, ['b']);
  transition.destroy(); await Promise.all([a, b]);
  assert.ok(animations.every(animation => animation.cancelled));
});

test('P8 background variant preload once', async () => {
  const doc = documentFixture(), clock = fakeClock(), images = [], decoded = [];
  class Image extends Element {
    constructor() { super('img', doc); images.push(this); }
    decode() { decoded.push(this.src); return Promise.resolve(); }
  }
  const layer = doc.createElement('div');
  const view = createBackgroundView({ layer, ImageClass: Image,
    setTimer: (fn, ms) => clock.setTimeout(fn, ms), clearTimer: id => clock.clearTimeout(id) });
  const list = [{ key: 'w/cafe', variants: { day: '/day', evening: '/evening', night: '/night' } }];
  view.setSelection({ id: 'w/cafe', time: 'day' }); view.setList(list); await microtasks();
  const preloads = () => images.filter(image => image.parentNode !== layer);
  assert.deepEqual(preloads().map(image => image.src).sort(), ['/day', '/evening', '/night']);
  assert.ok(decoded.includes('/night'), 'unshown variant is decoded');
  view.setSelection({ id: 'w/cafe', time: 'night' }); await microtasks();
  view.setSelection({ id: '', time: 'day' }); view.setSelection({ id: 'w/cafe', time: 'evening' });
  view.setList(list); await microtasks();
  assert.equal(preloads().length, 3);
  const retained = preloads(); view.destroy(); assert.ok(retained.every(image => image.src === ''));
});

test('P8 background preload keeps only the chosen background', async () => {
  const doc = documentFixture(), clock = fakeClock(), images = [];
  class Image extends Element { constructor() { super('img', doc); images.push(this); } decode() { return Promise.resolve(); } }
  const layer = doc.createElement('div');
  const view = createBackgroundView({ layer, ImageClass: Image,
    setTimer: (fn, ms) => clock.setTimeout(fn, ms), clearTimer: id => clock.clearTimeout(id) });
  const variants = id => ({ day: `/${id}/day`, evening: `/${id}/evening`, night: `/${id}/night` });
  view.setList([{ key: 'w/cafe', variants: variants('cafe') }, { key: 'w/park', variants: variants('park') }]);
  view.setSelection({ id: 'w/cafe', time: 'day' }); await microtasks();
  const cafe = images.filter(image => image.parentNode !== layer && image.src.startsWith('/cafe/'));
  assert.equal(cafe.length, 3);
  view.setSelection({ id: 'w/park', time: 'day' }); await microtasks();
  assert.ok(cafe.every(image => image.src === ''), 'switching backgrounds releases the old preloads');
  const live = images.filter(image => image.parentNode !== layer && image.src);
  assert.deepEqual(live.map(image => image.src).sort(), ['/park/day', '/park/evening', '/park/night']);
  view.destroy();
});

test('P8 title thumbnails resolve a stored background to its image URL', () => {
  const list = [{ key: 'w/cafe', variants: { day: '/cafe/day', night: '/cafe/night' } }];
  const noon = new Date(2026, 9, 21, 12), late = new Date(2026, 9, 21, 23);
  assert.equal(selectionUrl(list, { id: 'w/cafe', time: 'auto' }, noon), '/cafe/day');
  assert.equal(selectionUrl(list, { id: 'w/cafe', time: 'auto' }, late), '/cafe/night');
  assert.equal(selectionUrl(list, { id: 'w/cafe', time: 'night' }, noon), '/cafe/night');
  assert.equal(selectionUrl(list, { id: 'gone', time: 'day' }, noon), '');
  assert.equal(selectionUrl(list, { id: '', time: 'day' }, noon), '');
  assert.equal(selectionUrl(null, { id: 'w/cafe', time: 'day' }, noon), '');
});

for (const reduced of [false, true]) test(`P8 title ${reduced ? 'reduced-motion bypass' : 'fade 300ms'}`, async () => {
  const doc = documentFixture(), clock = fakeClock(), calls = [], mql = media(reduced);
  const overlay = doc.createElement('div'); overlay.className = 'adv-layer-overlay'; doc.body.appendChild(overlay);
  const title = showTitle({ startNew: () => calls.push('start'), listSessions: () => [], getStatus: () => ({ model: { state: 'ok' }, modelLoaded: true, engine: { state: 'ready' } }) }, {
    documentObj: doc, matchMedia: () => mql,
  });
  await microtasks();
  const animations = animateFixture(title.element, clock);
  title.element.querySelector('.home-continue').click();
  assert.deepEqual(calls, ['start']); assert.equal(title.isOpen, false);
  if (reduced) {
    assert.equal(animations.length, 0); assert.equal(title.element.hidden, true);
    mql.rotate(false); title.open();
    title.element.querySelector('.home-continue').click();
    assert.equal(animations.length, 1, 'normal motion must fade');
  }
  else {
    assert.equal(title.element.hidden, false); assert.equal(title.element.inert, true);
    assert.equal(animations[0].options.duration, 300);
    assert.deepEqual(animations[0].frames, [{ opacity: 1 }, { opacity: 0 }]);
    clock.tick(299); await microtasks(); assert.equal(title.element.hidden, false);
    clock.tick(1); await microtasks(); assert.equal(title.element.hidden, true);
  }
  title.destroy();
});

test('P8 touch long-press guards', () => {
  const doc = documentFixture(), clock = fakeClock(), actions = [], stage = doc.createElement('div');
  let overlayOpen = false;
  const shortcuts = createShortcuts(stage, { clock, getContext: () => ({ overlayOpen }), onAction: action => actions.push(action) });
  const down = target => stage.dispatchEvent({ type: 'pointerdown', pointerType: 'touch', clientX: 0, clientY: 0, target });
  down(stage); clock.tick(499); assert.deepEqual(actions, []);
  clock.tick(1); assert.deepEqual(actions, ['toggle_hide']);
  stage.dispatchEvent({ type: 'pointerup' }); stage.click();
  assert.deepEqual(actions, ['toggle_hide'], 'long press must not also advance');
  down(stage); stage.dispatchEvent({ type: 'pointermove', clientX: 20, clientY: 0 }); clock.tick(1000);
  assert.equal(actions.length, 1);
  const button = doc.createElement('button'); stage.appendChild(button); down(button); clock.tick(1000);
  assert.equal(actions.length, 1);
  down(stage); clock.tick(100); stage.dispatchEvent({ type: 'contextmenu', target: stage }); clock.tick(1000);
  assert.deepEqual(actions, ['toggle_hide', 'toggle_hide'], 'native long-press contextmenu cancels the custom timer');
  stage.click(); assert.equal(actions.length, 2);
  overlayOpen = true; down(stage); clock.tick(1000); assert.equal(actions.length, 2);
  overlayOpen = false; down(stage); shortcuts.destroy(); clock.tick(1000); assert.equal(actions.length, 2);
});

test('P8 next-face layered preload', async () => {
  const doc = documentFixture(), urls = [];
  class Image extends Element {
    constructor() { super('img', doc); this.complete = true; }
    decode() { urls.push(this.src); return Promise.resolve(); }
  }
  const sprite = createSpriteView({ spriteLayer: doc.body, fxLayer: doc.createElement('div'),
    faceSlot: doc.createElement('div'), ImageClass: Image, documentObj: doc });
  const layer = file => ({ file, rect: { width: 100, height: 100 } });
  sprite.setModel({ looks: { 'a/default': { crop: { width: 100, height: 100 }, body: [],
    faces: { next: { layers: ['next'], blink: { closed: 'blink' }, mouth: { open: 'mouth' } },
      blink: { layers: ['blink'] }, mouth: { layers: ['mouth'] } } } },
    layers: { next: layer('/next'), blink: layer('/blink'), mouth: layer('/mouth') } });
  await sprite.preloadState({ isV3: true, lookKey: 'a/default', state: { face: 'next' } });
  assert.deepEqual(urls.sort(), ['/blink', '/mouth', '/next']); sprite.destroy();
});

test('P8 queued-line preloading after done', async t => {
  const h = appFixture(t); await h.flush();
  const preloaded = []; h.app.sprite.preloadState = state => preloaded.push(state);
  h.events.get('turnstart')({ originId: 'p8', assistantMessage: { id: 'turn' } });
  const first = { index: 0, zh: 'current', emotion: 'gentle', act: {} };
  h.events.get('line')({ messageId: 'turn', line: first }); await h.flush();
  const before = h.app.sprite.getLastCommitted().faceUrl;
  h.events.get('line')({ messageId: 'turn', line: { index: 1, zh: 'next', emotion: 'shy', act: {} } });
  assert.equal(preloaded.length, 1); assert.equal(preloaded[0].state.face, 'shy');
  assert.equal(h.app.sprite.getLastCommitted().faceUrl, before);
  h.events.get('line')({ messageId: 'turn', line: { index: 2, zh: 'third', emotion: 'gentle', act: {} } });
  h.events.get('done')({ originId: 'p8', message: { content: 'synthetic' } });
  h.clock.tick(1500);
  h.app.action('advance'); await h.flush();
  assert.equal(h.app.player.currentIndex, 1);
  assert.equal(preloaded.at(-1).state.face, 'smile', 'queue stays available after stream completion');
});

