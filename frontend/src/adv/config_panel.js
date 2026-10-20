/**
 * config_panel.js - ADV settings overlay component.
 *
 * Implements the full-stage translucent settings panel inspired by 天使☆騒々 RE-BOOT!.
 * Features:
 * - 10 ADV session fields reusing rules and definitions from adv/settings.js
 * - Live preview on stage (opacity, outfit, languages) and revert on Cancel/Esc
 * - In-panel live text speed sample line with typewriter and reduced-motion support
 * - Global voice-mode toggle
 * - Navigation buttons to existing model settings and session settings modals
 * - Focus preservation, focus trapping, and keyboard navigation
 */

import { ADV_FIELDS, ADV_LABELS, validateAdvField, validateAdvLanguages } from './settings.js';
import { pickSettings, validAdvSetting } from '../session_data.js';
import { DEFAULT_SESSION_SETTINGS } from '../store.js';
import { splitGraphemes } from './graphemes.js';

export const CONFIG_PANEL_FIELDS = [
  'advPlayerName',
  'advPrimaryLanguage',
  'advSubtitleLanguage',
  'advWindowOpacity',
  'advTextIntervalMs',
  'advAutoBaseMs',
  'advAutoPerCharMs',
  'advAutoMode',
  'advInterruptVoice',
  'advOutfit',
];

const SAMPLE_TEXT = '这是一段用于测试文字显示速度的示例文本。';

function isElementHidden(el) {
  let cur = el;
  while (cur) {
    if (cur.hidden || (cur.getAttribute?.('hidden') ?? null) !== null || cur.style?.display === 'none') {
      return true;
    }
    cur = cur.parentNode;
  }
  return false;
}

/**
 * Creates the ADV config panel overlay inside container.
 *
 * @param {HTMLElement} container Parent layer element (typically stage.overlay)
 * @param {object} [options]
 * @returns {object} { open, close, load, read, isOpen, element, destroy }
 */
