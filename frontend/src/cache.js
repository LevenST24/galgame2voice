/**
 * Persistent Audio Cache for Gal2Voice (Web Cache Storage API)
 * Stores synthesized character audio Blobs and user microphone recordings
 * so historical audio can be replayed instantly across browser reloads & restarts.
 */

const AUDIO_CACHE_NAME = 'gal2voice-audio-v1';
const MAX_MEM_AUDIO_ENTRIES = 50;

// L1 Memory cache for synchronous fast access within session (bounded LRU)
const memAudioMap = new Map();

function storeInMemAudioMap(key, blob) {
  if (memAudioMap.has(key)) {
    memAudioMap.delete(key);
  }
  memAudioMap.set(key, blob);
  if (memAudioMap.size > MAX_MEM_AUDIO_ENTRIES) {
    const oldestKey = memAudioMap.keys().next().value;
    if (oldestKey !== undefined) {
      memAudioMap.delete(oldestKey);
    }
  }
}

/**
 * Clears the in-memory audio blob cache.
 */
export function clearMemAudioCache() {
  memAudioMap.clear();
}

function toCacheRequest(key) {
  const safeKey = String(key || '').replace(/^[/\\]+/, '');
  return new Request(`https://gal2voice.local/audio_cache/${encodeURIComponent(safeKey)}`);
}

/**
 * Retrieves cached audio Blob from Cache Storage or memory.
 * @param {string} key - Cache key or URL
 * @returns {Promise<Blob|null>}
 */
export async function getCachedAudioBlob(key) {
  if (!key) return null;
  if (memAudioMap.has(key)) {
    const blob = memAudioMap.get(key);
    memAudioMap.delete(key);
    memAudioMap.set(key, blob);
    return blob;
  }
  if (!('caches' in window)) return null;

  try {
    const cache = await caches.open(AUDIO_CACHE_NAME);
    const req = toCacheRequest(key);
    const res = await cache.match(req);
    if (res && res.ok) {
      const blob = await res.blob();
      storeInMemAudioMap(key, blob);
      return blob;
    }
  } catch (e) {
    console.debug('Failed to get cached audio blob:', e);
  }
  return null;
}

/**
 * Stores an audio Blob in Cache Storage and memory cache.
 * @param {string} key - Cache key or URL
 * @param {Blob} blob - Audio Blob
 */
export async function putCachedAudioBlob(key, blob) {
  if (!key || !blob) return;
  storeInMemAudioMap(key, blob);
  if (!('caches' in window)) return;

  try {
    const cache = await caches.open(AUDIO_CACHE_NAME);
    const req = toCacheRequest(key);
    const res = new Response(blob, {
      headers: {
        'Content-Type': blob.type || 'audio/wav',
        'Content-Length': String(blob.size),
        'X-Cached-At': String(Date.now()),
      },
    });
    await cache.put(req, res);
  } catch (e) {
    console.debug('Failed to put audio blob into cache:', e);
  }
}

/**
 * Fetches an audio URL and caches its Blob locally.
 * @param {string} url - Audio URL (e.g. /audio/cache/xxx.wav)
 * @returns {Promise<Blob|null>}
 */
export async function fetchAndCacheAudio(url) {
  if (!url) return null;
  const cached = await getCachedAudioBlob(url);
  if (cached) return cached;

  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    const blob = await res.blob();
    if (blob && blob.size > 0) {
      await putCachedAudioBlob(url, blob);
      return blob;
    }
  } catch (e) {
    console.debug('Failed to fetch and cache audio:', url, e);
  }
  return null;
}
