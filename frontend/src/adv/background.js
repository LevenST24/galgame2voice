/**
 * background.js - ADV stage background.
 *
 * Draws the selected artwork (day / evening / night variant) on the stage
 * background layer, crossfading between two persistent <img> buffers, or keeps
 * the CSS gradient sky when nothing is selected or nothing could be loaded.
 */

export const BG_TIME_CHOICES = ['auto', 'day', 'evening', 'night'];

// A missing variant is replaced by the next closest light level.
const FALLBACK_ORDER = {
  day: ['day', 'evening', 'night'],
  evening: ['evening', 'day', 'night'],
  night: ['night', 'evening', 'day'],
};

const AUTO_CHECK_MS = 60000;

export function timeOfDay(date) {
  const hour = typeof date?.getHours === 'function' ? date.getHours() : NaN;
  if (!Number.isInteger(hour) || hour < 0 || hour > 23) return 'day';
  if (hour >= 6 && hour <= 15) return 'day';
  if (hour >= 16 && hour <= 18) return 'evening';
  return 'night';
}

export function resolveVariant(background, time) {
  const variants = background?.variants;
  if (!variants || typeof variants !== 'object') return null;
  for (const name of FALLBACK_ORDER[time] || FALLBACK_ORDER.day) {
    const url = variants[name];
    if (typeof url === 'string' && url) return { time: name, url };
  }
  return null;
}

// Stored advBackground values: '' follows the character's work, 'none' is the plain sky,
// anything else is a global "<work>/<id>" key.
export const BG_AUTO = '';
export const BG_NONE = 'none';

// The key the stage shows for a stored value; '' (the sky) when nothing fits.
export function effectiveBackgroundKey(stored, { list, works, work } = {}) {
  if (stored === BG_NONE) return '';
  // Bare ids saved before per-work packs fall back to auto, like an empty value.
  if (typeof stored === 'string' && stored.includes('/')) return stored;
  if (!work) return '';
  const items = Array.isArray(list) ? list : [];
  const declared = (Array.isArray(works) ? works : []).find(entry => entry?.id === work)?.default;
  if (typeof declared === 'string' && items.some(item => item?.key === declared)) return declared;
  return items.find(item => item?.work === work && typeof item.key === 'string')?.key || '';
}

// The image a thumbnail of a background key shows: its chosen time, or the clock's for auto.
export function selectionUrl(list, value, now = new Date()) {
  const { id, time } = normalizeBackgroundSelection(value);
  const found = id && Array.isArray(list) ? list.find(item => item?.key === id) : null;
  return resolveVariant(found, time === 'auto' ? timeOfDay(now) : time)?.url || '';
}

function validTime(time) {
  return BG_TIME_CHOICES.includes(time) ? time : 'auto';
}

// A missing or malformed stored value falls back to the defaults: no artwork,
// and the day part follows the clock.
export function normalizeBackgroundSelection(value) {
  const raw = value || {};
  return { id: typeof raw.id === 'string' ? raw.id : '', time: validTime(raw.time) };
}

/**
 * @param {object} options
 * @param {HTMLElement} options.layer stage background layer
 * @param {() => Date} [options.now]
 * @param {(fn: () => void, ms: number) => any} [options.setTimer]
 * @param {(id: any) => void} [options.clearTimer]
 * @returns {{ setList: (list: any[]) => void, setSelection: (s: { id?: string, time?: string }) => void, destroy: () => void }}
 */
