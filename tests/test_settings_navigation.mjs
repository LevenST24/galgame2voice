import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { setup } from './helpers/adv_main.mjs';
import { initGlobalSettings, loadGlobalConfig, saveGlobalConfig } from '../frontend/src/controllers/global_settings.js';
import { createSettingsCenter } from '../frontend/src/controllers/settings_center.js';
import { fakeDocument } from './helpers/adv_dom.mjs';
import * as settingsUi from '../frontend/src/settings_ui.js';
import { createTitleScreen } from '../frontend/src/adv/title.js';
const { openModal, closeModal, hasOpenModal } = settingsUi;

function centerFixture(t) {
  let home;
  const h = setup(t, { callbacks: { showTitle: actions => {
    home?.destroy();
    home = createTitleScreen(document.querySelector('.adv-layer-overlay'), { actions });
    home.open();
    return home;
  } } });
  const doc = document;
  const modal = doc.createElement('div'); modal.className = 'hidden'; modal.hidden = true;
  const reading = doc.createElement('button'); reading.id = 'gOpenChatExperience';
  modal.appendChild(reading); doc.body.appendChild(modal);
  initGlobalSettings({ globalModal: modal }, { openModal, closeModal,
    onOpenReadingSettings: () => h.app.adv.action('reading_settings') });
  t.after(async () => {
    globalThis.document = doc;
    settingsUi.closeAllModals?.(); closeModal(modal); initGlobalSettings({}); home?.destroy();
    await new Promise(resolve => setImmediate(resolve));
    delete globalThis.document;
  });
  return { ...h, doc, modal, reading };
}

test('the chat settings button opens one center; reading settings can return to that center', async t => {
  const h = centerFixture(t);
  h.app.adv.stage.stage.querySelector('.adv-qm-btn-config').click();
  assert.equal(h.modal.hidden, false);
  assert.equal(h.app.adv.configPanel.isOpen, false);
  h.reading.click();
  assert.equal(h.modal.hidden, true);
  assert.equal(h.app.adv.configPanel.isOpen, true);
  h.app.adv.configPanel.element.querySelector('#cfg-btn-center').click();
  assert.equal(h.app.adv.configPanel.isOpen, false);
  assert.equal(h.modal.hidden, false);
  await new Promise(resolve => setImmediate(resolve));
});

test('return home closes settings, restores the stage and preserves the conversation and draft', async t => {
  const h = centerFixture(t);
  const session = h.getActive();
  session.messages.push({ role: 'user', content: '这条聊天记录必须保留' });
  h.app.adv.textbox.setDraft('尚未发送的草稿');
  h.app.adv.configPanel.open();
  openModal(h.modal);
  h.app.returnHome();
  assert.equal(hasOpenModal(), false);
  assert.equal(h.app.adv.configPanel.isOpen, false);
  assert.equal(h.app.adv.stage.stage.inert, false);
  assert.equal(h.app.adv.stage.stage.classList.contains('is-home'), true);
  assert.equal(session.messages[0].content, '这条聊天记录必须保留');
  assert.equal(h.app.adv.textbox.draft, '尚未发送的草稿');
  await new Promise(resolve => setImmediate(resolve));
});

