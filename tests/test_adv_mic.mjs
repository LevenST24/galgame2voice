import test from 'node:test';
import assert from 'node:assert/strict';
import { initAudioPlayerUi, toggleMic, cancelMic, finalizeVoice, clearMicTimers } from '../frontend/src/controllers/audio_player_ui.js';
import { mountAdv } from '../frontend/src/adv/adv_app.js';
import { initApp } from '../frontend/src/main.js';
import { openModal, closeAllModals } from '../frontend/src/settings_ui.js';
import { DEFAULT_SESSION_SETTINGS } from '../frontend/src/store.js';
import { startListening, stopListening, isListening, audioStore } from '../frontend/src/voice.js';
import { fakeDocument, fakeClock, Element } from './helpers/adv_dom.mjs';
import { fakeChatStream } from './helpers/chat_stream.mjs';

function setup(t, { syncEndOnStop = true, sendReturn = true } = {}) {
  const recognizers = [], sent = [];
  globalThis.document = fakeDocument();
  class SpeechRecognition {
    constructor() { this.stopped = false; recognizers.push(this); }
    start() {}
    stop() {
      this.stopped = true;
      if (syncEndOnStop) this.onend?.();
    }
  }
  globalThis.window = { SpeechRecognition };
  // Node 20 has no global navigator; one without mediaDevices keeps the recorder off on every version.
  Object.defineProperty(globalThis, 'navigator', { value: {}, configurable: true, writable: true });
  const oldInput = document.createElement('textarea'), input = document.createElement('textarea');
  const mic = document.createElement('button'); input.value = '原稿';
  initAudioPlayerUi({ input: oldInput }, {
    getInput: () => input, getMicButton: () => mic,
    isBusy: () => false, sendMessage: (...args) => { sent.push(args); return sendReturn; },
  });
  const result = (recognizer, text, final) => {
    const line = [{ transcript: text }]; line.isFinal = final;
    recognizer.onresult({ resultIndex: 0, results: [line] });
  };
  t.after(cancelMic);
  return { recognizers, sent, input, oldInput, mic, result };
}

test('speech writes into the ADV draft and retains the existing send path', t => {
  const h = setup(t); toggleMic();
  h.result(h.recognizers[0], '继续', false); assert.equal(h.input.value, '原稿 继续');
  assert.equal(h.oldInput.value, ''); assert.equal(h.mic.getAttribute('aria-pressed'), 'true');
  h.result(h.recognizers[0], '确认', true); finalizeVoice();
  assert.deepEqual(h.sent, [['原稿 确认', undefined]]);
});

test('cancelling an ADV mic owner rejects its late transcription after a session switch', t => {
  const h = setup(t); toggleMic(); const old = h.recognizers[0]; cancelMic();
  h.input.value = '新会话'; toggleMic();
  h.result(old, '旧转写', true); finalizeVoice();
  assert.equal(h.input.value, '新会话'); assert.deepEqual(h.sent, []);
  h.result(h.recognizers[1], '当前转写', true); finalizeVoice();
  assert.deepEqual(h.sent, [['新会话 当前转写', undefined]]);
});

test('final SpeechRecognition onresult delivered asynchronously after stopListening reaches onText and ADV draft', t => {
  const h = setup(t, { syncEndOnStop: false });
  const directTexts = [];
  assert.equal(startListening({ onText: text => directTexts.push(text) }).ok, true);
  const directRecognizer = h.recognizers[0];
  stopListening();
  assert.equal(directRecognizer.stopped, true);
  h.result(directRecognizer, '尾帧最终句', true);
  directRecognizer.onend?.();
  assert.deepEqual(directTexts, ['尾帧最终句']);

  toggleMic();
  assert.equal(h.mic.getAttribute('aria-pressed'), 'true');
  const uiRecognizer = h.recognizers[1];
  toggleMic();
  assert.equal(h.mic.getAttribute('aria-pressed'), 'false');
  assert.equal(uiRecognizer.stopped, true);
  h.result(uiRecognizer, '尾帧最终句', true);
  uiRecognizer.onend?.();
  assert.equal(h.input.value, '原稿 尾帧最终句');
  finalizeVoice();
  assert.deepEqual(h.sent, [['原稿 尾帧最终句', undefined]]);
});

