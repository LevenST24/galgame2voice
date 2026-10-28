/**
 * line_player.js - DOM-free line state machine for the visual-novel (ADV) UI.
 */

import { countGraphemes, countAutoGraphemes } from './graphemes.js';

export const DEFAULT_SETTINGS = {
  revealMsPerGrapheme: 30,
  autoBaseMs: 1500,
  autoPerCharMs: 45,
  autoMode: 'voice-priority', // 'voice-priority' | 'normal'
  interruptVoice: true,
  voiceEnabled: true,
  reducedMotion: false,
  audioWaitMs: 1500,
  skipIntervalMs: 80,
  primary: 'zh', // 'zh' | 'ja'
};

/** States where the stage is showing or about to show a line of the current turn. */
const PRESENTING_STATES = new Set(['waiting_line', 'waiting_audio', 'revealing', 'waiting_advance']);

/**
 * Creates a deterministic visual-novel line player state machine.
 *
 * @param {object} params
 * @param {object} params.clock { now(): number, setTimeout(fn, ms): id, clearTimeout(id) }
 * @param {object} params.voice { play(lineIndex, chunks): Promise<'ended'|'interrupted'|'failed'>, stop({ fadeMs }): void }
 * @param {object} [params.render] Render adapter (all methods optional)
 * @param {(line: object, primaryText: string) => string[]} [params.paginate] Split text into display pages
 * @param {object} [params.settings]
 * @returns {object} player
 */
