import assert from 'node:assert/strict';
import test from 'node:test';
import { pickSettings, normalizeSnapshot } from '../frontend/src/session_data.js';
import {
  DEFAULT_SESSION_SETTINGS as defaults,
  DEFAULT_GLOBAL,
  state,
  loadState,
  saveState,
  createSession,
  getActive,
  exportSessionData,
  prepareSessionImport,
  applySessionImport,
} from '../frontend/src/store.js';
import { createConfigPanel, CONFIG_PANEL_FIELDS } from '../frontend/src/adv/config_panel.js';
import { createAdvSettings, ADV_FIELDS, validateAdvField, validateAdvLanguages } from '../frontend/src/adv/settings.js';
import { mountAdv } from '../frontend/src/adv/adv_app.js';
import { fakeDocument, fakeClock, Element, microtasks } from './helpers/adv_dom.mjs';

const portrait = (file = '/art.png') => ({
  enabled: true,
  defaultCostume: 'uniform',
  costumes: [
    { id: 'uniform', faceRect: { x: 0, y: 0, width: 400, height: 450 }, headAnchor: { x: 200, y: 0 } },
    { id: 'summer', faceRect: { x: 0, y: 0, width: 400, height: 450 }, headAnchor: { x: 200, y: 0 } },
  ],
  sprites: {
    uniform: { smile: { file, width: 800, height: 1600 } },
    summer: { smile: { file: '/summer.png', width: 800, height: 1600 } },
  },
  faces: { smile: { primary: 'gentle' } },
  expressionSets: { gentle: [{ id: 'smile' }] },
});

function setup(t, options = {}) {
  const doc = fakeDocument();
  globalThis.document = doc;
  const clock = fakeClock();
  const frames = new Map();
  const loads = [];
  const listeners = new Map();
  const mediaListeners = new Set();
  const viewport = doc.createElement('div');
  let frameId = 0, voiceEnd, overlayChange, subscription;
  const sessions = [0, 1].map(n => ({
    id: `s_${n}`,
    title: `对话 ${n}`,
    settings: { ...defaults, advEnabled: true, ...options.settings },
    messages: [],
  }));
  let active = sessions[0];
  let character = { id: 1, name: '测试角色' };
  let voiceEnabledVal = options.voice !== false;

  class Image extends Element {
    constructor() { super('img', doc); }
    decode() {
      const load = { image: this, url: this.src };
      load.promise = new Promise((resolve, reject) => { load.resolve = resolve; load.reject = reject; });
      loads.push(load);
      return load.promise;
    }
  }

  const controller = {
    busy: false, sent: [], retried: [], stopped: 0,
    on(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
      return () => listeners.get(type).delete(fn);
    },
    send(text) {
      if (this.busy || active.loadingOlder || active.deleting) return false;
      this.sent.push(text);
      return true;
    },
    canRetry(user) {
      return !this.busy && !active.loadingOlder && !active.deleting && !!user?.failed && active.messages.at(-1) === user;
    },
    retry(user) { if (this.canRetry(user)) this.retried.push(user); },
    stop() { this.stopped++; },
  };

  const audio = {
    played: [], stopped: [],
    playLine(index, chunks, opts) {
      this.played.push({ index, chunks, opts });
      return new Promise(resolve => { voiceEnd = resolve; });
    },
    stopLine(opts) {
      this.stopped.push(opts);
      voiceEnd?.('interrupted');
    },
  };

  const dom = Object.fromEntries(
    ['root', 'sidebar', 'messages', 'input', 'micBtn'].map(k => [k, doc.createElement(k === 'input' ? 'textarea' : 'div')])
  );
  for (const el of Object.values(dom)) doc.body.appendChild(el);

  const mql = {
    matches: Boolean(options.reducedMotion),
    addEventListener: (_, fn) => mediaListeners.add(fn),
    removeEventListener: (_, fn) => mediaListeners.delete(fn),
  };

  const calls = { settings: 0, models: 0, newSession: 0, history: 0, mic: 0, voiceToggle: 0 };

  const app = mountAdv(dom, {
    controller, audio, clock,
    getSession: () => active,
    voiceEnabled: () => voiceEnabledVal,
    toggleVoice: () => {
      calls.voiceToggle++;
      voiceEnabledVal = !voiceEnabledVal;
    },
    getCharacter: () => character,
    ImageClass: Image,
    loadPortrait: options.loadPortrait || (async () => options.art ? portrait() : null),
    raf: fn => { frames.set(++frameId, fn); return frameId; },
    cancelRaf: id => frames.delete(id),
    matchMedia: () => mql,
    viewport,
    saveSettings: patch => {
      active.settings = pickSettings({ ...active.settings, ...patch }, defaults);
    },
    onSettings: () => { calls.settings++; },
    onSessionSettings: () => { calls.settings++; },
    onModelSettings: () => { calls.models++; },
    onNewSession: () => { calls.newSession++; },
    onMic: () => { calls.mic++; },
    subscribeOverlay: fn => { overlayChange = fn; return () => { overlayChange = null; }; },
    subscribe: fn => { subscription = fn; return () => { subscription = null; }; },
  });

  t.after(() => app.unmount());

  const emit = (type, event) => {
    for (const fn of [...listeners.get(type) || []]) fn(event);
  };

  let turn = 'turn_0';
  const start = (id = 'turn_0') => {
    turn = id;
    controller.busy = true;
    const userMessage = { id: 'user_0', role: 'user', content: '问题', recording: 'keep' };
    active.messages.push(userMessage);
    emit('turnstart', { originId: active.id, assistantMessage: { id }, userMessage });
  };
  const line = (index = 0, extra = {}) => {
    const record = { index, zh: '你好世界。', ja: 'こんにちは。', emotion: 'gentle', act: {}, ...extra };
    active.messages.push(record);
    emit('line', { messageId: turn, line: record });
    return record;
  };
  const audioEvent = (index = 0, status = 'ready', id = turn) => {
    const event = {
      messageId: id, line: index, status,
      chunks: status === 'ready' ? [{ index: 0, line: index, audio_url: '/audio/0.wav' }] : [],
    };
    if (id === turn) {
      const record = active.messages.find(m => m.index === index);
      if (record) record.audio = { status, chunks: event.chunks };
    }
    emit('line_audio', event);
  };
  const done = (type = 'done') => {
    controller.busy = false;
    emit(type, { originId: active.id });
  };

  return {
    app, doc, dom, clock, frames, loads, controller, audio, sessions, calls, listeners, mediaListeners,
    start, line, audioEvent, done,
    get active() { return active; },
    switchSession: n => { active = sessions[n]; subscription(); },
    motion: matches => {
      mql.matches = matches;
      for (const fn of mediaListeners) fn({ matches });
    },
  };
}

