/**
 * Targeted regression and adversarial test suites covering M19 frontend defect remediations:
 * 1. toggleMic() in audio_player_ui.js revoking pendingVoice.audio Blob URL.
 * 2. BoundedAudioStore in voice.js true LRU eviction (accessing an existing key refreshes recency).
 * 3. shortcuts.js decide() left-click returning null when overlayOpen is true.
 * 4. audio_player.js buffer lead check (nextStartTime <= now) applying adaptive lead cushion.
 * 5. ai.js SSE parser resetting eventName to 'text' and handling optional whitespace.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fakeDocument } from './helpers/adv_dom.mjs';

import { initAudioPlayerUi, toggleMic, cancelMic, revokeAllCachedAudioUrls } from '../frontend/src/controllers/audio_player_ui.js';
import { BoundedAudioStore, startListening, stopListening } from '../frontend/src/voice.js';
import { StreamAudioController } from '../frontend/src/audio_player.js';
import { decide } from '../frontend/src/adv/shortcuts.js';
import { streamChat } from '../frontend/src/ai.js';

// ============================================================================
// 1. Microphone Blob URL Leak Remediation in audio_player_ui.js
// ============================================================================

test('audio_player_ui: toggleMic revokes previously allocated pendingVoice.audio Blob URL', async (t) => {
  const originalDoc = globalThis.document;
  const originalWin = globalThis.window;
  const originalNav = globalThis.navigator;
  const originalURL = globalThis.URL;
  const originalMediaRecorder = globalThis.MediaRecorder;

  const revokedUrls = [];
  const createdUrls = [];

  try {
    globalThis.document = fakeDocument();
    globalThis.URL = {
      createObjectURL: (blob) => {
        const u = `blob:mock-uuid-${createdUrls.length + 1}`;
        createdUrls.push(u);
        return u;
      },
      revokeObjectURL: (url) => {
        revokedUrls.push(url);
      },
    };

    let activeRecorder = null;
    let activeStream = null;

    class FakeSpeechRecognition {
      start() {}
      stop() { this.onend?.(); }
    }

    class FakeMediaRecorder {
      constructor(stream, opts) {
        activeRecorder = this;
        this.stream = stream;
        this.mimeType = 'audio/webm';
      }
      start() {}
      stop() {
        this.onstop?.();
      }
    }
    FakeMediaRecorder.isTypeSupported = () => true;

    globalThis.MediaRecorder = FakeMediaRecorder;
    globalThis.window = {
      SpeechRecognition: FakeSpeechRecognition,
      MediaRecorder: FakeMediaRecorder,
      URL: globalThis.URL,
    };

    Object.defineProperty(globalThis, 'navigator', {
      value: {
        mediaDevices: {
          getUserMedia: async () => {
            activeStream = { getTracks: () => [{ stop() {} }] };
            return activeStream;
          },
        },
      },
      configurable: true,
      writable: true,
    });

    const input = globalThis.document.createElement('textarea');
    const micBtn = globalThis.document.createElement('button');

    initAudioPlayerUi({ input }, {
      getInput: () => input,
      getMicButton: () => micBtn,
      isBusy: () => false,
      sendMessage: () => {},
    });

    // Step 1: Start recording
    toggleMic();
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.ok(activeRecorder, 'MediaRecorder must be started');

    // Simulate audio data captured and recording stopped
    activeRecorder.ondataavailable?.({ data: new Blob(['audio_bytes'], { type: 'audio/webm' }) });
    toggleMic(); // Stops recording (calls stopMic -> recorder.stop -> onstop -> onRecorded)
    await new Promise((resolve) => setTimeout(resolve, 10));

    assert.equal(createdUrls.length, 1, 'Expected 1 Blob URL created for recording');
    const firstBlobUrl = createdUrls[0];
    assert.ok(!revokedUrls.includes(firstBlobUrl), 'URL must not be revoked while pending');

    // Step 2: User clicks mic button again to start a new recording session
    // toggleMic() must revoke the pending voice Blob URL before starting
    toggleMic();
    await new Promise((resolve) => setTimeout(resolve, 10));

    assert.ok(
      revokedUrls.includes(firstBlobUrl),
      `Expected ${firstBlobUrl} to be revoked by toggleMic(), got: ${JSON.stringify(revokedUrls)}`
    );

    cancelMic();
  } finally {
    cancelMic();
    globalThis.document = originalDoc;
    globalThis.window = originalWin;
    globalThis.MediaRecorder = originalMediaRecorder;
    Object.defineProperty(globalThis, 'navigator', { value: originalNav, configurable: true, writable: true });
    globalThis.URL = originalURL;
  }
});

// ============================================================================
// 2. BoundedAudioStore True LRU Eviction in voice.js
// ============================================================================

test('BoundedAudioStore: accessing existing key via get() refreshes recency and evicts unaccessed older key', () => {
  const originalURL = globalThis.URL;
  const revokedUrls = [];

  try {
    globalThis.URL = {
      ...globalThis.URL,
      revokeObjectURL: (url) => {
        revokedUrls.push(url);
      },
    };

    const store = new BoundedAudioStore();
    const MAX_ENTRIES = 30;

    // 1. Fill store to exact capacity (k1 .. k30)
    for (let i = 1; i <= MAX_ENTRIES; i++) {
      store.set(`k${i}`, { url: `blob:url_${i}` });
    }
    assert.equal(store.size, MAX_ENTRIES);

    // 2. Access k1 via get(). In a true LRU cache, this refreshes k1 to the most recent position.
    // k2 now becomes the least recently used key.
    const entry1 = store.get('k1');
    assert.ok(entry1, 'k1 must exist in store');
    assert.equal(entry1.url, 'blob:url_1');

    // 3. Insert key 31, exceeding capacity of 30.
    store.set('k31', { url: 'blob:url_31' });
    assert.equal(store.size, MAX_ENTRIES);

    // 4. Assert k1 is PRESERVED (recency refreshed) while k2 is EVICTED (FIFO bug would have evicted k1)
    assert.equal(store.has('k1'), true, 'k1 must be preserved due to LRU recency refresh');
    assert.equal(store.has('k2'), false, 'k2 must be evicted as the least recently used entry');
    assert.equal(store.has('k31'), true, 'k31 must be present as the newest entry');

    // Verify revoked URL
    assert.ok(revokedUrls.includes('blob:url_2'), 'Evicted entry k2 Blob URL must be revoked');
    assert.ok(!revokedUrls.includes('blob:url_1'), 'Preserved entry k1 Blob URL must not be revoked');

    // 5. Further eviction test: access k3, insert k32 -> k4 must be evicted
    store.get('k3');
    store.set('k32', { url: 'blob:url_32' });
    assert.equal(store.has('k3'), true, 'k3 must be preserved');
    assert.equal(store.has('k4'), false, 'k4 must be evicted');
    assert.ok(revokedUrls.includes('blob:url_4'));

    // 6. Delete test
    store.delete('k32');
    assert.ok(revokedUrls.includes('blob:url_32'));
  } finally {
    globalThis.URL = originalURL;
  }
});

// ============================================================================
// 3. Audio Buffer Lead Jitter Margin (nextStartTime <= now) in audio_player.js
// ============================================================================

test('audio_player: nextStartTime <= now applies adaptive buffer lead margin', async () => {
  const originalWin = globalThis.window;

  try {
    class MockAudioContext {
      constructor() {
        this.state = 'running';
        this.currentTime = 5.0;
        this.destination = {};
      }
      createGain() {
        return {
          gain: {
            value: 1.0,
            setValueAtTime() {},
            cancelScheduledValues() {},
            linearRampToValueAtTime() {},
          },
          connect() {},
          disconnect() {},
        };
      }
      createBufferSource() {
        return {
          buffer: null,
          connect() {},
          disconnect() {},
          start() {},
          stop() {},
        };
      }
      async resume() { this.state = 'running'; }
      async close() { this.state = 'closed'; }
    }

    globalThis.window = {
      AudioContext: MockAudioContext,
    };

    const controller = new StreamAudioController();
    controller.ensureContext();
    assert.ok(controller.ctx);

    const leadMargin = controller.currentBufferLeadSec; // typically 0.02s
    assert.ok(leadMargin > 0);

    // Provide a queue entry so _scheduleNext passes the if (!this.queue.length) guard
    controller.queue = [{ status: 'fetching' }];

    // Case A: Boundary condition nextStartTime === now (both 5.0)
    // Under the old '<' check, 5.0 < 5.0 was false, omitting the lead margin.
    // Under '<=', 5.0 <= 5.0 is true, setting nextStartTime = 5.0 + leadMargin.
    controller.ctx.currentTime = 5.0;
    controller.nextStartTime = 5.0;
    controller._scheduleNext();
    assert.equal(
      controller.nextStartTime,
      5.0 + leadMargin,
      'When nextStartTime === now, buffer lead margin must be scheduled'
    );

    // Case B: Boundary condition nextStartTime < now (e.g. 4.9 < 5.0)
    controller.queue = [{ status: 'fetching' }];
    controller.ctx.currentTime = 5.0;
    controller.nextStartTime = 4.9;
    controller._scheduleNext();
    assert.equal(
      controller.nextStartTime,
      5.0 + controller.currentBufferLeadSec,
      'When nextStartTime < now, buffer lead margin must be scheduled'
    );

    // Case C: nextStartTime is ahead in the future (e.g. 6.0 > 5.0)
    // Buffer lead must NOT override future scheduled chunks
    controller.queue = [{ status: 'fetching' }];
    controller.ctx.currentTime = 5.0;
    controller.nextStartTime = 6.0;
    controller._scheduleNext();
    assert.equal(
      controller.nextStartTime,
      6.0,
      'When nextStartTime > now, future scheduled time must be preserved'
    );

    await controller.close();
  } finally {
    globalThis.window = originalWin;
  }
});

// ============================================================================
// 4. Shortcuts Overlay Click Isolation in shortcuts.js
// ============================================================================

test('shortcuts decide: left-click returns null whenever overlayOpen is true', () => {
  const doc = fakeDocument();

  // Test across different targets and states with overlayOpen: true
  const stageDiv = doc.createElement('div');
  stageDiv.className = 'adv-stage';
  const overlayBackdrop = doc.createElement('div');
  overlayBackdrop.className = 'adv-backlog-backdrop';

  const overlayContexts = [
    { overlayOpen: true, stageFocused: true, hidden: false, inInput: false },
    { overlayOpen: true, stageFocused: false, hidden: false, inInput: false },
    { overlayOpen: true, stageFocused: true, hidden: true, inInput: false },
    { overlayOpen: true, stageFocused: true, hidden: false, inInput: true },
  ];

  for (const ctx of overlayContexts) {
    // Stage left click
    assert.equal(
      decide({ type: 'click', button: 0, target: stageDiv }, ctx),
      null,
      'Left click with overlayOpen must return null'
    );
    // Backdrop left click
    assert.equal(
      decide({ type: 'click', button: 0, target: overlayBackdrop }, ctx),
      null,
      'Backdrop click with overlayOpen must return null'
    );
    // Undefined button left click
    assert.equal(
      decide({ type: 'click', target: stageDiv }, ctx),
      null,
      'Click without button property with overlayOpen must return null'
    );
  }

  // Sanity check: when overlayOpen is false, standard left click advances
  const normalCtx = { overlayOpen: false, stageFocused: true, hidden: false, inInput: false };
  assert.equal(
    decide({ type: 'click', button: 0, target: stageDiv }, normalCtx),
    'advance',
    'Left click with overlayOpen false must return advance'
  );
});

// ============================================================================
// 5. SSE Parser Robustness in ai.js
// ============================================================================

test('ai.js SSE parser: resets eventName to text after block dispatch and handles optional whitespace', async () => {
  // Case A: Verify eventName resets to 'text' after dispatching an audio_chunk block
  // If eventName did NOT reset, the second data payload would leak the previous 'audio_chunk' event
  const eventsDispatched = [];
  const textChunksReceived = [];

  const ssePayloadA = [
    'event: audio_chunk\n',
    'data: {"sentence": "初めまして", "audio_url": "/audio/cache/ja_0.wav"}\n\n',
    'data: {"delta_chinese": "很高兴见到你"}\n\n',
  ].join('');

  const mockFetchA = async () => {
    const stream = new ReadableStream({
      start(ctl) {
        ctl.enqueue(new TextEncoder().encode(ssePayloadA));
        ctl.close();
      },
    });
    return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  await new Promise((resolve) => {
    streamChat({
      prompt: '你好',
      sessionId: 'test_sess_sse',
      fetch: mockFetchA,
      onEvent: (name, data) => eventsDispatched.push({ name, data }),
      onChunk: (acc, delta) => textChunksReceived.push(delta),
      onEnd: () => resolve(),
    });
  });

  assert.equal(eventsDispatched.length, 1, 'Expected 1 audio_chunk event');
  assert.equal(eventsDispatched[0].name, 'audio_chunk');
  assert.equal(eventsDispatched[0].data.sentence, '初めまして');

  assert.equal(textChunksReceived.length, 1, 'Second payload must be parsed as default text');
  assert.equal(textChunksReceived[0], '很高兴见到你');

  // Case B: Verify headers with optional whitespace (zero spaces, single space, multiple spaces)
  const ssePayloadB = [
    'event:audio_chunk\n',
    'data:{"sentence": "零空格", "audio_url": "/a.wav"}\n\n',
    'event: audio_chunk\n',
    'data: {"sentence": "单空格", "audio_url": "/b.wav"}\n\n',
    'event:   audio_chunk\n',
    'data:   {"sentence": "多空格", "audio_url": "/c.wav"}\n\n',
  ].join('');

  const eventsB = [];
  const mockFetchB = async () => {
    const stream = new ReadableStream({
      start(ctl) {
        ctl.enqueue(new TextEncoder().encode(ssePayloadB));
        ctl.close();
      },
    });
    return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  await new Promise((resolve) => {
    streamChat({
      prompt: 'test_spaces',
      sessionId: 'test_sess_spaces',
      fetch: mockFetchB,
      onEvent: (name, data) => eventsB.push({ name, data }),
      onChunk: () => {},
      onEnd: () => resolve(),
    });
  });

  assert.equal(eventsB.length, 3, 'All 3 whitespace variations must parse successfully');
  assert.equal(eventsB[0].data.sentence, '零空格');
  assert.equal(eventsB[1].data.sentence, '单空格');
  assert.equal(eventsB[2].data.sentence, '多空格');

  // Case C: Empty boundary line resets eventName to 'text'
  const ssePayloadC = [
    'event: temporary_unhandled_event\n\n',
    'data: {"delta_chinese": "空行重置测试"}\n\n',
  ].join('');

  const textC = [];
  const mockFetchC = async () => {
    const stream = new ReadableStream({
      start(ctl) {
        ctl.enqueue(new TextEncoder().encode(ssePayloadC));
        ctl.close();
      },
    });
    return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  await new Promise((resolve) => {
    streamChat({
      prompt: 'test_empty_reset',
      sessionId: 'test_sess_empty_reset',
      fetch: mockFetchC,
      onEvent: () => {},
      onChunk: (acc, delta) => textC.push(delta),
      onEnd: () => resolve(),
    });
  });

  assert.equal(textC.length, 1);
  assert.equal(textC[0], '空行重置测试');
});

test('ai.js SSE parser: handles CRLF (\\r\\n) line endings without dropping events or text', async () => {
  const events = [];
  const textChunks = [];

  const ssePayloadCRLF = [
    'event: audio_chunk\r\n',
    'data: {"sentence": "テスト", "audio_url": "/test.wav"}\r\n\r\n',
    'data: {"delta_chinese": "测试CRLF"}\r\n\r\n',
    'event: done\r\n',
    'data: {"japanese": "テスト", "chinese": "测试CRLF", "chunks": []}\r\n\r\n',
  ].join('');

  const mockFetch = async () => {
    const stream = new ReadableStream({
      start(ctl) {
        ctl.enqueue(new TextEncoder().encode(ssePayloadCRLF));
        ctl.close();
      },
    });
    return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  let endResult = null;
  await new Promise((resolve) => {
    streamChat({
      prompt: 'crlf_test',
      sessionId: 'sess_crlf',
      fetch: mockFetch,
      onEvent: (name, data) => events.push({ name, data }),
      onChunk: (acc, delta) => textChunks.push(delta),
      onEnd: (acc, meta) => {
        endResult = { acc, meta };
        resolve();
      },
    });
  });

  assert.equal(events.length, 1, 'Expected audio_chunk event under CRLF');
  assert.equal(events[0].name, 'audio_chunk');
  assert.equal(events[0].data.sentence, 'テスト');
  assert.equal(events[0].data.audio_url, '/test.wav');
  assert.equal(textChunks.length, 1, 'Expected delta_chinese chunk');
  assert.equal(textChunks[0], '测试CRLF');
  assert.ok(endResult, 'Expected onEnd to be called');
  assert.equal(endResult.acc, '测试CRLF');
  assert.equal(endResult.meta.japanese, 'テスト');
  assert.equal(endResult.meta.error, null);
  assert.equal(endResult.meta.cancelled, false);
});

// ============================================================================
// 6. MediaRecorder Asynchronous onstop Null-Guard in voice.js
// ============================================================================

test('voice.js: recorder.onstop does not throw TypeError when cancelMic() executes before onstop', async () => {
  const originalDoc = globalThis.document;
  const originalWin = globalThis.window;
  const originalNav = globalThis.navigator;
  const originalURL = globalThis.URL;
  const originalMediaRecorder = globalThis.MediaRecorder;

  try {
    globalThis.document = fakeDocument();
    globalThis.URL = {
      createObjectURL: () => 'blob:mock-audio',
      revokeObjectURL: () => {},
    };

    class FakeSpeechRecognition {
      start() {}
      stop() { this.onend?.(); }
    }

    class FakeMediaRecorder {
      constructor(stream) {
        this.stream = stream;
        this.mimeType = 'audio/webm';
        this.state = 'recording';
      }
      start() {}
      stop() {
        this.state = 'inactive';
        setTimeout(() => {
          this.ondataavailable?.({ data: new Blob(['bytes']) });
          this.onstop?.();
        }, 10);
      }
    }
    FakeMediaRecorder.isTypeSupported = () => true;

    globalThis.MediaRecorder = FakeMediaRecorder;
    globalThis.window = {
      SpeechRecognition: FakeSpeechRecognition,
      MediaRecorder: FakeMediaRecorder,
      URL: globalThis.URL,
    };

    Object.defineProperty(globalThis, 'navigator', {
      value: {
        mediaDevices: {
          getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }),
        },
      },
      configurable: true,
      writable: true,
    });

    const input = globalThis.document.createElement('textarea');
    const micBtn = globalThis.document.createElement('button');

    initAudioPlayerUi({ input }, {
      getInput: () => input,
      getMicButton: () => micBtn,
      isBusy: () => false,
      sendMessage: () => {},
    });

    // Start mic and wait for recorder to be initialized
    toggleMic();
    await new Promise((resolve) => setTimeout(resolve, 5));

    // Stop mic, then immediately cancelMic() while recorder.stop() is pending async onstop
    toggleMic();
    cancelMic();

    // Wait for the asynchronous onstop to fire. Without null guard, this throws unhandled TypeError.
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.ok(true, 'Asynchronous onstop completed without TypeError');
  } finally {
    cancelMic();
    globalThis.document = originalDoc;
    globalThis.window = originalWin;
    globalThis.MediaRecorder = originalMediaRecorder;
    Object.defineProperty(globalThis, 'navigator', { value: originalNav, configurable: true, writable: true });
    globalThis.URL = originalURL;
  }
});

// ============================================================================
// 7. Voice Input Capability Check for Firefox / Non-SpeechRecognition in voice.js
// ============================================================================

test('voice.js: startListening succeeds in Firefox / environments with MediaRecorder but no SpeechRecognition', async () => {
  const originalWin = globalThis.window;
  const originalNav = globalThis.navigator;
  const originalMediaRecorder = globalThis.MediaRecorder;

  try {
    let recorderStarted = false;
    class FakeMediaRecorder {
      constructor(stream) {
        this.stream = stream;
        this.mimeType = 'audio/webm';
      }
      start() {
        recorderStarted = true;
      }
      stop() {
        this.onstop?.();
      }
    }
    FakeMediaRecorder.isTypeSupported = () => true;

    globalThis.MediaRecorder = FakeMediaRecorder;
    // Environment without SpeechRecognition or webkitSpeechRecognition
    globalThis.window = {
      MediaRecorder: FakeMediaRecorder,
    };

    Object.defineProperty(globalThis, 'navigator', {
      value: {
        mediaDevices: {
          getUserMedia: async () => ({
            getTracks: () => [{ stop() {} }],
          }),
        },
      },
      configurable: true,
      writable: true,
    });

    const res = startListening({});
    assert.equal(res.ok, true, 'startListening must return { ok: true } when MediaRecorder is supported');

    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(recorderStarted, true, 'MediaRecorder must be started after getUserMedia resolves');
  } finally {
    stopListening();
    globalThis.window = originalWin;
    globalThis.MediaRecorder = originalMediaRecorder;
    Object.defineProperty(globalThis, 'navigator', { value: originalNav, configurable: true, writable: true });
  }
});

test('voice.js: startListening returns ok: false when neither MediaRecorder nor SpeechRecognition is supported', () => {
  const originalWin = globalThis.window;
  const originalNav = globalThis.navigator;
  const originalMediaRecorder = globalThis.MediaRecorder;

  try {
    delete globalThis.MediaRecorder;
    globalThis.window = {};
    Object.defineProperty(globalThis, 'navigator', {
      value: {},
      configurable: true,
      writable: true,
    });

    const res = startListening({});
    assert.equal(res.ok, false);
    assert.ok(res.reason.includes('不支持语音输入'));
  } finally {
    globalThis.window = originalWin;
    globalThis.MediaRecorder = originalMediaRecorder;
    Object.defineProperty(globalThis, 'navigator', { value: originalNav, configurable: true, writable: true });
  }
});

test('voice.js: rapid startListening -> stopListening -> startListening stops stale stream tracks and preserves active recorder', async () => {
  const originalWin = globalThis.window;
  const originalNav = globalThis.navigator;
  const originalMediaRecorder = globalThis.MediaRecorder;

  try {
    let resolveFirstGUM;
    let gumCallCount = 0;
    let firstStreamTrackStopped = false;
    let secondStreamTrackStopped = false;
    const recordedBlobs = [];

    class FakeMediaRecorder {
      constructor(stream) {
        this.stream = stream;
        this.mimeType = 'audio/webm';
        this.state = 'inactive';
      }
      start() {
        this.state = 'recording';
      }
      stop() {
        this.state = 'inactive';
        this.ondataavailable?.({ data: new Blob(['second_session_audio']) });
        this.onstop?.();
      }
    }
    FakeMediaRecorder.isTypeSupported = () => true;

    globalThis.MediaRecorder = FakeMediaRecorder;
    globalThis.window = { MediaRecorder: FakeMediaRecorder };

    Object.defineProperty(globalThis, 'navigator', {
      value: {
        mediaDevices: {
          getUserMedia: () => {
            gumCallCount++;
            if (gumCallCount === 1) {
              return new Promise((resolve) => {
                resolveFirstGUM = () => resolve({
                  getTracks: () => [{ stop() { firstStreamTrackStopped = true; } }],
                });
              });
            }
            return Promise.resolve({
              getTracks: () => [{ stop() { secondStreamTrackStopped = true; } }],
            });
          },
        },
      },
      configurable: true,
      writable: true,
    });

    // Session 1 starts (GUM #1 pending), then immediately stops
    startListening({ onRecorded: () => recordedBlobs.push('session1') });
    stopListening();

    // Session 2 starts immediately before GUM #1 resolves
    startListening({ onRecorded: () => recordedBlobs.push('session2') });
    await new Promise((resolve) => setTimeout(resolve, 10));

    // Now stale GUM #1 resolves; its track must be stopped immediately and must not clobber Session 2
    resolveFirstGUM();
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(firstStreamTrackStopped, true, 'Stale GUM #1 stream tracks must be stopped immediately');
    assert.equal(secondStreamTrackStopped, false, 'Active Session 2 stream must remain open while recording');

    // Stop Session 2 and verify its recording completes and stops its tracks
    stopListening();
    assert.deepEqual(recordedBlobs, ['session2']);
    assert.equal(secondStreamTrackStopped, true);
  } finally {
    stopListening();
    globalThis.window = originalWin;
    globalThis.MediaRecorder = originalMediaRecorder;
    Object.defineProperty(globalThis, 'navigator', { value: originalNav, configurable: true, writable: true });
  }
});

test('line_player: suspended AudioContext with working async resume() plays voice instead of prematurely aborting', async () => {
  const { createLinePlayer } = await import('../frontend/src/adv/line_player.js');

  const timers = [];
  const clock = {
    now: () => 0,
    setTimeout: (fn, ms) => {
      const id = timers.length + 1;
      timers.push({ id, fn, ms, cleared: false });
      return id;
    },
    clearTimeout: (id) => {
      const t = timers.find((x) => x.id === id);
      if (t) t.cleared = true;
    },
  };

  let playCalled = 0;
  const ctx = {
    state: 'suspended',
    async resume() {
      this.state = 'running';
    },
  };

  let resolvePlay;
  const voice = {
    ctx,
    play: () => {
      playCalled++;
      return new Promise((resolve) => {
        resolvePlay = resolve;
      });
    },
    stop: () => {},
  };

  const player = createLinePlayer({
    clock,
    voice,
    settings: { revealMsPerGrapheme: 0, autoMode: 'voice-priority' },
  });

  player.startTurn('turn_resume');
  player.setLineAudio('turn_resume', 0, { status: 'ready', chunks: ['/audio/cache/test.wav'] });
  player.pushLine('turn_resume', { index: 0, zh: '测试恢复播放', ja: 'テスト' });

  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(playCalled, 1, 'voice.play must be called so resumable AudioContext can resume');
  assert.equal(ctx.state, 'running');
  resolvePlay('ended');
  await new Promise((resolve) => setTimeout(resolve, 10));
});

