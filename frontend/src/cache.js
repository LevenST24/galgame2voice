// @ts-check
/**
 * Persistent Audio Cache for Gal2Voice (Web Cache Storage API)
 * Stores synthesized character audio Blobs and user microphone recordings
 * so historical audio can be replayed instantly across browser reloads & restarts.
 */

import { withAudioDeadline } from './audio_request.js';

/**
 * @typedef {import('./audio_request.js').AudioDeadlineOptions} AudioDeadlineOptions
 */

/**
 * @typedef {Error & { code?: string, status?: number, cause?: unknown }} AudioCacheError
 */

/**
 * @typedef {object} AudioKeyState
 * @property {number} revision
 * @property {Set<string>} invalid
 * @property {boolean} active
 */

/**
 * @typedef {{ kind: 'put', version: string, blob: Blob } | { kind: 'delete', version: string }} AudioCacheMutation
 */

/**
 * @typedef {object} AudioStorageWriter
 * @property {AudioCacheMutation | null} latest
 * @property {Promise<void> | null} promise
 */

/**
 * @typedef {Map<string, Blob>} BoundedAudioStore
 */

/**
 * @typedef {object} AudioLoadOptions
 * @property {boolean} [bypassCache]
 * @property {boolean} [cacheResult]
 * @property {AbortSignal | null} [signal]
 * @property {number} [timeoutMs]
 */

/**
 * @typedef {object} AudioBufferLoadOptions
 * @property {string} [url]
 * @property {Blob | null} [blob]
 * @property {AbortSignal | null} [signal]
 * @property {number} [timeoutMs]
 */

const AUDIO_CACHE_NAME = 'gal2voice-audio-v1';
const MAX_MEM_AUDIO_ENTRIES = 50;
const MAX_KEY_STATES = 100;
/** @type {Map<string, AudioKeyState>} */
const keyStates = new Map();
/** @type {WeakMap<Blob, Map<string, string>>} */
const blobVersions = new WeakMap();
/** @type {Map<string, AudioStorageWriter>} */
const storageWriters = new Map();
const versionPrefix = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
let versionSequence = 0;
/** @type {Promise<void> | null} */
let pruneRun = null;
let pruneAgain = false;

// L1 Memory cache for synchronous fast access within session (bounded LRU)
/** @type {BoundedAudioStore} */
const memAudioMap = new Map();

/**
 * @param {string} key
 * @returns {AudioKeyState}
 */
function stateForKey(key) {
  let state = keyStates.get(key);
  if (!state) state = { revision: 0, invalid: new Set(), active: true };
  keyStates.delete(key);
  keyStates.set(key, state);
  if (keyStates.size > MAX_KEY_STATES) {
    const oldest = /** @type {string} */ (keyStates.keys().next().value);
    /** @type {AudioKeyState} */ (keyStates.get(oldest)).active = false;
    keyStates.delete(oldest);
  }
  return state;
}

/**
 * @param {string} key
 * @param {Blob} blob
 * @param {string} version
 */
function rememberBlobVersion(key, blob, version) {
  let versions = blobVersions.get(blob);
  if (!versions) { versions = new Map(); blobVersions.set(blob, versions); }
  versions.delete(key);
  versions.set(key, version);
  if (versions.size > MAX_MEM_AUDIO_ENTRIES) versions.delete(/** @type {string} */ (versions.keys().next().value));
}

/**
 * @param {string} key
 * @param {Blob | null | undefined} blob
 * @returns {string | null | undefined}
 */
function blobVersion(key, blob) { return blob && blobVersions.get(blob)?.get(key); }

/**
 * @param {Response} response
 * @returns {string}
 */
function responseVersion(response) {
  /** @param {string} name */
  const header = name => response.headers?.get?.(name) || '';
  return header('X-Audio-Version') || `legacy:${header('X-Cached-At')}:${header('Content-Length')}`;
}

/**
 * @param {unknown} cause
 * @returns {AudioCacheError}
 */
