/**
 * ADV Whole-Image Sprite Renderer
 * Handles A/B buffers, crossfades, framing math, motion, head-anchored fx, and preload caps.
 */

import { createFxElement, FX_TYPES } from './sprite_fx.js';
import { createActingGate } from '../sprite_state.js';

export const FRAMING = {
  centerX: 1100,
  headTopY: 70,
  faceHeightU: 340,
};

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

  function hideAllBuffers() {
    stageImgA.classList.remove('is-visible');
    stageImgA.classList.add('is-hidden');
    stageImgB.classList.remove('is-visible');
    stageImgB.classList.add('is-hidden');
    faceImgA.classList.remove('is-visible');
    faceImgA.classList.add('is-hidden');
    faceImgB.classList.remove('is-visible');
    faceImgB.classList.add('is-hidden');
  }

  function invalidate() {
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
    lastCommitted = null;
  }

  // Player input retains the stage, but exits the line and hides its crop.
  function suspend() {
    if (destroyed) return;
    invalidate();
    clearFx();
    clearMotion();
    for (const image of [faceImgA, faceImgB]) {
      image.style.visibility = 'hidden';
      image.classList.remove('is-visible');
      image.classList.add('is-hidden');
    }
    actingGate.resetLine();
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    clear();
    model = null;
    preloads.destroy();
    motionWrapper.remove();
    faceImgA.remove();
    faceImgB.remove();
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

  function apply(input, options = {}) {
    if (destroyed || !model) return Promise.resolve({ stale: true });
    invalidate();
    clearFx();
    setReducedMotion(Boolean(options.reducedMotion));
    if (suppressed(options)) clearMotion();
    if (!input?.faceUrl) {
      clear();
      return Promise.resolve({ stale: true });
    }

    const rev = revision;
    const { state, faceUrl, costume } = input;
    const costumeId = typeof costume === 'string' ? costume : (costume?.id || state?.outfit);
    const currentCostume = (typeof costume === 'object' && costume) ||
      model.costumes?.find(c => c.id === costumeId) || model.costumes?.[0] || null;
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
        const stageFront = activeSlot === 'A' ? stageImgA : stageImgB;
        const stageBack = activeSlot === 'A' ? stageImgB : stageImgA;
        const faceFront = activeSlot === 'A' ? faceImgA : faceImgB;
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
        for (const image of [stageFront, faceFront]) {
          image.classList.remove('is-visible');
          image.classList.add('is-hidden');
        }
        activeSlot = activeSlot === 'A' ? 'B' : 'A';
        lastCommitted = { state, faceUrl, costume: currentCostume, revision: rev };

        clearMotion();
        clearFx();
        const enabled = !suppressed(options);
        const time = now();
        const motion = MOTIONS.has(options.motion) ? options.motion : null;
        const fx = FX_TYPES.has(state?.fx) ? state.fx : null;
        const startMotion = actingGate.allowMotion(motion, time, enabled);
        const startFx = actingGate.allowFx(fx, time, enabled && Boolean(anchor));
        if (startMotion) {
          const motionClass = `adv-motion-${motion}`;
          // Establish the base even when restarting the same CSS animation.
          void motionWrapper.offsetWidth;
          motionWrapper.classList.add(motionClass);
          motionEndHandler = clearMotion;
          motionWrapper.addEventListener('animationend', motionEndHandler);
        }
        if (startFx) {
          const fxEl = createFxElement(fx, documentObj);
          fxEl.style.left = `calc(${anchor.x} * var(--u))`;
          fxEl.style.top = `calc(${anchor.y} * var(--u))`;
          fxLayer.appendChild(fxEl);
          fxTimeout = clock.setTimeout(clearFx, 1200);
        }
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

  return {
    setModel, apply, clear, suspend, destroy,
    preload: urls => preloads.add(urls),
    getModel: () => model,
    getRevision: () => revision,
    getActiveSlot: () => activeSlot,
    getLastCommitted: () => lastCommitted,
  };
}