// ---------------------------------------------------------------------------
// 1. Live preview and revert
// ---------------------------------------------------------------------------
test('live preview: opacity immediately updates message band, cancel reverts', t => {
  const h = setup(t, { settings: { advWindowOpacity: 75 } });
  h.start(); h.line();

  const band = h.app.stage.stage.querySelector('.adv-band');
  assert.equal(band.style['--adv-window-opacity'], '75');

  // Open settings overlay
  h.app.action('settings');
  assert.equal(h.app.configPanel.isOpen, true);
  assert.equal(h.app.player.state, 'suspended');

  // Preview change opacity to 35
  const opacityInput = h.app.configPanel.controls.advWindowOpacity;
  opacityInput.value = '35';
  opacityInput.dispatchEvent({ type: 'input' });

  // Band shows previewed value immediately
  assert.equal(band.style['--adv-window-opacity'], '35');
  assert.equal(h.app.configPanel.valElements.advWindowOpacity.textContent, '35%');

  // Revert via Cancel button
  const cancelBtn = h.app.configPanel.element.querySelector('.adv-config-btn-cancel');
  cancelBtn.click();

  assert.equal(h.app.configPanel.isOpen, false);
  assert.equal(band.style['--adv-window-opacity'], '75');
});

test('live preview: text speed sample line types over time; instant when 0 or reduced-motion', t => {
  const h = setup(t, { settings: { advTextIntervalMs: 40 } });
  h.app.action('settings');

  const sampleText = h.app.configPanel.element.querySelector('.adv-config-sample-text');
  const speedInput = h.app.configPanel.controls.advTextIntervalMs;

  // Initial typewriter started with speed 40
  assert.equal(sampleText.textContent, '');
  h.clock.tick(40);
  assert.equal(sampleText.textContent.length, 1);
  h.clock.tick(120);
  assert.equal(sampleText.textContent.length, 4);

  // Speed set to 0: shows full sample at once
  speedInput.value = '0';
  speedInput.dispatchEvent({ type: 'input' });
  assert.equal(sampleText.textContent, '这是一段用于测试文字显示速度的示例文本。');

  // Speed set to 50: types out
  speedInput.value = '50';
  speedInput.dispatchEvent({ type: 'input' });
  assert.equal(sampleText.textContent, '');
  h.clock.tick(50);
  assert.equal(sampleText.textContent.length, 1);

  // Reduced motion: sample shows full text at once
  h.motion(true);
  speedInput.value = '50';
  speedInput.dispatchEvent({ type: 'input' });
  assert.equal(sampleText.textContent, '这是一段用于测试文字显示速度的示例文本。');

  h.app.configPanel.close({ revert: true });
});