function decodeFailure(cause) {
  const error = /** @type {AudioCacheError} */ (
    new Error('这句语音无法解码，原有内容已保留。请检查语音服务后重试，文字聊天仍可继续。')
  );
  error.code = 'AUDIO_DECODE_FAILED';
  error.cause = cause;
  return error;
}

// One writer per key applies only the latest desired state. Playback never waits
// for storage; a hanging browser API retains at most one pending body per writer.
/**
 * @param {string} key
 * @param {AudioCacheMutation} mutation
 * @returns {Promise<void>}
 */
function persistMutation(key, mutation) {
  if (typeof window === 'undefined' || !('caches' in window)) return Promise.resolve();
  const api = globalThis.caches || window.caches;
  let writer = storageWriters.get(key);
  if (!writer) {
    writer = { latest: null, promise: null };
    storageWriters.set(key, writer);
  }
  if (!(mutation.kind === 'delete' && writer.latest?.kind === 'put'
        && writer.latest.version !== mutation.version)) writer.latest = mutation;
  if (!writer.promise) {
    writer.promise = (async () => {
      try {
        while (writer.latest) {
          const desired = writer.latest;
          try {
            const cache = await withAudioDeadline(async signal => {
              const opened = await api.open(AUDIO_CACHE_NAME);
              if (signal.aborted) throw new DOMException('缓存更新已取消', 'AbortError');
              return opened;
            }, { timeoutMs: 1500 });
            if (desired !== writer.latest) continue;
            const request = toCacheRequest(key);
            if (desired.kind === 'put') {
              await cache.put(request, new Response(desired.blob, { headers: {
                'Content-Type': desired.blob.type || 'audio/wav',
                'Content-Length': String(desired.blob.size),
                'X-Cached-At': String(Date.now()),
                'X-Audio-Version': desired.version,
              } }));
              scheduleAudioCachePrune();
            } else {
              const existing = await cache.match(request);
              if (desired !== writer.latest) continue;
              if (existing && responseVersion(existing) === desired.version) await cache.delete(request);
            }
          } catch (error) { console.debug('Failed to update audio cache storage:', error); }
          if (writer.latest === desired) writer.latest = null;
        }
      } finally { if (storageWriters.get(key) === writer) storageWriters.delete(key); }
    })();
  }
  return withAudioDeadline(() => /** @type {Promise<void>} */ (writer.promise), { timeoutMs: 1500 }).catch(error => {
    console.debug('Audio cache storage update did not finish in time:', error);
  });
}

/**
 * @param {string} key
 * @param {Blob} blob
 */
function discardAudioBlob(key, blob) {
  const state = stateForKey(key);
  const version = blobVersion(key, blob);
  const current = memAudioMap.get(key);
  if (!current || current === blob || (version && blobVersion(key, current) === version)) {
    memAudioMap.delete(key);
    state.revision++;
  }
  if (!version) return;
  state.invalid.add(version);
  if (state.invalid.size > 4) state.invalid.delete(/** @type {string} */ (state.invalid.values().next().value));
  void persistMutation(key, { kind: 'delete', version });
}

/**
 * @param {string} key
 * @param {Blob} blob
 */
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
 * @returns {void}
 */
export function clearMemAudioCache() {
  memAudioMap.clear();
  for (const state of keyStates.values()) state.active = false;
  keyStates.clear();
}

/**
 * @param {string} key
 * @returns {Request}
 */
function toCacheRequest(key) {
  const safeKey = String(key || '').replace(/^[/\\]+/, '');
  return new Request(`https://gal2voice.local/audio_cache/${encodeURIComponent(safeKey)}`);
}

/**
 * Retrieves cached audio Blob from Cache Storage or memory.
 * @param {string} key - Cache key or URL
 * @param {AudioDeadlineOptions} [options]
 * @returns {Promise<Blob|null>}
 */