test('settings center: reset button is visible only on inference tab, and resets precision and audio retention', async t => {
  const origDocument = globalThis.document;
  const origFetch = globalThis.fetch;
  const origConfirm = globalThis.confirm;
  const doc = fakeDocument();
  globalThis.document = doc;

  const toastRoot = doc.createElement('div');
  toastRoot.id = 'toastRoot';
  doc.body.appendChild(toastRoot);

  const globalModal = doc.createElement('div');
  globalModal.id = 'globalModal';
  const modalInner = doc.createElement('div');
  modalInner.className = 'modal';
  const modalFoot = doc.createElement('div');
  modalFoot.className = 'modal-foot';
  const gResetBtn = doc.createElement('button');
  gResetBtn.id = 'gResetBtn';
  const gTgSave = doc.createElement('button');
  gTgSave.id = 'gTgSave';
  modalFoot.appendChild(gResetBtn);
  modalFoot.appendChild(gTgSave);
  modalInner.appendChild(modalFoot);
  globalModal.appendChild(modalInner);
  globalModal.querySelector = selector => {
    if (selector.includes('modal-foot')) return modalFoot;
    return doc.body.querySelector(selector);
  };

  const sessionModal = doc.createElement('div');
  sessionModal.id = 'sessionModal';
  const sessionInner = doc.createElement('div');
  sessionInner.className = 'modal';
  const sessionHead = doc.createElement('div');
  sessionHead.className = 'modal-head';
  const sessionFoot = doc.createElement('div');
  sessionFoot.className = 'modal-foot';
  sessionInner.appendChild(sessionHead);
  sessionInner.appendChild(sessionFoot);
  sessionModal.appendChild(sessionInner);

  const sessionHost = doc.createElement('div');
  sessionHost.id = 'gp-session';
  doc.body.appendChild(sessionHost);
  doc.body.appendChild(globalModal);
  doc.body.appendChild(sessionModal);

  const sReset = doc.createElement('button');
  const sSave = doc.createElement('button');
  const gPrecision = doc.createElement('select');
  gPrecision.id = 'gPrecision';
  gPrecision.value = 'fp16';
  const gAudioRetention = doc.createElement('input');
  gAudioRetention.id = 'gAudioRetention';
  gAudioRetention.value = '60';

  const dom = {
    globalModal,
    sessionModal,
    sReset,
    sSave,
    gResetBtn,
    gTgSave,
    gPrecision,
    gAudioRetention,
  };

  t.after(async () => {
    for (const toast of [...toastRoot.children]) clearTimeout(toast._timer);
    initGlobalSettings({});
    if (origDocument !== undefined) globalThis.document = origDocument; else delete globalThis.document;
    if (origFetch !== undefined) globalThis.fetch = origFetch; else delete globalThis.fetch;
    if (origConfirm !== undefined) globalThis.confirm = origConfirm; else delete globalThis.confirm;
    await new Promise(resolve => setImmediate(resolve));
  });

  const center = createSettingsCenter({
    dom,
    openCenter: () => {},
    getExperience: () => null,
    getSession: () => ({ id: 's1' }),
    loadSession: () => {},
    cancelSession: () => {},
  });
  assert.ok(center, 'Settings center instantiated');

  // Initialization: gResetBtn should be hidden (dashboard is default tab)
  assert.equal(dom.gResetBtn.hidden, true, 'gResetBtn should be hidden on initialization');

  // 1. Traverse switching 8 tabs:
  const tabs = ['dashboard', 'models', 'inference', 'telegram', 'memory', 'update', 'experience', 'session'];
  for (const tab of tabs) {
    center.select(tab);
    assert.equal(
      dom.gResetBtn.hidden,
      tab !== 'inference',
      `gResetBtn.hidden for tab "${tab}" should be ${tab !== 'inference'}`
    );
    assert.equal(
      dom.gTgSave.hidden,
      ['experience', 'session'].includes(tab),
      `gTgSave.hidden for tab "${tab}" should be ${['experience', 'session'].includes(tab)}`
    );
  }

  // 2. Click gResetBtn on inference tab, confirm reset, and save:
  center.select('inference');
  assert.equal(dom.gResetBtn.hidden, false);

  let savedPayload = null;
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/config' && options.method === 'POST') {
      savedPayload = JSON.parse(options.body);
      return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
    }
    if (url === '/api/config') {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          status: 'ok',
          settings: { inference_precision: 'fp16', audio_retention_minutes: 60 },
        }),
      };
    }
    if (url === '/api/providers') return { ok: true, status: 200, json: async () => ({ providers: [], presets: [] }) };
    return { ok: true, status: 200, json: async () => ({}) };
  };
  globalThis.confirm = () => true;

  // Track if ghost fields are accessed by reset button click
  let touchedGhost = false;
  for (const ghost of ['gParamSpeed', 'gParamTopK', 'gParamTopP', 'gParamTemp', 'gParamSliceMethod']) {
    Object.defineProperty(dom, ghost, {
      get() { touchedGhost = true; return { value: '' }; },
      set() { touchedGhost = true; },
      configurable: true,
    });
  }

  initGlobalSettings(dom);
  await loadGlobalConfig();
  assert.equal(String(dom.gPrecision.value), 'fp16');
  assert.equal(String(dom.gAudioRetention.value), '60');

  touchedGhost = false;
  dom.gResetBtn.click();
  assert.equal(String(dom.gPrecision.value), 'auto');
  assert.equal(String(dom.gAudioRetention.value), '30');
  assert.equal(touchedGhost, false, 'Reset click should not touch removed ghost fields');

  await loadGlobalConfig();
  assert.equal(String(dom.gPrecision.value), 'auto');
  assert.equal(String(dom.gAudioRetention.value), '30');

  // Assert removed ghost controls are not in index.html, while active fields are
  const indexHtml = fs.readFileSync(new URL('../frontend/index.html', import.meta.url), 'utf8');
  for (const ghostId of ['gParamSpeed', 'gParamTopK', 'gParamTopP', 'gParamTemp', 'gParamSliceMethod']) {
    assert.equal(indexHtml.includes(`id="${ghostId}"`), false, `${ghostId} should not exist in frontend/index.html`);
  }
  assert.ok(indexHtml.includes('id="gPrecision"'), 'gPrecision must exist in frontend/index.html');
  assert.ok(indexHtml.includes('id="gAudioRetention"'), 'gAudioRetention must exist in frontend/index.html');

  await saveGlobalConfig();
  assert.ok(savedPayload !== null, 'Config POST payload should have been captured');
  assert.equal(savedPayload.inference_precision, 'auto');
  assert.equal(savedPayload.audio_retention_minutes, 30);
});
