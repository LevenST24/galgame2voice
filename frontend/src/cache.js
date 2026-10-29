/**
 * Persistent Audio Cache for Gal2Voice (Web Cache Storage API)
 * Stores synthesized character audio Blobs and user microphone recordings
 * so historical audio can be replayed instantly across browser reloads & restarts.
 */

import { withAudioDeadline } from './audio_request.js';

const AUDIO_CACHE_NAME = 'gal2voice-audio-v1';
const MAX_MEM_AUDIO_ENTRIES = 50;
const MAX_KEY_STATES = 100;
const keyStates = new Map();
const blobVersions = new WeakMap();
const storageWriters = new Map();
const versionPrefix = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
let versionSequence = 0;

// L1 Memory cache for synchronous fast access within session (bounded LRU)
const memAudioMap = new Map();

function stateForKey(key) {
  let state = keyStates.get(key);
  if (!state) state = { revision: 0, invalid: new Set(), active: true };
  keyStates.delete(key);
  keyStates.set(key, state);
  if (keyStates.size > MAX_KEY_STATES) {
    const oldest = keyStates.keys().next().value;
    keyStates.get(oldest).active = false;
    keyStates.delete(oldest);
  }
  return state;
}

function rememberBlobVersion(key, blob, version) {
  let versions = blobVersions.get(blob);
  if (!versions) { versions = new Map(); blobVersions.set(blob, versions); }
  versions.delete(key);
  versions.set(key, version);
  if (versions.size > MAX_MEM_AUDIO_ENTRIES) versions.delete(versions.keys().next().value);
}

function blobVersion(key, blob) { return blob && blobVersions.get(blob)?.get(key); }
function responseVersion(response) {
  const header = name => response.headers?.get?.(name) || '';
  return header('X-Audio-Version') || `legacy:${header('X-Cached-At')}:${header('Content-Length')}`;
}

function decodeFailure(cause) {
  const error = new Error('这句语音无法解码，原有内容已保留。请检查语音服务后重试，文字聊天仍可继续。');
  error.code = 'AUDIO_DECODE_FAILED';
  error.cause = cause;
  return error;
}

// One writer per key applies only the latest desired state. Playback never waits
// for storage; a hanging browser API retains at most one pending body per writer.
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
  return withAudioDeadline(() => writer.promise, { timeoutMs: 1500 }).catch(error => {
    console.debug('Audio cache storage update did not finish in time:', error);
  });
}

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
  if (state.invalid.size > 4) state.invalid.delete(state.invalid.values().next().value);
  void persistMutation(key, { kind: 'delete', version });
}

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
  for (const state of keyStates.values()) state.active = false;
  keyStates.clear();
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
export async function getCachedAudioBlob(key, options = {}) {
  if (!key) return null;
  const state = stateForKey(key);
  const revision = state.revision;
  if (memAudioMap.has(key)) {
    const blob = memAudioMap.get(key);
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
 * @returns {Promise<Blob|null>}
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
      const error = new Error(`音频读取失败 (HTTP ${res.status})`);
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

/** Decode once, then retry the same URL once if its cached audio is invalid. */
export async function loadAudioBuffer(decode, { url = '', blob = null, signal, timeoutMs = 15000 } = {}) {
  return withAudioDeadline(async ownedSignal => {
    const checkCancelled = () => {
      if (ownedSignal.aborted) throw new DOMException('播放已取消', 'AbortError');
    };
    const canRefresh = /^(?:\/|https?:\/\/)/i.test(url);
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

export async function fetchAndCacheAudio(url, options = {}) {
  if (!url) return null;
  try {
    return await loadAudioBlob(url, options);
  } catch (e) {
    console.debug('Failed to fetch and cache audio:', url, e);
  }
  return null;
}

/** Keep microphone recordings and unknown legacy keys; they may be the only copy. */
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

/** Limit generated audio copies without silently removing user recordings. */
export async function pruneAudioCacheStorage(maxEntries = 100) {
  if (typeof window === 'undefined' || !('caches' in window)) return;
  try {
    const cache = await caches.open(AUDIO_CACHE_NAME);
    const requests = (await cache.keys()).filter(isGeneratedAudioRequest);
    if (requests && requests.length > maxEntries) {
      const toDelete = requests.slice(0, requests.length - maxEntries);
      await Promise.all(toDelete.map((req) => cache.delete(req)));
    }
  } catch (e) {
    console.debug('Failed to prune audio cache storage:', e);
  }
}

// 在应用空闲时自动维护 Cache Storage 配额
if (typeof window !== 'undefined') {
  if ('requestIdleCallback' in window) {
    window.requestIdleCallback(() => pruneAudioCacheStorage().catch(() => {}));
  } else {
    setTimeout(() => pruneAudioCacheStorage().catch(() => {}), 5000);
  }
}