export async function getCachedAudioBlob(key, options = {}) {
  if (!key) return null;
  const state = stateForKey(key);
  const revision = state.revision;
  if (memAudioMap.has(key)) {
    const blob = /** @type {Blob} */ (memAudioMap.get(key));
    memAudioMap.delete(key);
    memAudioMap.set(key, blob);
    return blob;
  }
  if (typeof window === 'undefined' || !('caches' in window)) return null;

  try {
    return await withAudioDeadline(async signal => {
      const cache = await (globalThis.caches || window.caches).open(AUDIO_CACHE_NAME);
      if (signal.aborted) return null;
      const res = await cache.match(toCacheRequest(key));
      if (res && res.ok) {
        const version = responseVersion(res);
        if (state.invalid.has(version)) return null;
        const blob = await res.blob();
        if (signal.aborted) return null;
        if (!state.active || state.revision !== revision) return memAudioMap.get(key) || null;
        if (state.invalid.has(version)) return null;
        rememberBlobVersion(key, blob, version);
        storeInMemAudioMap(key, blob);
        return blob;
      }
      return null;
    }, { signal: options.signal, timeoutMs: 1500 });
  } catch (e) {
    console.debug('Failed to get cached audio blob:', e);
  }
  return null;
}

/**
 * Stores an audio Blob in Cache Storage and memory cache.
 * @param {string} key - Cache key or URL
 * @param {Blob} blob - Audio Blob
 * @returns {Promise<void>}
 */
export async function putCachedAudioBlob(key, blob) {
  if (!key || !blob) return;
  const state = stateForKey(key);
  state.revision++;
  const version = `${versionPrefix}-${++versionSequence}`;
  rememberBlobVersion(key, blob, version);
  storeInMemAudioMap(key, blob);
  await persistMutation(key, { kind: 'put', version, blob });
}

/**
 * Fetches an audio URL and caches its Blob locally.
 * @param {string} url - Audio URL (e.g. /audio/cache/xxx.wav)
 * @param {AudioLoadOptions} [options]
 * @returns {Promise<Blob>}
 */
export async function loadAudioBlob(url, options = {}) {
  const { bypassCache = false, cacheResult = true, ...deadline } = options;
  return withAudioDeadline(async signal => {
    const cached = bypassCache ? null : await getCachedAudioBlob(url, { signal });
    if (signal.aborted) throw new DOMException('播放已取消', 'AbortError');
    if (cached?.size) return cached;
    const state = stateForKey(url), revision = state.revision;
    const res = await fetch(url, { signal, ...(bypassCache ? { cache: 'no-store' } : {}) });
    if (!res.ok) {
      const error = /** @type {AudioCacheError} */ (
        new Error('这句语音暂时读不出来，请检查语音引擎后重试，文字聊天仍可继续。')
      );
      error.status = res.status;
      throw error;
    }
    const blob = await res.blob();
    if (signal.aborted) throw new DOMException('播放已取消', 'AbortError');
    if (!blob?.size) throw new Error('音频文件为空，请重新生成语音。');
    // Memory is populated immediately; a slow disk cache must not delay playback.
    if (cacheResult && state.active && state.revision === revision) void putCachedAudioBlob(url, blob);
    return blob;
  }, deadline);
}

/**
 * Decode once, then retry the same URL once if its cached audio is invalid.
 * @template [TDecoded=AudioBuffer]
 * @param {(audioData: ArrayBuffer) => Promise<TDecoded>} decode - Typically AudioContext.prototype.decodeAudioData bound to an AudioContext
 * @param {AudioBufferLoadOptions} [options]
 * @returns {Promise<TDecoded>}
 */
