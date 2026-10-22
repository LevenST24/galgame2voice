// Layered (v3) sprite rendering: frame sets, blink, lip sync and source fx.
// The fake DOM cannot paint, so these tests assert the structure that makes
// painting correct: decoded images placed in <div> containers, paint order by
// z-index, and transient frames that only toggle visibility.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createSpriteView, planLayers, LIP_SYNC } from '../frontend/src/adv/sprite.js';
import { normalizePortrait, resolveSpriteState, resolveLookLayers } from '../frontend/src/sprite_state.js';
import { Element, fakeDocument, fakeClock, microtasks } from './helpers/adv_dom.mjs';

const CROP = { left: 100, top: 50, width: 800, height: 1600 };
const FACE_RECT = { x: 200, y: 100, width: 400, height: 360 };
const HEAD = { x: 400, y: 80 };
const FACE_AREA = { x: 300, y: 150, width: 400, height: 300 };

function pack() {
  const layer = (z, rect = FACE_AREA) => ({ canvas: 'A', rect, z, opacity: 1 });
  const layers = {
    body: layer(0, { x: 100, y: 50, width: 800, height: 1600 }),
    body_b: layer(0, { x: 100, y: 50, width: 800, height: 1600 }),
    arm: layer(5, { x: 500, y: 700, width: 300, height: 600 }),
    hair_front: layer(5, { x: 250, y: 60, width: 500, height: 500 }),
    blush_soft: layer(3), blush_strong: layer(3),
    fx_sweat: layer(9, { x: 600, y: 60, width: 100, height: 100 }),
  };
  for (const id of ['face_smile', 'eyes_half', 'eyes_closed', 'mouth_half', 'mouth_open', 'closed_open',
    'face_shy', 'shy_closed_l', 'shy_open_l', 'face_sleepy', 'face_plain', 'face_calm', 'face_smile_b']) {
    layers[id] = layer(2);
  }
  for (const [id, entry] of Object.entries(layers)) entry.file = `/l/${id}.webp`;
  const face = (id, extra = {}) => ({ layers: [id, 'hair_front'], eyesClosed: false, ...extra });
  const calibration = { crop: CROP, faceRect: FACE_RECT, headAnchor: HEAD };
  return {
    enabled: true, version: 3, defaultOutfit: 'uniform', defaultPose: 'a',
    canvases: { A: { width: 1000, height: 2000 } },
    outfits: { uniform: { name: '校服' } },
    poses: { a: { name: '站姿1', canvas: 'A' }, b: { name: '站姿2', canvas: 'A' } },
    faces: Object.fromEntries(['smile', 'shy', 'sleepy', 'plain', 'calm'].map(id => [id, { label: id, emotion: 'gentle' }])),
    expression_sets: { gentle: [{ id: 'smile', strength: 3 }] },
    looks: {
      'a/uniform': {
        pose: 'a', outfit: 'uniform', ...calibration, defaultFace: 'smile',
        body: ['body', 'arm'], blush: ['blush_soft'], fx: { sweat: ['fx_sweat'] },
        faces: {
          smile: face('face_smile', {
            blink: { half: 'smile_half', closed: 'smile_closed' },
            mouth: { closed: 'smile', half: 'smile_mouth_half', open: 'smile_mouth_open' },
            combinedFrames: { 'closed:open': 'smile_closed_open' },
          }),
          smile_half: face('eyes_half', { frameOnly: true }),
          smile_closed: face('eyes_closed', { eyesClosed: true, frameOnly: true }),
          smile_mouth_half: face('mouth_half', { frameOnly: true }),
          smile_mouth_open: face('mouth_open', { frameOnly: true }),
          smile_closed_open: face('closed_open', { eyesClosed: true, frameOnly: true }),
          shy: face('face_shy', { blush: ['blush_strong'], blink: { closed: 'shy_closed' }, mouth: { closed: 'shy', open: 'shy_open' } }),
          shy_closed: face('shy_closed_l', { eyesClosed: true, frameOnly: true }),
          shy_open: face('shy_open_l', { frameOnly: true }),
          sleepy: face('face_sleepy', { eyesClosed: true }),
          plain: face('face_plain'),
          calm: face('face_calm', { blush: [] }),
        },
      },
      'b/uniform': {
        pose: 'b', outfit: 'uniform', ...calibration, defaultFace: 'smile', body: ['body_b'],
        faces: { smile: { layers: ['face_smile_b'], eyesClosed: false } },
      },
    },
    layers,
  };
}