test('starting a new listening round fences off late onresult from the previous stopped recognizer', t => {
  const h = setup(t, { syncEndOnStop: false });
  const round1Texts = [], round2Texts = [];
  assert.equal(startListening({ onText: text => round1Texts.push(text) }).ok, true);
  const firstRecognizer = h.recognizers[0];
  stopListening();

  assert.equal(startListening({ onText: text => round2Texts.push(text) }).ok, true);
  const secondRecognizer = h.recognizers[1];
  h.result(firstRecognizer, '过期旧句', true);
  firstRecognizer.onend?.();
  assert.deepEqual(round1Texts, []);
  assert.deepEqual(round2Texts, []);

  h.result(secondRecognizer, '本轮新句', true);
  stopListening();
  secondRecognizer.onend?.();
  assert.deepEqual(round1Texts, []);
  assert.deepEqual(round2Texts, ['本轮新句']);
});

test('spontaneous SpeechRecognition end clears micTimer and avoids leaking intervals across retries', t => {
  const h = setup(t);
  const origSetInterval = globalThis.setInterval;
  const origClearInterval = globalThis.clearInterval;
  const active = new Set();
  globalThis.setInterval = (...args) => {
    const id = origSetInterval(...args);
    active.add(id);
    return id;
  };
  globalThis.clearInterval = (id) => {
    active.delete(id);
    return origClearInterval(id);
  };
  t.after(() => {
    for (const id of [...active]) origClearInterval(id);
    globalThis.setInterval = origSetInterval;
    globalThis.clearInterval = origClearInterval;
  });

  for (let i = 0; i < 3; i++) {
    toggleMic();
    assert.equal(h.mic.getAttribute('aria-pressed'), 'true');
    assert.equal(active.size, 1, `round ${i + 1}: expected 1 active interval while listening`);
    h.recognizers[i].onend?.();
    assert.equal(h.mic.getAttribute('aria-pressed'), 'false', `round ${i + 1}: mic UI should reset on spontaneous end`);
    assert.equal(active.size, 0, `round ${i + 1}: micTimer interval should be cleared on spontaneous end`);
  }

  cancelMic();
  assert.equal(active.size, 0);
});

function setupWithRecorder(t, { syncEndOnStop = false, asyncRecordStopMs = 15, sendReturn = true } = {}) {
  const h = setup(t, { syncEndOnStop, sendReturn });
  const toastRoot = document.createElement('div');
  toastRoot.id = 'toastRoot';
  document.body.appendChild(toastRoot);
  const recorders = [];
  const tracks = [];
  const origMediaRecorder = globalThis.MediaRecorder;
  const origCreateObjectURL = URL.createObjectURL;
  const origRevokeObjectURL = URL.revokeObjectURL;
  const createdUrls = [];
  const revokedUrls = [];
  let urlSeq = 0;

  class FakeMediaRecorder {
    static isTypeSupported() { return true; }
    constructor(stream, opts) {
      this.stream = stream;
      this.mimeType = opts?.mimeType || 'audio/webm';
      this.state = 'inactive';
      this.stopped = false;
      recorders.push(this);
    }
    start() {
      this.state = 'recording';
    }
    stop() {
      if (this.state === 'inactive') return;
      this.stopped = true;
      this.state = 'inactive';
      const finish = () => {
        this.ondataavailable?.({ data: new Blob(['audio-bytes'], { type: this.mimeType }) });
        this.onstop?.();
      };
      if (asyncRecordStopMs > 0) setTimeout(finish, asyncRecordStopMs);
      else finish();
    }
  }

  Object.defineProperty(globalThis, 'navigator', {
    value: {
      mediaDevices: {
        getUserMedia: async () => {
          const track = { stopped: false, stop() { this.stopped = true; } };
          tracks.push(track);
          return { getTracks: () => [track] };
        },
      },
    },
    configurable: true,
    writable: true,
  });
  globalThis.MediaRecorder = FakeMediaRecorder;
  globalThis.window.MediaRecorder = FakeMediaRecorder;
  URL.createObjectURL = () => {
    const url = `blob:fake-mic-${++urlSeq}`;
    createdUrls.push(url);
    return url;
  };
  URL.revokeObjectURL = (url) => {
    revokedUrls.push(url);
  };
  t.after(() => {
    globalThis.MediaRecorder = origMediaRecorder;
    URL.createObjectURL = origCreateObjectURL;
    URL.revokeObjectURL = origRevokeObjectURL;
    for (const toast of [...toastRoot.querySelectorAll('.toast')]) {
      clearTimeout(toast._timer);
      toast.remove();
    }
  });
  return {
    ...h,
    recorders,
    tracks,
    toastRoot,
    createdUrls,
    revokedUrls,
    toasts: () => [...toastRoot.querySelectorAll('.toast')].map((el) => el.dataset.msg),
  };
}

