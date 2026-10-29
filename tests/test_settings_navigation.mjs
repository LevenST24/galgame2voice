import assert from 'node:assert/strict';
import test from 'node:test';
import { setup } from './helpers/adv_main.mjs';
import { initGlobalSettings } from '../frontend/src/controllers/global_settings.js';
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
