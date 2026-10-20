/**
 * shortcuts.js - ADV shortcut keys and gestures handler.
 *
 * Implements §3.4 shortcuts table and interaction gestures:
 * - Enter, Space, left click -> advance (or unhide if hidden)
 * - Right click -> toggle hide (except inside editor)
 * - Wheel up -> LOG (except over input/overlay)
 * - Esc -> close overlay, unhide, or stop generation
 * - Ctrl+K -> new session
 * - Hold Ctrl -> skip-hold-start / skip-hold-end
 * - F5 -> LOG only when stageFocused; otherwise native reload
 * - Letter shortcuts (A, F, R, H, X) suppressed in input/composing
 * - Function keys (F6, F7, F9) suppressed in input
 */

function isInputElement(el) {
  if (!el || typeof el.tagName !== 'string') return false;
  const tag = el.tagName.toUpperCase();
  if (tag === 'INPUT' || tag === 'TEXTAREA') return true;
  if (el.isContentEditable) return true;
  return false;
}

function isInteractive(el) {
  return Boolean(el?.closest?.('button, a[href], input, textarea, select, [role="button"], [contenteditable]'));
}

/**
 * Pure decision function mapping an input event and state context to an action string or null.
 *
 * @param {object} event Synthetic or native event
 * @param {object} [context]
 * @param {boolean} [context.inInput]
 * @param {boolean} [context.composing]
 * @param {boolean} [context.overlayOpen]
 * @param {boolean} [context.stageFocused]
 * @param {boolean} [context.hidden]
 * @param {boolean} [context.subtitleNone]
 * @returns {string | null} Action name, or null if unhandled
 */
export function decide(event, context = {}) {
  if (!event) return null;

  const inInput = !!context.inInput || !!context.playerInput;
  const composing = !!context.composing || !!event.isComposing || event.keyCode === 229;
  const overlayOpen = !!context.overlayOpen;
  const stageFocused = !!context.stageFocused;
  const hidden = !!context.hidden;
  const subtitleNone = !!context.subtitleNone;

  // Nothing is handled during IME composition
  if (composing) {
    return null;
  }

  // Handle blur (window loss of focus) to clear temporary skip
  if (event.type === 'blur') {
    return 'skip-hold-end';
  }

  // Handle Ctrl release for temporary skip
  if (event.type === 'keyup') {
    if (event.key === 'Control') {
      if (!inInput) {
        return 'skip-hold-end';
      }
    }
    return null;
  }

  // Handle keydown events
  if (event.type === 'keydown') {
    // Hold Ctrl for temporary skip
    if (event.key === 'Control') {
      if (event.repeat) return null;
      if (!inInput) {
        return 'skip-hold-start';
      }
      return null;
    }

    // Ctrl+K: new session outside composition
    if ((event.ctrlKey || event.metaKey) && (event.key === 'k' || event.key === 'K')) {
      return 'new_session';
    }

    // Escape: close overlay, unhide, or stop generation
    if (event.key === 'Escape') {
      if (overlayOpen) {
        return 'close_overlay';
      }
      if (hidden) {
        return 'unhide';
      }
      return 'stop';
    }

    if (overlayOpen) return null;

    // Enter & Space: advance dialogue outside input; restore window if hidden
    if (event.key === 'Enter' || event.key === ' ') {
      if (isInteractive(event.target)) return null;
      if (hidden) return 'unhide';
      if (inInput) {
        // Native editor handling (Enter sends, Shift+Enter newlines, Space types space)
        return null;
      }
      return 'advance';
    }

    // Function keys
    if (event.key === 'F5') {
      // F5 is LOG only when stage is focused; otherwise allow browser reload
      if (stageFocused) {
        return 'log';
      }
      return null;
    }

    if (event.key === 'F6') {
      if (inInput) return null;
      return 'toggle_auto';
    }

    if (event.key === 'F7') {
      if (inInput) return null;
      return 'toggle_skip';
    }

    if (event.key === 'F8') {
      // Settings: available outside IME composition, preserves draft
      return 'settings';
    }

    if (event.key === 'F9') {
      if (inInput) return null;
      return 'replay_voice';
    }

    // Letter shortcuts: suppressed if modifier keys (Ctrl/Alt/Meta) are active, or when in input
    if (event.ctrlKey || event.altKey || event.metaKey) {
      return null;
    }

    if (inInput) {
      return null;
    }

    const key = event.key ? event.key.toLowerCase() : '';
    switch (key) {
      case 'a':
        return 'toggle_auto';
      case 'f':
        return 'toggle_skip';
      case 'r':
        return 'log';
      case 'h':
        return 'toggle_hide';
      case 'x':
        if (subtitleNone) return null;
        return 'swap_languages';
      default:
        return null;
    }
  }

  // Right click (contextmenu)
  if (overlayOpen) return null;
  if (event.type === 'contextmenu') {
    if (inInput) {
      // Keep native context menu inside editor
      return null;
    }
    return 'toggle_hide';
  }

  // Wheel up -> LOG
  if (event.type === 'wheel') {
    if (event.deltaY < 0) {
      // Scrolled up
      if (inInput || overlayOpen) {
        return null;
      }
      return 'log';
    }
    return null;
  }

  // Left click
  if (event.type === 'click') {
    if (event.button !== undefined && event.button !== 0) {
      return null;
    }
    if (hidden && !isInteractive(event.target)) return 'unhide';
    if (inInput) {
      return null;
    }

    const t = event.target;
    if (t) {
      if (isInputElement(t)) return null;

      if (typeof t.closest === 'function') {
        if (t.closest('input, textarea, [contenteditable="true"], .adv-player-input-wrap')) return null;
        if (t.closest('.adv-quick-menu, .adv-quick-menu-strip, .adv-layer-quickmenu')) return null;
        if (t.closest('.adv-error-actions')) return null;
        if (t.closest('.adv-shell') && !t.closest('.adv-stage')) return null;
      }
      if (t.classList?.contains?.('adv-shell')) return null;
      if (t.classList?.contains?.('adv-quick-menu') || t.classList?.contains?.('adv-quick-menu-strip')) return null;
    }

    if (hidden) {
      return 'unhide';
    }
    return 'advance';
  }

  return null;
}

