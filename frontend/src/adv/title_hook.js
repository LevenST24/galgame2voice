import { createTitleScreen } from './title.js';

let screen = null;

/** Called once after startup authentication/sync: shows the title screen over the ADV stage.
 * The title's look lives in title.js and title.css only, so a new design replaces those two files.
 * actions is the public API from actions.js (startNew, continueLatest, loadSession, ...).
 */
export function showTitle(actions, { documentObj = document } = {}) {
  const root = documentObj.querySelector('.adv-layer-overlay');
  if (!root) return null;
  screen?.destroy();
  screen = createTitleScreen(root, { actions, documentObj });
  screen.open();
  return screen;
}
