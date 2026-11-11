import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { setup } from './helpers/adv_main.mjs';
import { initGlobalSettings, openGlobalSettings, loadGlobalConfig, saveGlobalConfig } from '../frontend/src/controllers/global_settings.js';
import { createSettingsCenter } from '../frontend/src/controllers/settings_center.js';
import { fakeDocument } from './helpers/adv_dom.mjs';
import * as settingsUi from '../frontend/src/settings_ui.js';
import { createTitleScreen } from '../frontend/src/adv/title.js';
import { normalizeSnapshot } from '../frontend/src/session_data.js';
import { DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL } from '../frontend/src/store.js';
import { mergeChatBackup } from '../frontend/src/chat_backup.js';
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

  // 1. Traverse switching 7 tabs:
  const tabs = ['dashboard', 'models', 'inference', 'memory', 'update', 'experience', 'session'];
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

function mountRealIndexDom() {
  const html = fs.readFileSync(new URL('../frontend/index.html', import.meta.url), 'utf8');
  const bodyMatch = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
  assert.ok(bodyMatch, 'Expected <body> in frontend/index.html');
  const cleaned = bodyMatch[1]
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<svg[\s\S]*?<\/svg>/gi, '')
    .replace(/<script[\s\S]*?<\/script>/gi, '');

  const doc = fakeDocument();
  const voidTags = new Set([
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
  ]);
  const stack = [doc.body];
  const tagRe = /<(\/?)([a-zA-Z0-9-]+)((?:"[^"]*"|'[^']*'|[^>"'])*)>/g;
  const attrRe = /([a-zA-Z0-9_:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;

  for (const match of cleaned.matchAll(tagRe)) {
    const [, closing, rawTag, rawAttrs] = match;
    const tag = rawTag.toLowerCase();
    if (closing) {
      while (stack.length > 1) {
        const popped = stack.pop();
        if (popped.tagName.toLowerCase() === tag) {
          if (tag === 'select' && !popped.value) {
            const firstOpt = popped.children.find(c => c.tagName === 'OPTION');
            if (firstOpt) popped.value = firstOpt.getAttribute('value') ?? '';
          }
          break;
        }
      }
      continue;
    }

    const selfClosing = /\/\s*$/.test(rawAttrs) || voidTags.has(tag);
    const attrSource = rawAttrs.replace(/\/\s*$/, '');
    const el = doc.createElement(tag);
    el.scrollIntoView = () => {};
    el.getClientRects = () => [{}];

    for (const attrMatch of attrSource.matchAll(attrRe)) {
      const name = attrMatch[1];
      const val = attrMatch[2] ?? attrMatch[3] ?? attrMatch[4];
      if (name === 'id') {
        el.id = val ?? '';
        el.setAttribute('id', el.id);
      } else if (name === 'class') {
        el.className = val ?? '';
        el.setAttribute('class', val ?? '');
      } else if (name === 'value') {
        el.value = val ?? '';
        el.setAttribute('value', el.value);
      } else if (name === 'type') {
        el.type = val ?? '';
        el.setAttribute('type', el.type);
      } else if (name === 'checked') {
        el.checked = true;
        el.setAttribute('checked', val ?? '');
      } else if (name === 'hidden') {
        el.hidden = true;
        el.setAttribute('hidden', val ?? '');
      } else if (name === 'disabled') {
        el.disabled = true;
        el.setAttribute('disabled', val ?? '');
      } else if (name.startsWith('data-')) {
        const camel = name.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
        el.dataset[camel] = val ?? '';
        el.setAttribute(name, val ?? '');
      } else {
        el.setAttribute(name, val ?? '');
      }
    }

    stack.at(-1).appendChild(el);
    if (!selfClosing) stack.push(el);
  }

  const mainJs = fs.readFileSync(new URL('../frontend/src/main.js', import.meta.url), 'utf8');
  const domBlock = mainJs.match(/const dom = injectedDom \|\| \{([\s\S]*?)\n  \};/);
  assert.ok(domBlock, 'Expected dom mapping block in frontend/src/main.js');
  const dom = {};
  for (const [, prop, targetId] of domBlock[1].matchAll(/^\s*(\w+):\s*\$\(['"]([^'"]+)['"]\)/gm)) {
    dom[prop] = doc.getElementById(targetId);
  }
  return { doc, dom, html, mainJs };
}

test('global settings mounted from real index.html saves exact POST /api/config payload without ghost sampling fields', async t => {
  const origDocument = globalThis.document;
  const origFetch = globalThis.fetch;
  const origLocalStorage = globalThis.localStorage;
  const { doc, dom, html } = mountRealIndexDom();
  for (const removedId of [
    'gPreset', 'gParamSliceMethod', 'gParamFragmentInterval',
    'gParamSpeed', 'gParamSpeedVal', 'gParamTopK', 'gParamTopKVal',
    'gParamTopP', 'gParamTopPVal', 'gParamTemp', 'gParamTempVal',
    'gCustomBox', 'gCustomBaseUrl', 'gCustomApiKey', 'gCustomModel',
  ]) {
    assert.equal(html.includes(`id="${removedId}"`), false, `${removedId} must not exist in frontend/index.html`);
    assert.equal(Object.hasOwn(dom, removedId), false, `${removedId} must not be queried in frontend/src/main.js`);
  }
  globalThis.document = doc;
  globalThis.localStorage = { getItem: () => null, setItem: () => {} };

  const toastRoot = doc.getElementById('toastRoot');

  let savedRawBody = null;
  let savedPayload = null;
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/config' && options.method === 'POST') {
      savedRawBody = options.body;
      savedPayload = JSON.parse(options.body);
      return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
    }
    if (url === '/api/config') {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          status: 'ok',
          settings: {
            gpt_sovits_url: 'http://127.0.0.1:9880',
            inference_precision: 'fp16',
            audio_retention_minutes: 45,
            speed_factor: 1.25,
            top_k: 20,
            top_p: 0.85,
            temperature: 0.9,
            text_split_method: 'cut5',
            fragment_interval: 0.3,
            allow_private_llm_endpoints: false,
            telegram_enabled: false,
            telegram_chat_id: '987654321',
            telegram_admin_ids: '987654321',
            telegram_bot_token: '12****EF',
            telegram_proxy_enabled: true,
            telegram_proxy_host: '127.0.0.1',
            telegram_proxy_port: 10809,
          },
          active_provider: null,
        }),
      };
    }
    if (url === '/api/providers') {
      return { ok: true, status: 200, json: async () => ({ providers: [], presets: [] }) };
    }
    if (String(url).includes('/api/system/engine-directory')) {
      return { ok: true, status: 200, json: async () => ({ directory: '', configured: false }) };
    }
    if (String(url).includes('/api/system/engine-status')) {
      return { ok: true, status: 200, json: async () => ({ state: 'ready', ready: true, message: '已就绪' }) };
    }
    if (String(url).includes('/api/system/status')) {
      return { ok: true, status: 200, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };

  t.after(async () => {
    for (const toast of [...toastRoot.children]) clearTimeout(toast._timer);
    settingsUi.closeAllModals();
    closeModal(dom.globalModal);
    initGlobalSettings({});
    if (origDocument !== undefined) globalThis.document = origDocument; else delete globalThis.document;
    if (origFetch !== undefined) globalThis.fetch = origFetch; else delete globalThis.fetch;
    if (origLocalStorage !== undefined) globalThis.localStorage = origLocalStorage; else delete globalThis.localStorage;
    await new Promise(resolve => setImmediate(resolve));
  });

  initGlobalSettings(dom, { openModal, closeModal });
  openGlobalSettings('inference');
  await loadGlobalConfig();
  await saveGlobalConfig();

  assert.ok(savedPayload !== null, 'Expected POST /api/config payload');
  for (const ghostKey of ['text_split_method', 'speed_factor', 'top_k', 'top_p', 'temperature', 'fragment_interval', 'telegram_enabled', 'telegram_chat_id', 'telegram_admin_ids', 'telegram_bot_token', 'telegram_proxy_enabled', 'telegram_proxy_host', 'telegram_proxy_port']) {
    assert.equal(Object.hasOwn(savedPayload, ghostKey), false, `POST /api/config must not include ${ghostKey}`);
  }
  const expectedPayload = {
    gpt_sovits_url: 'http://127.0.0.1:9880',
    inference_precision: 'fp16',
    audio_retention_minutes: 45,
  };
  assert.deepEqual(Object.keys(savedPayload), Object.keys(expectedPayload));
  assert.deepEqual(savedPayload, expectedPayload);
  assert.equal(savedRawBody, JSON.stringify(expectedPayload));
});

