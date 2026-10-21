import test from 'node:test';
import assert from 'node:assert/strict';
import { createSpriteView } from '../frontend/src/adv/sprite.js';
import { normalizePortrait, createActingGate, inferEmotion } from '../frontend/src/sprite_state.js';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.style = {};
    this.src = '';
    this.naturalWidth = 800;
    this.naturalHeight = 1600;
    this.listeners = new Map();
    const classes = new Set();
    classes.remove = (...names) => names.forEach(n => classes.delete(n));
    classes.contains = n => classes.has(n);
    const add = classes.add.bind(classes);
    classes.add = (...names) => names.forEach(add);
    this.classList = classes;
  }
  set className(s) { this.classList.clear(); this.classList.add(...s.split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classList].join(' '); }
  set innerHTML(s) { this.children = []; this.html = s; }
  get innerHTML() { return this.html || ''; }
  appendChild(el) { el.parent = this; this.children.push(el); return el; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(el => el !== this); }
  removeAttribute(name) { if (name === 'src') this.src = ''; }
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  removeEventListener(name) { this.listeners.delete(name); }
  getAnimations() { return []; }
}

const tick = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
function tracked(promise) {
  const result = { settled: false, value: null };
  promise.then(value => { result.settled = true; result.value = value; });
  return result;
}
function setup(t) {
  const loads = [];
  const images = [];
  class Image extends Element {
    constructor() { super('img'); images.push(this); }
    decode() {
      const load = { image: this, url: this.src };
      load.promise = new Promise((resolve, reject) => { load.resolve = resolve; load.reject = reject; });
      loads.push(load);
      return load.promise;
    }
  }
  const doc = { hidden: false, createElement: tag => tag === 'img' ? new Image() : new Element(tag) };
  const spriteLayer = new Element(), faceSlot = new Element(), fxLayer = new Element();
  const frames = new Map();
  let frameId = 0, time = 0;
  const status = [];
  const view = createSpriteView({ spriteLayer, faceSlot, fxLayer, documentObj: doc, ImageClass: Image,
    now: () => time, raf: cb => { frames.set(++frameId, cb); return frameId; },
    cancelRaf: id => frames.delete(id), onStatus: s => status.push(s) });
  const model = { costumes: [{ id: 'one', faceRect: { x: 10, y: 20, width: 300, height: 320 },
    headAnchor: { x: 160, y: 0 } }], sprites: {} };
  view.setModel(model);
  t.after(() => view.destroy());
  const input = (id, fx = null) => ({ state: { outfit: 'one', face: id, fx }, faceUrl: '/' + id });
  const finish = (id, side = -1, error = false) => {
    const list = loads.filter(l => l.url === '/' + id);
    const selected = side < 0 ? list : [list[side]];
    assert.ok(selected.length && selected.every(Boolean), 'decode must have started');
    for (const load of selected) error ? load.reject(new Error('bad image')) : load.resolve();
  };
  const flush = () => { const q = [...frames.values()]; frames.clear(); q.forEach(cb => cb()); };
  const buffers = () => [...spriteLayer.children[0].children, ...faceSlot.children];
  const snapshot = () => buffers().map(el => ({ src: el.src, style: { ...el.style }, classes: el.className }));
  const visible = () => [spriteLayer.children[0]?.children || [], faceSlot.children].map(els =>
    els.filter(el => el.classList.contains('is-visible')).map(el => el.src));
  const commit = async (id, options = {}, fx = null) => {
    const pending = view.apply(input(id, fx), options);
    finish(id); await tick(); assert.equal(frames.size, 1); flush(); await pending;
  };
  return { view, model, doc, loads, images, frames, status, input, finish, flush, snapshot, visible, commit,
    spriteLayer, faceSlot, fxLayer, setTime: n => { time = n; } };
}

