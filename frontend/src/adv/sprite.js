/**
 * ADV Sprite Renderer
 * Handles A/B buffers, crossfades, framing math, motion, head-anchored fx, and preload caps.
 *
 * Whole-image packs (v1, v2) draw one <img> per buffer. Layered packs (v3) draw
 * a frame set per buffer: a <div> of decoded layer images that holds every face
 * the committed face can show, so blink and lip sync only switch which face
 * group is visible.
 */

import { createFxElement, FX_TYPES } from './sprite_fx.js';
import { createActingGate } from '../sprite_state.js';

export const FRAMING = {
  centerX: 1100,
  headTopY: 70,
  faceHeightU: 340,
};

// Blink and lip sync timing, docs/design/adv-ui.md §6.3.
export const BLINK = { minDelayMs: 2000, maxDelayMs: 6000, halfMs: 60, closedMs: 90 };
export const LIP_SYNC = { intervalMs: 34, closedBelow: 0.012, halfBelow: 0.045, hysteresisMs: 40 };

const PRELOAD_BUDGET = 96 * 1024 * 1024;
const MOTIONS = new Set(['nod', 'bounce', 'startle', 'sigh', 'fidget', 'head-shake', 'lean-in', 'step-back']);

function releaseImage(image) {
  image.onload = null;
  image.onerror = null;
  image.removeAttribute?.('src');
  // Test doubles and image implementations without removeAttribute.
  if (!image.removeAttribute) image.src = '';
}

// The image stays detached for its entire lifetime. Cancelling also settles the
// decode waiter, even if the browser's decode promise never completes.
function loadImage(ImageClass, url, { speculative = false, beforeDecode = () => true } = {}) {
  const image = new ImageClass();
  let settled = false;
  let decoding = false;
  let finish;
  const promise = new Promise((resolve, reject) => {
    finish = (error) => {
      if (settled) return;
      settled = true;
      image.onload = image.onerror = null;
      if (error) reject(error);
      else resolve(image);
    };
  });
  const decode = () => {
    if (settled || decoding) return;
    decoding = true;
    const bytes = image.naturalWidth * image.naturalHeight * 4;
    if (speculative && (!Number.isFinite(bytes) || bytes <= 0 || bytes > PRELOAD_BUDGET)) {
      finish(new Error('preload-budget'));
      return;
    }
    if (!beforeDecode(image)) { finish(new Error('preload-budget')); return; }
    try {
      if (typeof image.decode === 'function') image.decode().then(() => finish(), finish);
      else finish();
    } catch (error) { finish(error); }
  };
  image.onerror = () => finish(new Error('image-load-failed'));
  image.onload = decode;
  image.src = url;
  if (!speculative && typeof image.decode === 'function') decode();
  else if (image.complete && image.naturalWidth > 0) decode();
  return { image, promise, release: () => releaseImage(image), cancel() {
    finish(new Error('image-cancelled'));
    releaseImage(image);
  } };
}

// One bounded queue/cache belongs to each view. Ready entries hold the decoded
// image; pending entries hold a cancellable load. FIFO eviction is deliberate.
function createPreloadQueue(ImageClass) {
  const entries = new Map();
  let active = 0;
  let bytes = 0;
  let destroyed = false;
  function drop(entry) {
    if (!entries.delete(entry.url)) return;
    if (entry.state === 'loading') { active--; entry.load.cancel(); }
    else entry.load?.release();
    bytes -= entry.bytes;
    entry.resolve({ url: entry.url, success: false });
  }
  function reserve(entry, image) {
    const size = image.naturalWidth * image.naturalHeight * 4;
    if (entries.get(entry.url) !== entry || !Number.isFinite(size) || size <= 0 || size > PRELOAD_BUDGET) return false;
    // Reserve before decoding, so two concurrent decodes cannot exceed the
    // combined budget. Eviction cancels an older speculative decode too.
    while (bytes + size > PRELOAD_BUDGET) {
      drop([...entries.values()].find(item => item.bytes > 0));
    }
    entry.bytes = size;
    bytes += size;
    return true;
  }
  function pump() {
    if (destroyed) return;
    for (const entry of entries.values()) {
      if (active >= 2) break;
      if (entry.state !== 'queued') continue;
      entry.state = 'loading';
      active++;
      entry.load = loadImage(ImageClass, entry.url, { speculative: true,
        beforeDecode: image => reserve(entry, image),
      });
      entry.load.promise.then(image => complete(entry, image), () => complete(entry, null));
    }
  }
  function complete(entry, image) {
    if (entries.get(entry.url) !== entry || entry.state !== 'loading') return;
    active--;
    entry.state = 'ready';
    const size = image ? image.naturalWidth * image.naturalHeight * 4 : 0;
    if (!image || !Number.isFinite(size) || size <= 0 || size > PRELOAD_BUDGET) drop(entry);
    else entry.resolve({ url: entry.url, success: true });
    pump();
  }
  return {
    add(urls) {
      if (destroyed || typeof ImageClass !== 'function' || !Array.isArray(urls)) return Promise.resolve([]);
      const promises = [];
      for (const item of [...new Set(urls)].slice(0, 6)) {
        const url = typeof item === 'string' ? item : item?.url;
        if (!url) continue;
        const estimated = item?.width * item?.height * 4;
        if (Number.isFinite(estimated) && estimated > PRELOAD_BUDGET) continue;
        if (!entries.has(url)) {
          while (entries.size >= 6) drop(entries.values().next().value);
          let resolve;
          const promise = new Promise(done => { resolve = done; });
          entries.set(url, { url, state: 'queued', bytes: 0, promise, resolve });
        }
        promises.push(entries.get(url).promise);
      }
      pump();
      return Promise.all(promises);
    },
    cancelPending() {
      for (const entry of entries.values()) if (entry.state !== 'ready') drop(entry);
    },
    clear() { for (const entry of entries.values()) drop(entry); },
    destroy() { destroyed = true; this.clear(); },
  };
}