test('spontaneous SpeechRecognition end with MediaRecorder stops recording and sends text with audio', async (t) => {
  const h = setupWithRecorder(t, { asyncRecordStopMs: 20 });
  toggleMic();
  assert.match(h.input.placeholder, /停顿/);
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(h.recorders.length, 1);
  assert.equal(h.recorders[0].state, 'recording');

  h.result(h.recognizers[0], '你好世界', true);
  h.recognizers[0].onend?.();

  assert.equal(isListening(), false, 'spontaneous end should stop listening even when recorder is active');
  assert.equal(h.mic.getAttribute('aria-pressed'), 'false');
  assert.equal(h.recorders[0].stopped, true, 'spontaneous end should stop MediaRecorder');
  assert.equal(h.sent.length, 0, 'should wait for onRecorded instead of synchronously sending without audio');

  await new Promise((r) => setTimeout(r, 50));
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0][0], '原稿 你好世界');
  assert.ok(h.sent[0][1]?.url?.startsWith('blob:'), 'sent message should include recorded audio attachment');
  assert.ok(h.sent[0][1]?.blob instanceof Blob);
  assert.equal(h.tracks[0].stopped, true);
});

test('no-speech with MediaRecorder stops recording, leaks no Blob URL, and shows only one toast', async (t) => {
  const h = setupWithRecorder(t, { asyncRecordStopMs: 15 });
  toggleMic();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(h.recorders.length, 1);

  h.recognizers[0].onerror?.({ error: 'no-speech' });
  h.recognizers[0].onend?.();

  assert.equal(isListening(), false);
  assert.equal(h.mic.getAttribute('aria-pressed'), 'false');
  assert.equal(h.recorders[0].stopped, true);

  await new Promise((r) => setTimeout(r, 80));
  assert.deepEqual(h.sent, []);
  assert.deepEqual(h.toasts(), ['没有听到说话，请再试一次']);
  assert.equal(h.createdUrls.length, h.revokedUrls.length, 'must not leak any Blob URL when discarding recording');
  assert.equal(h.tracks[0].stopped, true);
});

test('a failed recognition says why instead of going quiet, and a cancelled one says nothing', async (t) => {
  const cases = [
    ['network', ['语音识别服务连不上，请检查网络；也可以改用 Edge 浏览器，或直接打字。']],
    ['audio-capture', ['没有找到可用的麦克风，请检查麦克风是否插好、是否被其他程序占用。']],
    ['language-not-supported', ['语音识别没有成功，请再试一次，或直接打字。']],
    ['aborted', []],
  ];
  for (const [error, expected] of cases) {
    const h = setupWithRecorder(t, { asyncRecordStopMs: 1 });
    toggleMic();
    await new Promise((r) => setTimeout(r, 0));
    h.recognizers[0].onerror?.({ error });
    h.recognizers[0].onend?.();
    await new Promise((r) => setTimeout(r, 10));
    assert.equal(isListening(), false);
    assert.deepEqual(h.sent, []);
    assert.deepEqual(h.toasts(), expected, error);
    cancelMic();
  }
});

test('manual mic stop with MediaRecorder keeps late onresult and sends text with audio', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const h = setupWithRecorder(t, { syncEndOnStop: false, asyncRecordStopMs: 20 });
  toggleMic();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(h.recorders.length, 1);

  toggleMic();
  assert.equal(isListening(), false);
  assert.equal(h.mic.getAttribute('aria-pressed'), 'false');
  assert.equal(h.recorders[0].stopped, true);

  h.result(h.recognizers[0], '手动结束句', true);
  h.recognizers[0].onend?.();
  t.mock.timers.tick(20);
  assert.equal(h.sent.length, 0);
  t.mock.timers.tick(600);
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0][0], '原稿 手动结束句');
  assert.ok(h.sent[0][1]?.url?.startsWith('blob:'));
});


