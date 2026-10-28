import test from 'node:test';
import assert from 'node:assert/strict';
import { createSpriteView, FRAMING, preload } from '../frontend/src/adv/sprite.js';

class FakeElement {
  constructor(tagName = 'div') {
    this.tagName = tagName;
    const classes = new Set();
    this.classList = classes;
    this.classList.add = (...names) => {
      names.forEach(n => Set.prototype.add.call(classes, n));
    };
    this.classList.remove = (...names) => {
      names.forEach(n => Set.prototype.delete.call(classes, n));
    };
    this.classList.contains = (name) => Set.prototype.has.call(classes, name);
    this.style = {};
    this.children = [];
    this.src = '';
    this.naturalWidth = 800;
    this.naturalHeight = 1600;
    this.width = 800;
    this.height = 1600;
    this._listeners = new Map();
    this._decodePromise = null;
    this._decodeResolve = null;
    this._decodeReject = null;
  }

  set innerHTML(val) {
    this.children = [];
    this._innerHTML = val;
  }

  get innerHTML() {
    return this._innerHTML || '';
  }

  get className() {
    return [...this.classList].join(' ');
  }

  set className(val) {
    this.classList.clear();
    String(val || '').split(/\s+/).filter(Boolean)
      .forEach(n => Set.prototype.add.call(this.classList, n));
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  remove() {
    this._removed = true;
  }

  addEventListener(event, fn) {
    if (!this._listeners.has(event)) this._listeners.set(event, []);
    this._listeners.get(event).push(fn);
  }

  removeEventListener(event, fn) {
    const list = this._listeners.get(event);
    if (!list) return;
    const idx = list.indexOf(fn);
    if (idx !== -1) list.splice(idx, 1);
  }

  dispatchEvent(event) {
    const list = this._listeners.get(event.type) || [];
    for (const fn of list) fn(event);
  }

  decode() {
    if (this._decodePromise) return this._decodePromise;
    return Promise.resolve();
  }
}

function createFakeDocument() {
  return {
    createElement(tag) {
      return new FakeElement(tag);
    },
    hidden: false,
  };
}

function setupView({ rafQueue = [] } = {}) {
  const doc = createFakeDocument();
  const spriteLayer = new FakeElement('div');
  const fxLayer = new FakeElement('div');
  const faceSlot = new FakeElement('div');
  const statusCalls = [];
  const imageOptions = { naturalWidth: 800, naturalHeight: 1600, decode: null };
  class DetachedImage extends FakeElement {
    constructor() {
      super('img');
      this.naturalWidth = imageOptions.naturalWidth;
      this.naturalHeight = imageOptions.naturalHeight;
    }
    decode() { return imageOptions.decode ? imageOptions.decode() : Promise.resolve(); }
  }

  const fakeRaf = (cb) => {
    rafQueue.push(cb);
  };

  const flushRaf = () => {
    const q = [...rafQueue];
    rafQueue.length = 0;
    q.forEach(cb => cb());
  };

  const waitForRaf = async () => {
    for (let i = 0; i < 100 && rafQueue.length === 0; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  };

  const view = createSpriteView({
    spriteLayer,
    fxLayer,
    faceSlot,
    onStatus: (st) => statusCalls.push(st),
    raf: fakeRaf,
    documentObj: doc,
    ImageClass: DetachedImage,
  });

  return {
    view,
    spriteLayer,
    fxLayer,
    faceSlot,
    statusCalls,
    imageOptions,
    doc,
    flushRaf,
    waitForRaf,
    rafQueue,
  };
}

function createTestModel() {
  return {
    defaultCostume: 'uniform',
    costumes: [
      {
        id: 'uniform',
        faceRect: { x: 175, y: 0, width: 362, height: 325 },
        headAnchor: { x: 356, y: 2 },
      },
      {
        id: 'stage_custom',
        faceRect: { x: 175, y: 0, width: 362, height: 325 },
        headAnchor: { x: 356, y: 2 },
        stage: { scale: 0.8, offset: { x: 340, y: -260 } },
      },
      {
        id: 'legacy',
      },
    ],
    sprites: {
      uniform: { '01': { file: '/uniform/01.webp' }, '02': { file: '/uniform/02.webp' } },
      stage_custom: { '01': { file: '/stage_custom/01.webp' } },
      legacy: { '01': { file: '/legacy/01.png' } },
    },
  };
}

test('nothing is visible before decode', async () => {
  const { view, spriteLayer, flushRaf, waitForRaf, imageOptions } = setupView();
  const model = createTestModel();
  view.setModel(model);

  const motionWrapper = spriteLayer.children[0].children[0];
  const stageImgA = motionWrapper.children[0];
  const stageImgB = motionWrapper.children[1];

  let resolveDecode;
  const decodeWaiters = [];
  imageOptions.decode = () => new Promise(resolve => decodeWaiters.push(resolve));
  resolveDecode = () => decodeWaiters.forEach(resolve => resolve());

  const applyPromise = view.apply({
    state: { outfit: 'uniform', face: '01' },
    faceUrl: '/uniform/01.webp',
  });

  // Decode has not resolved yet
  assert.equal(stageImgB.classList.contains('is-visible'), false);
  assert.equal(stageImgA.classList.contains('is-visible'), false);

  // Now resolve decode and flush raf
  resolveDecode();
  await waitForRaf();
  flushRaf();
  await applyPromise;

  assert.equal(stageImgB.classList.contains('is-visible'), true);
  assert.equal(stageImgA.classList.contains('is-visible'), false);
});

test('stage and crop swap in one frame with the same revision', async () => {
  const { view, spriteLayer, faceSlot, flushRaf, waitForRaf, imageOptions } = setupView();
  const model = createTestModel();
  view.setModel(model);

  const motionWrapper = spriteLayer.children[0].children[0];
  const stageImgA = motionWrapper.children[0];
  const stageImgB = motionWrapper.children[1];
  const faceImgA = faceSlot.children[0];
  const faceImgB = faceSlot.children[1];

  const applyPromise = view.apply({
    state: { outfit: 'uniform', face: '01' },
    faceUrl: '/uniform/01.webp',
  });

  await waitForRaf();
  flushRaf();
  await applyPromise;

  // Both stage and face swapped together to Slot B
  assert.equal(stageImgB.classList.contains('is-visible'), true);
  assert.equal(faceImgB.classList.contains('is-visible'), true);
  assert.equal(stageImgA.classList.contains('is-hidden'), true);
  assert.equal(faceImgA.classList.contains('is-hidden'), true);

  // Revision matches
  const comm = view.getLastCommitted();
  assert.ok(comm);
  assert.equal(comm.revision, view.getRevision());
});

test('a stale decode after setModel paints nothing', async () => {
  const { view, spriteLayer, flushRaf, imageOptions } = setupView();
  const model = createTestModel();
  view.setModel(model);

  const motionWrapper = spriteLayer.children[0].children[0];
  const stageImgB = motionWrapper.children[1];

  let resolveOldDecode;
  imageOptions.decode = () => new Promise((resolve) => { resolveOldDecode = resolve; });

  const firstApply = view.apply({
    state: { outfit: 'uniform', face: '01' },
    faceUrl: '/uniform/01.webp',
  });

  // Newer setModel or apply comes before first decode finishes
  view.setModel(null);

  // Now old decode finishes
  resolveOldDecode();
  await firstApply;
  flushRaf();

  // Nothing was painted because it was stale
  assert.equal(stageImgB.classList.contains('is-visible'), false);
  assert.equal(view.getLastCommitted(), null);
});

test('a decode failure keeps the old picture and reports a status', async () => {
  const { view, spriteLayer, statusCalls, flushRaf, waitForRaf, imageOptions } = setupView();
  const model = createTestModel();
  view.setModel(model);

  // First apply succeeds
  const firstApply = view.apply({
    state: { outfit: 'uniform', face: '01' },
    faceUrl: '/uniform/01.webp',
  });
  await waitForRaf();
  flushRaf();
  await firstApply;

  const motionWrapper = spriteLayer.children[0].children[0];
  const stageImgB = motionWrapper.children[1]; // active
  const stageImgA = motionWrapper.children[0]; // next back buffer
  assert.equal(stageImgB.classList.contains('is-visible'), true);

  // Second apply fails to decode
  imageOptions.decode = () => Promise.reject(new Error('Corrupt WebP'));

  await view.apply({
    state: { outfit: 'uniform', face: '02' },
    faceUrl: '/uniform/02.webp',
  });
  flushRaf();

  // Old picture is preserved
  assert.equal(stageImgB.classList.contains('is-visible'), true);
  assert.equal(stageImgA.classList.contains('is-visible'), false);

  // Status reported
  assert.equal(statusCalls.length, 1);
  assert.equal(statusCalls[0].kind, 'decode-failed');
});

test('reduced motion skips the crossfade', async () => {
  const { view, spriteLayer, fxLayer, faceSlot, flushRaf, waitForRaf, imageOptions } = setupView();
  const model = createTestModel();
  view.setModel(model);

  const applyPromise = view.apply(
    { state: { outfit: 'uniform', face: '01' }, faceUrl: '/uniform/01.webp' },
    { reducedMotion: true }
  );
  await waitForRaf();
  flushRaf();
  await applyPromise;

  const motionWrapper = spriteLayer.children[0].children[0];
  const stageImgB = motionWrapper.children[1];
  const faceImgB = faceSlot.children[1];

  assert.equal(stageImgB.classList.contains('is-visible'), true);
  assert.equal(faceImgB.classList.contains('is-visible'), true);
  for (const el of [spriteLayer, fxLayer, faceSlot]) {
    assert.equal(el.classList.contains('adv-reduced-motion'), true);
  }
  const normalApply = view.apply(
    { state: { outfit: 'uniform', face: '02' }, faceUrl: '/uniform/02.webp' },
    { reducedMotion: false }
  );
  await waitForRaf(); flushRaf(); await normalApply;
  for (const el of [spriteLayer, fxLayer, faceSlot]) {
    assert.equal(el.classList.contains('adv-reduced-motion'), false);
  }
});

test('framing and crop math for the calibrated, stage-override and legacy cases', async () => {
  const { view, spriteLayer, faceSlot, flushRaf, waitForRaf, imageOptions } = setupView();
  const model = createTestModel();
  view.setModel(model);

  const motionWrapper = spriteLayer.children[0].children[0];
  const stageImgB = motionWrapper.children[1];
  const faceImgB = faceSlot.children[1];

  // 1. Calibrated case
  imageOptions.naturalWidth = 792;
  imageOptions.naturalHeight = 1623;

  const calibratedApply = view.apply({
    state: { outfit: 'uniform', face: '01' },
    faceUrl: '/uniform/01.webp',
    costume: model.costumes[0],
  });
  await waitForRaf();
  flushRaf();
  await calibratedApply;

  const expectedScale = FRAMING.faceHeightU / 325; // 340 / 325 = 1.0461538...
  const expectedLeft = FRAMING.centerX - 356 * expectedScale;
  const expectedTop = FRAMING.headTopY - 2 * expectedScale;
  const expectedWidth = 792 * expectedScale;
  const expectedHeight = 1623 * expectedScale;

  assert.equal(stageImgB.style.left, `calc(${expectedLeft} * var(--u))`);
  assert.equal(stageImgB.style.top, `calc(${expectedTop} * var(--u))`);
  assert.equal(stageImgB.style.width, `calc(${expectedWidth} * var(--u))`);
  assert.equal(stageImgB.style.height, `calc(${expectedHeight} * var(--u))`);

  // Face crop in calibrated case
  const expectedScaleFace = 450 / 362;
  assert.equal(faceImgB.style.left, `calc(${-175 * expectedScaleFace} * var(--u))`);
  assert.equal(faceImgB.style.top, `calc(${0} * var(--u))`);
  assert.equal(faceImgB.style.width, `calc(${792 * expectedScaleFace} * var(--u))`);
  assert.equal(faceImgB.style.height, `calc(${1623 * expectedScaleFace} * var(--u))`);
  assert.equal(faceImgB.style.display, '');

  // 2. Stage-override case
  const stageImgA = motionWrapper.children[0];
  const faceImgA = faceSlot.children[0];
  imageOptions.naturalWidth = 792;
  imageOptions.naturalHeight = 1623;

  const stageApply = view.apply({
    state: { outfit: 'stage_custom', face: '01' },
    faceUrl: '/stage_custom/01.webp',
    costume: model.costumes[1],
  });
  await waitForRaf();
  flushRaf();
  await stageApply;

  assert.equal(stageImgA.style.left, 'calc(340 * var(--u))');
  assert.equal(stageImgA.style.top, 'calc(-260 * var(--u))');
  assert.equal(stageImgA.style.width, `calc(${792 * 0.8} * var(--u))`);
  assert.equal(stageImgA.style.height, `calc(${1623 * 0.8} * var(--u))`);

  // 3. Legacy case: 351x1080 without calibration
  imageOptions.naturalWidth = 351;
  imageOptions.naturalHeight = 1080;

  const legacyApply = view.apply({
    state: { outfit: 'legacy', face: '01' },
    faceUrl: '/legacy/01.png',
    costume: model.costumes[2],
  });
  await waitForRaf();
  flushRaf();
  await legacyApply;

  const legacyScale = 1080 / 1080; // 1
  const legacyWidth = 351 * legacyScale;
  const legacyLeft = 1100 - (legacyWidth / 2); // 924.5
  assert.equal(stageImgB.style.left, `calc(${legacyLeft} * var(--u))`);
  assert.equal(stageImgB.style.top, 'calc(0 * var(--u))');
  assert.equal(stageImgB.style.width, `calc(${legacyWidth} * var(--u))`);
  assert.equal(stageImgB.style.height, 'calc(1080 * var(--u))');
  assert.equal(faceImgB.style.display, 'none', 'Legacy pack has no face crop');
});

test('a motion class is applied, then cleared, and suppressed while skipping', async () => {
  const { view, spriteLayer, flushRaf, waitForRaf, imageOptions } = setupView();
  const model = createTestModel();
  view.setModel(model);

  const motionWrapper = spriteLayer.children[0].children[0];

  // Applied
  const motionApply = view.apply(
    { state: { outfit: 'uniform', face: '01' }, faceUrl: '/uniform/01.webp' },
    { motion: 'nod' }
  );
  await waitForRaf();
  flushRaf();
  await motionApply;
  assert.equal(motionWrapper.classList.contains('adv-motion-nod'), true);

  // Animation end clears class
  motionWrapper.dispatchEvent({ type: 'animationend' });
  assert.equal(motionWrapper.classList.contains('adv-motion-nod'), false);

  // Suppressed while skipping
  const skipApply = view.apply(
    { state: { outfit: 'uniform', face: '01' }, faceUrl: '/uniform/01.webp' },
    { motion: 'bounce', skipping: true }
  );
  await waitForRaf();
  flushRaf();
  await skipApply;
  assert.equal(motionWrapper.classList.contains('adv-motion-bounce'), false);

  // Suppressed while reducedMotion
  const reducedApply = view.apply(
    { state: { outfit: 'uniform', face: '01' }, faceUrl: '/uniform/01.webp' },
    { motion: 'bounce', reducedMotion: true }
  );
  await waitForRaf();
  flushRaf();
  await reducedApply;
  assert.equal(motionWrapper.classList.contains('adv-motion-bounce'), false);

  // Suppressed while suppressActing
  const suppressApply = view.apply(
    { state: { outfit: 'uniform', face: '01' }, faceUrl: '/uniform/01.webp' },
    { motion: 'bounce', suppressActing: true }
  );
  await waitForRaf();
  flushRaf();
  await suppressApply;
  assert.equal(motionWrapper.classList.contains('adv-motion-bounce'), false);
});

test('an fx is placed at mapped headAnchor, then expires at 1200ms', async () => {
  const { view, fxLayer, flushRaf, waitForRaf } = setupView();
  const model = createTestModel();
  view.setModel(model);

  const uniformApply = view.apply({
    state: { outfit: 'uniform', face: '01', fx: 'sweat' },
    faceUrl: '/uniform/01.webp',
  });
  await waitForRaf();
  flushRaf();
  await uniformApply;

  assert.equal(fxLayer.children[0].children.length, 1);
  const fxEl = fxLayer.children[0].children[0];
  assert.equal(fxEl.classList.contains('adv-fx-sweat'), true);
  assert.equal(fxEl.style.left, `calc(${FRAMING.centerX} * var(--u))`);
  assert.equal(fxEl.style.top, `calc(${FRAMING.headTopY} * var(--u))`);

  // Wait 1250ms for expiry
  await new Promise(r => setTimeout(r, 1250));
  assert.equal(fxLayer.children[0].children.length, 0, 'FX should expire after 1200ms');

  // Legacy without headAnchor shows no fx
  const legacyApply = view.apply({
    state: { outfit: 'legacy', face: '01', fx: 'sweat' },
    faceUrl: '/legacy/01.png',
    costume: model.costumes[2],
  });
  await waitForRaf();
  flushRaf();
  await legacyApply;
  assert.equal(fxLayer.children[0].children.length, 0, 'No fx when headAnchor is absent');
});

test('the preload caps', async () => {
  const requested = [];
  let concurrent = 0;
  let maxObservedConcurrent = 0;

  class FakePreloadImage {
    constructor() { this.naturalWidth = 800; this.naturalHeight = 1600; }
    removeAttribute() {}
    set src(url) {
      requested.push(url);
      concurrent++;
      if (concurrent > maxObservedConcurrent) maxObservedConcurrent = concurrent;
      setTimeout(() => {
        concurrent--;
        if (typeof this.onload === 'function') this.onload();
      }, 5);
    }
  }

  const urls = [
    '/face/01.png',
    '/face/02.png',
    '/face/03.png',
    '/face/04.png',
    '/face/05.png',
    '/face/06.png',
    '/face/07.png',
    '/face/08.png',
  ];

  const results = await preload(urls, {
    maxConcurrent: 2,
    maxUrls: 6,
    ImageClass: FakePreloadImage,
  });

  assert.equal(results.length, 6, 'Should cap total URLs to 6');
  assert.equal(requested.length, 6);
  assert.ok(maxObservedConcurrent <= 2, `Concurrent requests was ${maxObservedConcurrent}, expected <= 2`);
});

test('staging and acting use separate transforms and fx follows the stage', async () => {
  const { view, spriteLayer, fxLayer, flushRaf, waitForRaf } = setupView();
  view.setModel(createTestModel());
  view.setStaging({ x: 1, depth: 2 });
  const pending = view.apply({ state: { outfit: 'uniform', face: '01', fx: 'heart' }, faceUrl: '/uniform/01.webp' }, { motion: 'nod' });
  await waitForRaf(); flushRaf(); await pending;
  const staging = spriteLayer.children[0];
  const motion = staging.children[0];
  assert.match(staging.style.transform, /scale\(/);
  assert.equal(fxLayer.children[0].style.transform, staging.style.transform);
  assert.equal(motion.classList.contains('adv-motion-nod'), true);
  view.setStaging({ x: 0, depth: 1 }, { instant: true });
  assert.equal(staging.style.transition, 'none');
  assert.equal(motion.classList.contains('adv-motion-nod'), true);
  view.destroy();
});