// Standalone convenience helper; views use their own persistent queue instead.
export async function preload(urls, { ImageClass = globalThis.Image } = {}) {
  const queue = createPreloadQueue(ImageClass);
  try { return await queue.add(urls); }
  finally { queue.destroy(); }
}

// The faces one committed face can show: itself, its blink frames, its mouth
// frames and its reviewed blink-during-speech frames. The loader has already
// checked that every partner is a face of the same look.
function frameFaces(look, baseId) {
  const entry = look.faces[baseId];
  const ids = [baseId];
  const add = id => { if (typeof id === 'string' && look.faces[id] && !ids.includes(id)) ids.push(id); };
  if (entry.eyesClosed !== true && entry.blink?.closed) { add(entry.blink.half); add(entry.blink.closed); }
  if (entry.mouth?.open) { add(entry.mouth.closed); add(entry.mouth.half); add(entry.mouth.open); }
  for (const id of Object.values(entry.combinedFrames || {})) add(id);
  return ids;
}

function faceLayerIds(look, faceId) {
  const layers = look.faces[faceId]?.layers;
  return Array.isArray(layers) ? layers : [];
}

// While blushing: the face's own blush override, else the look's blush.
function blushLayerIds(look, faceId, blush) {
  if (!blush) return [];
  const entry = look.faces[faceId];
  if (Array.isArray(entry?.blush)) return entry.blush;
  return Array.isArray(look.blush) ? look.blush : [];
}

/**
 * Plans the layered frame set for a resolved v3 state.
 * Items come in append order: body, each face group, the blush, then fx. The
 * blush belongs to the line's face, so blink and mouth frames keep it. Every
 * element gets the rank of its `z` as z-index; equal `z` then keeps list order,
 * because each group's list is body, its face layers, blush, fx.
 */
export function planLayers(model, lookKey, state) {
  const look = model?.looks?.[lookKey];
  const base = state?.face;
  const crop = look?.crop;
  if (!look?.faces?.[base] || !(crop?.width > 0) || !(crop?.height > 0)) return null;
  const blush = state.blush === true;
  const items = [];
  const push = (layerId, group, fx = false) => {
    const layer = model.layers?.[layerId];
    if (!layer?.file || !layer.rect) return;
    const z = Number.isFinite(layer.z) ? layer.z : 0;
    const opacity = Number.isFinite(layer.opacity) ? Math.min(1, Math.max(0, layer.opacity)) : 1;
    items.push({ id: layerId, file: layer.file, rect: layer.rect, z, opacity, group, fx });
  };
  for (const id of Array.isArray(look.body) ? look.body : []) push(id, null);
  const groups = frameFaces(look, base);
  for (const faceId of groups) {
    for (const id of faceLayerIds(look, faceId)) push(id, faceId);
  }
  for (const id of blushLayerIds(look, base, blush)) push(id, null);
  const fxName = typeof state.fx === 'string' && Array.isArray(look.fx?.[state.fx]) ? state.fx : null;
  if (fxName) for (const id of look.fx[fxName]) push(id, null, true);
  const ranks = [...new Set(items.map(item => item.z))].sort((a, b) => a - b);
  for (const item of items) item.rank = ranks.indexOf(item.z);
  return { lookKey, look, base, crop, blush, groups, items, fxName: items.some(item => item.fx) ? fxName : null };
}

const percent = (value, total) => `${(value / total) * 100}%`;

