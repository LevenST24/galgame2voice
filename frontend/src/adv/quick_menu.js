/**
 * quick_menu.js - ADV quick menu component with model status indicator.
 */

/**
 * Creates the quick menu component inside container.
 *
 * @param {HTMLElement} container
 * @param {object} options
 * @param {(action: string, data?: any) => void} options.onAction
 * @returns {object} { element, setToggle, setEnabled, setModelStatus, destroy }
 */
export function createQuickMenu(container, { onAction } = {}) {
  const eventDisposers = [];
  function listen(el, type, handler) {
    el.addEventListener(type, handler);
    eventDisposers.push(() => el.removeEventListener(type, handler));
  }

  const strip = document.createElement('div');
  strip.className = 'adv-quick-menu-strip';

  const element = document.createElement('nav');
  element.className = 'adv-quick-menu';
  element.setAttribute('aria-label', '快捷操作菜单');

  strip.appendChild(element);

  // Stop clicks on quick menu or strip from advancing dialogue
  listen(strip, 'click', (e) => {
    e.stopPropagation();
  });
  listen(element, 'click', (e) => {
    e.stopPropagation();
  });

  const track = document.createElement('div');
  track.className = 'adv-qm-track';

  const buttons = new Map();

  function makeButton({ id, action, label, title, isToggle, iconSvg }) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `adv-qm-btn adv-qm-btn-${id}`;
    btn.dataset.action = action;
    btn.title = title;
    if (isToggle) {
      btn.setAttribute('aria-pressed', 'false');
    }

    const iconSpan = document.createElement('span');
    iconSpan.className = 'adv-qm-icon';
    iconSpan.setAttribute('aria-hidden', 'true');
    iconSpan.innerHTML = iconSvg;

    const textSpan = document.createElement('span');
    textSpan.className = 'adv-qm-text';
    textSpan.textContent = label;

    btn.appendChild(iconSpan);
    btn.appendChild(textSpan);

    listen(btn, 'click', (e) => {
      e.stopPropagation();
      onAction?.(action);
    });

    buttons.set(action, btn);
    if (action === 'replay_voice') {
      buttons.set('voice', btn);
    }
    return btn;
  }

  // Icons (original clean SVGs)
  const icons = {
    auto: '<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 14.5v-9l6 4.5-6 4.5z" fill="currentColor"/></svg>',
    skip: '<svg viewBox="0 0 24 24"><path d="M4 18l8.5-6L4 6v12zm9-12v12l8.5-6L13 6z" fill="currentColor"/></svg>',
    log: '<svg viewBox="0 0 24 24"><path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z" fill="currentColor"/></svg>',
    voice: '<svg viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z" fill="currentColor"/></svg>',
    hide: '<svg viewBox="0 0 24 24"><path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z" fill="currentColor"/></svg>',
    config: '<svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z" fill="currentColor"/></svg>',
    background: '<svg viewBox="0 0 24 24"><path d="M21 4H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h18c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 14H3V6h18v12zM8.5 14.5L11 11l3.5 4.5 2-2.5L20 18H4l4.5-3.5z" fill="currentColor"/><circle cx="8" cy="8.5" r="1.8" fill="currentColor"/></svg>',
  };

  const btnAuto = makeButton({ id: 'auto', action: 'auto', label: 'AUTO', title: '自动播放 (A / F6)', isToggle: true, iconSvg: icons.auto });
  const btnSkip = makeButton({ id: 'skip', action: 'skip', label: 'SKIP', title: '快进模式 (F / F7 / 按住Ctrl)', isToggle: true, iconSvg: icons.skip });
  const btnLog = makeButton({ id: 'log', action: 'log', label: 'LOG', title: '历史台词 (R / F5 / 滚轮向上)', isToggle: false, iconSvg: icons.log });
  const btnVoice = makeButton({ id: 'voice', action: 'replay_voice', label: 'VOICE', title: '语音重播 (F9)', isToggle: false, iconSvg: icons.voice });
  const btnHide = makeButton({ id: 'hide', action: 'hide', label: 'HIDE', title: '隐藏窗口 (H / 右键)', isToggle: false, iconSvg: icons.hide });
  const btnConfig = makeButton({ id: 'config', action: 'config', label: 'CONFIG', title: '设置 (F8)', isToggle: false, iconSvg: icons.config });
  const btnBackground = makeButton({ id: 'background', action: 'background', label: '背景', title: '舞台背景与时间', isToggle: false, iconSvg: icons.background });

  // Model status indicator point
  const statusBtn = document.createElement('button');
  statusBtn.type = 'button';
  statusBtn.className = 'adv-qm-status';
  statusBtn.dataset.action = 'model_status';
  statusBtn.title = '模型状态 (点击打开设置)';
  statusBtn.setAttribute('aria-label', '模型状态: 未就绪');

  const statusDot = document.createElement('span');
  statusDot.className = 'adv-qm-status-dot state-ok';

  const statusLabel = document.createElement('span');
  statusLabel.className = 'adv-qm-status-label';
  statusLabel.textContent = 'Model';

  statusBtn.appendChild(statusDot);
  statusBtn.appendChild(statusLabel);

  listen(statusBtn, 'click', (e) => {
    e.stopPropagation();
    onAction?.('model_status');
  });

  track.appendChild(btnAuto);
  track.appendChild(btnSkip);
  track.appendChild(btnLog);
  track.appendChild(btnVoice);
  track.appendChild(btnBackground);
  track.appendChild(btnHide);
  track.appendChild(btnConfig);
  track.appendChild(statusBtn);

  element.appendChild(track);

  if (container) {
    container.appendChild(strip);
  }

  function setToggle(type, on) {
    const btn = type === 'auto' ? btnAuto : type === 'skip' ? btnSkip : null;
    if (!btn) return;
    const active = !!on;
    btn.setAttribute('aria-pressed', String(active));
    btn.classList.toggle('is-active', active);
  }

  function setEnabled(action, bool) {
    const btn = buttons.get(action);
    if (!btn) return;
    btn.disabled = !bool;
  }

  function setModelStatus({ label, title, state: modelState = 'ok' } = {}) {
    const displayLabel = label ?? 'Model';
    statusLabel.textContent = displayLabel;

    statusDot.classList.remove('state-ok', 'state-busy', 'state-error');
    statusDot.classList.add(`state-${modelState}`);

    statusBtn.setAttribute('aria-label', `模型状态: ${displayLabel} (${modelState})`);
    statusBtn.title = title || displayLabel;
  }

  function setHidden(bool) {
    const isHidden = !!bool;
    strip.classList.toggle('is-hidden', isHidden);
    element.classList.toggle('is-hidden', isHidden);
  }

  function setRevealed(bool) {
    element.classList.toggle('is-revealed', !!bool);
  }

  function destroy() {
    for (const off of eventDisposers.splice(0)) off();
    if (strip.parentNode) {
      strip.parentNode.removeChild(strip);
    } else if (element.parentNode) {
      element.parentNode.removeChild(element);
    }
  }

  return {
    element,
    strip,
    setToggle,
    setEnabled,
    setModelStatus,
    setHidden,
    setRevealed,
    destroy,
  };
}