test('live preview: outfit changes sprite preview and Esc reverts to baseline', async t => {
  const h = setup(t, { art: true, settings: { advOutfit: 'uniform' } });
  await microtasks();
  h.loads.forEach(l => l.resolve());
  await microtasks();

  h.app.action('settings');
  const outfitSelect = h.app.configPanel.controls.advOutfit;
  assert.equal(outfitSelect.tagName, 'SELECT');
  assert.equal(outfitSelect.value, 'uniform');

  outfitSelect.value = 'summer';
  outfitSelect.dispatchEvent({ type: 'change' });

  // Escape key reverts
  h.app.configPanel.element.querySelector('.adv-config-dialog').dispatchEvent({
    type: 'keydown',
    key: 'Escape',
  });
  assert.equal(h.app.configPanel.isOpen, false);
});

// ---------------------------------------------------------------------------
// 2. Saving persists through pickSettings, survives session switch, reload, export/import
// ---------------------------------------------------------------------------
test('saving persists through pickSettings and survives session switch, reload, and export/import', t => {
  const data = new Map();
  globalThis.localStorage = {
    getItem: k => data.get(k) ?? null,
    setItem: (k, v) => data.set(k, v),
    removeItem: k => data.delete(k),
  };

  loadState();
  const h = setup(t);
  const activeSess = h.active;

  const custom = {
    advPlayerName: '爱丽丝',
    advPrimaryLanguage: 'ja',
    advSubtitleLanguage: 'zh',
    advWindowOpacity: 45,
    advTextIntervalMs: 15,
    advAutoBaseMs: 3500,
    advAutoPerCharMs: 85,
    advAutoMode: 'normal',
    advInterruptVoice: false,
    advOutfit: 'summer_uniform',
  };

  h.app.action('settings');
  const c = h.app.configPanel.controls;
  c.advPlayerName.value = custom.advPlayerName;
  c.advPrimaryLanguage.value = custom.advPrimaryLanguage;
  c.advSubtitleLanguage.value = custom.advSubtitleLanguage;
  c.advWindowOpacity.value = String(custom.advWindowOpacity);
  c.advTextIntervalMs.value = String(custom.advTextIntervalMs);
  c.advAutoBaseMs.value = String(custom.advAutoBaseMs);
  c.advAutoPerCharMs.value = String(custom.advAutoPerCharMs);
  c.advAutoMode.value = custom.advAutoMode;
  c.advInterruptVoice.value = String(custom.advInterruptVoice);
  c.advOutfit.value = custom.advOutfit;

  const saveBtn = h.app.configPanel.element.querySelector('.adv-config-btn-save');
  saveBtn.click();
  assert.equal(h.app.configPanel.isOpen, false);

  // Settings are persisted on active session
  for (const [k, v] of Object.entries(custom)) {
    assert.equal(activeSess.settings[k], v, `key ${k}`);
  }

  // Session switch: switch to session 1, verify defaults, switch back, verify custom
  h.switchSession(1);
  assert.equal(h.active.settings.advPlayerName, 'Player');
  assert.equal(h.active.settings.advWindowOpacity, 75);

  h.switchSession(0);
  for (const [k, v] of Object.entries(custom)) {
    assert.equal(h.active.settings[k], v, `switched back ${k}`);
  }

  // Store reload
  Object.assign(getActive().settings, activeSess.settings);
  saveState();
  loadState();
  for (const [k, v] of Object.entries(custom)) {
    assert.equal(getActive().settings[k], v, `reloaded ${k}`);
  }

  // Export and import
  const incoming = prepareSessionImport(exportSessionData());
  applySessionImport(incoming);
  const restored = state.sessions.find(s => s.id.startsWith('s_import_'));
  assert.ok(restored, 'imported session found');
  for (const [k, v] of Object.entries(custom)) {
    assert.equal(restored.settings[k], v, `imported ${k}`);
  }
});