function setupMountedAdvWithRecorder(t, { syncEndOnStop = false, asyncRecordStopMs = 20 } = {}) {
  const h = setupWithRecorder(t, { syncEndOnStop, asyncRecordStopMs });
  const doc = globalThis.document;
  const clock = fakeClock();
  const listeners = new Map();
  const session = { id: 's_mic', settings: { ...DEFAULT_SESSION_SETTINGS, advEnabled: true }, messages: [] };
  let turnSeq = 0;
  let cancelMicCalls = 0;
  const sent = [];

  class FakeImage extends Element {
    constructor() { super('img', doc); }
    decode() { return Promise.resolve(); }
  }

  const emit = (type, event) => {
    for (const fn of [...(listeners.get(type) || [])]) fn(event);
  };

  const controller = {
    busy: false,
    on(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
      return () => listeners.get(type)?.delete(fn);
    },
    send(rawText = '', voiceMeta) {
      clearMicTimers();
      const text = String(rawText).trim();
      if (!text || this.busy) return false;
      sent.push([text, voiceMeta]);
      const id = `turn_${++turnSeq}`;
      const userMessage = { id: `user_${turnSeq}`, role: 'user', content: text };
      session.messages.push(userMessage);
      emit('turnstart', { originId: session.id, userMessage, assistantMessage: { id, lines: [] } });
      return true;
    },
    canRetry() { return false; },
    retry() {},
    stop() {},
  };

  const audio = {
    playLine: async () => 'done',
    stopLine() {},
  };

  const root = doc.createElement('div');
  doc.body.appendChild(root);
  const mql = { matches: false, addEventListener() {}, removeEventListener() {} };
  const app = mountAdv({ root }, {
    controller,
    audio,
    clock,
    getSession: () => session,
    voiceEnabled: () => false,
    getCharacter: () => ({ id: 1, name: '测试角色' }),
    ImageClass: FakeImage,
    loadPortrait: async () => null,
    raf: () => 1,
    cancelRaf: () => {},
    matchMedia: () => mql,
    viewport: doc.createElement('div'),
    onMic: () => toggleMic(),
    cancelMic: () => {
      cancelMicCalls += 1;
      cancelMic();
    },
  });
  t.after(() => app.unmount());

  initAudioPlayerUi({}, {
    getInput: () => app.textbox.inputElement,
    getMicButton: () => app.textbox.micButton,
    isBusy: () => controller.busy,
    sendMessage: (text, voiceMeta) => controller.send(text, voiceMeta),
  });

  return {
    ...h,
    app,
    controller,
    sent,
    input: app.textbox.inputElement,
    mic: app.textbox.micButton,
    getCancelMicCalls: () => cancelMicCalls,
  };
}

test('manual send while listening cancels mic, restores placeholder, keeps draft cleared, and avoids duplicate send or Blob leak', async (t) => {
  const h = setupMountedAdvWithRecorder(t, { syncEndOnStop: false, asyncRecordStopMs: 20 });
  toggleMic();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(h.recorders.length, 1);
  assert.match(h.input.placeholder, /停顿/);

  // Interim transcription appears in the input, user manually sends without waiting for auto-end.
  h.result(h.recognizers[0], '不等停顿直接发', false);
  assert.equal(h.input.value, '不等停顿直接发');

  assert.equal(h.controller.send(h.input.value), true);
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0][0], '不等停顿直接发');
  assert.equal(h.input.value, '');
  assert.equal(h.input.placeholder, '说点什么，或点麦克风用语音输入…');
  assert.equal(isListening(), false);
  assert.equal(h.mic.getAttribute('aria-pressed'), 'false');
  assert.equal(h.recorders[0].stopped, true);

  // Late final result, onend, and MediaRecorder onRecorded arrive after the turn already cleared.
  h.result(h.recognizers[0], '不等停顿直接发', true);
  h.recognizers[0].onend?.();
  await new Promise((r) => setTimeout(r, 60));

  assert.equal(h.sent.length, 1, 'late recognition end and onRecorded must not trigger a second send');
  assert.equal(h.input.value, '', 'late onText must not write sent text back into cleared input');
  assert.equal(h.createdUrls.length, h.revokedUrls.length, 'must not leak any Blob URL');
  assert.equal(h.tracks[0].stopped, true);
});

