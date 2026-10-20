/**
 * textbox.js - ADV message window component and line_player render adapter.
 */

import { splitGraphemes, countGraphemes } from './graphemes.js';
import { paginate as purePaginate, pairSubtitle } from './paginate.js';

/**
 * Wraps text with display-only quotation marks if it is character speech
 * and not already enclosed in quotes. Never mutates the raw stored string.
 */
function formatDisplayQuote(text, mode) {
  if (!text) return '';
  if (mode !== 'character') return text;
  const trimmed = text.trim();
  const startsWithQuote = /^[「“"『]/.test(trimmed);
  const endsWithQuote = /[」”"』]$/.test(trimmed);
  let res = text;
  if (!startsWithQuote) {
    res = `「${res}`;
  }
  if (!endsWithQuote) {
    res = `${res}」`;
  }
  return res;
}

/**
 * Creates the ADV textbox inside container.
 *
 * @param {HTMLElement} container
 * @param {object} opts
 * @param {() => object} [opts.getSettings]
 * @param {() => void} [opts.onAdvance]
 * @param {(text: string) => boolean} [opts.onSend] Return true only after accepting the send.
 * @param {() => void} [opts.onMic]
 * @param {(action: string, data?: any) => void} [opts.onAction]
 * @param {object} [opts.clock]
 * @param {(candidate: string) => boolean} [opts.fits]
 * @returns {object} line_player render adapter + extras
 */
export function createTextbox(container, opts = {}) {
  const clock = opts.clock || {
    now: () => (typeof performance !== 'undefined' ? performance.now() : Date.now()),
    setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
    clearTimeout: (id) => globalThis.clearTimeout(id),
  };

  const eventDisposers = [];
  const errorDisposers = [];
  function listen(el, type, handler, disposers = eventDisposers) {
    el.addEventListener(type, handler);
    disposers.push(() => el.removeEventListener(type, handler));
  }

  // Root wrapper
  const rootEl = document.createElement('div');
  rootEl.className = 'adv-textbox-root';

  // 1. Gradient band
  const bandEl = document.createElement('div');
  bandEl.className = 'adv-band';
  bandEl.setAttribute('aria-hidden', 'true');

  // 2. Face window slot (x=0, y=696, w=450, h=404)
  const faceSlot = document.createElement('div');
  faceSlot.className = 'adv-face-slot';
  faceSlot.setAttribute('aria-hidden', 'true');

  // 3. Name plate (x=468, y=822, w=1120, h=48)
  const namePlateEl = document.createElement('div');
  namePlateEl.className = 'adv-name-plate';

  const bracketLeft = document.createElement('span');
  bracketLeft.className = 'adv-name-bracket adv-name-bracket-left';
  bracketLeft.setAttribute('aria-hidden', 'true');
  bracketLeft.textContent = '【';

  const nameTextEl = document.createElement('span');
  nameTextEl.className = 'adv-name-text';

  const bracketRight = document.createElement('span');
  bracketRight.className = 'adv-name-bracket adv-name-bracket-right';
  bracketRight.setAttribute('aria-hidden', 'true');
  bracketRight.textContent = '】';

  namePlateEl.appendChild(bracketLeft);
  namePlateEl.appendChild(nameTextEl);
  namePlateEl.appendChild(bracketRight);

  // 4. Text column
  const textColumnEl = document.createElement('div');
  textColumnEl.className = 'adv-text-column';

  // Hidden DOM measurer for paginate()
  const measurerEl = document.createElement('div');
  measurerEl.className = 'adv-text-measurer';
  measurerEl.setAttribute('aria-hidden', 'true');
  textColumnEl.appendChild(measurerEl);

  const subMeasurerEl = document.createElement('div');
  subMeasurerEl.className = 'adv-subtitle-measurer';
  subMeasurerEl.setAttribute('aria-hidden', 'true');
  textColumnEl.appendChild(subMeasurerEl);

  // Speech view (primary + subtitle + thinking)
  const speechViewEl = document.createElement('div');
  speechViewEl.className = 'adv-speech-view';

  const primaryTextEl = document.createElement('div');
  primaryTextEl.className = 'adv-primary-text';
  primaryTextEl.setAttribute('role', 'text');

  const subtitleTextEl = document.createElement('div');
  subtitleTextEl.className = 'adv-subtitle-text';
  subtitleTextEl.setAttribute('role', 'text');

  const thinkingDotsEl = document.createElement('span');
  thinkingDotsEl.className = 'adv-thinking-dots is-hidden';
  thinkingDotsEl.setAttribute('aria-hidden', 'true');
  thinkingDotsEl.textContent = '……';

  speechViewEl.appendChild(primaryTextEl);
  speechViewEl.appendChild(subtitleTextEl);
  speechViewEl.appendChild(thinkingDotsEl);

  // Player input view
  const inputWrapEl = document.createElement('div');
  inputWrapEl.className = 'adv-player-input-wrap is-hidden';

  const textareaEl = document.createElement('textarea');
  textareaEl.className = 'adv-player-textarea';
  textareaEl.rows = 2;
  textareaEl.placeholder = '请输入你的回答（Enter 发送，Shift+Enter 换行）...';

  function isOverlayBlocked() {
    let cur = rootEl;
    while (cur) {
      if (cur.inert || (cur.getAttribute?.('inert') ?? null) !== null) return true;
      cur = cur.parentNode;
    }
    if (container) {
      let curC = container;
      while (curC) {
        if (curC.inert || (curC.getAttribute?.('inert') ?? null) !== null) return true;
        curC = curC.parentNode;
      }
    }
    return Boolean(opts.isOverlayOpen?.());
  }

  const origTextareaFocus = textareaEl.focus;
  if (typeof origTextareaFocus === 'function') {
    textareaEl.focus = function (...args) {
      if (isOverlayBlocked()) {
        return;
      }
      return origTextareaFocus.apply(this, args);
    };
  }

  const playerControlsEl = document.createElement('div');
  playerControlsEl.className = 'adv-player-controls';

  const micBtn = document.createElement('button');
  micBtn.type = 'button';
  micBtn.className = 'adv-player-mic-btn';
  micBtn.title = '语音输入';
  micBtn.setAttribute('aria-label', '语音输入');
  micBtn.innerHTML = '<svg viewBox="0 0 24 24" class="adv-icon"><path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z" fill="currentColor"/><path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" fill="currentColor"/></svg>';

  const sendBtn = document.createElement('button');
  sendBtn.type = 'button';
  sendBtn.className = 'adv-player-send-btn';
  sendBtn.title = '发送 (Enter)';
  sendBtn.setAttribute('aria-label', '发送');
  sendBtn.innerHTML = '<svg viewBox="0 0 24 24" class="adv-icon"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z" fill="currentColor"/></svg>';

  playerControlsEl.appendChild(micBtn);
  playerControlsEl.appendChild(sendBtn);

  inputWrapEl.appendChild(textareaEl);
  inputWrapEl.appendChild(playerControlsEl);

  // Error view
  const errorWrapEl = document.createElement('div');
  errorWrapEl.className = 'adv-error-wrap is-hidden';
  errorWrapEl.setAttribute('role', 'alert');

  const errorMessageEl = document.createElement('span');
  errorMessageEl.className = 'adv-error-message';

  const errorActionsEl = document.createElement('div');
  errorActionsEl.className = 'adv-error-actions';

  errorWrapEl.appendChild(errorMessageEl);
  errorWrapEl.appendChild(errorActionsEl);

  textColumnEl.appendChild(speechViewEl);
  textColumnEl.appendChild(inputWrapEl);
  textColumnEl.appendChild(errorWrapEl);

  // 5. Click-wait glyph (x=1640, y=990, w=30, h=36)
  const clickGlyphEl = document.createElement('div');
  clickGlyphEl.className = 'adv-click-glyph is-hidden';
  clickGlyphEl.setAttribute('aria-hidden', 'true');
  clickGlyphEl.innerHTML = '<svg viewBox="0 0 32 32" class="adv-glyph-svg"><path d="M 6 26 C 8 20 12 14 18 10 C 24 6 28 5 28 5 C 28 5 27 10 23 15 C 19 20 14 24 7 27 Z" fill="currentColor"/><path d="M 12 21 C 15 17 18 14 23 12" fill="none" stroke="#ffffff" stroke-width="1.5" stroke-linecap="round"/><circle cx="26" cy="7" r="2" fill="#ffe27a"/></svg>';

  // 6. VOICE display (x=1710, y=988, w=160, h=28)
  const voiceDisplayEl = document.createElement('div');
  voiceDisplayEl.className = 'adv-voice-display is-hidden';
  voiceDisplayEl.setAttribute('aria-hidden', 'true');

  const voiceLabelEl = document.createElement('span');
  voiceLabelEl.className = 'adv-voice-label';
  voiceLabelEl.textContent = 'VOICE';

  const voiceMeterEl = document.createElement('div');
  voiceMeterEl.className = 'adv-voice-meter';
  for (let i = 1; i <= 5; i++) {
    const bar = document.createElement('span');
    bar.className = `adv-voice-bar bar-${i}`;
    voiceMeterEl.appendChild(bar);
  }

  const voiceProgressEl = document.createElement('span');
  voiceProgressEl.className = 'adv-voice-progress';

  voiceDisplayEl.appendChild(voiceLabelEl);
  voiceDisplayEl.appendChild(voiceMeterEl);
  voiceDisplayEl.appendChild(voiceProgressEl);

  // 7. Visually hidden aria-live polite region
  const liveRegionEl = document.createElement('div');
  liveRegionEl.className = 'adv-live-region';
  liveRegionEl.setAttribute('aria-live', 'polite');
  liveRegionEl.setAttribute('aria-atomic', 'true');

  rootEl.appendChild(bandEl);
  rootEl.appendChild(faceSlot);
  rootEl.appendChild(namePlateEl);
  rootEl.appendChild(textColumnEl);
  rootEl.appendChild(clickGlyphEl);
  rootEl.appendChild(voiceDisplayEl);
  rootEl.appendChild(liveRegionEl);

  if (container) {
    container.appendChild(rootEl);
  }

  // State
  let currentSpeaker = { name: '', mode: 'character' };
  let currentLine = null;
  let currentPrimaryText = '';
  let currentSubText = '';
  let currentFullPageText = '';
  let currentPageText = '';
  let currentPageIndex = 0;
  let currentPageCount = 1;
  let currentSubPages = [];
  let typewriterTimerId = null;
  let isHidden = false;
  let canSendInput = false;

  let boundPlayer = opts.player || null;
  function getBoundPlayer() {
    return boundPlayer || opts.player || opts.getPlayer?.() || null;
  }
  function setPlayer(p) {
    boundPlayer = p;
  }

  // Reduced motion tracking
  let systemReducedMotion = false;
  let mql = null;
  let mqlHandler = null;
  const matchMedia = opts.matchMedia || (query => globalThis.matchMedia?.(query));
  if (typeof matchMedia === 'function') {
    try {
      mql = matchMedia('(prefers-reduced-motion: reduce)');
      systemReducedMotion = Boolean(mql?.matches);
      mqlHandler = (e) => {
        systemReducedMotion = !!e.matches;
        rootEl.classList.toggle('reduced-motion', systemReducedMotion);
        if (systemReducedMotion && typewriterTimerId !== null) {
          completeReveal();
        }
      };
      if (typeof mql?.addEventListener === 'function') {
        mql.addEventListener('change', mqlHandler);
      } else if (typeof mql?.addListener === 'function') {
        mql.addListener(mqlHandler);
      }
    } catch {
      // Ignore in non-browser environment
    }
  }

  function getEffectiveSettings() {
    const s = opts.getSettings?.() || {};
    return {
      primary: s.primary || 'zh',
      subtitle: s.subtitle !== undefined ? s.subtitle : 'ja',
      playerName: s.playerName || '你',
      advWindowOpacity: s.advWindowOpacity !== undefined ? s.advWindowOpacity : 75,
      reducedMotion: s.reducedMotion !== undefined ? s.reducedMotion : systemReducedMotion,
    };
  }

  function applyWindowOpacity() {
    const { advWindowOpacity } = getEffectiveSettings();
    const alpha = Math.max(0, Math.min(100, advWindowOpacity));
    bandEl.style.setProperty('--adv-window-opacity', String(alpha));
  }
  applyWindowOpacity();

  function updateSpeakerUI() {
    const { mode, name } = currentSpeaker;

    if (mode === 'narration') {
      faceSlot.classList.add('is-hidden');
      namePlateEl.classList.add('is-hidden');
      bracketLeft.style.display = 'none';
      bracketRight.style.display = 'none';
      nameTextEl.textContent = '';
    } else if (mode === 'player') {
      faceSlot.classList.add('is-hidden');
      namePlateEl.classList.remove('is-hidden');
      bracketLeft.style.display = 'none';
      bracketRight.style.display = 'none';
      const settings = getEffectiveSettings();
      nameTextEl.textContent = name || settings.playerName;
    } else {
      // Character
      faceSlot.classList.remove('is-hidden');
      bracketLeft.style.display = '';
      bracketRight.style.display = '';
      if (name) {
        namePlateEl.classList.remove('is-hidden');
        nameTextEl.textContent = name;
      } else {
        namePlateEl.classList.add('is-hidden');
        nameTextEl.textContent = '';
      }
    }
  }

  function resolveLineTexts(line, settings) {
    const primaryLang = settings.primary || 'zh';
    const subLang = settings.subtitle;

    let primary = line?.[primaryLang] ?? '';
    // Empty text falls back without duplicating
    if (!primary) {
      const fallbackLang = primaryLang === 'zh' ? 'ja' : 'zh';
      primary = line?.[fallbackLang] ?? '';
    }

    let sub = '';
    if (subLang && subLang !== 'none') {
      if (subLang !== primaryLang) {
        sub = line?.[subLang] ?? '';
      }
      if (sub === primary) {
        sub = '';
      }
    }

    return { primaryText: primary, subText: sub };
  }

  // Player input IME composition state
  let isComposing = false;
  let justComposed = false;

  listen(textareaEl, 'compositionstart', () => {
    isComposing = true;
    opts.onAction?.('player_edit');
  });

  listen(textareaEl, 'compositionend', () => {
    isComposing = false;
    justComposed = true;
  });

  listen(textareaEl, 'keyup', () => {
    justComposed = false;
  });

  listen(textareaEl, 'focus', () => {
    if (isOverlayBlocked()) textareaEl.blur?.();
  });

  listen(textareaEl, 'input', () => {
    opts.onAction?.('player_edit');
  });

  function trySend() {
    if (isComposing || justComposed || !canSendInput) return;
    const text = textareaEl.value.trim();
    if (!text) return;
    if (opts.onSend?.(text) !== true) return;
    opts.onAction?.('send', text);
    textareaEl.value = '';
  }

  listen(textareaEl, 'keydown', (e) => {
    if (e.key === 'Enter') {
      if (e.isComposing || e.keyCode === 229 || isComposing || justComposed) {
        return;
      }
      if (!e.shiftKey) {
        e.preventDefault();
        trySend();
      }
    }
  });

  listen(micBtn, 'click', (e) => {
    e.stopPropagation();
    opts.onMic?.();
    opts.onAction?.('mic');
  });

  listen(sendBtn, 'click', (e) => {
    e.stopPropagation();
    trySend();
  });

  function clearTypewriter() {
    if (typewriterTimerId !== null) {
      clock.clearTimeout(typewriterTimerId);
      typewriterTimerId = null;
    }
  }

  // --- Render Adapter Methods ---

  function activate(line, { skipping = false, reducedMotion = false } = {}) {
    currentLine = line;
    clearTypewriter();
    applyWindowOpacity();

    // Mode is character by default unless setSpeaker called
    if (currentSpeaker.mode !== 'narration' && currentSpeaker.mode !== 'player') {
      currentSpeaker.mode = 'character';
      if (line?.speaker && !currentSpeaker.name) {
        currentSpeaker.name = line.speaker;
      }
    }
    updateSpeakerUI();

    speechViewEl.classList.remove('is-hidden');
    inputWrapEl.classList.add('is-hidden');
    errorWrapEl.classList.add('is-hidden');
    thinkingDotsEl.classList.add('is-hidden');

    const settings = getEffectiveSettings();
    const { primaryText, subText } = resolveLineTexts(line, settings);
    currentPrimaryText = primaryText;
    currentSubText = subText;

    // Accessibility: announce full line once on activate
    const speakerLabel = currentSpeaker.mode === 'character' && currentSpeaker.name ? `${currentSpeaker.name}：` : '';
    liveRegionEl.textContent = `${speakerLabel}${primaryText}`;
  }

  function reveal(pageText, { durationMs = 0, pageIndex = 0, pageCount = 1 } = {}) {
    clearTypewriter();
    currentPageText = pageText;
    currentPageIndex = pageIndex;
    currentPageCount = pageCount;

    speechViewEl.classList.remove('is-hidden');
    inputWrapEl.classList.add('is-hidden');
    thinkingDotsEl.classList.add('is-hidden');

    // Subtitle rendering
    const settings = getEffectiveSettings();
    if (currentSubText && settings.subtitle !== 'none') {
      let subPage = '';
      if (Array.isArray(currentSubPages) && currentSubPages.length === pageCount) {
        subPage = currentSubPages[pageIndex] ?? '';
      } else {
        const subPages = pairSubtitle(currentSubText, pageCount);
        subPage = subPages[pageIndex] ?? '';
      }
      subtitleTextEl.textContent = formatDisplayQuote(subPage, currentSpeaker.mode);
      subtitleTextEl.classList.toggle('is-hidden', !subPage);
    } else {
      subtitleTextEl.textContent = '';
      subtitleTextEl.classList.add('is-hidden');
    }

    const fullText = formatDisplayQuote(pageText, currentSpeaker.mode);
    currentFullPageText = fullText;

    const isInstant = durationMs === 0 || settings.reducedMotion;
    if (isInstant) {
      primaryTextEl.textContent = fullText;
      return;
    }

    const clusters = splitGraphemes(fullText);
    if (clusters.length === 0) {
      primaryTextEl.textContent = '';
      return;
    }

    const intervalMs = Math.max(1, durationMs / clusters.length);
    let currentClusterIdx = 1;
    primaryTextEl.textContent = clusters.slice(0, 1).join('');

    function tick() {
      currentClusterIdx++;
      primaryTextEl.textContent = clusters.slice(0, currentClusterIdx).join('');
      if (currentClusterIdx < clusters.length) {
        typewriterTimerId = clock.setTimeout(tick, intervalMs);
      } else {
        typewriterTimerId = null;
      }
    }

    typewriterTimerId = clock.setTimeout(tick, intervalMs);
  }

  function completeReveal() {
    clearTypewriter();
    primaryTextEl.textContent = currentFullPageText;
  }

  function glyph(on) {
    clickGlyphEl.classList.toggle('is-hidden', !on);
  }

  function thinking(on) {
    clearTypewriter();
    if (on) {
      speechViewEl.classList.remove('is-hidden');
      inputWrapEl.classList.add('is-hidden');
      primaryTextEl.textContent = '';
      subtitleTextEl.textContent = '';
      thinkingDotsEl.classList.remove('is-hidden');
      if (currentSpeaker.mode === 'character') {
        updateSpeakerUI();
      }
    } else {
      thinkingDotsEl.classList.add('is-hidden');
    }
  }

  function enterInput({ canSend = true } = {}) {
    clearTypewriter();
    setCanSend(canSend);
    clearError();

    setSpeaker({ name: '', mode: 'player' });

    speechViewEl.classList.add('is-hidden');
    errorWrapEl.classList.add('is-hidden');
    inputWrapEl.classList.remove('is-hidden');
    glyph(false);

    try {
      textareaEl.focus();
    } catch {
      // Ignore if focus is unavailable in headless / test environment
    }
  }

  // Opening the editor alone does not discard unread lines. Only its first edit
  // invokes player_edit; the application can therefore preserve IME drafts.
  function prepareInput({ canSend = false } = {}) {
    clearError();
    setCanSend(canSend);
    speechViewEl.classList.add('is-hidden');
    errorWrapEl.classList.add('is-hidden');
    inputWrapEl.classList.remove('is-hidden');
    setSpeaker({ name: '', mode: 'player' });
    try {
      textareaEl.focus();
    } catch {
      // Ignore if focus is unavailable in headless / test environment
    }
  }

  function logOnly(line) {
    opts.onAction?.('logged', line);
  }

  // --- Extras ---

  function paginate(line, primaryText) {
    const speakerMode = line?.mode || currentSpeaker?.mode || 'character';

    function fitsPrimary(candidate) {
      const displayCandidate = formatDisplayQuote(candidate, speakerMode);
      if (typeof opts.fits === 'function') {
        return opts.fits(candidate);
      }
      if (measurerEl && typeof measurerEl.getBoundingClientRect === 'function') {
        measurerEl.textContent = displayCandidate;
        const rect = measurerEl.getBoundingClientRect();
        const colRect = textColumnEl.getBoundingClientRect?.() || {};
        if (rect.width > 0 && colRect.width > 0) {
          return rect.width <= colRect.width;
        }
      }
      return countGraphemes(displayCandidate) <= 32;
    }

    function fitsSubtitle(candidate) {
      const displayCandidate = formatDisplayQuote(candidate, speakerMode);
      if (typeof opts.fitsSubtitle === 'function') {
        return opts.fitsSubtitle(candidate);
      }
      if (subMeasurerEl && typeof subMeasurerEl.getBoundingClientRect === 'function') {
        subMeasurerEl.textContent = displayCandidate;
        const rect = subMeasurerEl.getBoundingClientRect();
        const colRect = textColumnEl.getBoundingClientRect?.() || {};
        if (rect.width > 0 && colRect.width > 0) {
          return rect.width <= colRect.width;
        }
      }
      return countGraphemes(displayCandidate) <= 37;
    }

    const pPages = purePaginate(primaryText, fitsPrimary);

    const settings = getEffectiveSettings();
    const subField = settings.subtitle !== 'none' ? settings.subtitle : null;
    const subText = (subField && line) ? (line[subField] ?? '') : '';

    if (!subText) {
      currentSubPages = [];
      return pPages;
    }

    const sPages = purePaginate(subText, fitsSubtitle);
    const targetCount = Math.max(pPages.length, sPages.length);

    let finalPrimaryPages = pPages;
    if (pPages.length < targetCount) {
      finalPrimaryPages = pairSubtitle(primaryText, targetCount);
    }

    let finalSubPages = sPages;
    if (sPages.length < targetCount) {
      finalSubPages = pairSubtitle(subText, targetCount);
    }

    currentSubPages = finalSubPages;
    return finalPrimaryPages;
  }

  function setSpeaker({ name, mode = 'character' } = {}) {
    currentSpeaker = {
      name: name !== undefined ? name : currentSpeaker.name,
      mode,
    };
    updateSpeakerUI();
  }

  function setVoice({ active = false, progress } = {}) {
    if (active) {
      voiceDisplayEl.classList.remove('is-hidden');
      voiceDisplayEl.classList.add('is-active');
      if (progress !== undefined) {
        voiceProgressEl.textContent = typeof progress === 'number' ? `${Math.round(progress * 100)}%` : String(progress);
      } else {
        voiceProgressEl.textContent = '';
      }
    } else {
      voiceDisplayEl.classList.add('is-hidden');
      voiceDisplayEl.classList.remove('is-active');
      voiceProgressEl.textContent = '';
    }
  }

  function showError({ message = '', actions = [] } = {}) {
    clearError();
    clearTypewriter();
    glyph(false);

    errorMessageEl.textContent = message;
    errorActionsEl.innerHTML = '';

    for (const act of actions) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'adv-error-btn';
      btn.textContent = act.label || '';
      listen(btn, 'click', (e) => {
        e.stopPropagation();
        act.run?.();
      }, errorDisposers);
      errorActionsEl.appendChild(btn);
    }

    speechViewEl.classList.add('is-hidden');
    inputWrapEl.classList.add('is-hidden');
    errorWrapEl.classList.remove('is-hidden');
  }

  function clearError() {
    for (const off of errorDisposers.splice(0)) off();
    errorActionsEl.innerHTML = '';
    errorMessageEl.textContent = '';
    errorWrapEl.classList.add('is-hidden');
    speechViewEl.classList.remove('is-hidden');
  }

  function setCanSend(value) {
    canSendInput = Boolean(value);
    sendBtn.disabled = !canSendInput;
  }

  function setHidden(bool) {
    isHidden = !!bool;
    rootEl.classList.toggle('is-hidden', isHidden);
  }

  function swapLanguages() {
    const settings = getEffectiveSettings();
    if (settings.subtitle === 'none') return false;

    const oldPrimary = settings.primary;
    const oldSubtitle = settings.subtitle;
    const newPrimary = oldSubtitle;
    const newSubtitle = oldPrimary;

    opts.onAction?.('swap_languages', { primary: newPrimary, subtitle: newSubtitle });

    const p = getBoundPlayer();
    if (p) {
      p.setSettings?.({ primary: newPrimary });
      p.repaginate?.();
    }

    if (currentLine) {
      const resolved = resolveLineTexts(currentLine, { primary: newPrimary, subtitle: newSubtitle });
      currentPrimaryText = resolved.primaryText;
      currentSubText = resolved.subText;

      // Reflow current page without replaying voice
      const newPages = paginate(currentLine, currentPrimaryText);
      const newPageCount = newPages.length;
      const targetPageIdx = Math.min(currentPageIndex, newPageCount - 1);
      const newPageText = newPages[targetPageIdx] ?? '';

      reveal(newPageText, {
        durationMs: 0,
        pageIndex: targetPageIdx,
        pageCount: newPageCount,
      });
      completeReveal();
    }

    return true;
  }

  function reflow({ pageIndex = currentPageIndex } = {}) {
    if (!currentLine) return;
    const resolved = resolveLineTexts(currentLine, getEffectiveSettings());
    currentPrimaryText = resolved.primaryText;
    currentSubText = resolved.subText;
    const newPages = paginate(currentLine, currentPrimaryText);
    const target = Math.min(pageIndex, newPages.length - 1);
    reveal(newPages[target] || '', { durationMs: 0, pageIndex: target, pageCount: newPages.length });
    completeReveal();
  }

  function destroy() {
    clearError();
    for (const off of eventDisposers.splice(0)) off();
    clearTypewriter();
    if (mql && mqlHandler) {
      if (typeof mql.removeEventListener === 'function') {
        mql.removeEventListener('change', mqlHandler);
      } else if (typeof mql.removeListener === 'function') {
        mql.removeListener(mqlHandler);
      }
    }
    if (rootEl.parentNode) {
      rootEl.parentNode.removeChild(rootEl);
    }
  }

  return {
    // Render adapter methods for line_player
    activate,
    reveal,
    completeReveal,
    glyph,
    thinking,
    enterInput,
    prepareInput,
    setCanSend,
    logOnly,
    showError,

    // Extras
    paginate,
    setSpeaker,
    setVoice,
    clearError,
    setHidden,
    swapLanguages,
    reflow,
    refreshSettings() { applyWindowOpacity(); updateSpeakerUI(); },
    setPlayer,
    faceSlot,
    rootEl,
    destroy,

    // Internal getters for inspection & tests
    get currentSpeaker() { return { ...currentSpeaker }; },
    get currentLine() { return currentLine; },
    get currentPrimaryText() { return currentPrimaryText; },
    get currentSubText() { return currentSubText; },
    get isHidden() { return isHidden; },
    get draft() { return textareaEl.value; },
    setDraft(text) { textareaEl.value = text; },
    get inputElement() { return textareaEl; },
    get composing() { return isComposing || justComposed; },
    get micButton() { return micBtn; },
  };
}