export function createConfigPanel(container, {
  getSession,
  getCharacter,
  getCostumes,
  saveSettings,
  voiceEnabled,
  toggleVoice,
  onModelSettings,
  onSessionSettings,
  onPreview,
  onRevert,
  onOpen,
  onClose,
  clock = {
    now: () => (typeof performance !== 'undefined' ? performance.now() : Date.now()),
    setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
    clearTimeout: (id) => globalThis.clearTimeout(id),
  },
  matchMedia = (query => globalThis.matchMedia?.(query)),
  documentObj,
} = {}) {
  const doc = documentObj || container?.ownerDocument || globalThis.document;
  const disposers = [];
  function listen(el, type, fn, opts) {
    if (!el?.addEventListener) return;
    el.addEventListener(type, fn, opts);
    disposers.push(() => el.removeEventListener(type, fn, opts));
  }

  let isOpen = false;
  let savedFocus = null;
  let baseline = { ...DEFAULT_SESSION_SETTINGS };
  let baselineVoiceMode = true;
  let draftVoiceMode = true;
  let sampleTimer = null;

  // Root overlay
  const overlay = doc.createElement('div');
  overlay.className = 'adv-config-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-label', 'ADV 系统设置');
  overlay.hidden = true;
  overlay.setAttribute('hidden', '');

  // Backdrop
  const backdrop = doc.createElement('div');
  backdrop.className = 'adv-config-backdrop';
  overlay.appendChild(backdrop);

  // Dialog card
  const dialog = doc.createElement('div');
  dialog.className = 'adv-config-dialog';
  overlay.appendChild(dialog);

  // Stop clicks inside dialog from bubbling to backdrop
  listen(dialog, 'click', (e) => e.stopPropagation());
  listen(backdrop, 'click', () => close({ revert: true }));

  // Header
  const header = doc.createElement('header');
  header.className = 'adv-config-header';

  const titleGroup = doc.createElement('div');
  titleGroup.className = 'adv-config-title-group';

  const title = doc.createElement('h2');
  title.className = 'adv-config-title';
  title.textContent = 'CONFIG';

  const subtitle = doc.createElement('span');
  subtitle.className = 'adv-config-subtitle';
  subtitle.textContent = '系统环境设定';

  titleGroup.appendChild(title);
  titleGroup.appendChild(subtitle);

  const btnClose = doc.createElement('button');
  btnClose.type = 'button';
  btnClose.className = 'adv-config-btn-close';
  btnClose.setAttribute('aria-label', '关闭设置');
  btnClose.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
  listen(btnClose, 'click', () => close({ revert: true }));

  header.appendChild(titleGroup);
  header.appendChild(btnClose);
  dialog.appendChild(header);

  // Body
  const body = doc.createElement('div');
  body.className = 'adv-config-body';
  dialog.appendChild(body);

  const controls = {};
  const valEls = {};
  const errorEls = {};
  const rowEls = {};

  function makeRow(key, labelText, hintText, controlEl, { valSuffix = '', hasError = true } = {}) {
    const row = doc.createElement('div');
    row.className = 'adv-config-row';
    row.dataset.field = key;
    rowEls[key] = row;

    const rowMain = doc.createElement('div');
    rowMain.className = 'adv-config-row-main';

    const label = doc.createElement('label');
    label.className = 'adv-config-label';
    if (controlEl.id) label.htmlFor = controlEl.id;

    const labelSpan = doc.createElement('span');
    labelSpan.textContent = labelText;
    label.appendChild(labelSpan);

    if (hintText) {
      const hintSpan = doc.createElement('span');
      hintSpan.className = 'adv-config-hint';
      hintSpan.textContent = hintText;
      label.appendChild(hintSpan);
    }

    rowMain.appendChild(label);

    const controlWrap = doc.createElement('div');
    controlWrap.className = 'adv-config-control';
    controlWrap.appendChild(controlEl);

    if (valSuffix !== undefined) {
      const valSpan = doc.createElement('span');
      valSpan.className = 'adv-config-val';
      valSpan.id = `cfg-${key}-val`;
      valEls[key] = valSpan;
      controlWrap.appendChild(valSpan);
    }

    rowMain.appendChild(controlWrap);
    row.appendChild(rowMain);

    if (hasError) {
      const errEl = doc.createElement('div');
      errEl.className = 'adv-config-field-error';
      errEl.hidden = true;
      errEl.setAttribute('hidden', '');
      errorEls[key] = errEl;
      row.appendChild(errEl);
    }

    return row;
  }

  // --- Group 1: 画面与视效 ---
  const groupDisplay = doc.createElement('section');
  groupDisplay.className = 'adv-config-group';

  const groupDisplayTitle = doc.createElement('h3');
  groupDisplayTitle.className = 'adv-config-group-title';
  groupDisplayTitle.textContent = '画面与视效';
  groupDisplay.appendChild(groupDisplayTitle);

  // advWindowOpacity
  const opacityInput = doc.createElement('input');
  opacityInput.type = 'range';
  opacityInput.id = 'cfg-advWindowOpacity';
  opacityInput.className = 'adv-config-slider';
  opacityInput.min = '0';
  opacityInput.max = '100';
  opacityInput.step = '1';
  controls.advWindowOpacity = opacityInput;

  const rowOpacity = makeRow('advWindowOpacity', '窗口不透明度', '对话框底色透明度 (0–100%)', opacityInput, { valSuffix: '%' });
  groupDisplay.appendChild(rowOpacity);

  // advOutfit
  const outfitSelect = doc.createElement('select');
  outfitSelect.id = 'cfg-advOutfit';
  outfitSelect.className = 'adv-config-select';
  controls.advOutfit = outfitSelect;

  const rowOutfit = makeRow('advOutfit', '服装', '选择角色服装', outfitSelect, { valSuffix: undefined });
  groupDisplay.appendChild(rowOutfit);

  body.appendChild(groupDisplay);

  // --- Group 2: 文字与语言 ---
  const groupText = doc.createElement('section');
  groupText.className = 'adv-config-group';

  const groupTextTitle = doc.createElement('h3');
  groupTextTitle.className = 'adv-config-group-title';
  groupTextTitle.textContent = '文字与语言';
  groupText.appendChild(groupTextTitle);

  // advPlayerName
  const playerNameInput = doc.createElement('input');
  playerNameInput.type = 'text';
  playerNameInput.id = 'cfg-advPlayerName';
  playerNameInput.className = 'adv-config-input';
  playerNameInput.maxLength = 40;
  controls.advPlayerName = playerNameInput;

  const rowPlayerName = makeRow('advPlayerName', '玩家名字', '对话框显示的玩家名称（最多40字）', playerNameInput, { valSuffix: undefined });
  groupText.appendChild(rowPlayerName);

  // advPrimaryLanguage & advSubtitleLanguage
  const langWrap = doc.createElement('div');
  langWrap.className = 'adv-config-control-dual';

  const priWrap = doc.createElement('div');
  priWrap.className = 'adv-config-select-wrap';
  const priSublabel = doc.createElement('span');
  priSublabel.className = 'adv-config-sublabel';
  priSublabel.textContent = '主要：';
  const priSelect = doc.createElement('select');
  priSelect.id = 'cfg-advPrimaryLanguage';
  priSelect.className = 'adv-config-select';
  for (const [v, text] of [['zh', '中文'], ['ja', '日文']]) {
    const opt = doc.createElement('option');
    opt.value = v; opt.textContent = text;
    priSelect.appendChild(opt);
  }
  controls.advPrimaryLanguage = priSelect;
  priWrap.appendChild(priSublabel);
  priWrap.appendChild(priSelect);

  const subWrap = doc.createElement('div');
  subWrap.className = 'adv-config-select-wrap';
  const subSublabel = doc.createElement('span');
  subSublabel.className = 'adv-config-sublabel';
  subSublabel.textContent = '字幕：';
  const subSelect = doc.createElement('select');
  subSelect.id = 'cfg-advSubtitleLanguage';
  subSelect.className = 'adv-config-select';
  for (const [v, text] of [['ja', '日文'], ['zh', '中文'], ['none', '无']]) {
    const opt = doc.createElement('option');
    opt.value = v; opt.textContent = text;
    subSelect.appendChild(opt);
  }
  controls.advSubtitleLanguage = subSelect;
  subWrap.appendChild(subSublabel);
  subWrap.appendChild(subSelect);

  langWrap.appendChild(priWrap);
  langWrap.appendChild(subWrap);

  const rowLanguages = makeRow('advLanguages', '双语字幕设定', '主要语言与副字幕语言', langWrap, { valSuffix: undefined });
  groupText.appendChild(rowLanguages);

  // advTextIntervalMs (Text Speed) with live sample box
  const textSpeedInput = doc.createElement('input');
  textSpeedInput.type = 'range';
  textSpeedInput.id = 'cfg-advTextIntervalMs';
  textSpeedInput.className = 'adv-config-slider';
  textSpeedInput.min = '0';
  textSpeedInput.max = '100';
  textSpeedInput.step = '1';
  controls.advTextIntervalMs = textSpeedInput;

  const rowTextSpeed = makeRow('advTextIntervalMs', '文字显现速度', '逐字间隔毫秒（0为瞬间显示）', textSpeedInput, { valSuffix: ' ms' });

  // Sample box inside text speed row
  const sampleBox = doc.createElement('div');
  sampleBox.className = 'adv-config-sample-box';
  sampleBox.setAttribute('aria-live', 'off');
  sampleBox.title = '点击重新播放文字速度预览';

  const sampleLabel = doc.createElement('span');
  sampleLabel.className = 'adv-config-sample-label';
  sampleLabel.textContent = '速度预览：';

  const sampleText = doc.createElement('span');
  sampleText.className = 'adv-config-sample-text';
  sampleText.textContent = SAMPLE_TEXT;

  sampleBox.appendChild(sampleLabel);
  sampleBox.appendChild(sampleText);
  rowTextSpeed.appendChild(sampleBox);

  listen(sampleBox, 'click', () => {
    startSampleTypewriter(Number(controls.advTextIntervalMs.value));
  });

  groupText.appendChild(rowTextSpeed);
  body.appendChild(groupText);

  // --- Group 3: 自动播放与语音 ---
  const groupPlay = doc.createElement('section');
  groupPlay.className = 'adv-config-group';

  const groupPlayTitle = doc.createElement('h3');
  groupPlayTitle.className = 'adv-config-group-title';
  groupPlayTitle.textContent = '自动播放与语音';
  groupPlay.appendChild(groupPlayTitle);

  // advAutoMode
  const autoModeSelect = doc.createElement('select');
  autoModeSelect.id = 'cfg-advAutoMode';
  autoModeSelect.className = 'adv-config-select';
  for (const [v, text] of [['voice-priority', '语音优先'], ['normal', '通常']]) {
    const opt = doc.createElement('option');
    opt.value = v; opt.textContent = text;
    autoModeSelect.appendChild(opt);
  }
  controls.advAutoMode = autoModeSelect;
  const rowAutoMode = makeRow('advAutoMode', 'AUTO 模式', '语音优先等待配音播放完成；通常仅依据字数', autoModeSelect, { valSuffix: undefined });
  groupPlay.appendChild(rowAutoMode);

  // advAutoBaseMs
  const autoBaseInput = doc.createElement('input');
  autoBaseInput.type = 'range';
  autoBaseInput.id = 'cfg-advAutoBaseMs';
  autoBaseInput.className = 'adv-config-slider';
  autoBaseInput.min = '0';
  autoBaseInput.max = '6000';
  autoBaseInput.step = '50';
  controls.advAutoBaseMs = autoBaseInput;
  const rowAutoBase = makeRow('advAutoBaseMs', 'AUTO 基础等待', '每页基础停留时间 (0–6000 ms)', autoBaseInput, { valSuffix: ' ms' });
  groupPlay.appendChild(rowAutoBase);

  // advAutoPerCharMs
  const autoPerCharInput = doc.createElement('input');
  autoPerCharInput.type = 'range';
  autoPerCharInput.id = 'cfg-advAutoPerCharMs';
  autoPerCharInput.className = 'adv-config-slider';
  autoPerCharInput.min = '0';
  autoPerCharInput.max = '200';
  autoPerCharInput.step = '5';
  controls.advAutoPerCharMs = autoPerCharInput;
  const rowAutoPerChar = makeRow('advAutoPerCharMs', 'AUTO 每字等待', '依主要语言字数累加 (0–200 ms)', autoPerCharInput, { valSuffix: ' ms' });
  groupPlay.appendChild(rowAutoPerChar);

  // advInterruptVoice
  const interruptSelect = doc.createElement('select');
  interruptSelect.id = 'cfg-advInterruptVoice';
  interruptSelect.className = 'adv-config-select';
  for (const [v, text] of [['true', '开启'], ['false', '关闭']]) {
    const opt = doc.createElement('option');
    opt.value = v; opt.textContent = text;
    interruptSelect.appendChild(opt);
  }
  controls.advInterruptVoice = interruptSelect;
  const rowInterrupt = makeRow('advInterruptVoice', '推进时打断语音', '推进下一句对话时立即停止上一句语音', interruptSelect, { valSuffix: undefined });
  groupPlay.appendChild(rowInterrupt);

  // Global voiceMode toggle
  const voiceModeBtn = doc.createElement('button');
  voiceModeBtn.type = 'button';
  voiceModeBtn.id = 'cfg-voiceMode';
  voiceModeBtn.className = 'adv-config-btn-toggle';
  voiceModeBtn.setAttribute('aria-pressed', 'true');

  const toggleDot = doc.createElement('span');
  toggleDot.className = 'adv-config-toggle-indicator';
  const toggleTxt = doc.createElement('span');
  toggleTxt.className = 'adv-config-toggle-text';
  toggleTxt.textContent = '开启';
  voiceModeBtn.appendChild(toggleDot);
  voiceModeBtn.appendChild(toggleTxt);

  listen(voiceModeBtn, 'click', () => {
    draftVoiceMode = !draftVoiceMode;
    voiceModeBtn.setAttribute('aria-pressed', String(draftVoiceMode));
    toggleTxt.textContent = draftVoiceMode ? '开启' : '关闭';
  });

  const rowVoiceMode = makeRow('voiceMode', '自动播放语音（全局）', '开启后角色回复自动播放合成配音', voiceModeBtn, { valSuffix: undefined, hasError: false });
  groupPlay.appendChild(rowVoiceMode);

  body.appendChild(groupPlay);

  // --- Group 4: 高级与外部设定 ---
  const groupNav = doc.createElement('section');
  groupNav.className = 'adv-config-group adv-config-nav-group';

  const groupNavTitle = doc.createElement('h3');
  groupNavTitle.className = 'adv-config-group-title';
  groupNavTitle.textContent = '更多设置';
  groupNav.appendChild(groupNavTitle);

  const navBtns = doc.createElement('div');
  navBtns.className = 'adv-config-nav-buttons';

  const btnModels = doc.createElement('button');
  btnModels.type = 'button';
  btnModels.id = 'cfg-btn-models';
  btnModels.className = 'adv-config-btn-link';
  btnModels.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z" fill="currentColor"/></svg><span>对话模型设置…</span>';
  listen(btnModels, 'click', () => {
    onModelSettings?.();
  });

  const btnSession = doc.createElement('button');
  btnSession.type = 'button';
  btnSession.id = 'cfg-btn-session';
  btnSession.className = 'adv-config-btn-link';
  btnSession.innerHTML = '<svg viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-5 14H7v-2h7v2zm3-4H7v-2h10v2zm0-4H7V7h10v2z" fill="currentColor"/></svg><span>角色音色与会话参数…</span>';
  listen(btnSession, 'click', () => {
    onSessionSettings?.();
  });

  navBtns.appendChild(btnModels);
  navBtns.appendChild(btnSession);
  groupNav.appendChild(navBtns);
  body.appendChild(groupNav);

  // Footer
  const footer = doc.createElement('footer');
  footer.className = 'adv-config-footer';

  const globalError = doc.createElement('div');
  globalError.className = 'adv-config-footer-status';
  globalError.id = 'cfg-global-error';
  globalError.hidden = true;
  globalError.setAttribute('hidden', '');
  footer.appendChild(globalError);

  const footerBtns = doc.createElement('div');
  footerBtns.className = 'adv-config-footer-buttons';

  const btnCancel = doc.createElement('button');
  btnCancel.type = 'button';
  btnCancel.className = 'adv-config-btn adv-config-btn-cancel';
  btnCancel.dataset.action = 'cancel';
  btnCancel.textContent = '取消 (Esc)';
  listen(btnCancel, 'click', () => close({ revert: true }));

  const btnSave = doc.createElement('button');
  btnSave.type = 'button';
  btnSave.className = 'adv-config-btn adv-config-btn-save';
  btnSave.dataset.action = 'save';
  btnSave.textContent = '保存设置';
  listen(btnSave, 'click', () => save());

  footerBtns.appendChild(btnCancel);
  footerBtns.appendChild(btnSave);
  footer.appendChild(footerBtns);
  dialog.appendChild(footer);

  // Live typewriter for text speed sample
  function startSampleTypewriter(speed) {
    if (sampleTimer !== null) {
      clock.clearTimeout(sampleTimer);
      sampleTimer = null;
    }
    const mql = matchMedia?.('(prefers-reduced-motion: reduce)');
    const isReduced = Boolean(mql?.matches);
    if (isReduced || speed === 0) {
      sampleText.textContent = SAMPLE_TEXT;
      return;
    }
    const clusters = splitGraphemes(SAMPLE_TEXT);
    let index = 0;
    sampleText.textContent = '';
    function step() {
      index++;
      sampleText.textContent = clusters.slice(0, index).join('');
      if (index < clusters.length) {
        sampleTimer = clock.setTimeout(step, speed);
      } else {
        sampleTimer = null;
      }
    }
    sampleTimer = clock.setTimeout(step, speed);
  }

  // Subscribe to reduced motion changes to update typewriter
  const mql = matchMedia?.('(prefers-reduced-motion: reduce)');
  const handleMotionChange = (e) => {
    if (e.matches) {
      if (sampleTimer !== null) {
        clock.clearTimeout(sampleTimer);
        sampleTimer = null;
      }
      sampleText.textContent = SAMPLE_TEXT;
    }
  };
  if (mql?.addEventListener) {
    mql.addEventListener('change', handleMotionChange);
    disposers.push(() => mql.removeEventListener('change', handleMotionChange));
  } else if (mql?.addListener) {
    mql.addListener(handleMotionChange);
    disposers.push(() => mql.removeListener(handleMotionChange));
  }

  // Live input handlers
  listen(controls.advWindowOpacity, 'input', () => {
    const v = controls.advWindowOpacity.value;
    valEls.advWindowOpacity.textContent = `${v}%`;
    clearFieldError('advWindowOpacity');
    const parsed = validateAdvField('advWindowOpacity', v, DEFAULT_SESSION_SETTINGS);
    if (parsed.valid) onPreview?.({ advWindowOpacity: parsed.value });
  });

  listen(controls.advTextIntervalMs, 'input', () => {
    const v = controls.advTextIntervalMs.value;
    valEls.advTextIntervalMs.textContent = `${v} ms`;
    clearFieldError('advTextIntervalMs');
    const parsed = validateAdvField('advTextIntervalMs', v, DEFAULT_SESSION_SETTINGS);
    startSampleTypewriter(Number(v));
    if (parsed.valid) onPreview?.({ advTextIntervalMs: parsed.value });
  });

  listen(controls.advAutoBaseMs, 'input', () => {
    const v = controls.advAutoBaseMs.value;
    valEls.advAutoBaseMs.textContent = `${v} ms`;
    clearFieldError('advAutoBaseMs');
  });

  listen(controls.advAutoPerCharMs, 'input', () => {
    const v = controls.advAutoPerCharMs.value;
    valEls.advAutoPerCharMs.textContent = `${v} ms`;
    clearFieldError('advAutoPerCharMs');
  });

  const handleOutfitChange = () => {
    const v = controls.advOutfit.value;
    clearFieldError('advOutfit');
    if (validAdvSetting('advOutfit', v)) {
      onPreview?.({ advOutfit: v });
    }
  };
  listen(controls.advOutfit, 'change', handleOutfitChange);
  listen(controls.advOutfit, 'input', handleOutfitChange);

  listen(controls.advPrimaryLanguage, 'change', () => {
    clearFieldError('advLanguages');
    if (controls.advPrimaryLanguage.value === controls.advSubtitleLanguage.value) {
      controls.advSubtitleLanguage.value = controls.advPrimaryLanguage.value === 'zh' ? 'ja' : 'zh';
    }
    onPreview?.({
      advPrimaryLanguage: controls.advPrimaryLanguage.value,
      advSubtitleLanguage: controls.advSubtitleLanguage.value,
    });
  });

  listen(controls.advSubtitleLanguage, 'change', () => {
    clearFieldError('advLanguages');
    onPreview?.({
      advPrimaryLanguage: controls.advPrimaryLanguage.value,
      advSubtitleLanguage: controls.advSubtitleLanguage.value,
    });
  });

  listen(controls.advPlayerName, 'input', () => {
    clearFieldError('advPlayerName');
  });

  // Keyboard navigation & Focus trapping
  listen(dialog, 'keydown', (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close({ revert: true });
      return;
    }
    if (e.key === 'Tab') {
      const focusable = getFocusableElements(dialog);
      if (!focusable.length) {
        e.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!dialog.contains(doc.activeElement) ||
          (e.shiftKey && doc.activeElement === first) ||
          (!e.shiftKey && doc.activeElement === last)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      }
    }
  });

  function getFocusableElements(root) {
    return [...root.querySelectorAll('button, input, select, textarea, [tabindex]')]
      .filter(el => !el.disabled && el.tabIndex >= 0 && !isElementHidden(el));
  }

  function clearFieldError(key) {
    const err = errorEls[key];
    if (err) {
      err.hidden = true;
      err.setAttribute('hidden', '');
      err.textContent = '';
    }
    rowEls[key]?.classList.remove('has-error');
    if (globalError) {
      globalError.hidden = true;
      globalError.setAttribute('hidden', '');
      globalError.textContent = '';
    }
  }

  function setFieldError(key, msg) {
    const err = errorEls[key];
    if (err) {
      err.hidden = false;
      err.removeAttribute('hidden');
      err.textContent = msg;
    }
    rowEls[key]?.classList.add('has-error');
  }

  function clearAllErrors() {
    for (const key of Object.keys(errorEls)) {
      clearFieldError(key);
    }
    if (globalError) {
      globalError.hidden = true;
      globalError.setAttribute('hidden', '');
      globalError.textContent = '';
    }
  }

  function load(settings) {
    const values = pickSettings(settings, DEFAULT_SESSION_SETTINGS);
    controls.advPlayerName.value = String(values.advPlayerName);
    controls.advPrimaryLanguage.value = String(values.advPrimaryLanguage);
    controls.advSubtitleLanguage.value = String(values.advSubtitleLanguage);
    controls.advWindowOpacity.value = String(values.advWindowOpacity);
    controls.advTextIntervalMs.value = String(values.advTextIntervalMs);
    controls.advAutoBaseMs.value = String(values.advAutoBaseMs);
    controls.advAutoPerCharMs.value = String(values.advAutoPerCharMs);
    controls.advAutoMode.value = String(values.advAutoMode);
    controls.advInterruptVoice.value = String(values.advInterruptVoice);
    // Populate advOutfit options from current costumes
    const costumes = typeof getCostumes === 'function' ? (getCostumes() || []) : [];
    controls.advOutfit.innerHTML = '';

    const optDefault = doc.createElement('option');
    optDefault.value = '';
    optDefault.textContent = '默认（角色包默认）';
    controls.advOutfit.appendChild(optDefault);

    const installedIds = new Set();
    for (const c of costumes) {
      if (!c || !c.id) continue;
      const id = String(c.id);
      installedIds.add(id);
      const opt = doc.createElement('option');
      opt.value = id;
      opt.textContent = String(c.name || c.id);
      controls.advOutfit.appendChild(opt);
    }

    const storedOutfit = String(values.advOutfit || '').trim();
    if (storedOutfit && !installedIds.has(storedOutfit)) {
      const optExtra = doc.createElement('option');
      optExtra.value = storedOutfit;
      optExtra.textContent = `（未安装）${storedOutfit}`;
      controls.advOutfit.appendChild(optExtra);
    }
    controls.advOutfit.value = storedOutfit;

    valEls.advWindowOpacity.textContent = `${values.advWindowOpacity}%`;
    valEls.advTextIntervalMs.textContent = `${values.advTextIntervalMs} ms`;
    valEls.advAutoBaseMs.textContent = `${values.advAutoBaseMs} ms`;
    valEls.advAutoPerCharMs.textContent = `${values.advAutoPerCharMs} ms`;

    voiceModeBtn.setAttribute('aria-pressed', String(draftVoiceMode));
    toggleTxt.textContent = draftVoiceMode ? '开启' : '关闭';

    startSampleTypewriter(values.advTextIntervalMs);
  }

  function read() {
    const result = {};
    for (const key of CONFIG_PANEL_FIELDS) {
      const raw = controls[key].value;
      const parsed = validateAdvField(key, raw, DEFAULT_SESSION_SETTINGS);
      if (!parsed.valid) {
        throw new Error(parsed.error);
      }
      result[key] = parsed.value;
    }
    const langErr = validateAdvLanguages(result.advPrimaryLanguage, result.advSubtitleLanguage);
    if (langErr) {
      throw new Error(langErr);
    }
    return result;
  }

  function getFirstControl() {
    const bodyControls = getFocusableElements(body);
    return bodyControls[0] || getFocusableElements(dialog)[0] || null;
  }

  function focusFirstControl() {
    const target = getFirstControl();
    target?.focus?.();
    return target;
  }

  listen(doc, 'focusin', () => {
    if (!isOpen) return;
    if (!dialog.contains(doc.activeElement)) {
      focusFirstControl();
    }
  });

  listen(doc, 'keydown', (e) => {
    if (!isOpen) return;
    if (e.key === 'Tab' && !dialog.contains(doc.activeElement)) {
      e.preventDefault();
      focusFirstControl();
    }
  });

  function open() {
    if (isOpen) return;
    isOpen = true;
    savedFocus = doc.activeElement;

    const session = getSession?.();
    baseline = pickSettings(session?.settings, DEFAULT_SESSION_SETTINGS);
    baselineVoiceMode = Boolean(voiceEnabled?.());
    draftVoiceMode = baselineVoiceMode;

    clearAllErrors();
    load(baseline);

    overlay.hidden = false;
    overlay.removeAttribute('hidden');
    onOpen?.();

    focusFirstControl();
  }

  function close({ revert = false } = {}) {
    if (!isOpen) return;

    if (sampleTimer !== null) {
      clock.clearTimeout(sampleTimer);
      sampleTimer = null;
    }

    if (revert) {
      onRevert?.(baseline);
      if (Boolean(voiceEnabled?.()) !== baselineVoiceMode) {
        toggleVoice?.();
      }
    }

    overlay.hidden = true;
    overlay.setAttribute('hidden', '');
    isOpen = false;
    onClose?.();

    if (savedFocus?.isConnected && typeof savedFocus.focus === 'function') {
      savedFocus.focus();
    }
  }

  function save() {
    clearAllErrors();
    let hasError = false;
    let firstInvalid = null;
    const values = {};

    for (const key of CONFIG_PANEL_FIELDS) {
      const raw = controls[key].value;
      const parsed = validateAdvField(key, raw, DEFAULT_SESSION_SETTINGS);
      if (!parsed.valid) {
        setFieldError(key, parsed.error);
        hasError = true;
        firstInvalid ||= controls[key];
      } else {
        values[key] = parsed.value;
      }
    }

    const langErr = validateAdvLanguages(controls.advPrimaryLanguage.value, controls.advSubtitleLanguage.value);
    if (langErr) {
      setFieldError('advLanguages', langErr);
      hasError = true;
      firstInvalid ||= controls.advSubtitleLanguage;
    }

    if (hasError) {
      if (globalError) {
        globalError.hidden = false;
        globalError.removeAttribute('hidden');
        globalError.textContent = '请检查并修正上述标红设置项。';
      }
      firstInvalid?.focus?.();
      return false;
    }

    // Persist through the store path (main.js saveSettings)
    saveSettings?.(values);

    // Sync global voiceMode if toggled
    if (draftVoiceMode !== Boolean(voiceEnabled?.())) {
      toggleVoice?.();
    }

    close({ revert: false });
    return true;
  }

  if (container) {
    container.appendChild(overlay);
  }

  return {
    open,
    close,
    load,
    read,
    save,
    focusFirstControl,
    getFirstControl,
    get isOpen() { return isOpen; },
    get element() { return overlay; },
    get controls() { return controls; },
    get errorElements() { return errorEls; },
    get valElements() { return valEls; },
    destroy() {
      for (const off of disposers.splice(0)) off();
      if (sampleTimer !== null) {
        clock.clearTimeout(sampleTimer);
        sampleTimer = null;
      }
      overlay.remove();
    },
  };
}