test('manual send after spontaneous recognition end but before onRecorded arrives sends only once without Blob leak', async (t) => {
  const h = setupWithRecorder(t, { syncEndOnStop: false, asyncRecordStopMs: 30 });
  toggleMic();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(h.recorders.length, 1);

  h.result(h.recognizers[0], '识别刚结束手动发', true);
  h.recognizers[0].onend?.();
  assert.equal(h.sent.length, 0, 'waiting for onRecorded before auto-finalize');

  // Simulate conversation.send() manually sending before onRecorded arrives (calls clearMicTimers()).
  clearMicTimers();
  h.sent.push([h.input.value, undefined]);
  h.input.value = '';

  // MediaRecorder finishes and delivers onRecorded afterwards.
  await new Promise((r) => setTimeout(r, 70));

  assert.equal(h.sent.length, 1, 'clearMicTimers must disarm pending autoEnded finalize before onRecorded');
  assert.equal(h.createdUrls.length, h.revokedUrls.length, 'discarded recording must not leak a Blob URL');
});

test('finalizeVoice revokes recorded Blob URL when sendMessage returns false', async (t) => {
  const h = setupWithRecorder(t, { syncEndOnStop: false, asyncRecordStopMs: 15, sendReturn: false });
  toggleMic();
  await new Promise((r) => setTimeout(r, 0));

  h.result(h.recognizers[0], '忙碌时被拒绝', true);
  h.recognizers[0].onend?.();
  await new Promise((r) => setTimeout(r, 60));

  assert.equal(h.sent.length, 1);
  assert.equal(h.createdUrls.length, 1);
  assert.deepEqual(h.revokedUrls, h.createdUrls, 'rejected send must revoke the created Blob URL');
});

test('auto-finalized voice send through ADV turnstart keeps its recording URL unrevoked', async (t) => {
  const h = setupMountedAdvWithRecorder(t, { syncEndOnStop: false, asyncRecordStopMs: 20 });
  toggleMic();
  await new Promise((r) => setTimeout(r, 0));

  const callsBefore = h.getCancelMicCalls();
  h.result(h.recognizers[0], '自动结束带录音', true);
  h.recognizers[0].onend?.();
  await new Promise((r) => setTimeout(r, 60));

  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0][0], '自动结束带录音');
  assert.ok(h.sent[0][1]?.url?.startsWith('blob:'), 'auto-finalized send must carry the recording URL');
  assert.equal(h.getCancelMicCalls(), callsBefore + 1, 'turnstart should invoke deps.cancelMic');
  assert.deepEqual(h.revokedUrls, [], 'turnstart cancelMic must not revoke the recording already handed to sendMessage');
  assert.equal(h.input.value, '');
});