for (const order of [[1, 2], [2, 1]]) {
  test(`two applies decode in order ${order}: supersede settles and both views commit the newest`, async t => {
    const h = setup(t);
    await h.commit('base');
    const old = tracked(h.view.apply(h.input('1')));
    const latest = h.view.apply(h.input('2'));
    await tick();
    assert.equal(old.settled, true, 'supersede must settle without waiting for old decode');
    assert.equal(old.value.stale, true);
    for (const id of order) { h.finish(String(id)); await tick(); }
    h.flush(); await latest;
    assert.deepEqual(h.visible(), [['/2'], ['/2']]);
    assert.equal(h.view.getLastCommitted().faceUrl, '/2');
  });
}

test('a one-sided decode failure preserves both display buffers and drops the other decode', async t => {
  const h = setup(t); await h.commit('base');
  const before = h.snapshot();
  const pending = h.view.apply(h.input('broken'));
  h.finish('broken', 1, true); await tick(); await pending;
  assert.deepEqual(h.snapshot(), before);
  assert.equal(h.status.length, 1);
  assert.equal(h.status[0].kind, 'decode-failed');
  assert.equal(h.frames.size, 0);
  for (const load of h.loads.filter(l => l.url === '/broken')) {
    assert.equal(load.image.src, '');
    assert.equal(load.image.onload, null);
    assert.equal(load.image.onerror, null);
  }
});

for (const action of ['clear', 'destroy', 'setModel', 'supersede']) {
  test(`invalidation after decode before commit: ${action} cancels rAF and settles`, async t => {
    const h = setup(t); await h.commit('base');
    const before = h.snapshot();
    const old = tracked(h.view.apply(h.input('old')));
    h.finish('old'); await tick(); assert.equal(h.frames.size, 1);
    assert.deepEqual(h.snapshot(), before, 'decode completion cannot install framing before rAF');
    if (action === 'setModel') h.view.setModel(null);
    else if (action === 'supersede') h.view.apply(h.input('new'));
    else h.view[action]();
    await tick();
    assert.equal(old.settled, true, 'no frame flush is needed');
    assert.equal(old.value.stale, true);
    assert.equal(h.frames.size, 0);
    h.flush();
    assert.notEqual(h.view.getLastCommitted()?.faceUrl, '/old');
    assert.ok(h.visible().every(urls => !urls.includes('/old')));
  });
}

for (const action of ['clear', 'destroy']) {
  test(`${action} settles while decode remains unresolved and releases image handlers`, async t => {
    const h = setup(t);
    const speculative = tracked(h.view.preload(['/speculative', '/queued']));
    const old = tracked(h.view.apply(h.input('old')));
    h.view[action](); await tick();
    assert.equal(speculative.settled, true, 'supersede drops pending speculative work');
    assert.ok(h.images.every(image => !image.src));
    assert.equal(old.settled, true);
    assert.equal(old.value.stale, true);
    for (const load of h.loads) {
      assert.equal(load.image.src, '');
      assert.equal(load.image.onload, null);
      assert.equal(load.image.onerror, null);
    }
  });
}

test('three rapid applies: a failing third decode never mutates a fading buffer or its framing', async t => {
  const h = setup(t); await h.commit('1'); await h.commit('2');
  const before = h.snapshot();
  const third = h.view.apply(h.input('3'));
  assert.deepEqual(h.snapshot(), before, 'loading must stay off DOM');
  h.finish('3', 0, true); await tick(); await third;
  assert.deepEqual(h.snapshot(), before);
  assert.deepEqual(h.visible(), [['/2'], ['/2']]);
  assert.equal(h.view.getLastCommitted().faceUrl, '/2');
});

for (const mode of ['no model', 'destroyed']) {
  test(`apply is permanently stale with ${mode}`, async t => {
    const h = setup(t);
    if (mode === 'destroyed') { h.view.destroy(); h.view.setModel(h.model); }
    else h.view.setModel(null);
    const result = tracked(h.view.apply(h.input('late'))); await tick();
    assert.equal(result.settled, true);
    assert.equal(result.value.stale, true);
    assert.equal(h.loads.length, 0);
    assert.equal(h.view.getLastCommitted(), null);
  });
}

