import test from 'node:test';
import assert from 'node:assert/strict';
import { initAudioPlayerUi, toggleMic, cancelMic, finalizeVoice } from '../frontend/src/controllers/audio_player_ui.js';
import { startListening, stopListening } from '../frontend/src/voice.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

function setup(t, { syncEndOnStop = true } = {}) {
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
    isBusy: () => false, sendMessage: (...args) => sent.push(args),
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