function setupRealAppWithRecorder(t, { syncEndOnStop = false, asyncRecordStopMs = 20 } = {}) {
  const h = setupWithRecorder(t, { syncEndOnStop, asyncRecordStopMs });
  const doc = globalThis.document;
  class FakeImage extends Element {
    constructor() { super('img', doc); }
    decode() { return Promise.resolve(); }
  }
  const origRaf = globalThis.requestAnimationFrame;
  const origCancelRaf = globalThis.cancelAnimationFrame;
  const origImage = globalThis.Image;
  const origFetch = globalThis.fetch;
  const origLocalStorage = globalThis.localStorage;
  globalThis.requestAnimationFrame = () => 1;
  globalThis.cancelAnimationFrame = () => {};
  globalThis.Image = FakeImage;
  globalThis.localStorage = { getItem: () => null, setItem() {} };
  globalThis.window.innerWidth = 1280;
  globalThis.window.addEventListener = () => {};
  globalThis.window.removeEventListener = () => {};
  globalThis.fetch = async () => Response.json({ backgrounds: [], works: [] });

  const advRoot = doc.createElement('div');
  const sidebar = doc.createElement('div');
  const globalModal = doc.createElement('div');
  const modalBox = doc.createElement('div');
  modalBox.className = 'modal';
  globalModal.appendChild(modalBox);
  doc.body.append(advRoot, sidebar, globalModal);

  const session = {
    id: 's_real_mic',
    title: '新对话',
    messages: [],
    settings: { ...DEFAULT_SESSION_SETTINGS, advEnabled: true, advTextIntervalMs: 0 },
  };
  const state = { sessions: [session], activeId: session.id, global: { voiceMode: false, ttsPreset: '' } };
  const stream = fakeChatStream();
  const cachedBlobs = [];
  const blocked = [];
  let seq = 0;
  const audio = {
    isPlaying: false,
    currentMsgId: null,
    playLine: async () => 'ended',
    stopLine() {},
    interrupt() {},
  };

  audioStore.clear();
  const app = initApp({
    initialize: false,
    dom: { advRoot, sidebar, globalModal },
    state,
    getActive: () => session,
    getSession: (id) => (id === session.id ? session : null),
    isSessionDeleting: () => false,
    getCurrentActiveProvider: () => null,
    getVoiceProfiles: () => [],
    haveProvidersLoaded: () => true,
    saveState: () => {},
    autoTitle: () => {},
    persistSession: async () => {},
    getCurrentVoice: () => null,
    stopCurrentVoice: () => {},
    uid: (prefix = 'm') => `${prefix}_${++seq}`,
    fetch: stream.fetch,
    showToast: (msg) => blocked.push(msg),
    fetchAndCacheAudio: async () => {},
    putCachedAudioBlob: (key, blob) => cachedBlobs.push([key, blob]),
    streamAudioController: audio,
    addMessage(role, content) {
      const msg = { id: `m_${++seq}`, role, content, ts: Date.now() };
      session.messages.push(msg);
      return msg;
    },
    fetchSessionHistoryPage: async () => ({ messages: [], has_more: false }),
  });
  app.fullRender();

  initAudioPlayerUi({}, {
    getInput: () => app.adv?.textbox.inputElement,
    getMicButton: () => app.adv?.textbox.micButton,
    sendMessage: app.sendMessage,
    isBusy: () => app.conversation.busy,
  });

  t.after(() => {
    closeAllModals();
    app.conversation.stop();
    app.adv?.unmount();
    audioStore.clear();
    globalThis.requestAnimationFrame = origRaf;
    globalThis.cancelAnimationFrame = origCancelRaf;
    globalThis.Image = origImage;
    globalThis.fetch = origFetch;
    globalThis.localStorage = origLocalStorage;
  });

  return {
    ...h,
    app,
    session,
    stream,
    cachedBlobs,
    blocked,
    globalModal,
    input: app.adv.textbox.inputElement,
    mic: app.adv.textbox.micButton,
  };
}

test('manual send within 600ms after recording attaches pending voice and does not send twice', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  const h = setupRealAppWithRecorder(t, { syncEndOnStop: false, asyncRecordStopMs: 20 });

  h.mic.click();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(h.recorders.length, 1);
  assert.equal(isListening(), true);

  // Stop mic (schedules 600ms and 1400ms auto-finalize timers)
  h.mic.click();
  h.result(h.recognizers[0], '手动抢先发送', true);
  h.recognizers[0].onend?.();
  t.mock.timers.tick(20);
  assert.equal(h.input.value, '手动抢先发送');
  assert.equal(h.stream.requests.length, 0);

  // Within 600ms, user presses Enter in the ADV textbox
  t.mock.timers.tick(100);
  h.input.dispatchEvent({ type: 'keydown', key: 'Enter' });

  assert.equal(h.stream.requests.length, 1);
  assert.equal(h.stream.requests[0].prompt, '手动抢先发送');
  const userMsg = h.session.messages[0];
  assert.equal(userMsg.content, '手动抢先发送');
  assert.ok(userMsg.dur > 0, 'manual send within 600ms must preserve recording duration');
  assert.equal(userMsg.voiceKey, `user_${userMsg.id}`, 'manual send must persist voiceKey');
  const storedAudio = audioStore.get(userMsg.id);
  assert.ok(storedAudio?.url?.startsWith('blob:fake-mic-'), 'manual send must save recording URL in audioStore');
  assert.equal(h.cachedBlobs.length, 1);
  assert.equal(h.cachedBlobs[0][0], `user_${userMsg.id}`);
  assert.ok(h.cachedBlobs[0][1] instanceof Blob);
  assert.deepEqual(h.revokedUrls, [], 'handed-off recording URL must not be revoked');

  // Complete the first turn before 600ms so conversation is no longer busy when timers would fire
  const donePromise = h.stream.requests[0].onEnd('收到');
  t.mock.timers.tick(1);
  await donePromise;
  for (let i = 0; i < 6; i++) await Promise.resolve();
  assert.equal(h.app.conversation.busy, false);

  t.mock.timers.tick(2000);
  assert.equal(h.stream.requests.length, 1, 'timers firing after manual send must not send a second message');
});