test('reduced-motion classes are set immediately and removed on all three containers', async t => {
  const h = setup(t);
  const pending = h.view.apply(h.input('1'), { reducedMotion: true });
  for (const el of [h.spriteLayer, h.fxLayer, h.faceSlot]) assert.equal(el.classList.contains('adv-reduced-motion'), true);
  h.finish('1'); await tick(); h.flush(); await pending;
  h.view.apply(h.input('2'), { reducedMotion: false });
  for (const el of [h.spriteLayer, h.fxLayer, h.faceSlot]) assert.equal(el.classList.contains('adv-reduced-motion'), false);
});

for (const flag of ['skipping', 'reducedMotion', 'suppressActing', 'hidden']) {
  test(`${flag} immediately stops motion and fx even when the next decode fails`, async t => {
    const h = setup(t); await h.commit('base', { motion: 'sigh' }, 'heart');
    assert.ok(h.spriteLayer.children[0].classList.contains('adv-motion-sigh'));
    assert.equal(h.fxLayer.children.length, 1);
    if (flag === 'hidden') h.doc.hidden = true;
    const pending = h.view.apply(h.input('slow'), flag === 'hidden' ? {} : { [flag]: true });
    assert.equal(h.spriteLayer.children[0].classList.contains('adv-motion-sigh'), false);
    assert.equal(h.fxLayer.children.length, 0);
    h.finish('slow', 0, true); await tick(); await pending;
  });
}

test('document visibility is checked again in the commit frame', async t => {
  const h = setup(t);
  const pending = h.view.apply(h.input('1', 'heart'), { motion: 'nod' });
  h.finish('1'); await tick(); h.doc.hidden = true; h.flush(); await pending;
  assert.equal(h.spriteLayer.children[0].classList.contains('adv-motion-nod'), false);
  assert.equal(h.fxLayer.children.length, 0);
});

test('renderer gates each committed line using injected time, including a rejected intervening motion', async t => {
  const h = setup(t);
  await h.commit('1', { motion: 'nod' }, 'sweat');
  h.setTime(1000); await h.commit('2', { motion: 'bounce' }, 'sweat');
  assert.equal(h.spriteLayer.children[0].classList.contains('adv-motion-bounce'), false);
  assert.equal(h.fxLayer.children.length, 0);
  h.setTime(3000); await h.commit('3', { motion: 'nod' }, 'sweat');
  assert.equal(h.spriteLayer.children[0].classList.contains('adv-motion-nod'), true);
  assert.equal(h.fxLayer.children.length, 1);
  h.setTime(6000); await h.commit('4', { motion: 'nod' });
  assert.equal(h.spriteLayer.children[0].classList.contains('adv-motion-nod'), false);
  h.setTime(7000); await h.commit('5');
  h.setTime(9000); await h.commit('6', { motion: 'nod' });
  assert.equal(h.spriteLayer.children[0].classList.contains('adv-motion-nod'), true, 'a line without motion breaks consecutive repetition');
});

test('gate tracks preceding line rather than preceding accepted motion', () => {
  const gate = createActingGate();
  assert.equal(gate.allowMotion('nod', 0), true);
  assert.equal(gate.allowMotion('bounce', 1000), false);
  assert.equal(gate.allowMotion('nod', 3000), true);
});

test('calibration accepts only finite number fields', () => {
  for (const bad of [null, '', true, false, '12', Infinity, NaN]) {
    const model = normalizePortrait({ enabled: true, costumes: [{ id: 'one',
      faceRect: { x: bad, y: 0, width: 300, height: 320 }, headAnchor: { x: bad, y: 0 },
      crop: { left: bad, top: 0, width: 300, height: 320 }, stage: { scale: 1, offset: { x: bad, y: 0 } } }] });
    for (const key of ['faceRect', 'headAnchor', 'crop', 'stage']) assert.equal(model.costumes[0][key], undefined, key);
    const invalidSize = normalizePortrait({ enabled: true, costumes: [{ id: 'one',
      faceRect: { x: 0, y: 0, width: bad, height: 320 },
      crop: { left: 0, top: 0, width: 300, height: bad }, stage: { scale: bad, offset: { x: 0, y: 0 } } }] });
    for (const key of ['faceRect', 'crop', 'stage']) assert.equal(invalidSize.costumes[0][key], undefined, key);
  }
});

