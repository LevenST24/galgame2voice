import assert from 'node:assert/strict';
import test from 'node:test';
import { pickSettings, normalizeSnapshot } from '../frontend/src/session_data.js';
import { DEFAULT_SESSION_SETTINGS as defaults, DEFAULT_GLOBAL, state, loadState, saveState,
  createSession, getActive, exportSessionData, prepareSessionImport, applySessionImport } from '../frontend/src/store.js';
import { createAdvSettings } from '../frontend/src/adv/settings.js';
import { fakeDocument } from './helpers/adv_dom.mjs';

const expected = { advPlayerName: 'Player', advPrimaryLanguage: 'zh', advSubtitleLanguage: 'ja',
  advWindowOpacity: 75, advTextIntervalMs: 30, advInterruptVoice: true, advAutoBaseMs: 1500,
  advAutoPerCharMs: 45, advAutoMode: 'voice-priority', advOutfit: '', advBlink: true, advLipSync: true };
const custom = { advPlayerName: '旅行者', advPrimaryLanguage: 'ja', advSubtitleLanguage: 'zh',
  advWindowOpacity: 50, advTextIntervalMs: 0, advInterruptVoice: false, advAutoBaseMs: 6000,
  advAutoPerCharMs: 200, advAutoMode: 'normal', advOutfit: '制服_2', advBlink: true, advLipSync: true };
const backgroundSettings = { advBackground: 'cafe/stella', advBgTime: 'evening' };

test('background defaults and global key/time validation', () => {
  assert.equal(defaults.advBackground, '');
  assert.equal(defaults.advBgTime, 'auto');
  for (const advBackground of ['', 'cafe/stella', 'A_-09/a', 'a'.repeat(64) + '/' + 'b'.repeat(64)]) {
    for (const advBgTime of ['auto', 'day', 'evening', 'night']) {
      const picked = pickSettings({ advBackground, advBgTime }, defaults);
      assert.equal(picked.advBackground, advBackground);
      assert.equal(picked.advBgTime, advBgTime);
    }
  }
});

test('ADV preferences have exact store defaults and pickSettings preserves every valid value', () => {
  for (const [key, value] of Object.entries(expected)) assert.equal(defaults[key], value, key);
  const picked = pickSettings(custom, defaults);
  for (const [key, value] of Object.entries(custom)) assert.equal(picked[key], value, key);
  assert.equal(pickSettings({ ...custom, unrelatedSecret: 'drop' }, defaults).unrelatedSecret, undefined);
});

const invalid = {
  advInterruptVoice: ['false', 1, null],
  advPlayerName: ['', '   ', '字'.repeat(41), null, 3],
  advPrimaryLanguage: ['en', 'none', null, 1], advSubtitleLanguage: ['en', null, false],
  advWindowOpacity: [-1, 101, 1.5, Infinity, '75'], advTextIntervalMs: [-1, 101, 0.5, NaN, '30'],
  advAutoBaseMs: [-1, 6001, 1.1, Infinity, null], advAutoPerCharMs: [-1, 201, 0.5, NaN],
  advAutoMode: ['fast', '', null, 2], advOutfit: ['../private', '/absolute', 'a/b', 'a\\b', {}, null],
  advBackground: ['../cafe', 'cafe', 'cafe/stella/day', 'cafe//stella', '/stella', 'cafe/', 'cafe\\day',
    '咖啡店', 'a'.repeat(65), 'cafe\n', 'cafe/' + 'a'.repeat(65), {}, null, 1],
  advBgTime: ['dawn', '', 'DAY', null, 1],
};
for (const [key, values] of Object.entries(invalid)) test(`${key}: invalid values fall back at the snapshot boundary`, () => {
  for (const value of values) {
    const result = normalizeSnapshot({ sessions: [{ id: 's', title: 'test', messages: [], settings: { [key]: value } }] }, defaults, DEFAULT_GLOBAL);
    const fallback = key === 'advBackground' ? '' : key === 'advBgTime' ? 'auto' : defaults[key];
    assert.equal(result.sessions[0].settings[key], fallback, `${key}=${String(value)}`);
  }
});

test('language collision, none subtitle, numeric endpoints and forty grapheme names', () => {
  assert.equal(pickSettings({ advPrimaryLanguage: 'ja', advSubtitleLanguage: 'ja' }, defaults).advSubtitleLanguage, 'zh');
  assert.equal(pickSettings({ advSubtitleLanguage: 'none' }, defaults).advSubtitleLanguage, 'none');
  for (const key of ['advWindowOpacity', 'advTextIntervalMs', 'advAutoBaseMs', 'advAutoPerCharMs']) {
    assert.equal(pickSettings({ [key]: 0 }, defaults)[key], 0);
  }
  const name = '👩‍💻'.repeat(40);
  assert.equal(pickSettings({ advPlayerName: name }, defaults).advPlayerName, name);
  assert.equal(pickSettings({ advPlayerName: name + 'a' }, defaults).advPlayerName, 'Player');
});

test('store reload, browser backup/import and switching keep each session settings isolated', () => {
  const data = new Map(); globalThis.localStorage = { getItem: key => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key) };
  const settings = { ...custom, ...backgroundSettings };
  loadState(); const first = getActive(); Object.assign(first.settings, settings); saveState();
  const second = createSession('default'); assert.equal(getActive(), second);
  assert.equal(second.settings.advEnabled, undefined); state.activeId = first.id; saveState();
  loadState(); for (const [key, value] of Object.entries(settings)) assert.equal(getActive().settings[key], value, key);
  state.activeId = second.id; assert.equal(getActive().settings.advPlayerName, 'Player');
  assert.equal(getActive().settings.advBackground, ''); assert.equal(getActive().settings.advBgTime, 'auto');
  const incoming = prepareSessionImport(exportSessionData()); applySessionImport(incoming);
  const restored = state.sessions.find(session => session.id.startsWith('s_import_') && session.title === first.title);
  for (const [key, value] of Object.entries(settings)) assert.equal(restored.settings[key], value, key);
  state.activeId = restored.id; assert.equal(getActive().settings.advBackground, 'cafe/stella');
  state.activeId = second.id; assert.equal(getActive().settings.advBgTime, 'auto');
  assert.equal(restored.settings.voiceProfileId, null);
});

test('session settings group validates draft values, resets and safely renders literal text', () => {
  globalThis.document = fakeDocument();
  const panel = document.createElement('div'); const ui = createAdvSettings(panel, defaults);
  ui.load({ ...defaults, ...custom }); assert.deepEqual(ui.read(), custom);
  const name = panel.querySelector('#s-advPlayerName'); name.value = '<img src=x onerror=alert(1)>';
  assert.equal(ui.read().advPlayerName, name.value);
  const opacity = panel.querySelector('#s-advWindowOpacity'); opacity.value = '101'; assert.throws(() => ui.read(), /ADV/);
  opacity.value = ''; assert.throws(() => ui.read(), /ADV/);
  assert.equal(panel.querySelector('#s-advEnabled'), null);
  ui.load(defaults); assert.deepEqual(ui.read(), expected);
  assert.equal(panel.querySelector('summary').textContent, 'ADV 显示与交互');
});