test('editing transcribed text in input cancels auto-send and preserves recording for manual send', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  const h = setupRealAppWithRecorder(t, { syncEndOnStop: false, asyncRecordStopMs: 20 });

  h.mic.click();
  await Promise.resolve();
  await Promise.resolve();

  h.mic.click();
  h.result(h.recognizers[0], '识别原文', true);
  h.recognizers[0].onend?.();
  t.mock.timers.tick(20);
  assert.equal(h.input.value, '识别原文');

  // User edits a few characters in the textbox
  h.input.value = '识别后改过的文字';
  h.input.dispatchEvent({ type: 'input' });

  // Over 2 seconds pass without auto-sending the original transcription
  t.mock.timers.tick(2000);
  assert.equal(h.stream.requests.length, 0, 'editing the input must cancel the 600ms/1400ms auto-send timers');
  assert.equal(h.input.value, '识别后改过的文字');
  assert.deepEqual(h.revokedUrls, [], 'recording must stay available while user edits text');

  // User then manually sends the edited text
  h.input.dispatchEvent({ type: 'keydown', key: 'Enter' });
  assert.equal(h.stream.requests.length, 1);
  assert.equal(h.stream.requests[0].prompt, '识别后改过的文字');
  const userMsg = h.session.messages[0];
  assert.equal(userMsg.content, '识别后改过的文字');
  assert.equal(userMsg.voiceKey, `user_${userMsg.id}`);
  assert.ok(audioStore.get(userMsg.id)?.url?.startsWith('blob:fake-mic-'));
  assert.equal(h.cachedBlobs.length, 1);
  assert.ok(h.cachedBlobs[0][1] instanceof Blob);
});

test('opening overlays while recording cancels mic and prevents background auto-send', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  const h = setupRealAppWithRecorder(t, { syncEndOnStop: false, asyncRecordStopMs: 20 });

  const cases = [
    {
      name: 'log',
      open: () => h.app.adv.action('log'),
      close: () => h.app.adv.action('close_overlay'),
    },
    {
      name: 'sessions',
      open: () => h.app.adv.action('sessions'),
      close: () => h.app.adv.action('close_overlay'),
    },
    {
      name: 'background',
      open: () => h.app.adv.action('background'),
      close: () => h.app.adv.action('close_overlay'),
    },
    {
      name: 'reading_settings',
      open: () => h.app.adv.action('reading_settings'),
      close: () => h.app.adv.action('close_overlay'),
    },
    {
      name: 'settings_center',
      open: () => openModal(h.globalModal),
      close: () => closeAllModals(),
    },
  ];

  for (let i = 0; i < cases.length; i++) {
    const c = cases[i];
    h.mic.click();
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(isListening(), true, `${c.name}: mic should be listening before overlay opens`);
    assert.equal(h.mic.getAttribute('aria-pressed'), 'true');
    assert.equal(h.recorders[i].state, 'recording');

    c.open();

    assert.equal(isListening(), false, `${c.name}: opening overlay must stop mic immediately`);
    assert.equal(h.mic.getAttribute('aria-pressed'), 'false', `${c.name}: mic button must reset`);
    assert.equal(h.recorders[i].stopped, true, `${c.name}: MediaRecorder must stop when overlay opens`);

    // Late speech recognition result and recorder finish arrive while overlay is open
    h.result(h.recognizers[i], `背后语音_${c.name}`, true);
    h.recognizers[i].onend?.();
    t.mock.timers.tick(2000);

    assert.equal(h.stream.requests.length, 0, `${c.name}: must not auto-send behind overlay`);
    assert.equal(h.createdUrls.length, h.revokedUrls.length, `${c.name}: must not leak Blob URL`);

    c.close();
  }
});