// ---------------------------------------------------------------------------
// 3. Invalid input blocks saving
// ---------------------------------------------------------------------------
test('invalid input blocks saving and displays field error', t => {
  const h = setup(t);
  h.app.action('settings');

  const c = h.app.configPanel.controls;
  const errs = h.app.configPanel.errorElements;
  const saveBtn = h.app.configPanel.element.querySelector('.adv-config-btn-save');

  // A. Empty player name
  c.advPlayerName.value = '   ';
  saveBtn.click();
  assert.equal(h.app.configPanel.isOpen, true, 'saving should be blocked');
  assert.equal(errs.advPlayerName.hidden, false);
  assert.match(errs.advPlayerName.textContent, /ADV 设置无效/);
  assert.equal(h.active.settings.advPlayerName, 'Player', 'store must remain unchanged');

  // B. Player name > 40 graphemes
  c.advPlayerName.value = '🌸'.repeat(41);
  saveBtn.click();
  assert.equal(h.app.configPanel.isOpen, true);
  assert.equal(errs.advPlayerName.hidden, false);

  // Restore valid name
  c.advPlayerName.value = '有效名字';

  // C. Out of range opacity
  c.advWindowOpacity.value = '150';
  saveBtn.click();
  assert.equal(h.app.configPanel.isOpen, true);
  assert.equal(errs.advWindowOpacity.hidden, false);
  c.advWindowOpacity.value = '75';

  // D. Language collision (primary === subtitle)
  c.advPrimaryLanguage.value = 'zh';
  c.advSubtitleLanguage.value = 'zh';
  saveBtn.click();
  assert.equal(h.app.configPanel.isOpen, true);
  assert.equal(errs.advLanguages.hidden, false);
  assert.match(errs.advLanguages.textContent, /主要语言和字幕语言/);
  c.advSubtitleLanguage.value = 'ja';

  // E. Invalid outfit ID with path characters
  c.advOutfit.value = '../evil/path';
  saveBtn.click();
  assert.equal(h.app.configPanel.isOpen, true);
  assert.equal(errs.advOutfit.hidden, false);
  c.advOutfit.value = 'valid_costume-1';

  // Valid values now save successfully
  saveBtn.click();
  assert.equal(h.app.configPanel.isOpen, false);
  assert.equal(h.active.settings.advPlayerName, '有效名字');
  assert.equal(h.active.settings.advOutfit, 'valid_costume-1');
});

// ---------------------------------------------------------------------------
// 4. Focus return
// ---------------------------------------------------------------------------
test('opening panel pauses under reason dialog, preserves draft, and closing restores focus', t => {
  const h = setup(t, { settings: { advTextIntervalMs: 0 } });
  h.start(); h.line();

  // Enter a player draft
  h.app.textbox.setDraft('保留玩家草稿');

  // Focus a specific stage button before opening
  const qmConfigBtn = h.app.stage.stage.querySelector('.adv-qm-btn-config');
  qmConfigBtn.focus();
  assert.equal(h.doc.activeElement, qmConfigBtn);

  // Open panel via action
  h.app.action('settings');
  assert.equal(h.app.configPanel.isOpen, true);
  assert.equal(h.app.player.state, 'suspended');

  // Verify focus moved into the panel
  assert.ok(h.app.configPanel.element.contains(h.doc.activeElement));

  // Close panel via cancel
  const cancelBtn = h.app.configPanel.element.querySelector('.adv-config-btn-cancel');
  cancelBtn.click();

  // Focus returned to the button
  assert.equal(h.doc.activeElement, qmConfigBtn);

  // Draft preserved
  assert.equal(h.app.textbox.draft, '保留玩家草稿');
});

// ---------------------------------------------------------------------------
// 5. F8, and its exclusion during IME composition
// ---------------------------------------------------------------------------
test('F8 opens panel outside composition; excluded during IME composition', t => {
  const h = setup(t);

  // A. During IME composition (isComposing = true)
  h.app.stage.stage.dispatchEvent({
    type: 'keydown',
    key: 'F8',
    isComposing: true,
  });
  assert.equal(h.app.configPanel.isOpen, false);

  // B. During IME composition (keyCode = 229)
  h.app.stage.stage.dispatchEvent({
    type: 'keydown',
    key: 'F8',
    keyCode: 229,
  });
  assert.equal(h.app.configPanel.isOpen, false);

  // C. Outside composition: F8 opens config panel
  h.app.stage.stage.dispatchEvent({
    type: 'keydown',
    key: 'F8',
  });
  assert.equal(h.app.configPanel.isOpen, true);

  // D. Pressing F8 while panel is open does not disrupt it
  h.app.stage.stage.dispatchEvent({
    type: 'keydown',
    key: 'F8',
  });
  assert.equal(h.app.configPanel.isOpen, true);

  // E. Escape key closes overlay
  h.app.configPanel.element.querySelector('.adv-config-dialog').dispatchEvent({
    type: 'keydown',
    key: 'Escape',
  });
  assert.equal(h.app.configPanel.isOpen, false);
});