export function createLinePlayer({
  clock,
  voice,
  render,
  paginate = (line, primaryText) => [primaryText],
  settings: initialSettings = {},
} = {}) {
  let settings = { ...DEFAULT_SETTINGS, ...initialSettings };

  // Core state
  let state = 'idle'; // 'idle' | 'waiting_line' | 'waiting_audio' | 'revealing' | 'waiting_advance' | 'input' | 'suspended' | 'error'
  let currentTurnId = null;
  let currentIndex = null;
  let page = 0;
  let pages = [];
  let lines = [];
  const lineAudio = new Map(); // index -> { status, chunks }
  let turnSettled = false;
  let isPlayerEditing = false;
  let voicePlaying = false;
  let pendingAdvance = false;
  let suspendedFrom = null;
  const pauseReasons = new Set();

  // Track play calls with unique token to avoid stale voice promise resolutions
  let playSequence = 0;
  let currentVoiceToken = null;

  let auto = false;
  let skip = false;
  let hidden = false;

  // Active timers managed through clock
  let revealTimerId = null;
  let audioWaitTimerId = null;
  let autoTimerId = null;
  let skipTimerId = null;

  // Event listeners
  const listeners = new Map();

  function on(event, fn) {
    if (!listeners.has(event)) {
      listeners.set(event, new Set());
    }
    listeners.get(event).add(fn);
    return () => {
      listeners.get(event)?.delete(fn);
    };
  }

  function emit(event, ...args) {
    const set = listeners.get(event);
    if (!set) return;
    for (const fn of Array.from(set)) {
      try {
        fn(...args);
      } catch (err) {
        console.error(err);
      }
    }
  }

  function changeState(newState) {
    if (state === newState) return;
    if (newState === 'input' || newState === 'error') {
      auto = false; skip = false; clearAllTimers();
    }
    state = newState;
    render?.waitingForAudio?.(newState === 'waiting_audio');
    emit('state', newState);
  }

  function clearAllTimers() {
    if (revealTimerId !== null) {
      clock.clearTimeout(revealTimerId);
      revealTimerId = null;
    }
    if (audioWaitTimerId !== null) {
      clock.clearTimeout(audioWaitTimerId);
      audioWaitTimerId = null;
    }
    if (autoTimerId !== null) {
      clock.clearTimeout(autoTimerId);
      autoTimerId = null;
    }
    if (skipTimerId !== null) {
      clock.clearTimeout(skipTimerId);
      skipTimerId = null;
    }
  }

  function getPagesForLine(line) {
    const primaryField = settings.primary || 'zh';
    const selected = line?.[primaryField] || '';
    const primaryText = selected.trim() ? selected : (line?.[primaryField === 'zh' ? 'ja' : 'zh'] || '');
    let result = paginate(line, primaryText);
    if (!Array.isArray(result) || result.length === 0) {
      result = [primaryText];
    }
    return result;
  }

  function isAudioPlayable(status, chunks) {
    return (status === 'ready' || status === 'failed') && Array.isArray(chunks) && chunks.length > 0;
  }

  function playVoice(lineIndex, chunks, onStart) {
    playSequence++;
    const token = {
      turnId: currentTurnId,
      lineIndex,
      seq: playSequence,
    };
    currentVoiceToken = token;
    voicePlaying = true;

    let playPromise;
    try {
      playPromise = Promise.resolve(voice?.play?.(lineIndex, chunks || [], { onStart: () => {
        if (currentVoiceToken === token) onStart?.();
      } }));
    } catch (err) {
      playPromise = Promise.reject(err);
    }
    playPromise
      .then((result) => {
        onVoiceEnd(token, result);
      })
      .catch(() => {
        onVoiceEnd(token, 'failed');
      });
  }

  function onVoiceEnd(token, _result) {
    if (
      !currentVoiceToken ||
      currentVoiceToken.turnId !== token.turnId ||
      currentVoiceToken.lineIndex !== token.lineIndex ||
      currentVoiceToken.seq !== token.seq
    ) {
      // Stale resolution from superseded or stopped playback
      return;
    }
    currentVoiceToken = null;
    voicePlaying = false;

    // Download/decode or AudioContext activation can fail before playback starts.
    // Text must remain accessible even when there was no start notification.
    if (state === 'waiting_audio') revealPage(page);

    if (pendingAdvance) {
      pendingAdvance = false;
      advance();
      return;
    }

    if (auto && !hidden && state === 'waiting_advance' && settings.autoMode === 'voice-priority' && page === pages.length - 1) {
      startAutoTimer();
    }
  }

  function playAndReveal(index, chunks, pageIndex = 0) {
    if (voice?.supportsStartNotification === true) {
      changeState('waiting_audio');
      playVoice(index, chunks, () => {
        if (state === 'waiting_audio') revealPage(pageIndex);
      });
    } else {
      playVoice(index, chunks);
      revealPage(pageIndex);
    }
  }

  function activateLine(index) {
    currentIndex = index;
    page = 0;
    pendingAdvance = false;
    const line = lines[index];
    pages = getPagesForLine(line);

    const skipping = !!skip;
    const reducedMotion = !!settings.reducedMotion;

    render?.activate?.(line, { skipping, reducedMotion });
    emit('activate', line, { skipping, reducedMotion });

    if (skipping || !settings.voiceEnabled) {
      revealPage(0);
      return;
    }

    const audioInfo = lineAudio.get(index);
    if (audioInfo) {
      if (isAudioPlayable(audioInfo.status, audioInfo.chunks)) {
        playAndReveal(index, audioInfo.chunks);
      } else {
        // skipped, or failed without playable chunks: reveal immediately
        revealPage(0);
      }
    } else {
      waitForAudio();
    }
  }

  function waitForAudio() {
    if (turnSettled) {
      revealPage(page);
      return;
    }
    changeState('waiting_audio');
    // A preview deadline is useful in reading mode, but breaks voice sync when
    // synthesis takes longer. Stream completion/failure bounds the synced wait;
    // manual advance and disabling voice always remain available.
    if (settings.autoMode !== 'voice-priority') {
      audioWaitTimerId = clock.setTimeout(() => {
        audioWaitTimerId = null;
        revealPage(page);
      }, settings.audioWaitMs);
    }
  }

  function revealPage(pageIndex) {
    page = pageIndex;
    const pageText = pages[pageIndex] ?? '';
    const pageCount = pages.length;

    render?.glyph?.(false);

    if (skip) {
      changeState('waiting_advance');
      render?.reveal?.(pageText, { durationMs: 0, pageIndex, pageCount });
      render?.completeReveal?.();
      scheduleSkipAdvance();
      return;
    }

    const isInstant = settings.reducedMotion || settings.revealMsPerGrapheme === 0;
    const durationMs = isInstant ? 0 : countGraphemes(pageText) * settings.revealMsPerGrapheme;

    if (durationMs === 0) {
      changeState('revealing');
      render?.reveal?.(pageText, { durationMs: 0, pageIndex, pageCount });
      render?.completeReveal?.();
      changeState('waiting_advance');
      onRevealComplete();
    } else {
      changeState('revealing');
      render?.reveal?.(pageText, { durationMs, pageIndex, pageCount });
      revealTimerId = clock.setTimeout(() => {
        revealTimerId = null;
        render?.completeReveal?.();
        changeState('waiting_advance');
        onRevealComplete();
      }, durationMs);
    }
  }

  function onRevealComplete() {
    if (revealTimerId !== null) {
      clock.clearTimeout(revealTimerId);
      revealTimerId = null;
    }
    if (!skip) {
      render?.glyph?.(true);
    }
    if (auto && !hidden && state !== 'suspended') {
      startAutoTimer();
    }
  }

  function startAutoTimer() {
    if (autoTimerId !== null) {
      clock.clearTimeout(autoTimerId);
      autoTimerId = null;
    }
    if (!auto || hidden || state === 'suspended' || state === 'error' || state === 'input') {
      return;
    }

    const isFinalPage = page === pages.length - 1;
    if (isFinalPage && settings.autoMode === 'voice-priority' && voicePlaying) {
      // In voice-priority mode on final page, delay starts after voice ends
      return;
    }
    if (isFinalPage && settings.autoMode === 'voice-priority' && settings.voiceEnabled
      && !turnSettled && !lineAudio.has(currentIndex)) {
      // The text preview deadline is not an audio failure. Do not AUTO past a
      // sentence whose synthesis is still pending, discarding its late audio.
      return;
    }

    const pageText = pages[page] ?? '';
    const count = countAutoGraphemes(pageText);
    const D = Math.min(6000, Math.max(1200, settings.autoBaseMs + settings.autoPerCharMs * count));

    autoTimerId = clock.setTimeout(() => {
      autoTimerId = null;
      advance();
    }, D);
  }

  function scheduleSkipAdvance() {
    if (skipTimerId !== null) {
      clock.clearTimeout(skipTimerId);
      skipTimerId = null;
    }
    if (!skip) return;

    skipTimerId = clock.setTimeout(() => {
      skipTimerId = null;
      advance();
    }, settings.skipIntervalMs);
  }

  function moveToNextLine() {
    render?.glyph?.(false);
    const nextIndex = currentIndex + 1;
    if (lines[nextIndex]) {
      activateLine(nextIndex);
    } else {
      // Out of lines
      if (turnSettled) {
        if (skip) {
          skip = false;
          if (skipTimerId !== null) {
            clock.clearTimeout(skipTimerId);
            skipTimerId = null;
          }
        }
        changeState('input');
        render?.enterInput?.({ canSend: true });
        emit('input', { canSend: true });
      } else {
        changeState('waiting_line');
        render?.thinking?.(true);
      }
    }
  }

  function advance() {
    if (state === 'error' || state === 'input' || state === 'idle' || state === 'suspended') {
      return;
    }

    if (state === 'waiting_line') {
      return;
    }

    if (state === 'waiting_audio') {
      if (audioWaitTimerId !== null) {
        clock.clearTimeout(audioWaitTimerId);
        audioWaitTimerId = null;
      }
      revealPage(0);
      return;
    }

    if (state === 'revealing') {
      if (revealTimerId !== null) {
        clock.clearTimeout(revealTimerId);
        revealTimerId = null;
      }
      render?.completeReveal?.();
      changeState('waiting_advance');
      onRevealComplete();
      return;
    }

    if (state === 'waiting_advance') {
      if (autoTimerId !== null) {
        clock.clearTimeout(autoTimerId);
        autoTimerId = null;
      }

      if (page < pages.length - 1) {
        revealPage(page + 1);
        return;
      }

      // Final page: moving to next line
      if (voicePlaying && !skip) {
        if (settings.interruptVoice) {
          currentVoiceToken = null;
          voicePlaying = false;
          voice?.stop?.({ fadeMs: 40 });
        } else {
          pendingAdvance = true;
          return;
        }
      }

      moveToNextLine();
    }
  }

  function setAuto(on) {
    const next = !!on;
    if (auto === next) return;
    auto = next;
    if (auto) {
      if (skip) {
        setSkip(false);
      }
      if (state === 'waiting_advance' && !hidden) {
        startAutoTimer();
      }
    } else {
      if (autoTimerId !== null) {
        clock.clearTimeout(autoTimerId);
        autoTimerId = null;
      }
    }
  }

  function setSkip(on) {
    const next = !!on;
    if (skip === next) return;
    skip = next;
    if (skip) {
      if (auto) {
        setAuto(false);
      }
      if (voicePlaying) {
        currentVoiceToken = null;
        voicePlaying = false;
        voice?.stop?.({ fadeMs: 40 });
      }
      if (state === 'waiting_audio') {
        if (audioWaitTimerId !== null) {
          clock.clearTimeout(audioWaitTimerId);
          audioWaitTimerId = null;
        }
        revealPage(page);
      } else if (state === 'revealing') {
        if (revealTimerId !== null) {
          clock.clearTimeout(revealTimerId);
          revealTimerId = null;
        }
        render?.completeReveal?.();
        changeState('waiting_advance');
        scheduleSkipAdvance();
      } else if (state === 'waiting_advance') {
        scheduleSkipAdvance();
      }
    } else {
      if (skipTimerId !== null) {
        clock.clearTimeout(skipTimerId);
        skipTimerId = null;
      }
      if (state === 'waiting_advance') {
        render?.glyph?.(true);
        if (auto && !hidden) {
          startAutoTimer();
        }
      }
    }
  }

  function hide() {
    if (hidden) return;
    hidden = true;
    setSkip(false);
    if (autoTimerId !== null) {
      clock.clearTimeout(autoTimerId);
      autoTimerId = null;
    }
  }

  function unhide() {
    if (!hidden) return;
    hidden = false;
    if (state === 'waiting_line' && !hidden) {
      const nextIndex = currentIndex === null ? 0 : currentIndex + 1;
      if (lines[nextIndex]) {
        render?.thinking?.(false);
        activateLine(nextIndex);
      } else if (turnSettled) {
        render?.thinking?.(false);
        changeState('input');
        render?.enterInput?.({ canSend: true });
        emit('input', { canSend: true });
      }
    } else if (auto && state === 'waiting_advance') {
      startAutoTimer();
    }
  }

  function suspend(reason = 'default') {
    pauseReasons.add(reason);
    if (state === 'suspended') return;
    suspendedFrom = state;
    // Clearing pendingAdvance explicitly on suspend: after resume the user advances again
    pendingAdvance = false;
    if (skip) {
      setSkip(false);
    }
    if (autoTimerId !== null) {
      clock.clearTimeout(autoTimerId);
      autoTimerId = null;
    }
    if (voicePlaying) {
      currentVoiceToken = null;
      voicePlaying = false;
      voice?.stop?.({ fadeMs: 40 });
    }
    if (revealTimerId !== null) {
      clock.clearTimeout(revealTimerId);
      revealTimerId = null;
    }
    if (audioWaitTimerId !== null) {
      clock.clearTimeout(audioWaitTimerId);
      audioWaitTimerId = null;
    }
    changeState('suspended');
  }

  function resume(reason = 'default') {
    pauseReasons.delete(reason);
    if (pauseReasons.size) return;
    if (state !== 'suspended') return;
    const restoreTo = suspendedFrom;
    suspendedFrom = null;

    if (restoreTo === 'waiting_advance' || restoreTo === 'revealing') {
      changeState('waiting_advance');
      render?.completeReveal?.();
      render?.glyph?.(true);
      if (auto && !hidden) {
        startAutoTimer();
      }
    } else if (restoreTo === 'waiting_audio') {
      const audioInfo = lineAudio.get(currentIndex);
      if (!settings.voiceEnabled) {
        // Voice may have been disabled while the settings overlay was open.
        // Do not keep waiting for synthesis when the user returns to the line.
        revealPage(page);
      } else if (audioInfo) {
        if (isAudioPlayable(audioInfo.status, audioInfo.chunks)) {
          if (!skip && settings.voiceEnabled) {
            playAndReveal(currentIndex, audioInfo.chunks, page);
          } else revealPage(page);
        } else {
          revealPage(page);
        }
      } else {
        waitForAudio();
      }
    } else if (restoreTo === 'waiting_line') {
      // Re-evaluate line queue and settlement that may have arrived while suspended
      const nextIndex = currentIndex === null ? 0 : currentIndex + 1;
      if (hidden) {
        changeState('waiting_line');
      } else if (lines[nextIndex]) {
        render?.thinking?.(false);
        activateLine(nextIndex);
      } else if (turnSettled) {
        render?.thinking?.(false);
        changeState('input');
        render?.enterInput?.({ canSend: true });
        emit('input', { canSend: true });
      } else {
        changeState('waiting_line');
        render?.thinking?.(true);
      }
    } else {
      changeState(restoreTo || 'idle');
      if (restoreTo === 'input') {
        render?.enterInput?.({ canSend: turnSettled });
        emit('input', { canSend: turnSettled });
      }
    }
  }

  function beginPlayerEdit() {
    if (isPlayerEditing) return;
    isPlayerEditing = true;

    auto = false;
    skip = false;
    pendingAdvance = false;
    clearAllTimers();

    if (voicePlaying) {
      currentVoiceToken = null;
      voicePlaying = false;
      voice?.stop?.({ fadeMs: 40 });
    }

    render?.completeReveal?.();
    render?.glyph?.(false);
    render?.thinking?.(false);

    const startLogIdx = currentIndex === null ? 0 : currentIndex + 1;
    for (let i = startLogIdx; i < lines.length; i++) {
      const l = lines[i];
      if (l) {
        render?.logOnly?.(l);
        emit('logged', l);
      }
    }

    changeState('input');
    const canSend = !!turnSettled;
    render?.enterInput?.({ canSend });
    emit('input', { canSend });
  }

  function dismissError() {
    if (state !== 'error') return;
    changeState('input');
    render?.enterInput?.({ canSend: true });
    emit('input', { canSend: true });
  }

  function reset() {
    pauseReasons.clear();
    currentTurnId = null;
    currentVoiceToken = null;
    clearAllTimers();
    if (voicePlaying) {
      voicePlaying = false;
      voice?.stop?.({ fadeMs: 40 });
    }
    auto = false;
    skip = false;
    hidden = false;
    isPlayerEditing = false;
    turnSettled = false;
    pendingAdvance = false;
    suspendedFrom = null;
    currentIndex = null;
    page = 0;
    pages = [];
    lines = [];
    lineAudio.clear();

    render?.glyph?.(false);
    render?.thinking?.(false);
    changeState('idle');
  }

  function startTurn(turnId) {
    pauseReasons.clear();
    clearAllTimers();
    currentVoiceToken = null;
    if (voicePlaying) {
      voicePlaying = false;
      voice?.stop?.({ fadeMs: 40 });
    }
    currentTurnId = turnId;
    currentIndex = null;
    page = 0;
    pages = [];
    lines = [];
    lineAudio.clear();
    turnSettled = false;
    isPlayerEditing = false;
    pendingAdvance = false;
    suspendedFrom = null;

    render?.glyph?.(false);
    changeState('waiting_line');
    render?.thinking?.(true);
  }

  function pushLine(turnId, line) {
    if (turnId !== currentTurnId) return;
    lines[line.index] = line;

    if (isPlayerEditing) {
      render?.logOnly?.(line);
      emit('logged', line);
      return;
    }

    if (state === 'waiting_line' && !hidden) {
      if (currentIndex === null && line.index === 0) {
        render?.thinking?.(false);
        activateLine(0);
      } else if (currentIndex !== null && line.index === currentIndex + 1) {
        render?.thinking?.(false);
        activateLine(currentIndex + 1);
      }
    }
  }

  function setLineAudio(turnId, index, { status, chunks }) {
    if (turnId !== currentTurnId) return;
    const chunkList = chunks || [];
    lineAudio.set(index, { status, chunks: chunkList });

    if (index === currentIndex) {
      const playable = isAudioPlayable(status, chunkList);
      if (state === 'waiting_audio') {
        if (audioWaitTimerId !== null) {
          clock.clearTimeout(audioWaitTimerId);
          audioWaitTimerId = null;
        }
        if (playable) {
          if (!skip && settings.voiceEnabled) {
            playAndReveal(index, chunkList);
          } else revealPage(0);
        } else {
          // skipped or failed without chunks
          revealPage(0);
        }
      } else {
        // Late audio while line is still current and on screen; never while
        // suspended under an overlay, typing in input mode, or in an error.
        const onScreen = state === 'revealing' || state === 'waiting_advance';
        if (onScreen && playable && !skip && settings.voiceEnabled && !voicePlaying) {
          playVoice(index, chunkList);
          if (auto && autoTimerId !== null && settings.autoMode === 'voice-priority' && page === pages.length - 1) {
            clock.clearTimeout(autoTimerId);
            autoTimerId = null;
          }
        } else if (state === 'waiting_advance' && !playable && auto) {
          startAutoTimer();
        }
      }
    }
  }

  function settle(turnId, result) {
    if (turnId !== currentTurnId) return;

    if (result.ok === true) {
      turnSettled = true;
      if (state === 'waiting_audio' && !voicePlaying) {
        if (audioWaitTimerId !== null) clock.clearTimeout(audioWaitTimerId);
        audioWaitTimerId = null;
        revealPage(page);
      }
      if (isPlayerEditing) {
        if (pauseReasons.size) {
          suspendedFrom = 'input';
          changeState('suspended');
        } else {
          render?.enterInput?.({ canSend: true });
          emit('input', { canSend: true });
        }
        return;
      }
      if (state === 'waiting_line') {
        render?.thinking?.(false);
        const nextIndex = currentIndex === null ? 0 : currentIndex + 1;
        if (lines[nextIndex]) {
          if (!hidden) activateLine(nextIndex);
          return;
        }
        if (skip) {
          skip = false;
          if (skipTimerId !== null) {
            clock.clearTimeout(skipTimerId);
            skipTimerId = null;
          }
        }
        changeState('input');
        render?.enterInput?.({ canSend: true });
        emit('input', { canSend: true });
        return;
      }
      // Otherwise unread lines are still current; do not jump over them
      if (state === 'waiting_advance' && auto && autoTimerId === null) startAutoTimer();
    } else {
      turnSettled = true;
      pendingAdvance = false;
      clearAllTimers();
      if (voicePlaying) {
        currentVoiceToken = null;
        voicePlaying = false;
        voice?.stop?.({ fadeMs: 40 });
      }
      if (skip) {
        skip = false;
      }
      render?.glyph?.(false);
      render?.thinking?.(false);

      if (result.kind === 'error') {
        if (pauseReasons.size) {
          suspendedFrom = 'error';
          changeState('suspended');
        } else changeState('error');
        render?.showError?.(result.error);
      } else if (result.kind === 'cancelled') {
        if (pauseReasons.size) {
          suspendedFrom = 'input';
          changeState('suspended');
        } else {
          changeState('input');
          render?.enterInput?.({ canSend: true });
          emit('input', { canSend: true });
        }
      }
    }
  }

  function setSettings(patch) {
    const previousVoice = settings.voiceEnabled;
    const previousMode = settings.autoMode;
    settings = { ...settings, ...patch };
    if (state === 'waiting_audio' && !voicePlaying && previousMode !== settings.autoMode) {
      if (audioWaitTimerId !== null) clock.clearTimeout(audioWaitTimerId);
      audioWaitTimerId = null;
      waitForAudio();
    }
    if (!settings.voiceEnabled && voicePlaying) {
      currentVoiceToken = null;
      voicePlaying = false;
      voice?.stop?.({ fadeMs: 40 });
      if (pendingAdvance && state === 'waiting_advance') {
        pendingAdvance = false;
        advance();
      }
    }
    if (!settings.voiceEnabled && state === 'waiting_audio') {
      if (audioWaitTimerId !== null) clock.clearTimeout(audioWaitTimerId);
      audioWaitTimerId = null;
      revealPage(page);
    }
    if (settings.reducedMotion && state === 'revealing') {
      if (revealTimerId !== null) {
        clock.clearTimeout(revealTimerId);
        revealTimerId = null;
      }
      render?.completeReveal?.();
      changeState('waiting_advance');
      onRevealComplete();
    }
    if (state === 'waiting_advance' && auto
      && (previousVoice !== settings.voiceEnabled || previousMode !== settings.autoMode)) startAutoTimer();
  }

  function repaginate() {
    if (currentIndex === null || !lines[currentIndex]) return;
    const offset = pages.slice(0, page).join('').length;
    pages = getPagesForLine(lines[currentIndex]);
    let start = 0;
    page = pages.findIndex(text => { start += text.length; return start > offset; });
    if (page < 0) page = Math.max(0, pages.length - 1);
  }

  function isPresenting() {
    return PRESENTING_STATES.has(state === 'suspended' ? suspendedFrom : state);
  }

  return {
    // Stream input
    startTurn,
    pushLine,
    setLineAudio,
    settle,

    // User input
    advance,
    setAuto,
    setSkip,
    hide,
    unhide,
    suspend,
    resume,
    beginPlayerEdit,
    dismissError,
    reset,
    repaginate,

    // Settings
    setSettings,

    // Introspection
    isPresenting,
    get state() { return state; },
    get currentIndex() { return currentIndex; },
    get page() { return page; },
    get auto() { return auto; },
    get skip() { return skip; },
    get hidden() { return hidden; },
    get settings() { return { ...settings }; },
    get canSend() { return turnSettled; },

    // Subscription
    on,
  };
}