test('frameOnly partners never become expressions through tags, pools, drift or sticky state', () => {
  const raw = pack();
  const look = raw.looks['a/uniform'];
  const partners = Object.keys(look.faces).filter(id => look.faces[id].frameOnly);
  for (const id of partners) raw.faces[id] = { label: `partner ${id}`, parts: [], emotion: 'happy' };
  raw.expression_sets.happy = [...partners, 'smile', 'shy'].map(id => ({ id, strength: 100 }));
  const model = normalizePortrait(raw);
  assert.equal(model.looks['a/uniform'].faces.smile_closed.frameOnly, true);
  for (const id of partners) {
    for (const face of [id, `partner ${id}`]) {
      assert.equal(resolveSpriteState(model, null, { act: { face } }).state.face, 'smile');
    }
    for (const emotion of ['happy', 'thinking']) {
      const prev = { outfit: 'uniform', pose: 'a', face: id };
      assert.ok(['smile', 'shy'].includes(resolveSpriteState(model, prev, { emotion }).state.face));
    }
    const switched = resolveSpriteState(model, { outfit: 'other', face: id }, { emotion: 'happy' });
    assert.equal(switched.state.face, 'smile');
  }
  assert.equal(resolveSpriteState(model, { outfit: 'uniform', face: 'smile' }, { emotion: 'happy' }).state.face, 'shy');
  assert.equal(planLayers(model, 'a/uniform', { face: partners[0] }), null);
});

test('frameOnly is excluded from default and first-face fallbacks, even with animation partners', () => {
  const raw = pack();
  const look = raw.looks['a/uniform'];
  const partner = look.faces.smile_mouth_open;
  partner.blink = { closed: 'smile_closed' };
  partner.mouth = { closed: 'smile_mouth_open', open: 'smile_mouth_half' };
  delete look.faces.smile.blink;
  delete look.faces.smile.mouth;
  // Leave the frameOnly face as the only animated one; an animated selectable face would win the fallback.
  delete look.faces.shy.blink;
  delete look.faces.shy.mouth;
  look.faces = { smile_mouth_open: partner, ...look.faces };
  look.defaultFace = 'smile_mouth_open';
  raw.expression_sets = { gentle: [{ id: 'smile_mouth_open', strength: 999 }] };
  const model = normalizePortrait(raw);
  for (const act of [{}, { face: 'unknown' }, { face: 'smile_mouth_open' }]) {
    assert.equal(resolveSpriteState(model, null, { act }).state.face, 'smile');
  }
  assert.ok(resolveLookLayers(model, 'a/uniform', 'unknown').some(layer => layer.id === 'face_smile'));
  delete look.defaultFace;
  assert.equal(resolveSpriteState(model, null, {}).state.face, 'smile');
  look.faces = { smile_mouth_open: partner };
  assert.equal(resolveSpriteState(model, null, {}), null);
});

test('frameOnly is scoped to a look and false keeps a face selectable', () => {
  const raw = pack();
  raw.looks['b/uniform'].faces.smile_mouth_open = { layers: ['mouth_open'], frameOnly: false };
  const model = normalizePortrait(raw);
  assert.equal(resolveSpriteState(model, null, { act: { pose: 'b', face: 'smile_mouth_open' } }).state.face, 'smile_mouth_open');
  assert.equal(resolveSpriteState(model, null, { act: { pose: 'a', face: 'smile_mouth_open' } }).state.face, 'smile');
});