export async function loadAudioBuffer(decode, { url = '', blob = null, signal, timeoutMs = 15000 } = {}) {
  return withAudioDeadline(async ownedSignal => {
    const checkCancelled = () => {
      if (ownedSignal.aborted) throw new DOMException('播放已取消', 'AbortError');
    };
    const canRefresh = /^(?:\/|https?:\/\/)/i.test(url);
    /** @param {Blob} value */
    const decodeBlob = async value => {
      const buffer = await value.arrayBuffer(); checkCancelled();
      const decoded = await decode(buffer); checkCancelled(); return decoded;
    };
    let state = stateForKey(url), revision = state.revision;
    const initial = blob || await loadAudioBlob(url, { signal: ownedSignal, timeoutMs, cacheResult: false });
    try {
      const decoded = await decodeBlob(initial);
      if (canRefresh && !blobVersion(url, initial) && state.active && state.revision === revision) {
        void putCachedAudioBlob(url, initial);
      }
      return decoded;
    } catch (error) {
      checkCancelled();
      if (error.name !== 'EncodingError') throw error;
      if (!canRefresh) throw decodeFailure(error);
      discardAudioBlob(url, initial);
    }
    checkCancelled();
    // Another playback may have repaired this key while the old decode waited.
    // Reuse that newer copy instead of replacing it with an older GET result.
    const replacement = memAudioMap.get(url);
    if (replacement && replacement !== initial) {
      try { return await decodeBlob(replacement); }
      catch (error) {
        checkCancelled();
        if (error.name !== 'EncodingError') throw error;
        discardAudioBlob(url, replacement);
      }
    }
    state = stateForKey(url); revision = state.revision;
    const fresh = await loadAudioBlob(url, {
      signal: ownedSignal, timeoutMs, bypassCache: true, cacheResult: false,
    });
    let decoded;
    try { decoded = await decodeBlob(fresh); }
    catch (error) {
      checkCancelled();
      if (error.name === 'EncodingError') throw decodeFailure(error);
      throw error;
    }
    if (state.active && state.revision === revision) void putCachedAudioBlob(url, fresh);
    return decoded;
  }, { signal, timeoutMs });
}

/**
 * @param {string} url
 * @param {AudioLoadOptions} [options]
 * @returns {Promise<Blob | null>}
 */
export async function fetchAndCacheAudio(url, options = {}) {
  if (!url) return null;
  try {
    return await loadAudioBlob(url, options);
  } catch (e) {
    console.debug('Failed to fetch and cache audio:', url, e);
  }
  return null;
}

/**
 * Keep microphone recordings and unknown legacy keys; they may be the only copy.
 * @param {Request} request
 * @returns {boolean}
 */
function isGeneratedAudioRequest(request) {
  try {
    const url = new URL(request.url);
    const prefix = '/audio_cache/';
    if (url.origin !== 'https://gal2voice.local' || !url.pathname.startsWith(prefix)) return false;
    const key = decodeURIComponent(url.pathname.slice(prefix.length));
    return key.startsWith('audio/') || key.startsWith('msg_');
  } catch {
    // Unknown/malformed legacy keys have no proven recoverable source.
    return false;
  }
}

/**
 * Limit generated audio copies without silently removing user recordings.
 * @param {number} [maxEntries]
 * @returns {Promise<void>}
 */
export async function pruneAudioCacheStorage(maxEntries = 100) {
  if (typeof window === 'undefined' || !('caches' in window)) return;
  try {
    const cache = await (globalThis.caches || window.caches).open(AUDIO_CACHE_NAME);
    const requests = (await cache.keys()).filter(isGeneratedAudioRequest);
    if (requests && requests.length > maxEntries) {
      const toDelete = requests.slice(0, requests.length - maxEntries);
      await Promise.all(toDelete.map((req) => cache.delete(req)));
    }
  } catch (e) {
    console.debug('Failed to prune audio cache storage:', e);
  }
}

// One pass at a time; a write that lands during a pass gets one more pass afterwards.
function scheduleAudioCachePrune() {
  if (pruneRun) {
    pruneAgain = true;
    return;
  }
  pruneRun = (async () => {
    try {
      do {
        pruneAgain = false;
        await pruneAudioCacheStorage();
      } while (pruneAgain);
    } finally {
      pruneRun = null;
    }
  })();
}

// 在应用空闲时自动维护 Cache Storage 配额
if (typeof window !== 'undefined') {
  if ('requestIdleCallback' in window) {
    window.requestIdleCallback(() => scheduleAudioCachePrune());
  } else {
    setTimeout(() => scheduleAudioCachePrune(), 5000);
  }
}