test('retry and explicit voiceMeta sends are unaffected by pendingVoice', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  const h = setupRealAppWithRecorder(t, { syncEndOnStop: false, asyncRecordStopMs: 20 });

  // 1. Send a plain text message that fails so it is eligible for retry
  assert.equal(h.app.sendMessage('失败待重试'), true);
  for (let i = 0; i < 6; i++) await Promise.resolve();
  const failEnd = h.stream.requests[0].onEnd('', { error: '连接中断', errorCode: 'NETWORK_ERROR' });
  t.mock.timers.tick(1);
  await failEnd;
  for (let i = 0; i < 6; i++) await Promise.resolve();
  const failedMsg = h.session.messages[0];
  assert.equal(h.app.conversation.canRetry(failedMsg), true);
  assert.equal(failedMsg.voiceKey, undefined);

  // Record a pending voice clip, do not let 600ms auto-send yet
  h.mic.click();
  await Promise.resolve();
  await Promise.resolve();
  h.mic.click();
  h.result(h.recognizers[0], '不该绑到重试上的录音', true);
  h.recognizers[0].onend?.();
  t.mock.timers.tick(20);

  // Retry the failed message: retryOf is non-null, so it must NOT consume pendingVoice as its voiceMeta
  assert.equal(h.app.conversation.retry(failedMsg), true);
  assert.equal(failedMsg.voiceKey, undefined, 'retry must not attach pendingVoice to retried message');
  assert.equal(audioStore.get(failedMsg.id), undefined);
  for (let i = 0; i < 6; i++) await Promise.resolve();
  const retryEnd = h.stream.requests[1].onEnd('重试成功');
  t.mock.timers.tick(1);
  await retryEnd;
  for (let i = 0; i < 6; i++) await Promise.resolve();
  t.mock.timers.tick(2000);
  assert.equal(h.stream.requests.length, 2);

  // 2. Record another pending voice clip, then call sendMessage with an explicit voiceMeta
  h.mic.click();
  await Promise.resolve();
  await Promise.resolve();
  h.mic.click();
  h.result(h.recognizers[1], '待发语音', true);
  h.recognizers[1].onend?.();
  t.mock.timers.tick(20);

  const explicitBlob = new Blob(['explicit-audio'], { type: 'audio/webm' });
  assert.equal(
    h.app.sendMessage('显式语音消息', { url: 'blob:explicit-voice', dur: 9, blob: explicitBlob }),
    true
  );
  const explicitMsg = h.session.messages.at(-1);
  assert.equal(explicitMsg.content, '显式语音消息');
  assert.equal(explicitMsg.dur, 9, 'explicit voiceMeta duration must be preserved');
  assert.equal(audioStore.get(explicitMsg.id)?.url, 'blob:explicit-voice');
  assert.equal(h.cachedBlobs.at(-1)?.[1], explicitBlob);

  for (let i = 0; i < 6; i++) await Promise.resolve();
  const explicitEnd = h.stream.requests[2].onEnd('收到显式');
  t.mock.timers.tick(1);
  await explicitEnd;
  for (let i = 0; i < 6; i++) await Promise.resolve();
  t.mock.timers.tick(2000);
  assert.equal(h.stream.requests.length, 3);
  assert.equal(h.createdUrls.length, h.revokedUrls.length, 'unconsumed pending mic URL should be revoked cleanly');
});

test('a mic refused on a LAN http address says why instead of asking for permission', t => {
  const h = setup(t, { syncEndOnStop: false });
  const errors = [];
  for (const secure of [false, true]) {
    globalThis.window.isSecureContext = secure;
    assert.equal(startListening({ onError: message => errors.push(message) }).ok, true);
    const recognizer = h.recognizers.at(-1);
    recognizer.onerror({ error: 'not-allowed' });
  }
  delete globalThis.window.isSecureContext;
  assert.match(errors[0], /局域网地址（http）打开时，浏览器不允许使用麦克风/);
  assert.match(errors[1], /麦克风权限被拒绝/);
});
