import { createTitleScreen } from './title.js';

let screen = null;

/** Called once after startup authentication/sync: shows the title screen over the ADV stage.
 * The title's look lives in title.js and title.css only, so a new design replaces those two files.
 * actions is the public API from actions.js (startNew, continueLatest, loadSession, ...).
 */
export function showTitle(actions, { documentObj = document, matchMedia = query => globalThis.matchMedia?.(query) } = {}) {
  const root = documentObj.querySelector('.adv-layer-overlay');
  if (!root) return null;
  screen?.destroy();
  let animation = null;
  let revision = 0;
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  const motionChange = event => { if (event.matches) animation?.cancel(); };
  if (motion?.addEventListener) motion.addEventListener('change', motionChange);
  else motion?.addListener?.(motionChange);
  const wrapped = { ...actions };
  for (const name of ['startNew', 'continueLatest', 'loadSession']) {
    wrapped[name] = (...args) => {
      const element = screen.element;
      const token = ++revision;
      const result = actions[name]?.(...args);
      if (!matchMedia('(prefers-reduced-motion: reduce)')?.matches && element.animate) {
        element.hidden = false;
        element.removeAttribute('hidden');
        element.inert = true;
        element.style.pointerEvents = 'none';
        animation = element.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 300 });
        animation.finished.catch(() => {}).finally(() => { if (token === revision) element.hidden = true; });
      }
      return result;
    };
  }
  screen = createTitleScreen(root, { actions: wrapped, documentObj, matchMedia });
  const { open, destroy } = screen;
  screen.open = () => {
    ++revision; animation?.cancel(); animation = null;
    screen.element.inert = false;
    screen.element.style.pointerEvents = '';
    open();
  };
  screen.destroy = () => {
    ++revision; animation?.cancel();
    if (motion?.removeEventListener) motion.removeEventListener('change', motionChange);
    else motion?.removeListener?.(motionChange);
    destroy();
  };
  screen.open();
  return screen;
}