/**
 * Creates shortcut and gesture event listeners on a target element (e.g. window or stage).
 *
 * @param {EventTarget} target
 * @param {object} options
 * @param {() => object} options.getContext
 * @param {(action: string, event: Event) => void} options.onAction
 * @returns {object} { destroy }
 */
export function createShortcuts(target, { getContext, onAction } = {}) {
  function handleEvent(event) {
    const baseContext = getContext?.() || {};
    const targetEl = event.target;

    const inInput = baseContext.inInput ?? isInputElement(targetEl);
    let stageFocused = baseContext.stageFocused;
    if (typeof stageFocused === 'function') {
      stageFocused = stageFocused();
    }
    if (stageFocused === undefined && typeof document !== 'undefined') {
      const stageEl = baseContext.stage || document.querySelector?.('.adv-stage') || (typeof target.querySelector === 'function' ? target.querySelector('.adv-stage') : null);
      const activeEl = document.activeElement;
      if (stageEl && activeEl) {
        stageFocused = (activeEl === stageEl) || (typeof stageEl.contains === 'function' && stageEl.contains(activeEl));
      } else {
        stageFocused = false;
      }
    }

    const context = {
      ...baseContext,
      inInput,
      stageFocused: !!stageFocused,
    };

    const action = decide(event, context);
    if (action) {
      if (typeof event.preventDefault === 'function') {
        event.preventDefault();
      }
      onAction?.(action, event);
    }
  }

  const events = ['keydown', 'keyup', 'contextmenu', 'wheel', 'click', 'blur'];
  for (const name of events) {
    target.addEventListener(name, handleEvent, name === 'wheel' ? { passive: false } : undefined);
  }

  const windowBlurHandler = (target !== globalThis && typeof globalThis.addEventListener === 'function')
    ? handleEvent
    : null;
  if (windowBlurHandler) {
    globalThis.addEventListener('blur', windowBlurHandler);
  }

  function destroy() {
    for (const name of events) {
      target.removeEventListener(name, handleEvent);
    }
    if (windowBlurHandler) {
      globalThis.removeEventListener('blur', windowBlurHandler);
    }
  }

  return {
    destroy,
  };
}
