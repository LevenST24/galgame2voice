/**
 * Challenger R4-2: Empirical Frontend LRU & URL Revocation Stress Harness
 * 
 * Objectives:
 * 1. Simulate adding 200+ audio entries into BoundedAudioStore.
 * 2. Verify store.size stays strictly <= 30 at all times.
 * 3. Verify URL.revokeObjectURL is invoked 170+ times.
 * 4. Stress-test boundary conditions: duplicate keys, manual delete, clear,
 *    and exception-resilient revocation.
 */

const assert = require('assert');
const path = require('path');

async function runTests() {
  console.log('=== Challenger R4-2: Frontend BoundedAudioStore Empirical Stress Test ===\n');

  // Track all invocations of revokeObjectURL
  const revokedLog = [];
  let shouldThrowInRevoke = false;

  // Mock browser URL.revokeObjectURL
  global.URL = {
    revokeObjectURL: (url) => {
      revokedLog.push({ url, time: Date.now() });
      if (shouldThrowInRevoke) {
        throw new Error('Simulated DOMException in URL.revokeObjectURL');
      }
    }
  };

  // Import real production module
  const voiceModule = await import('../frontend/src/voice.js');
  const { BoundedAudioStore, MAX_AUDIO_STORE_ENTRIES } = voiceModule;

  assert.strictEqual(MAX_AUDIO_STORE_ENTRIES, 30, 'MAX_AUDIO_STORE_ENTRIES must be 30');

  // -------------------------------------------------------------------------
  // Test 1: High-Volume 250 Sequential Insertions (200+ requirement)
  // -------------------------------------------------------------------------
  console.log('Test 1: Simulating 250 audio entries (exceeds 200+ objective)...');
  revokedLog.length = 0;
  const store = new BoundedAudioStore();

  const totalEntries = 250;
  for (let i = 1; i <= totalEntries; i++) {
    const msgId = `msg_burst_${i}`;
    const blobUrl = `blob:http://localhost:8080/audio_uuid_${i}`;
    store.set(msgId, { url: blobUrl, dur: 3.5, index: i });

    // Assert boundary invariant at EVERY single insertion
    assert(
      store.size <= 30,
      `Store size invariant violated at step ${i}: size is ${store.size} > 30`
    );

    if (i >= 30) {
      assert.strictEqual(
        store.size,
        30,
        `Store size must remain at exactly 30 once filled (at step ${i})`
      );
    }
  }

  console.log(`  -> Final store size: ${store.size} (asserted <= 30)`);
  console.log(`  -> Total revokeObjectURL calls: ${revokedLog.length}`);

  // Unique URLs revoked
  const uniqueRevoked = new Set(revokedLog.map((r) => r.url));
  console.log(`  -> Unique URLs revoked: ${uniqueRevoked.size}`);

  const expectedEvictions = totalEntries - 30; // 220 evictions
  assert(
    revokedLog.length >= 170,
    `revokeObjectURL must be invoked 170+ times, got ${revokedLog.length}`
  );
  assert.strictEqual(
    uniqueRevoked.size,
    expectedEvictions,
    `Expected exactly ${expectedEvictions} unique revoked URLs, got ${uniqueRevoked.size}`
  );

  // Verify FIFO / LRU eviction order
  for (let i = 1; i <= expectedEvictions; i++) {
    const expectedUrl = `blob:http://localhost:8080/audio_uuid_${i}`;
    assert(
      uniqueRevoked.has(expectedUrl),
      `Expected ${expectedUrl} to be revoked during LRU eviction`
    );
  }

  // Verify remaining 30 items in store are the most recent ones (221 to 250)
  for (let i = totalEntries - 29; i <= totalEntries; i++) {
    const key = `msg_burst_${i}`;
    assert(store.has(key), `Expected recent key ${key} to remain in store`);
    const val = store.get(key);
    assert.strictEqual(val.url, `blob:http://localhost:8080/audio_uuid_${i}`);
  }

  console.log('  [PASS] Test 1: 250 insertions bounded at 30 with full FIFO URL revocation.\n');

  // -------------------------------------------------------------------------
  // Test 2: Adversarial Boundary — Re-inserting / Updating Existing Key
  // -------------------------------------------------------------------------
  console.log('Test 2: Re-setting existing key with new URL vs same URL...');
  revokedLog.length = 0;
  const updateStore = new BoundedAudioStore();

  // Insert initial entry
  updateStore.set('msg_alpha', { url: 'blob:http://localhost/alpha_v1' });
  assert.strictEqual(revokedLog.length, 0);

  // Update with different URL -> old URL must be revoked
  updateStore.set('msg_alpha', { url: 'blob:http://localhost/alpha_v2' });
  assert(
    revokedLog.some((r) => r.url === 'blob:http://localhost/alpha_v1'),
    'Old URL must be revoked when key is updated with a new URL'
  );
  console.log('  -> Updating key with new URL successfully revoked old URL');

  // Edge case observation: re-setting with same URL
  revokedLog.length = 0;
  const sameUrlBefore = updateStore.get('msg_alpha').url;
  updateStore.set('msg_alpha', { url: sameUrlBefore, played: true });
  // Note: in BoundedAudioStore, this.delete(key) is invoked in set(), which unconditionally
  // calls safeRevokeUrl. We record whether same-URL was revoked.
  const sameUrlRevoked = revokedLog.some((r) => r.url === sameUrlBefore);
  console.log(`  -> Adversarial Discovery: Re-setting same URL triggers revocation: ${sameUrlRevoked}`);
  console.log('  [PASS] Test 2: Update semantics stress-tested.\n');

  // -------------------------------------------------------------------------
  // Test 3: Explicit delete() and clear()
  // -------------------------------------------------------------------------
  console.log('Test 3: Explicit delete() and clear() URL cleanup...');
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
  console.log('  [PASS] Test 3: delete() and clear() properly revoke all Blob URLs.\n');

  // -------------------------------------------------------------------------
  // Test 4: Resilience against URL.revokeObjectURL Throwing
  // -------------------------------------------------------------------------
  console.log('Test 4: Resilience against exceptions in URL.revokeObjectURL...');
  shouldThrowInRevoke = true;
  const resilientStore = new BoundedAudioStore();
  // Fill beyond capacity while revokeObjectURL throws
  try {
    for (let i = 1; i <= 35; i++) {
      resilientStore.set(`err_test_${i}`, { url: `blob:http://localhost/err_${i}` });
    }
    assert.strictEqual(resilientStore.size, 30);
    resilientStore.delete('err_test_35');
    resilientStore.clear();
    console.log('  [PASS] Test 4: safeRevokeUrl swallowed exceptions safely without breaking store.\n');
  } finally {
    shouldThrowInRevoke = false;
  }

  // -------------------------------------------------------------------------
  // Test 5: Non-Blob URLs & Null Safe-Guards
  // -------------------------------------------------------------------------
  console.log('Test 5: Non-blob URLs and malformed values...');
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
  console.log('  [PASS] Test 5: Non-blob URLs safely ignored by safeRevokeUrl.\n');

  console.log('=== All BoundedAudioStore Tests PASSED Successfully ===');
}

runTests().catch((err) => {
  console.error('Test failed with error:', err);
  process.exit(1);
});