test('telegram settings tab is removed from index.html while preserving gTgSave and legacy backup compatibility', () => {
  const indexHtml = fs.readFileSync(new URL('../frontend/index.html', import.meta.url), 'utf8');
  assert.equal(indexHtml.includes('data-gtab="telegram"'), false, 'Telegram nav tab should not exist in frontend/index.html');
  assert.equal(indexHtml.includes('id="gp-telegram"'), false, 'Telegram panel should not exist in frontend/index.html');
  for (const removedId of ['gTgEnabled', 'gTgFieldsGroup', 'gTgToken', 'gTgChatId', 'gTgProxyHost', 'gTgProxyPort', 'gTgProxyEnabled', 'gTgTest']) {
    assert.equal(indexHtml.includes(`id="${removedId}"`), false, `${removedId} should not exist in frontend/index.html`);
  }
  assert.equal(indexHtml.includes('id="gTgSave"'), true, 'Shared global save button #gTgSave must remain in frontend/index.html');

  const legacyBackup = {
    format: 'galgame2voice.chat',
    version: 1,
    activeId: 'tg_99',
    global: {
      voiceMode: false,
      telegram_enabled: true,
      telegram_bot_token: 'secret',
    },
    sessions: [
      {
        id: 'tg_99',
        channel: 'telegram',
        title: '旧Telegram对话',
        createdAt: 1700000000000,
        updatedAt: 1700000005000,
        settings: {
          temperature: 0.9,
          telegram_chat_id: '123456',
          telegram_admin_ids: '123456',
        },
        messages: [
          { id: 'm_1', role: 'user', content: '你好', ts: 1700000001000 },
          { id: 'm_2', role: 'assistant', content: '你好呀', japanese: 'こんにちは', ts: 1700000002000 },
        ],
      },
    ],
  };

  const parsed = normalizeSnapshot(legacyBackup, DEFAULT_SESSION_SETTINGS, DEFAULT_GLOBAL);
  assert.equal(parsed.activeId, 'tg_99');
  assert.equal(parsed.global.voiceMode, false);
  assert.equal('telegram_enabled' in parsed.global, false);
  assert.equal('telegram_bot_token' in parsed.global, false);
  assert.equal(parsed.sessions.length, 1);
  assert.equal(parsed.sessions[0].id, 'tg_99');
  assert.equal(parsed.sessions[0].title, '旧Telegram对话');
  assert.equal(parsed.sessions[0].settings.temperature, 0.9);
  assert.equal('telegram_chat_id' in parsed.sessions[0].settings, false);
  assert.equal('telegram_admin_ids' in parsed.sessions[0].settings, false);

  const merged = mergeChatBackup(legacyBackup, { sessions: [], activeId: null, global: {} });
  assert.equal(merged.count, 1);
  assert.equal(JSON.parse(merged.content).sessions[0].id, 'tg_99');
});
