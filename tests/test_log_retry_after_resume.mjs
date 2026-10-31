import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createBacklog } from '../frontend/src/adv/backlog.js';
import { StreamAudioController } from '../frontend/src/audio_player.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

test('LOG lets a user retry after the browser rejects audio resume', async () => {
  class Context {
    state = 'suspended'; currentTime = 0; destination = {};
    createGain() { return { gain: { setValueAtTime() {} }, connect() {}, disconnect() {} }; }
    async resume() { throw new Error('gesture required'); }
    async close() {}
  }
  globalThis.window = { AudioContext: Context };
  const errors = [], audio = new StreamAudioController({ onError: e => errors.push(e.message) });
  const doc = fakeDocument(), root = doc.createElement('div'); doc.body.appendChild(root);
  const session = { messages: [{ id: 'm', role: 'assistant', ts: Date.now(), lines: [{ index: 0, zh: '你好', ja: 'こんにちは', audio: { status: 'ready', chunks: [{ audio_url: '/audio/a.wav' }] } }] }] };
  const log = createBacklog(root, { getSession: () => session, getSettings: () => ({}), voiceEnabled: () => true, audio, regenerateLineAudio() {} });
  const button = log.element.querySelector('.adv-log-replay-btn');
  await button.onclick({ stopPropagation() {} });
  console.log('error:', errors[0], 'retry disabled:', button.disabled);
  try { assert.equal(button.disabled, false); } finally { log.destroy(); await audio.close(); delete globalThis.window; }
});