export function createSpriteView({
  spriteLayer,
  fxLayer,
  faceSlot,
  onStatus,
  now = () => performance.now(),
  raf = (typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (cb) => setTimeout(cb, 16)),
  cancelRaf = (typeof cancelAnimationFrame === 'function' ? cancelAnimationFrame : clearTimeout),
  documentObj = globalThis.document,
  ImageClass = globalThis.Image,
  clock = { setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: id => clearTimeout(id) },
  random = Math.random,
}) {
  let model = null;
  let revision = 0;
  let activeSlot = 'A';
  let fxTimeout = null;
  let destroyed = false;
  let pending = null;
  let actingGate = createActingGate();
  const preloads = createPreloadQueue(ImageClass);
  let lastCommitted = null;
  let motionEndHandler = null;
  // Layered state. `frame` is the frame set of the active slot. Blink and mouth
  // frames are transient: they change which group is visible, never the base.
  let layerBuffers = null;
  const layerImages = new Map();
  let frame = null;
  let reducedMotion = false;
  const enabledAnimations = { blink: true, lipSync: true };
  let blinkTimer = null;
  let blinkPhase = null;
  let speech = null;
  let speechToken = 0;

  // Build stage DOM
  const motionWrapper = documentObj.createElement('div');
  motionWrapper.className = 'adv-sprite-motion';
  spriteLayer.appendChild(motionWrapper);

  const stageImgA = documentObj.createElement('img');
  stageImgA.className = 'adv-sprite-buffer is-hidden';
  const stageImgB = documentObj.createElement('img');
  stageImgB.className = 'adv-sprite-buffer is-hidden';
  motionWrapper.appendChild(stageImgA);
  motionWrapper.appendChild(stageImgB);

  // Build face slot DOM
  faceSlot.style.overflow = 'hidden';
  faceSlot.style.position = 'relative';

  const faceImgA = documentObj.createElement('img');
  faceImgA.className = 'adv-face-buffer is-hidden';
  const faceImgB = documentObj.createElement('img');
  faceImgB.className = 'adv-face-buffer is-hidden';
  faceSlot.appendChild(faceImgA);
  faceSlot.appendChild(faceImgB);

  documentObj.addEventListener?.('visibilitychange', refreshAnimation);

  // Layer containers exist only once a layered pack draws, so whole-image packs
  // keep exactly the DOM they had before.
  function ensureLayerBuffers() {
    if (layerBuffers) return layerBuffers;
    const make = (parent, className) => {
      const element = documentObj.createElement('div');
      element.className = `${className} adv-layer-buffer is-hidden`;
      parent.appendChild(element);
      return element;
    };
    layerBuffers = {
      stage: { A: make(motionWrapper, 'adv-sprite-buffer'), B: make(motionWrapper, 'adv-sprite-buffer') },
      face: { A: make(faceSlot, 'adv-face-buffer'), B: make(faceSlot, 'adv-face-buffer') },
    };
    return layerBuffers;
  }

  function layerContainers() {
    if (!layerBuffers) return [];
    return [layerBuffers.stage.A, layerBuffers.stage.B, layerBuffers.face.A, layerBuffers.face.B];
  }

  function clearFx() {
    if (fxTimeout !== null) {
      clock.clearTimeout(fxTimeout);
      fxTimeout = null;
    }
    fxLayer.innerHTML = '';
  }

  function clearMotion() {
    if (motionEndHandler) {
      motionWrapper.removeEventListener('animationend', motionEndHandler);
      motionEndHandler = null;
    }
    motionWrapper.className = 'adv-sprite-motion';
  }

  function setReducedMotion(reduced) {
    for (const el of [spriteLayer, fxLayer, faceSlot]) {
      if (!el) continue;
      if (reduced) el.classList.add('adv-reduced-motion');
      else el.classList.remove('adv-reduced-motion');
    }
    if (reducedMotion !== Boolean(reduced)) {
      reducedMotion = Boolean(reduced);
      refreshAnimation();
    }
  }

  function preloadUpcomingFaces(costume, faceId) {
    if (!model || !costume) return;
    const sprites = model.sprites?.[costume.id] || {};
    const ids = Object.keys(sprites).filter((id) => sprites[id]?.file);
    if (!ids.length) return;
    const start = ids.indexOf(faceId);
    const upcoming = [];
    for (let step = 1; step <= 2; step += 1) {
      upcoming.push(ids[(start + step + ids.length) % ids.length]);
    }
    const urls = upcoming.filter(id => id !== faceId).map(id => ({
      url: sprites[id].file, width: sprites[id].width, height: sprites[id].height,
    }));
    if (urls.length) preloads.add(urls);
  }

  // Layered packs preload the frame sets of the look's next two faces, in the
  // same queue as whole images: one budget, two decodes and six entries. Faces
  // already in a frame set, such as blink and mouth partners, are skipped.
  function preloadUpcomingLayers(plan) {
    const ids = Object.keys(plan.look.faces);
    const start = ids.indexOf(plan.base);
    const covered = new Set(plan.groups);
    const upcoming = [];
    for (let step = 1; step < ids.length && upcoming.length < 2; step += 1) {
      const id = ids[(start + step) % ids.length];
      if (covered.has(id)) continue;
      upcoming.push(id);
      for (const faceId of frameFaces(plan.look, id)) covered.add(faceId);
    }
    const shown = new Set(plan.items.map(item => item.file));
    const urls = new Map();
    for (const id of upcoming) {
      const layerIds = frameFaces(plan.look, id).flatMap(faceId => faceLayerIds(plan.look, faceId));
      for (const layerId of [...layerIds, ...blushLayerIds(plan.look, id, plan.blush)]) {
        const layer = model.layers?.[layerId];
        if (!layer?.file || shown.has(layer.file) || urls.has(layer.file)) continue;
        urls.set(layer.file, { url: layer.file, width: layer.rect?.width, height: layer.rect?.height });
      }
    }
    if (urls.size) preloads.add([...urls.values()]);
  }

  function hideAllBuffers() {
    stageImgA.classList.remove('is-visible');
    stageImgA.classList.add('is-hidden');
    stageImgB.classList.remove('is-visible');
    stageImgB.classList.add('is-hidden');
    faceImgA.classList.remove('is-visible');
    faceImgA.classList.add('is-hidden');
    faceImgB.classList.remove('is-visible');
    faceImgB.classList.add('is-hidden');
    for (const container of layerContainers()) {
      container.classList.remove('is-visible');
      container.classList.add('is-hidden');
    }
  }

  function hideSlot(slot) {
    const elements = slot === 'A' ? [stageImgA, faceImgA] : [stageImgB, faceImgB];
    if (layerBuffers) elements.push(layerBuffers.stage[slot], layerBuffers.face[slot]);
    for (const element of elements) {
      element.classList.remove('is-visible');
      element.classList.add('is-hidden');
    }
  }

  function invalidate() {
    cancelBlink();
    revision++;
    if (pending) pending.finish({ stale: true });
    preloads.cancelPending();
  }

  function setModel(newModel, { preserveCurrent = false } = {}) {
    if (destroyed) return;
    if (preserveCurrent && model && newModel) {
      invalidate(); preloads.clear(); clearFx(); clearMotion();
    } else clear();
    model = newModel;
    actingGate = createActingGate();
  }

  function clearLayers() {
    cancelBlink();
    frame = null;
    for (const container of layerContainers()) {
      container.style.visibility = 'hidden';
      for (const image of layerImages.get(container) || []) releaseImage(image);
      layerImages.delete(container);
      container.replaceChildren();
    }
  }

  function clear() {
    invalidate();
    preloads.clear();
    clearFx();
    clearMotion();
    hideAllBuffers();
    for (const image of [stageImgA, stageImgB, faceImgA, faceImgB]) {
      image.style.visibility = 'hidden';
      releaseImage(image);
    }
    clearLayers();
    lastCommitted = null;
  }

  // Player input retains the stage, but exits the line and hides its crop.
  function suspend() {
    if (destroyed) return;
    invalidate();
    clearFx();
    clearMotion();
    const faces = [faceImgA, faceImgB];
    if (layerBuffers) faces.push(layerBuffers.face.A, layerBuffers.face.B);
    for (const image of faces) {
      image.style.visibility = 'hidden';
      image.classList.remove('is-visible');
      image.classList.add('is-hidden');
    }
    showSourceFx(false);
    actingGate.resetLine();
    // The stage keeps its frame, and keeps blinking while the player types.
    refreshAnimation();
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    stopLipSync();
    clear();
    model = null;
    preloads.destroy();
    documentObj.removeEventListener?.('visibilitychange', refreshAnimation);
    motionWrapper.remove();
    faceImgA.remove();
    faceImgB.remove();
    if (layerBuffers) {
      layerBuffers.face.A.remove();
      layerBuffers.face.B.remove();
    }
  }

  function computeFraming(costume, sourceWidth, sourceHeight) {
    // 1. Stage override
    if (costume?.stage) {
      const scale = costume.stage.scale;
      const ox = costume.stage.offset.x;
      const oy = costume.stage.offset.y;
      const stageStyle = {
        left: `calc(${ox} * var(--u))`,
        top: `calc(${oy} * var(--u))`,
        width: `calc(${sourceWidth * scale} * var(--u))`,
        height: `calc(${sourceHeight * scale} * var(--u))`,
      };

      let faceStyle = null;
      if (costume.faceRect) {
        const scaleFace = 450 / costume.faceRect.width;
        faceStyle = {
          left: `calc(${-costume.faceRect.x * scaleFace} * var(--u))`,
          top: `calc(${-costume.faceRect.y * scaleFace} * var(--u))`,
          width: `calc(${sourceWidth * scaleFace} * var(--u))`,
          height: `calc(${sourceHeight * scaleFace} * var(--u))`,
        };
      }

      let anchor = null;
      if (costume.headAnchor) {
        anchor = {
          x: ox + costume.headAnchor.x * scale,
          y: oy + costume.headAnchor.y * scale,
        };
      }

      return { stageStyle, faceStyle, anchor };
    }

    // 2. Calibrated
    if (costume?.faceRect && costume?.headAnchor) {
      const scale = FRAMING.faceHeightU / costume.faceRect.height;
      const leftU = FRAMING.centerX - costume.headAnchor.x * scale;
      const topU = FRAMING.headTopY - costume.headAnchor.y * scale;
      const widthU = sourceWidth * scale;
      const heightU = sourceHeight * scale;

      const stageStyle = {
        left: `calc(${leftU} * var(--u))`,
        top: `calc(${topU} * var(--u))`,
        width: `calc(${widthU} * var(--u))`,
        height: `calc(${heightU} * var(--u))`,
      };

      const scaleFace = 450 / costume.faceRect.width;
      const faceStyle = {
        left: `calc(${-costume.faceRect.x * scaleFace} * var(--u))`,
        top: `calc(${-costume.faceRect.y * scaleFace} * var(--u))`,
        width: `calc(${sourceWidth * scaleFace} * var(--u))`,
        height: `calc(${sourceHeight * scaleFace} * var(--u))`,
      };

      const anchor = {
        x: FRAMING.centerX,
        y: FRAMING.headTopY,
      };

      return { stageStyle, faceStyle, anchor };
    }

    // 3. Legacy fallback (without calibration)
    const scale = sourceHeight > 0 ? 1080 / sourceHeight : 1;
    const widthU = sourceWidth * scale;
    const leftU = 1100 - (widthU / 2);
    const topU = 1080 - (sourceHeight * scale);

    const stageStyle = {
      left: `calc(${leftU} * var(--u))`,
      top: `calc(${topU} * var(--u))`,
      width: `calc(${widthU} * var(--u))`,
      height: `calc(1080 * var(--u))`,
    };

    return { stageStyle, faceStyle: null, anchor: null };
  }

  function suppressed(options) {
    return Boolean(options.skipping || options.reducedMotion || options.suppressActing || documentObj.hidden);
  }

  // Finish any fade on a buffer before changing its source or framing. Flush
  // the hidden base once so restoring the transition starts a fresh fade.
  function prepareBack(image) {
    for (const animation of image.getAnimations?.() || []) animation.cancel();
    image.style.transition = 'none';
    image.style.opacity = '0';
    image.classList.remove('is-visible');
    image.classList.add('is-hidden');
  }
  function showBack(image, visible) {
    void image.offsetWidth;
    image.style.transition = '';
    image.style.opacity = '';
    image.style.visibility = 'visible';
    if (visible) {
      image.classList.remove('is-hidden');
      image.classList.add('is-visible');
    }
  }

  // Motion and fx for a committed frame. Source fx layers replace the SVG mark,
  // never both, and need no head anchor.
  function startActing(state, options, anchor, sourceFx) {
    clearMotion();
    clearFx();
    const enabled = !suppressed(options);
    const time = now();
    const motion = MOTIONS.has(options.motion) ? options.motion : null;
    const fx = FX_TYPES.has(state?.fx) ? state.fx : null;
    const layered = fx !== null && sourceFx === fx;
    const startMotion = actingGate.allowMotion(motion, time, enabled);
    const startFx = actingGate.allowFx(fx, time, enabled && (layered || Boolean(anchor)));
    if (startMotion) {
      const motionClass = `adv-motion-${motion}`;
      // Establish the base even when restarting the same CSS animation.
      void motionWrapper.offsetWidth;
      motionWrapper.classList.add(motionClass);
      motionEndHandler = clearMotion;
      motionWrapper.addEventListener('animationend', motionEndHandler);
    }
    if (startFx && layered) {
      showSourceFx(true);
    } else if (startFx) {
      const fxEl = createFxElement(fx, documentObj);
      fxEl.style.left = `calc(${anchor.x} * var(--u))`;
      fxEl.style.top = `calc(${anchor.y} * var(--u))`;
      fxLayer.appendChild(fxEl);
      fxTimeout = clock.setTimeout(clearFx, 1200);
    }
  }

  function createRequest(rev) {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    const request = { frame: null, loads: [], done: false, finish(result) {
      if (request.done) return;
      request.done = true;
      if (request.frame !== null) cancelRaf(request.frame);
      request.frame = null;
      for (const load of request.loads) load.cancel();
      request.loads = [];
      if (pending === request) pending = null;
      resolve(result);
    } };
    pending = request;
    const current = () => !request.done && !destroyed && model && rev === revision;
    return { request, promise, current };
  }

  function apply(input, options = {}) {
    if (destroyed || !model) return Promise.resolve({ stale: true });
    invalidate();
    clearFx();
    setReducedMotion(Boolean(options.reducedMotion));
    if (suppressed(options)) clearMotion();
    if (input?.isV3) return applyLayers(input, options);
    if (!input?.faceUrl) {
      clear();
      return Promise.resolve({ stale: true });
    }

    const rev = revision;
    const { state, faceUrl, costume } = input;
    const costumeId = typeof costume === 'string' ? costume : (costume?.id || state?.outfit);
    const currentCostume = (typeof costume === 'object' && costume) ||
      model.costumes?.find(c => c.id === costumeId) || model.costumes?.[0] || null;
    const { request, promise, current } = createRequest(rev);
    try {
      for (let i = 0; i < 2; i++) request.loads.push(loadImage(ImageClass, faceUrl));
    } catch (error) {
      request.finish({ stale: false, error });
      onStatus?.({ kind: 'decode-failed', error });
      return promise;
    }
    Promise.all(request.loads.map(load => load.promise)).then(([decoded]) => {
      if (!current()) return;
      const width = decoded.naturalWidth || decoded.width;
      const height = decoded.naturalHeight || decoded.height;
      const { stageStyle, faceStyle, anchor } = computeFraming(currentCostume, width, height);
      request.frame = raf(() => {
        request.frame = null;
        if (!current()) return;
        const stageBack = activeSlot === 'A' ? stageImgB : stageImgA;
        const faceBack = activeSlot === 'A' ? faceImgB : faceImgA;
        setReducedMotion(Boolean(options.reducedMotion));
        prepareBack(stageBack);
        prepareBack(faceBack);
        stageBack.src = faceUrl;
        faceBack.src = faceUrl;
        Object.assign(stageBack.style, stageStyle);
        if (faceStyle) Object.assign(faceBack.style, faceStyle);
        faceBack.style.display = faceStyle ? '' : 'none';
        showBack(stageBack, true);
        showBack(faceBack, Boolean(faceStyle));
        hideSlot(activeSlot);
        activeSlot = activeSlot === 'A' ? 'B' : 'A';
        lastCommitted = { state, faceUrl, costume: currentCostume, revision: rev };
        if (frame) clearLayers();

        startActing(state, options, anchor, null);
        request.finish({ stale: false, revision: rev });
        preloadUpcomingFaces(currentCostume, state?.face);
      });
    }, error => {
      if (!current()) return;
      request.finish({ stale: false, error });
      onStatus?.({ kind: 'decode-failed', error });
    });
    return promise;
  }

  function applyLayers(input, options) {
    const rev = revision;
    const state = input.state || {};
    const plan = planLayers(model, input.lookKey, state);
    if (!plan) {
      clear();
      return Promise.resolve({ stale: true });
    }
    // The old frame stays until the new one is decoded; end its blink now.
    cancelBlink();
    const { request, promise, current } = createRequest(rev);
    try {
      // Two decoded copies of every layer: one for the stage, one for the face window.
      for (let copy = 0; copy < 2; copy++) {
        for (const item of plan.items) request.loads.push(loadImage(ImageClass, item.file));
      }
    } catch (error) {
      request.finish({ stale: false, error });
      onStatus?.({ kind: 'decode-failed', error });
      refreshAnimation();
      return promise;
    }
    Promise.all(request.loads.map(load => load.promise)).then(images => {
      if (!current()) return;
      const framing = computeFraming(plan.look, plan.crop.width, plan.crop.height);
      request.frame = raf(() => {
        request.frame = null;
        if (!current()) return;
        // The decoded images become the layer elements, so finishing the
        // request must not cancel and release them.
        request.loads = [];
        commitLayers(plan, images, framing, state, options, rev);
        request.finish({ stale: false, revision: rev });
        preloadUpcomingLayers(plan);
      });
    }, error => {
      if (!current()) return;
      request.finish({ stale: false, error });
      onStatus?.({ kind: 'decode-failed', error });
      // A failed load keeps the previous complete frame, which may blink again.
      refreshAnimation();
    });
    return promise;
  }

  // Fills a back container with decoded layer images. Images of other face
  // groups and fx start hidden; showFace and showSourceFx only toggle visibility.
  function fillLayers(container, plan, images) {
    for (const image of layerImages.get(container) || []) releaseImage(image);
    const groups = new Map(plan.groups.map(faceId => [faceId, []]));
    const fx = [];
    const { crop } = plan;
    plan.items.forEach((item, index) => {
      const image = images[index];
      image.className = 'adv-sprite-layer';
      image.alt = '';
      Object.assign(image.style, {
        left: percent(item.rect.x - crop.left, crop.width),
        top: percent(item.rect.y - crop.top, crop.height),
        width: percent(item.rect.width, crop.width),
        height: percent(item.rect.height, crop.height),
        zIndex: String(item.rank),
        opacity: item.opacity < 1 ? String(item.opacity) : '',
        visibility: item.fx || (item.group !== null && item.group !== plan.base) ? 'hidden' : '',
      });
      if (item.group !== null) groups.get(item.group).push(image);
      if (item.fx) fx.push(image);
    });
    container.replaceChildren(...images);
    layerImages.set(container, images);
    return { groups, fx };
  }

  function commitLayers(plan, images, { stageStyle, faceStyle, anchor }, state, options, rev) {
    cancelBlink();
    const buffers = ensureLayerBuffers();
    const back = activeSlot === 'A' ? 'B' : 'A';
    const stageBack = buffers.stage[back];
    const faceBack = buffers.face[back];
    setReducedMotion(Boolean(options.reducedMotion));
    prepareBack(stageBack);
    prepareBack(faceBack);
    const count = plan.items.length;
    const stage = fillLayers(stageBack, plan, images.slice(0, count));
    const face = fillLayers(faceBack, plan, images.slice(count));
    Object.assign(stageBack.style, stageStyle);
    if (faceStyle) Object.assign(faceBack.style, faceStyle);
    faceBack.style.display = faceStyle ? '' : 'none';
    showBack(stageBack, true);
    showBack(faceBack, Boolean(faceStyle));
    hideSlot(activeSlot);
    activeSlot = back;
    frame = { plan, stage, face, shown: plan.base };
    lastCommitted = { state, lookKey: plan.lookKey, costume: plan.look, revision: rev };
    pending = null;
    // Show the current mouth level on the new face and schedule its blink.
    refreshAnimation();
    startActing(state, options, anchor, plan.fxName);
  }

  function showSourceFx(visible) {
    if (!frame) return;
    for (const image of [...frame.stage.fx, ...frame.face.fx]) image.style.visibility = visible ? '' : 'hidden';
  }

  // The visible face: the base, a blink frame, a mouth frame, or a reviewed
  // combined frame for a blink during speech. Without one, the blink frame wins.
  function visibleFace() {
    const { plan } = frame;
    const entry = plan.look.faces[plan.base];
    const eyes = blinkPhase === 'half' ? entry.blink?.half : (blinkPhase === 'closed' ? entry.blink?.closed : null);
    let mouth = null;
    let mouthFace = null;
    if (speech && entry.mouth?.open && lipSyncAllowed()) {
      mouth = speech.level === 'half' && !entry.mouth.half ? 'open' : speech.level;
      mouthFace = mouth === 'closed' ? (entry.mouth.closed || plan.base) : entry.mouth[mouth];
    }
    let target = plan.base;
    if (eyes && mouth && mouth !== 'closed') target = entry.combinedFrames?.[`${blinkPhase}:${mouth}`] || eyes;
    else if (eyes) target = eyes;
    else if (mouthFace) target = mouthFace;
    return frame.stage.groups.has(target) ? target : plan.base;
  }

  // Shows one face group in the stage and the face window together. Every
  // frame was decoded before the swap, so this never waits and never flickers.
  function showFace() {
    if (!frame) return;
    const target = visibleFace();
    if (target === frame.shown) return;
    frame.shown = target;
    for (const built of [frame.stage, frame.face]) {
      for (const [faceId, images] of built.groups) {
        const visibility = faceId === target ? '' : 'hidden';
        for (const image of images) image.style.visibility = visibility;
      }
    }
  }

  function blinkAllowed() {
    const entry = frame?.plan.look.faces[frame.plan.base];
    return Boolean(entry && entry.eyesClosed !== true && entry.blink?.closed &&
      !pending && enabledAnimations.blink && !reducedMotion && !documentObj.hidden && !destroyed);
  }

  function cancelBlink() {
    if (blinkTimer !== null) clock.clearTimeout(blinkTimer);
    blinkTimer = null;
    if (blinkPhase) {
      blinkPhase = null;
      showFace();
    }
  }

  function scheduleBlink() {
    cancelBlink();
    if (!blinkAllowed()) return;
    const delay = BLINK.minDelayMs + random() * (BLINK.maxDelayMs - BLINK.minDelayMs);
    blinkTimer = clock.setTimeout(runBlink, delay);
  }

  function runBlink() {
    blinkTimer = null;
    if (!blinkAllowed()) return;
    const { half } = frame.plan.look.faces[frame.plan.base].blink;
    const steps = half
      ? [['half', BLINK.halfMs], ['closed', BLINK.closedMs], ['half', BLINK.halfMs]]
      : [['closed', BLINK.closedMs]];
    const step = index => {
      blinkTimer = null;
      if (index === steps.length) {
        blinkPhase = null;
        showFace();
        scheduleBlink();
        return;
      }
      blinkPhase = steps[index][0];
      showFace();
      blinkTimer = clock.setTimeout(() => step(index + 1), steps[index][1]);
    };
    step(0);
  }

  function lipSyncAllowed() {
    return enabledAnimations.lipSync && !reducedMotion && !documentObj.hidden && !destroyed;
  }

  function pauseSpeech() {
    if (speech.timer !== null) clock.clearTimeout(speech.timer);
    speech.timer = null;
    speech.level = 'closed';
    speech.candidate = null;
  }

  // Samples the voice level at most 30 times a second. A new level must hold
  // for the hysteresis time before the mouth follows it.
  function pollSpeech() {
    speech.timer = null;
    if (!lipSyncAllowed()) {
      pauseSpeech();
      showFace();
      return;
    }
    // A suspended context keeps its last analyser buffer, so paused reads as silent.
    const rms = speech.source.paused ? 0 : Number(speech.source.getRms()) || 0;
    const level = rms < LIP_SYNC.closedBelow ? 'closed' : (rms < LIP_SYNC.halfBelow ? 'half' : 'open');
    const time = now();
    if (level === speech.level) {
      speech.candidate = null;
    } else if (speech.candidate !== level) {
      speech.candidate = level;
      speech.since = time;
    } else if (time - speech.since >= LIP_SYNC.hysteresisMs) {
      speech.level = level;
      speech.candidate = null;
      showFace();
    }
    speech.timer = clock.setTimeout(pollSpeech, LIP_SYNC.intervalMs);
  }

  /**
   * Drives mouth frames from the voice level while a character line plays.
   * It outlives face changes: the voice can start before the line's face
   * commits, and faces without a mouth partner just keep their mouth closed.
   * Returns a token; stopLipSync(token) from an older line is ignored.
   */
  function startLipSync({ audioPlayer, characterVoice = true } = {}) {
    if (destroyed || !characterVoice || typeof audioPlayer?.getRms !== 'function') return 0;
    if (speech) pauseSpeech();
    speech = { source: audioPlayer, token: ++speechToken, timer: null, level: 'closed', candidate: null, since: 0 };
    if (lipSyncAllowed()) pollSpeech();
    showFace();
    return speech.token;
  }

  function stopLipSync(token) {
    if (!speech || (token !== undefined && token !== speech.token)) return;
    pauseSpeech();
    speech = null;
    showFace();
  }

  // Re-checks blink and lip sync after a setting, reduced-motion, visibility or
  // frame change. A disallowed animation stops at once and shows the base face.
  function refreshAnimation() {
    if (!blinkAllowed()) cancelBlink();
    else if (blinkTimer === null && !blinkPhase) scheduleBlink();
    if (speech) {
      if (!lipSyncAllowed()) pauseSpeech();
      else if (speech.timer === null) pollSpeech();
    }
    showFace();
  }

  function setAnimationOptions({ advBlink, advLipSync, reducedMotion: reduced } = {}) {
    if (destroyed) return;
    if (typeof advBlink === 'boolean') enabledAnimations.blink = advBlink;
    if (typeof advLipSync === 'boolean') enabledAnimations.lipSync = advLipSync;
    if (typeof reduced === 'boolean') setReducedMotion(reduced);
    refreshAnimation();
  }

  return {
    setModel, apply, clear, suspend, destroy,
    startLipSync, stopLipSync, setAnimationOptions,
    preload: urls => preloads.add(urls),
    preloadState(input) {
      if (!input || !model) return;
      if (!input.isV3) return preloads.add(input.faceUrl ? [input.faceUrl] : []);
      const plan = planLayers(model, input.lookKey, input.state);
      if (!plan) return;
      // Keep the queue's six-entry budget for distinct facial frames first.
      const urls = new Map();
      const items = [...plan.items].sort((a, b) => Number(b.group !== null) - Number(a.group !== null));
      for (const item of items) if (!urls.has(item.file)) urls.set(item.file, {
        url: item.file, width: item.rect?.width, height: item.rect?.height,
      });
      return preloads.add([...urls.values()]);
    },
    getModel: () => model,
    getRevision: () => revision,
    getActiveSlot: () => activeSlot,
    getLastCommitted: () => lastCommitted,
    getVisibleFace: () => frame?.shown ?? null,
  };
}