const v3 = (face, state = {}, lookKey = 'a/uniform') => ({
  isV3: true, lookKey, state: { outfit: 'uniform', pose: lookKey.split('/')[0], face, blush: false, fx: null, ...state },
});
const walk = (element, out = []) => { out.push(element); element.children.forEach(child => walk(child, out)); return out; };
const layerId = image => image.src.replace(/^\/l\//, '').replace(/\.webp$/, '');
// Visible layer ids of a container in paint order: z-index, then DOM order.
const painted = container => container.children
  .map((image, index) => ({ image, index }))
  .filter(({ image }) => image.style.visibility !== 'hidden')
  .sort((a, b) => Number(a.image.style.zIndex) - Number(b.image.style.zIndex) || a.index - b.index)
  .map(({ image }) => layerId(image));
const source = () => ({ rms: 0, calls: 0, getRms() { this.calls++; return this.rms; } });

function setup({ random = () => 0 } = {}) {
  const doc = fakeDocument();
  const clock = fakeClock();
  const frames = new Map();
  const images = [];
  const decodes = [];
  const mode = { decode: 'auto' };
  let created = 0;
  const createElement = doc.createElement;
  doc.createElement = tag => { created++; return createElement(tag); };
  class LayerImage extends Element {
    constructor() { super('img', doc); created++; images.push(this); }
    decode() {
      if (mode.decode === 'auto') return Promise.resolve();
      return new Promise((resolve, reject) => decodes.push({ image: this, url: this.src, resolve, reject }));
    }
  }
  const spriteLayer = doc.createElement('div');
  const fxLayer = doc.createElement('div');
  const faceSlot = doc.createElement('div');
  const status = [];
  let frameId = 0;
  const view = createSpriteView({
    spriteLayer, fxLayer, faceSlot, onStatus: event => status.push(event),
    now: clock.now, clock, random, documentObj: doc, ImageClass: LayerImage,
    raf: fn => { frames.set(++frameId, fn); return frameId; }, cancelRaf: id => frames.delete(id),
  });
  const model = normalizePortrait(pack());
  view.setModel(model);
  const flush = () => { for (const [id, fn] of [...frames]) { frames.delete(id); fn(); } };
  const layerBuffers = parent => parent.children.filter(el => el.classList.contains('adv-layer-buffer'));
  return {
    doc, clock, view, model, images, decodes, mode, status, spriteLayer, fxLayer, faceSlot, LayerImage, flush,
    created: () => created,
    stageBuffers: () => layerBuffers(spriteLayer.children[0]),
    faceBuffers: () => layerBuffers(faceSlot),
    active() {
      const visible = list => list.find(el => el.classList.contains('is-visible'));
      return { stage: visible(this.stageBuffers()), face: visible(this.faceBuffers()) };
    },
    async commit(input, options) {
      const result = view.apply(input, options);
      await microtasks(); flush();
      return result;
    },
  };
}

test('layered packs draw in div containers and never put children in an img', async () => {
  const h = setup();
  const motion = h.spriteLayer.children[0];
  assert.deepEqual(motion.children.map(el => el.tagName), ['IMG', 'IMG'], 'whole-image DOM until a layered pack draws');
  assert.deepEqual(h.faceSlot.children.map(el => el.tagName), ['IMG', 'IMG']);
  assert.ok(h.model?.isV3, 'the normalized v3 payload reaches the renderer');
  await h.commit(v3('smile'));
  assert.deepEqual(motion.children.map(el => el.tagName), ['IMG', 'IMG', 'DIV', 'DIV']);
  assert.deepEqual(h.faceSlot.children.map(el => el.tagName), ['IMG', 'IMG', 'DIV', 'DIV']);
  for (const element of [...walk(h.spriteLayer), ...walk(h.faceSlot)]) {
    if (element.tagName === 'IMG') assert.equal(element.children.length, 0, 'an <img> never has children');
  }
  const { stage, face } = h.active();
  assert.ok(stage.classList.contains('adv-sprite-buffer'));
  assert.ok(face.classList.contains('adv-face-buffer'));
  for (const container of [stage, face]) {
    assert.ok(container.children.length > 0);
    assert.ok(container.children.every(image => image instanceof h.LayerImage), 'the decoded images are the layers');
  }
});

test('draw order follows z, equal z keeps list order, and layers sit at their crop offset', async () => {
  const h = setup();
  await h.commit(v3('smile', { blush: true }));
  const { stage, face } = h.active();
  assert.deepEqual(painted(stage), ['body', 'face_smile', 'blush_soft', 'arm', 'hair_front']);
  assert.deepEqual(painted(face), painted(stage), 'the face window shows the same layers');
  const smile = stage.children.find(image => layerId(image) === 'face_smile');
  assert.deepEqual([smile.style.left, smile.style.top, smile.style.width, smile.style.height],
    ['25%', '6.25%', '50%', '18.75%']);
  // The crop box reuses the whole-image framing of a calibrated sprite.
  assert.equal(stage.style.width, `calc(${800 * (340 / FACE_RECT.height)} * var(--u))`);
  assert.equal(face.style.left, `calc(${-FACE_RECT.x * (450 / FACE_RECT.width)} * var(--u))`);
});

test('blush belongs to the line face: its override, the look blush, or none', () => {
  const { model } = setup();
  const blush = (face, on = true) => planLayers(model, 'a/uniform', { face, blush: on })
    .items.filter(item => item.group === null && item.id.startsWith('blush')).map(item => item.id);
  assert.deepEqual(blush('shy'), ['blush_strong']);
  assert.deepEqual(blush('smile'), ['blush_soft']);
  assert.deepEqual(blush('calm'), []);
  assert.deepEqual(blush('shy', false), []);
  const plan = planLayers(model, 'a/uniform', { face: 'shy', blush: true });
  assert.deepEqual(plan.groups, ['shy', 'shy_closed', 'shy_open']);
});

test('the swap waits until every layer of every face group is decoded', async () => {
  const h = setup();
  h.mode.decode = 'manual';
  const result = h.view.apply(v3('smile'));
  await microtasks();
  const urls = new Set(h.decodes.map(entry => layerId({ src: entry.url })));
  for (const id of ['body', 'arm', 'face_smile', 'hair_front', 'eyes_half', 'eyes_closed', 'mouth_half', 'mouth_open', 'closed_open']) {
    assert.ok(urls.has(id), `${id} is decoded before the swap`);
  }
  assert.equal(h.decodes.length, 2 * planLayers(h.model, 'a/uniform', { face: 'smile' }).items.length,
    'one copy for the stage and one for the face window');
  h.decodes.slice(0, -1).forEach(entry => entry.resolve());
  await microtasks(); h.flush();
  assert.equal(h.active().stage, undefined, 'nothing paints before the last decode');
  h.decodes.at(-1).resolve();
  await microtasks(); h.flush();
  assert.equal((await result).stale, false);
  assert.deepEqual(painted(h.active().stage), ['body', 'face_smile', 'arm', 'hair_front']);
});

test('a stale decode never paints and its images are released', async () => {
  const h = setup();
  h.mode.decode = 'manual';
  const first = h.view.apply(v3('smile'));
  await microtasks();
  const old = [...h.decodes];
  const second = h.view.apply(v3('plain'));
  assert.deepEqual(await first, { stale: true });
  old.forEach(entry => entry.resolve());
  await microtasks(); h.flush();
  assert.equal(h.active().stage, undefined);
  assert.ok(old.every(entry => entry.image.src === ''), 'cancelled loads are released');
  h.decodes.slice(old.length).forEach(entry => entry.resolve());
  await microtasks(); h.flush();
  assert.equal((await second).stale, false);
  assert.equal(h.view.getLastCommitted().state.face, 'plain');
});

test('a failed layer load keeps the previous frame and reports it', async () => {
  const h = setup();
  await h.commit(v3('smile'));
  h.mode.decode = 'manual';
  const failed = h.view.apply(v3('plain'));
  await microtasks();
  h.decodes[0].reject(new Error('broken'));
  await microtasks(); h.flush();
  const result = await failed;
  assert.equal(result.stale, false);
  assert.ok(result.error);
  assert.deepEqual(h.status.map(event => event.kind), ['decode-failed']);
  assert.equal(h.view.getLastCommitted().state.face, 'smile');
  assert.deepEqual(painted(h.active().stage), ['body', 'face_smile', 'arm', 'hair_front']);
  assert.equal(h.clock.timers.size, 1, 'the kept frame blinks again');
});

test('blink waits 2–6 s, shows half 60, closed 90, half 60 ms, and only toggles visibility', async () => {
  const h = setup({ random: () => 0 });
  await h.commit(v3('smile'));
  const { stage, face } = h.active();
  const nodes = () => [...walk(h.spriteLayer), ...walk(h.faceSlot)];
  const before = nodes();
  const created = h.created();
  const seen = [];
  const sample = ms => {
    h.clock.tick(ms);
    assert.deepEqual(painted(face), painted(stage), 'the stage and the face window show the same frame');
    seen.push([h.clock.now(), h.view.getVisibleFace()]);
  };
  [1999, 1, 60, 90, 60].forEach(sample);
  assert.deepEqual(seen, [[1999, 'smile'], [2000, 'smile_half'], [2060, 'smile_closed'], [2150, 'smile_half'], [2210, 'smile']]);
  assert.deepEqual(nodes(), before, 'no element was added or removed');
  assert.equal(h.created(), created, 'no element was created');
  assert.equal(h.view.getLastCommitted().state.face, 'smile', 'the base face is kept');
  assert.equal(h.clock.timers.size, 1, 'the next blink is scheduled');

  const late = setup({ random: () => 0.9999 });
  await late.commit(v3('smile'));
  late.clock.tick(5999);
  assert.equal(late.view.getVisibleFace(), 'smile');
  late.clock.tick(1);
  assert.equal(late.view.getVisibleFace(), 'smile_half');
});

test('a blink without a half frame shows closed for 90 ms and keeps the face blush', async () => {
  const h = setup();
  await h.commit(v3('shy', { blush: true }));
  h.clock.tick(2000);
  assert.equal(h.view.getVisibleFace(), 'shy_closed');
  assert.ok(painted(h.active().stage).includes('blush_strong'));
  h.clock.tick(89);
  assert.equal(h.view.getVisibleFace(), 'shy_closed');
  h.clock.tick(1);
  assert.equal(h.view.getVisibleFace(), 'shy');
});

for (const refresh of ['options', 'visibility']) {
  test(`R12 blink waits for decode and commit after ${refresh} refresh`, async () => {
    const h = setup();
    await h.commit(v3('smile'));
    h.clock.tick(2000); // Interrupt a blink already in progress.
    h.mode.decode = 'manual';
    const next = h.view.apply(v3('shy'));
    await microtasks();
    const update = () => refresh === 'options'
      ? h.view.setAnimationOptions({ advBlink: true })
      : h.doc.dispatchEvent({ type: 'visibilitychange' });
    update();
    assert.equal(h.clock.timers.size, 0, 'no blink timer during decode');
    assert.equal(h.view.getVisibleFace(), 'smile');
    h.clock.tick(6000);
    assert.equal(h.view.getLastCommitted().state.face, 'smile');
    h.decodes.forEach(entry => entry.resolve());
    await microtasks();
    update(); // Decode is complete, but the RAF has not committed it.
    assert.equal(h.clock.timers.size, 0, 'no blink timer before RAF commit');
    assert.equal(h.view.getVisibleFace(), 'smile');
    h.flush();
    assert.equal((await next).stale, false);
    assert.equal(h.view.getVisibleFace(), 'shy', 'new frame starts open');
    assert.equal(h.clock.timers.size, 1, 'exactly one new-frame timer');
    update(); update();
    assert.equal(h.clock.timers.size, 1, 'repeated refresh cannot duplicate timers');
    h.clock.tick(1999);
    assert.equal(h.view.getVisibleFace(), 'shy');
    h.clock.tick(1);
    assert.equal(h.view.getVisibleFace(), 'shy_closed');
    h.clock.tick(90);
    assert.equal(h.view.getVisibleFace(), 'shy', 'new blink uses its own steps');
    assert.equal(h.clock.timers.size, 1);
    h.view.destroy();
    assert.equal(h.clock.timers.size, 0);
  });
}

test('R12 model invalidation cancels an active blink', async () => {
  const h = setup();
  await h.commit(v3('smile'));
  h.clock.tick(2000);
  h.view.setModel(h.model, { preserveCurrent: true });
  assert.equal(h.clock.timers.size, 0);
  assert.equal(h.view.getVisibleFace(), 'smile');
  h.clock.tick(210);
  assert.equal(h.view.getVisibleFace(), 'smile');
  h.view.destroy();
});

test('eyes-closed faces and faces without a blink partner never blink', async () => {
  for (const face of ['sleepy', 'plain']) {
    const h = setup();
    await h.commit(v3(face));
    assert.equal(h.clock.timers.size, 0);
    h.clock.tick(10000);
    assert.equal(h.view.getVisibleFace(), face);
  }
});

test('blink stops on a face or pose change, a hidden tab, the setting, reduced motion and unmount', async () => {
  const closedEyes = async () => {
    const h = setup();
    await h.commit(v3('smile'));
    h.clock.tick(2060);
    assert.equal(h.view.getVisibleFace(), 'smile_closed');
    return h;
  };
  let h = await closedEyes();
  const next = h.view.apply(v3('plain'));
  assert.equal(h.view.getVisibleFace(), 'smile', 'the old frame opens its eyes at once');
  await microtasks(); h.flush(); await next;
  assert.equal(h.view.getVisibleFace(), 'plain');
  assert.equal(h.clock.timers.size, 0);

  h = await closedEyes();
  await h.commit(v3('smile', {}, 'b/uniform'));
  assert.equal(h.view.getVisibleFace(), 'smile');
  assert.equal(h.clock.timers.size, 0, 'pose b has no blink partner');

  h = await closedEyes();
  h.doc.hidden = true;
  h.doc.dispatchEvent({ type: 'visibilitychange' });
  assert.equal(h.view.getVisibleFace(), 'smile');
  assert.equal(h.clock.timers.size, 0);
  h.doc.hidden = false;
  h.doc.dispatchEvent({ type: 'visibilitychange' });
  assert.equal(h.clock.timers.size, 1, 'a visible tab blinks again');

  h = await closedEyes();
  h.view.setAnimationOptions({ advBlink: false });
  assert.equal(h.view.getVisibleFace(), 'smile');
  assert.equal(h.clock.timers.size, 0);
  h.view.setAnimationOptions({ advBlink: true });
  assert.equal(h.clock.timers.size, 1);

  h = await closedEyes();
  await h.commit(v3('smile'), { reducedMotion: true });
  assert.equal(h.view.getVisibleFace(), 'smile');
  assert.equal(h.clock.timers.size, 0);
  await h.commit(v3('smile'), { reducedMotion: false });
  assert.equal(h.clock.timers.size, 1);

  h = await closedEyes();
  h.view.setAnimationOptions({ reducedMotion: true });
  assert.equal(h.view.getVisibleFace(), 'smile', 'an OS change needs no new line');
  assert.equal(h.clock.timers.size, 0);
  h.view.setAnimationOptions({ reducedMotion: false });
  assert.equal(h.clock.timers.size, 1);

  h = await closedEyes();
  h.view.destroy();
  assert.equal(h.clock.timers.size, 0);
});

test('lip sync follows the voice level with 40 ms hysteresis; half uses open when missing', async () => {
  const h = setup();
  h.view.setAnimationOptions({ advBlink: false });
  await h.commit(v3('smile'));
  const voice = source();
  h.view.startLipSync({ audioPlayer: voice });
  const step = rms => { voice.rms = rms; h.clock.tick(LIP_SYNC.intervalMs); return h.view.getVisibleFace(); };
  assert.deepEqual([0.05, 0.05, 0.05].map(step), ['smile', 'smile', 'smile_mouth_open']);
  assert.deepEqual([0.02, 0.02, 0.02].map(step), ['smile_mouth_open', 'smile_mouth_open', 'smile_mouth_half']);
  assert.deepEqual([0.05, 0.02, 0.02, 0.02].map(step), Array(4).fill('smile_mouth_half'), 'a spike under 40 ms is ignored');
  assert.deepEqual([0.001, 0.001, 0.001].map(step), ['smile_mouth_half', 'smile_mouth_half', 'smile']);
  assert.deepEqual([0.05, 0.05, 0.05].map(step).at(-1), 'smile_mouth_open');
  voice.paused = true;
  assert.equal([0.05, 0.05, 0.05].map(step).at(-1), 'smile', 'a paused player reads as silent');
  voice.paused = false;
  await h.commit(v3('shy'));
  assert.equal([0.02, 0.02, 0.02].map(step).at(-1), 'shy_open', 'without a half frame, half shows open');
});

test('lip sync keeps sampling through a face without a mouth partner', async () => {
  const h = setup();
  h.view.setAnimationOptions({ advBlink: false });
  await h.commit(v3('plain'));
  const voice = source();
  voice.rms = 0.05;
  h.view.startLipSync({ audioPlayer: voice });
  h.clock.tick(200);
  assert.equal(h.view.getVisibleFace(), 'plain', 'no mouth partner: the mouth stays closed');
  const calls = voice.calls;
  // The line's face commits after its voice started.
  await h.commit(v3('smile'));
  assert.equal(h.view.getVisibleFace(), 'smile_mouth_open');
  h.clock.tick(LIP_SYNC.intervalMs);
  assert.ok(voice.calls > calls, 'still sampling');
});

test('stopping lip sync shows the base face; an older line token cannot stop a newer line', async () => {
  const h = setup();
  h.view.setAnimationOptions({ advBlink: false });
  await h.commit(v3('smile'));
  const first = source();
  const second = source();
  const one = h.view.startLipSync({ audioPlayer: first });
  const two = h.view.startLipSync({ audioPlayer: second });
  second.rms = 0.05;
  h.clock.tick(120);
  assert.equal(h.view.getVisibleFace(), 'smile_mouth_open');
  h.view.stopLipSync(one);
  h.clock.tick(120);
  assert.equal(h.view.getVisibleFace(), 'smile_mouth_open', 'the older line cannot stop the newer one');
  assert.equal(first.calls, 1, 'only the current line is sampled');
  h.view.stopLipSync(two);
  assert.equal(h.view.getVisibleFace(), 'smile');
  const calls = second.calls;
  h.clock.tick(500);
  assert.equal(second.calls, calls);
  assert.equal(h.clock.timers.size, 0);
  h.view.startLipSync({ audioPlayer: second });
  h.view.stopLipSync();
  assert.equal(h.clock.timers.size, 0, 'an interrupt stops any line');
  assert.equal(h.view.startLipSync({ audioPlayer: second, characterVoice: false }), 0, 'user recordings never move the mouth');
  assert.equal(h.clock.timers.size, 0);
});

test('a blink during speech uses the combined frame; without one the blink frame wins', async () => {
  const h = setup();
  await h.commit(v3('smile'));
  const voice = source();
  voice.rms = 0.05;
  h.view.startLipSync({ audioPlayer: voice });
  h.clock.tick(200);
  assert.equal(h.view.getVisibleFace(), 'smile_mouth_open');
  const seen = [1800, 60, 90, 60].map(ms => { h.clock.tick(ms); return h.view.getVisibleFace(); });
  assert.deepEqual(seen, ['smile_half', 'smile_closed_open', 'smile_half', 'smile_mouth_open']);
});

test('the lip sync setting and reduced motion close the mouth at once and resume after', async () => {
  const h = setup();
  h.view.setAnimationOptions({ advBlink: false });
  await h.commit(v3('smile'));
  const voice = source();
  voice.rms = 0.05;
  h.view.startLipSync({ audioPlayer: voice });
  h.clock.tick(120);
  assert.equal(h.view.getVisibleFace(), 'smile_mouth_open');
  h.view.setAnimationOptions({ advLipSync: false });
  assert.equal(h.view.getVisibleFace(), 'smile');
  let calls = voice.calls;
  h.clock.tick(200);
  assert.equal(voice.calls, calls, 'sampling stops');
  h.view.setAnimationOptions({ advLipSync: true });
  h.clock.tick(120);
  assert.equal(h.view.getVisibleFace(), 'smile_mouth_open');
  await h.commit(v3('smile'), { reducedMotion: true });
  assert.equal(h.view.getVisibleFace(), 'smile');
  calls = voice.calls;
  h.clock.tick(200);
  assert.equal(voice.calls, calls);
  await h.commit(v3('smile'), { reducedMotion: false });
  h.clock.tick(120);
  assert.equal(h.view.getVisibleFace(), 'smile_mouth_open');
});

test('source fx layers replace the SVG mark and follow the acting gate', async () => {
  const h = setup();
  await h.commit(v3('smile', { fx: 'sweat' }));
  const { stage, face } = h.active();
  assert.ok(painted(stage).includes('fx_sweat'));
  assert.ok(painted(face).includes('fx_sweat'));
  assert.equal(h.fxLayer.children.length, 0, 'no SVG mark next to source fx');
  await h.commit(v3('smile', { fx: 'sweat' }));
  assert.ok(!painted(h.active().stage).includes('fx_sweat'), 'the gate limits a repeated fx');
  await h.commit(v3('plain', { fx: 'heart' }));
  assert.equal(h.fxLayer.children.length, 1, 'without source layers the SVG mark draws');
  assert.ok(!painted(h.active().stage).includes('fx_sweat'));
});

test('preloads the next two faces frame sets, skipping partners already decoded', async () => {
  const h = setup();
  await h.commit(v3('smile'));
  const seen = new Set();
  for (let round = 0; round < 10; round++) {
    const loading = h.images.filter(image => image.src && !image.parentNode && image.onload);
    if (!loading.length) break;
    for (const image of loading) { seen.add(layerId(image)); image.onload(); }
    await microtasks();
  }
  assert.deepEqual([...seen].sort(), ['face_shy', 'face_sleepy', 'shy_closed_l', 'shy_open_l']);
});

test('suspend hides the face window and source fx while the stage keeps blinking', async () => {
  const h = setup();
  await h.commit(v3('smile', { fx: 'sweat' }));
  const { stage } = h.active();
  h.view.suspend();
  assert.ok(h.faceBuffers().every(el => el.style.visibility === 'hidden' && el.classList.contains('is-hidden')));
  assert.ok(!painted(stage).includes('fx_sweat'));
  h.clock.tick(2000);
  assert.equal(h.view.getVisibleFace(), 'smile_half');
});
