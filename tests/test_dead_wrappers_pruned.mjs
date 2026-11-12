import assert from 'node:assert/strict';
import test from 'node:test';
import * as api from '../frontend/src/api.js';
import * as audioPlayerUi from '../frontend/src/controllers/audio_player_ui.js';
import * as voice from '../frontend/src/voice.js';

test('api.js does not export the unused fetchProvider wrapper', () => {
  assert.equal('fetchProvider' in api, false);
  assert.equal(typeof api.fetchProviders, 'function');
  assert.equal(typeof api.activateProvider, 'function');
});

test('legacy whole-message audio helpers are no longer exported', () => {
  assert.equal('playAiVoice' in audioPlayerUi, false);
  assert.equal('plainSpeechText' in voice, false);
  assert.equal('setAudioStore' in voice, false);
  assert.equal(typeof audioPlayerUi.playVoice, 'function');
  assert.equal(typeof voice.audioStore.set, 'function');
});