// ---------------------------------------------------------------------------
// 6. Navigation buttons and global voice mode toggle
// ---------------------------------------------------------------------------
test('global voice-mode toggle and model/session settings navigation buttons', t => {
  const h = setup(t);
  h.app.action('settings');

  // Model settings button
  const btnModels = h.app.configPanel.element.querySelector('#cfg-btn-models');
  btnModels.click();
  assert.equal(h.calls.models, 1);

  // Session settings button
  const btnSession = h.app.configPanel.element.querySelector('#cfg-btn-session');
  btnSession.click();
  assert.equal(h.calls.settings, 1);

  // Voice mode toggle
  const voiceBtn = h.app.configPanel.element.querySelector('#cfg-voiceMode');
  assert.equal(voiceBtn.getAttribute('aria-pressed'), 'true');
  voiceBtn.click();
  assert.equal(voiceBtn.getAttribute('aria-pressed'), 'false');

  // Save saves toggled voice mode
  const saveBtn = h.app.configPanel.element.querySelector('.adv-config-btn-save');
  saveBtn.click();
  assert.equal(h.calls.voiceToggle, 1);
});

// ---------------------------------------------------------------------------
// 7. Old modal compatibility
// ---------------------------------------------------------------------------
test('old modal createAdvSettings continues working and shares validation rules', () => {
  const doc = fakeDocument();
  globalThis.document = doc;
  const container = doc.createElement('div');
  const oldUi = createAdvSettings(container, defaults);

  oldUi.load({ advWindowOpacity: 60, advPlayerName: '老面板玩家' });
  const readValues = oldUi.read();
  assert.equal(readValues.advWindowOpacity, 60);
  assert.equal(readValues.advPlayerName, '老面板玩家');

  // Invalid value throws /ADV/
  container.querySelector('#s-advWindowOpacity').value = '200';
  assert.throws(() => oldUi.read(), /ADV/);
});

// ---------------------------------------------------------------------------
// 8. Outfit select & Focus confinement
// ---------------------------------------------------------------------------
test('outfit select: default option, installed costumes with names, uninstalled costume preserved with （未安装）<id>', async t => {
  const customPortrait = {
    enabled: true,
    defaultCostume: 'school_uniform',
    costumes: [
      { id: 'school_uniform', name: '水手服' },
      { id: 'casual_wear', name: '便服' },
    ],
    sprites: {},
    faces: {},
    expressionSets: {},
  };

  // Case A: Stored ID is an installed costume ('casual_wear')
  const h = setup(t, { loadPortrait: async () => customPortrait, settings: { advOutfit: 'casual_wear' } });
  await microtasks();
  h.app.action('settings');

  const selectA = h.app.configPanel.controls.advOutfit;
  assert.equal(selectA.tagName, 'SELECT');
  const optionsA = selectA.querySelectorAll('option');
  assert.equal(optionsA.length, 3);
  assert.equal(optionsA[0].value, '');
  assert.equal(optionsA[0].textContent, '默认（角色包默认）');
  assert.equal(optionsA[1].value, 'school_uniform');
  assert.equal(optionsA[1].textContent, '水手服');
  assert.equal(optionsA[2].value, 'casual_wear');
  assert.equal(optionsA[2].textContent, '便服');
  assert.equal(selectA.value, 'casual_wear');

  h.app.configPanel.close({ revert: true });

  // Case B: Stored ID is not an installed costume ('dlc_gothic') -> preserved as （未安装）<id>
  h.active.settings.advOutfit = 'dlc_gothic';
  h.app.action('settings');
  const selectB = h.app.configPanel.controls.advOutfit;
  const optionsB = selectB.querySelectorAll('option');
  assert.equal(optionsB.length, 4);
  assert.equal(optionsB[0].value, '');
  assert.equal(optionsB[0].textContent, '默认（角色包默认）');
  assert.equal(optionsB[1].value, 'school_uniform');
  assert.equal(optionsB[2].value, 'casual_wear');
  assert.equal(optionsB[3].value, 'dlc_gothic');
  assert.equal(optionsB[3].textContent, '（未安装）dlc_gothic');
  assert.equal(selectB.value, 'dlc_gothic');

  // Saving preserves the uninstalled ID
  const saveBtn = h.app.configPanel.element.querySelector('.adv-config-btn-save');
  saveBtn.click();
  assert.equal(h.app.configPanel.isOpen, false);
  assert.equal(h.active.settings.advOutfit, 'dlc_gothic');

  // Case C: Selecting default option '' clears stored ID
  h.app.action('settings');
  const selectC = h.app.configPanel.controls.advOutfit;
  selectC.value = '';
  selectC.dispatchEvent({ type: 'change' });
  const saveBtnC = h.app.configPanel.element.querySelector('.adv-config-btn-save');
  saveBtnC.click();
  assert.equal(h.active.settings.advOutfit, '');
});

