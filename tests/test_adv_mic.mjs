import test from 'node:test';
import assert from 'node:assert/strict';
import { initAudioPlayerUi, toggleMic, cancelMic, finalizeVoice } from '../frontend/src/controllers/audio_player_ui.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

function setup(t) {
  const recognizers = [], sent = [];
  globalThis.document = fakeDocument();
  class SpeechRecognition {
    constructor() { recognizers.push(this); }
    start() {}
    stop() { this.onend?.(); }
  }
  globalThis.window = { SpeechRecognition };
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