test('legacy emotion precedence ignores whitespace metadata', () => {
  assert.equal(inferEmotion('[angry] text', ' happy '), 'angry');
  assert.equal(inferEmotion('[angry] text', 'HAPPY'), 'happy');
});


test('one view shares the preload concurrency, URL dedupe and six-entry limit across calls', async t => {
  const h = setup(t);
  h.view.preload(['/a', '/b', '/c', '/a']);
  h.view.preload(['/d', '/e', '/f']);
  await tick();
  const requested = () => h.images.filter(img => img.src);
  assert.equal(requested().length, 2, 'only two preload requests across batches');
  assert.equal(new Set(requested().map(img => img.src)).size, 2);
  for (let step = 0; step < 8; step++) {
    for (const img of requested()) img.onload?.();
    for (const load of h.loads) load.resolve();
    await tick();
  }
  assert.equal(requested().length, 6);
  h.view.preload(['/a']); await tick();
  assert.equal(h.images.length, 10, 'four display buffers plus six deduped preload images');
  h.view.preload(['/g']); await tick();
  assert.equal(requested().length, 6, 'oldest speculative entry must be evicted');
  assert.equal(requested().some(img => img.src === '/a'), false);
});

test('preloads enforce 96 MiB, skip oversized images and release everything on destroy', async t => {
  const h = setup(t);
  await h.view.preload([{ url: '/known-huge', width: 8192, height: 8192 }]);
  assert.equal(h.images.length, 4, 'known oversized art never starts a speculative request');
  const batches = tracked(h.view.preload(['/a', '/b', '/huge']));
  await tick();
  let requested = h.images.filter(img => img.src);
  assert.equal(requested.length, 2);
  for (const img of requested) { img.naturalWidth = 4096; img.naturalHeight = 4096; img.onload?.(); }
  assert.ok(h.images.filter(img => img.src).reduce((sum, img) => sum + img.naturalWidth * img.naturalHeight * 4, 0) <= 96 * 1024 * 1024);
  for (const load of h.loads) load.resolve();
  await tick();
  requested = h.images.filter(img => img.src);
  const huge = requested.find(img => img.src === '/huge');
  assert.ok(huge);
  huge.naturalWidth = 8192; huge.naturalHeight = 8192; huge.onload?.();
  for (const load of h.loads) load.resolve();
  await tick();
  const retained = h.images.filter(img => img.src);
  assert.equal(retained.length, 1, 'only one 64 MiB image fits; a 256 MiB image is skipped');
  assert.equal(retained[0].src, '/b', 'evict oldest decoded preload');
  assert.equal(batches.settled, true);
  const pending = tracked(h.view.preload(['/slow', '/later', '/queued']));
  h.view.destroy(); await tick();
  assert.equal(pending.settled, true);
  for (const img of h.images) {
    assert.equal(img.src, '');
    assert.equal(img.onload ?? null, null);
    assert.equal(img.onerror ?? null, null);
  }
  const count = h.images.length;
  await h.view.preload(['/after']);
  assert.equal(h.images.length, count);
});

test('player suspension cancels pending apply and acting while retaining stage and hiding crop', async t => {
  const h = setup(t); await h.commit('base', { motion: 'sigh' }, 'heart');
  const old = tracked(h.view.apply(h.input('slow')));
  h.view.suspend(); await tick();
  assert.equal(old.settled, true);
  assert.equal(old.value.stale, true);
  assert.deepEqual(h.visible()[0], ['/base']);
  assert.deepEqual(h.visible()[1], []);
  assert.equal(h.fxLayer.children.length, 0);
  assert.equal(h.spriteLayer.children[0].classList.contains('adv-motion-sigh'), false);
  h.finish('slow'); await tick(); h.flush();
  assert.equal(h.view.getLastCommitted().faceUrl, '/base');
});
