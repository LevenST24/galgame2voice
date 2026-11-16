import assert from 'node:assert/strict';
import test from 'node:test';
import { BoundedAudioStore, MAX_AUDIO_STORE_ENTRIES } from '../frontend/src/voice.js';

function withMockedRevoke(fn) {
  const originalURL = globalThis.URL;
  const revokedLog = [];
  let shouldThrowInRevoke = false;

  globalThis.URL = {
    ...originalURL,
    revokeObjectURL: (url) => {
      revokedLog.push({ url, time: Date.now() });
      if (shouldThrowInRevoke) {
        throw new Error('Simulated DOMException in URL.revokeObjectURL');
      }
    },
  };

  try {
    return fn({
      revokedLog,
      setThrowInRevoke: (v) => { shouldThrowInRevoke = v; },
    });
  } finally {
    globalThis.URL = originalURL;
  }
}

test('BoundedAudioStore enforces 30-entry ceiling and FIFO/LRU blob URL revocation across 250 entries', () => {
  withMockedRevoke(({ revokedLog }) => {
    assert.strictEqual(MAX_AUDIO_STORE_ENTRIES, 30, 'MAX_AUDIO_STORE_ENTRIES must be 30');

    const store = new BoundedAudioStore();
    const totalEntries = 250;
    for (let i = 1; i <= totalEntries; i++) {
      const msgId = `msg_burst_${i}`;
      const blobUrl = `blob:http://localhost:8080/audio_uuid_${i}`;
      store.set(msgId, { url: blobUrl, dur: 3.5, index: i });

      assert(
        store.size <= 30,
        `Store size invariant violated at step ${i}: size is ${store.size} > 30`,
      );

      if (i >= 30) {
        assert.strictEqual(
          store.size,
          30,
          `Store size must remain at exactly 30 once filled (at step ${i})`,
        );
      }
    }

    const uniqueRevoked = new Set(revokedLog.map((r) => r.url));
    const expectedEvictions = totalEntries - 30;
    assert(
      revokedLog.length >= 170,
      `revokeObjectURL must be invoked 170+ times, got ${revokedLog.length}`,
    );
    assert.strictEqual(
      uniqueRevoked.size,
      expectedEvictions,
      `Expected exactly ${expectedEvictions} unique revoked URLs, got ${uniqueRevoked.size}`,
    );

    for (let i = 1; i <= expectedEvictions; i++) {
      const expectedUrl = `blob:http://localhost:8080/audio_uuid_${i}`;
      assert(
        uniqueRevoked.has(expectedUrl),
        `Expected ${expectedUrl} to be revoked during LRU eviction`,
      );
    }

    for (let i = totalEntries - 29; i <= totalEntries; i++) {
      const key = `msg_burst_${i}`;
      assert(store.has(key), `Expected recent key ${key} to remain in store`);
      const val = store.get(key);
      assert.strictEqual(val.url, `blob:http://localhost:8080/audio_uuid_${i}`);
    }
  });
});

test('BoundedAudioStore revokes old URL on key update, delete, and clear', () => {
  withMockedRevoke(({ revokedLog }) => {
    const updateStore = new BoundedAudioStore();
    updateStore.set('msg_alpha', { url: 'blob:http://localhost/alpha_v1' });
    assert.strictEqual(revokedLog.length, 0);

    updateStore.set('msg_alpha', { url: 'blob:http://localhost/alpha_v2' });
    assert(
      revokedLog.some((r) => r.url === 'blob:http://localhost/alpha_v1'),
      'Old URL must be revoked when key is updated with a new URL',
    );

    revokedLog.length = 0;
    const cleanupStore = new BoundedAudioStore();
    cleanupStore.set('item_1', { url: 'blob:http://localhost/item_1' });
    cleanupStore.set('item_2', { url: 'blob:http://localhost/item_2' });
    cleanupStore.set('item_3', { url: 'blob:http://localhost/item_3' });

    cleanupStore.delete('item_1');
    assert.strictEqual(cleanupStore.size, 2);
    assert(revokedLog.some((r) => r.url === 'blob:http://localhost/item_1'));

    revokedLog.length = 0;
    cleanupStore.clear();
    assert.strictEqual(cleanupStore.size, 0);
    assert.strictEqual(revokedLog.length, 2);
    assert(revokedLog.some((r) => r.url === 'blob:http://localhost/item_2'));
    assert(revokedLog.some((r) => r.url === 'blob:http://localhost/item_3'));
  });
});

test('BoundedAudioStore tolerates URL.revokeObjectURL errors and ignores non-blob URLs', () => {
  withMockedRevoke(({ revokedLog, setThrowInRevoke }) => {
    setThrowInRevoke(true);
    const resilientStore = new BoundedAudioStore();
    for (let i = 1; i <= 35; i++) {
      resilientStore.set(`err_test_${i}`, { url: `blob:http://localhost/err_${i}` });
    }
    assert.strictEqual(resilientStore.size, 30);
    resilientStore.delete('err_test_35');
    resilientStore.clear();
    setThrowInRevoke(false);

    revokedLog.length = 0;
    const nonBlobStore = new BoundedAudioStore();
    nonBlobStore.set('http_url', { url: 'https://cdn.example.com/audio.wav' });
    nonBlobStore.set('null_url', { url: null });
    nonBlobStore.set('no_url', {});
    nonBlobStore.set('raw_primitive', 'invalid');

    nonBlobStore.delete('http_url');
    nonBlobStore.delete('null_url');
    nonBlobStore.clear();
    assert.strictEqual(revokedLog.length, 0, 'Non-blob URLs must not be revoked');
  });
});