export function createBackgroundView({
  layer,
  now = () => new Date(),
  setTimer = (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimer = id => globalThis.clearTimeout(id),
  matchMedia = query => globalThis.matchMedia?.(query),
  ImageClass = globalThis.Image,
  documentObj,
} = {}) {
  const doc = documentObj || layer?.ownerDocument || globalThis.document;

  const sky = doc.createElement('div');
  sky.className = 'adv-bg-sky';
  sky.setAttribute('aria-hidden', 'true');
  sky.dataset.time = timeOfDay(now());
  layer?.appendChild(sky);

  const buffers = [];
  for (let index = 0; index < 2; index++) {
    const image = typeof ImageClass === 'function' ? new ImageClass() : doc.createElement('img');
    image.className = 'adv-bg-image';
    image.setAttribute?.('alt', '');
    image.setAttribute?.('aria-hidden', 'true');
    image.classList.add('is-hidden');
    buffers.push(image);
    layer?.appendChild(image);
  }

  let backgrounds = new Map();
  let selection = { id: '', time: 'auto' };
  let front = -1;
  let shownUrl = null;
  let request = 0;
  let timer = null;
  let destroyed = false;
  const preloaded = new Map();
  let preloadedFor = null;

  function preloadVariants() {
    const variants = backgrounds.get(selection.id)?.variants;
    if (!variants) return;
    // Hold decoded variants for the chosen background only, so browsing the panel
    // does not pin every full-size image in memory.
    if (preloadedFor !== selection.id) {
      for (const image of preloaded.values()) release(image);
      preloaded.clear();
      preloadedFor = selection.id;
    }
    for (const url of Object.values(variants)) {
      if (typeof url !== 'string' || !url || preloaded.has(url)) continue;
      const image = typeof ImageClass === 'function' ? new ImageClass() : doc.createElement('img');
      preloaded.set(url, image);
      image.src = url;
      // Warm the HTTP and decode caches; failure must not interrupt playback.
      try { image.decode?.().catch(() => {}); } catch { /* unsupported decode */ }
    }
  }

  const reducedMotion = () => Boolean(matchMedia?.('(prefers-reduced-motion: reduce)')?.matches);

  function release(image) {
    image.onload = null;
    image.onerror = null;
    image.classList.remove('is-visible', 'is-instant');
    image.classList.add('is-hidden');
    if (image.removeAttribute) image.removeAttribute('src');
    else image.src = '';
  }

  function part() {
    return selection.time === 'auto' ? timeOfDay(now()) : selection.time;
  }

  function target() {
    const background = selection.id ? backgrounds.get(selection.id) : null;
    return background ? resolveVariant(background, part()) : null;
  }

  function showSky() {
    ++request;
    const instant = reducedMotion();
    for (const image of buffers) {
      image.classList.toggle('is-instant', instant);
      image.classList.remove('is-visible');
      image.classList.add('is-hidden');
    }
    front = -1;
    shownUrl = null;
  }

  function show(url) {
    const token = ++request;
    const backIndex = front === 0 ? 1 : 0;
    const incoming = buffers[backIndex];
    const outgoing = front >= 0 ? buffers[front] : null;
    incoming.classList.remove('is-visible', 'is-instant');
    incoming.src = url;
    const settle = ok => {
      // A newer selection owns the buffers; an aborted decode must not touch them.
      if (destroyed || token !== request) return;
      if (!ok) {
        release(incoming);
        return;
      }
      const instant = reducedMotion();
      incoming.classList.toggle('is-instant', instant);
      layer?.appendChild(incoming);
      incoming.classList.remove('is-hidden');
      incoming.classList.add('is-visible');
      if (outgoing && outgoing !== incoming) {
        outgoing.classList.toggle('is-instant', instant);
        outgoing.classList.remove('is-visible');
        outgoing.classList.add('is-hidden');
      }
      front = backIndex;
      shownUrl = url;
    };
    try {
      if (typeof incoming.decode === 'function') incoming.decode().then(() => settle(true), () => settle(false));
      else settle(true);
    } catch (error) {
      settle(false);
    }
  }

  function render() {
    if (destroyed) return;
    preloadVariants();
    sky.dataset.time = part();
    const found = target();
    if (!found) {
      if (front !== -1 || shownUrl !== null) showSky();
      return;
    }
    if (found.url !== shownUrl) show(found.url);
  }

  function schedule() {
    if (timer !== null) {
      clearTimer(timer);
      timer = null;
    }
    if (destroyed || selection.time !== 'auto') return;
    timer = setTimer(() => {
      timer = null;
      render();
      schedule();
    }, AUTO_CHECK_MS);
  }

  return {
    setList(list) {
      const next = new Map();
      for (const item of Array.isArray(list) ? list : []) {
        if (item && typeof item.key === 'string' && item.key) next.set(item.key, item);
      }
      backgrounds = next;
      render();
    },
    setSelection(value) {
      selection = normalizeBackgroundSelection(value);
      schedule();
      render();
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      ++request;
      if (timer !== null) {
        clearTimer(timer);
        timer = null;
      }
      for (const image of buffers) {
        release(image);
        image.remove?.();
      }
      sky.remove?.();
      for (const image of preloaded.values()) release(image);
      preloaded.clear();
    },
  };
}