test('focus confinement: player in input mode, quick-menu settings lands focus on first control, outside elements cannot steal focus', t => {
  const h = setup(t, { voice: false, settings: { advTextIntervalMs: 0 } });
  // Player is in input mode with textarea focused
  assert.equal(h.app.player.state, 'input');
  assert.equal(h.doc.activeElement, h.app.textbox.inputElement);

  const button = h.app.stage.stage.querySelector('.adv-qm-btn-config');
  button.click();

  assert.equal(h.app.configPanel.isOpen, true);
  // Focus lands on the first control in the panel (advWindowOpacity)
  assert.equal(h.doc.activeElement, h.app.configPanel.controls.advWindowOpacity);
  assert.equal(h.app.configPanel.element.contains(h.doc.activeElement), true);

  // Outside elements cannot take focus while panel is open
  h.app.textbox.inputElement.focus();
  assert.notEqual(h.doc.activeElement, h.app.textbox.inputElement);
  assert.equal(h.app.configPanel.element.contains(h.doc.activeElement), true);

  h.app.textbox.enterInput();
  assert.notEqual(h.doc.activeElement, h.app.textbox.inputElement);
  assert.equal(h.app.configPanel.element.contains(h.doc.activeElement), true);

  h.app.update();
  assert.notEqual(h.doc.activeElement, h.app.textbox.inputElement);
  assert.equal(h.app.configPanel.element.contains(h.doc.activeElement), true);

  // Closing panel restores focus to the input element
  h.app.configPanel.close({ revert: true });
  assert.equal(h.app.configPanel.isOpen, false);
  assert.equal(h.doc.activeElement, h.app.textbox.inputElement);
});

test('clicking model settings or session settings closes panel so focus is not stolen back', t => {
  const h = setup(t, { voice: false });
  h.app.stage.stage.querySelector('.adv-qm-btn-config').click();
  assert.equal(h.app.configPanel.isOpen, true);

  const modelBtn = h.app.configPanel.element.querySelector('#cfg-btn-models');
  assert.ok(modelBtn, 'Model settings button must exist');
  modelBtn.click();
  assert.equal(h.app.configPanel.isOpen, false, 'Panel must close when model settings clicked');
  assert.equal(h.calls.models, 1);

  h.app.stage.stage.querySelector('.adv-qm-btn-config').click();
  assert.equal(h.app.configPanel.isOpen, true);

  const sessionBtn = h.app.configPanel.element.querySelector('#cfg-btn-session');
  assert.ok(sessionBtn, 'Session settings button must exist');
  sessionBtn.click();
  assert.equal(h.app.configPanel.isOpen, false, 'Panel must close when session settings clicked');
  assert.equal(h.calls.settings, 1);
});

test('focus lands in the panel when the element chain reaches a document node, as in a browser', t => {
  const h = setup(t, { voice: false, settings: { advTextIntervalMs: 0 } });
  // A real body's parent is the Document, which has no getAttribute.
  const body = h.doc.body, parent = body.parentNode;
  body.parentNode = { nodeType: 9, dispatchEvent() {} };
  t.after(() => { body.parentNode = parent; });
  h.app.stage.stage.querySelector('.adv-qm-btn-config').click();
  assert.equal(h.app.configPanel.isOpen, true);
  assert.equal(h.app.configPanel.element.contains(h.doc.activeElement), true);
});
