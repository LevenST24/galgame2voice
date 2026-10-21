/**
 * stage.js - ADV Stage and viewport letterboxing container.
 *
 * Implements 1920x1080 logical canvas scaled via CSS --u (never transform: scale).
 * Layer hierarchy:
 *   0: Shell (black letterbox)
 *  10: Background
 *  20: Sprite
 *  30: Sprite effects (fx)
 *  40: Textbox (message band, name plate, text column, VOICE display)
 *  50: Quick menu
 *  60: Overlays (LOG, drawer, dialogs)
 */

/**
 * Creates the ADV stage inside root.
 *
 * @param {HTMLElement} root
 * @param {object} [options]
 * @param {(event: MouseEvent) => void} [options.onLetterboxClick]
 * @returns {object} { shell, stage, background, sprite, fx, textbox, quickmenu, overlay, destroy }
 */
export function createStage(root, { onLetterboxClick, matchMedia = query => globalThis.matchMedia?.(query) } = {}) {
  const shell = document.createElement('div');
  shell.className = 'adv-shell';

  const stage = document.createElement('div');
  stage.className = 'adv-stage';
  const portrait = matchMedia('(orientation: portrait)');
  const rotate = () => stage.classList.toggle('is-portrait', Boolean(portrait?.matches));
  rotate();
  if (portrait?.addEventListener) portrait.addEventListener('change', rotate);
  else portrait?.addListener?.(rotate);
  stage.setAttribute('role', 'region');
  stage.setAttribute('aria-label', 'ADV 舞台');

  const background = document.createElement('div');
  background.className = 'adv-layer adv-layer-bg';
  background.dataset.layer = '10';

  const sprite = document.createElement('div');
  sprite.className = 'adv-layer adv-layer-sprite';
  sprite.dataset.layer = '20';

  const fx = document.createElement('div');
  fx.className = 'adv-layer adv-layer-fx';
  fx.dataset.layer = '30';

  const textbox = document.createElement('div');
  textbox.className = 'adv-layer adv-layer-textbox';
  textbox.dataset.layer = '40';

  const quickmenu = document.createElement('div');
  quickmenu.className = 'adv-layer adv-layer-quickmenu';
  quickmenu.dataset.layer = '50';

  const overlay = document.createElement('div');
  overlay.className = 'adv-layer adv-layer-overlay';
  overlay.dataset.layer = '60';

  stage.setAttribute('tabindex', '-1');

  function handleStagePointerDown(e) {
    if (e.target?.closest?.('.adv-layer-overlay')) {
      return;
    }
    const tag = e.target?.tagName?.toUpperCase?.();
    if (tag !== 'INPUT' && tag !== 'TEXTAREA' && tag !== 'BUTTON' && tag !== 'SELECT') {
      stage.focus?.();
    }
  }
  stage.addEventListener('pointerdown', handleStagePointerDown);

  stage.appendChild(background);
  stage.appendChild(sprite);
  stage.appendChild(fx);
  stage.appendChild(textbox);
  stage.appendChild(quickmenu);
  stage.appendChild(overlay);

  shell.appendChild(stage);

  if (root) {
    root.appendChild(shell);
  }

  function handleShellClick(e) {
    // Check if the click target is the letterbox area outside the stage
    if (e.target === shell || !stage.contains(e.target)) {
      e.stopPropagation();
      onLetterboxClick?.(e);
    }
  }

  shell.addEventListener('click', handleShellClick);

  function destroy() {
    if (portrait?.removeEventListener) portrait.removeEventListener('change', rotate);
    else portrait?.removeListener?.(rotate);
    stage.removeEventListener('pointerdown', handleStagePointerDown);
    shell.removeEventListener('click', handleShellClick);
    if (shell.parentNode) {
      shell.parentNode.removeChild(shell);
    }
  }

  return {
    shell,
    stage,
    background,
    sprite,
    fx,
    textbox,
    quickmenu,
    overlay,
    destroy,
  };
}
