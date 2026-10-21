/**
 * bg_panel.js - ADV 背景 overlay panel.
 *
 * Picks one stage background plus a time-of-day choice. Every click applies at
 * once (preview) and writes both session settings; Esc or the close button ends
 * the panel. Tiles and time buttons are real <button>s, and the active choice
 * carries aria-pressed="true".
 */

import { normalizeBackgroundSelection, resolveVariant, timeOfDay } from './background.js';

const TIMES = [['auto', '自动'], ['day', '白天'], ['evening', '傍晚'], ['night', '夜晚']];
const SKY_ID = '';
const SKY_NAME = '无（天空）';

export function createBgPanel(container, {
  getList = () => [],
  getSelection = () => ({ id: SKY_ID, time: 'auto' }),
  saveSettings = () => {},
  onPreview = () => {},
  onOpen,
  onClose,
  now = () => new Date(),
  documentObj,
} = {}) {
  const doc = documentObj || container?.ownerDocument || globalThis.document;
  const disposers = [];
  const listen = (el, type, fn) => {
    if (!el?.addEventListener) return;
    el.addEventListener(type, fn);
    disposers.push(() => el.removeEventListener(type, fn));
  };

  let isOpen = false;
  let savedFocus = null;
  let selection = { id: SKY_ID, time: 'auto' };
  const tiles = [];

  const overlay = doc.createElement('div');
  overlay.className = 'adv-bg-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-label', '舞台背景');
  overlay.hidden = true;
  overlay.setAttribute('hidden', '');

  const backdrop = doc.createElement('div');
  backdrop.className = 'adv-bg-backdrop';
  overlay.appendChild(backdrop);

  const dialog = doc.createElement('div');
  dialog.className = 'adv-bg-dialog';
  overlay.appendChild(dialog);

  listen(overlay, 'click', event => event.stopPropagation());
  listen(backdrop, 'click', () => close());

  const header = doc.createElement('header');
  header.className = 'adv-bg-header';
  const titleGroup = doc.createElement('div');
  const title = doc.createElement('h2');
  title.className = 'adv-bg-title';
  title.textContent = '背景';
  const subtitle = doc.createElement('span');
  subtitle.className = 'adv-bg-subtitle';
  subtitle.textContent = '舞台背景与时间';
  titleGroup.appendChild(title);
  titleGroup.appendChild(subtitle);

  const closeButton = doc.createElement('button');
  closeButton.type = 'button';
  closeButton.className = 'adv-bg-close';
  closeButton.setAttribute('aria-label', '关闭背景设置');
  closeButton.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
  listen(closeButton, 'click', () => close());

  header.appendChild(titleGroup);
  header.appendChild(closeButton);
  dialog.appendChild(header);

  const body = doc.createElement('div');
  body.className = 'adv-bg-body';
  dialog.appendChild(body);

  const timeSection = doc.createElement('section');
  timeSection.className = 'adv-bg-section';
  const timeTitle = doc.createElement('h3');
  timeTitle.className = 'adv-bg-section-title';
  timeTitle.textContent = '时间';
  const timeRow = doc.createElement('div');
  timeRow.className = 'adv-bg-times';
  timeRow.setAttribute('role', 'group');
  timeRow.setAttribute('aria-label', '背景时间');
  timeSection.appendChild(timeTitle);
  timeSection.appendChild(timeRow);
  body.appendChild(timeSection);

  const timeButtons = TIMES.map(([value, label]) => {
    const button = doc.createElement('button');
    button.type = 'button';
    button.className = 'adv-bg-time';
    button.dataset.time = value;
    button.setAttribute('aria-pressed', 'false');
    button.textContent = label;
    listen(button, 'click', () => apply({ id: selection.id, time: value }));
    timeRow.appendChild(button);
    return button;
  });

  const gridSection = doc.createElement('section');
  gridSection.className = 'adv-bg-section';
  const gridTitle = doc.createElement('h3');
  gridTitle.className = 'adv-bg-section-title';
  gridTitle.textContent = '选择背景';
  const grid = doc.createElement('div');
  grid.className = 'adv-bg-grid';
  grid.setAttribute('role', 'group');
  grid.setAttribute('aria-label', '背景列表');
  gridSection.appendChild(gridTitle);
  gridSection.appendChild(grid);
  body.appendChild(gridSection);

  const hint = doc.createElement('p');
  hint.className = 'adv-bg-hint';
  hint.textContent = '点击立即生效并保存到当前会话；按 Esc 关闭。';
  body.appendChild(hint);

  function buildTiles() {
    tiles.length = 0;
    grid.replaceChildren();
    const items = [{ id: SKY_ID, name: SKY_NAME, sky: true },
      ...(Array.isArray(getList()) ? getList() : [])];
    for (const item of items) {
      if (!item || typeof item.id !== 'string') continue;
      const tile = doc.createElement('button');
      tile.type = 'button';
      tile.className = 'adv-bg-tile';
      tile.dataset.background = item.id;
      tile.setAttribute('aria-pressed', 'false');
      let thumb = null;
      if (item.sky) {
        thumb = doc.createElement('span');
        thumb.className = 'adv-bg-swatch';
      } else {
        thumb = doc.createElement('img');
        thumb.className = 'adv-bg-thumb';
        thumb.setAttribute('alt', '');
      }
      const name = doc.createElement('span');
      name.className = 'adv-bg-name';
      name.textContent = String(item.name || item.id);
      tile.appendChild(thumb);
      tile.appendChild(name);
      tile.onclick = () => apply({ id: item.id, time: selection.time });
      grid.appendChild(tile);
      tiles.push({ tile, thumb, item });
    }
  }

  function refresh() {
    const effective = selection.time === 'auto' ? timeOfDay(now()) : selection.time;
    for (const button of timeButtons) {
      const on = button.dataset.time === selection.time;
      button.setAttribute('aria-pressed', String(on));
      button.classList.toggle('is-selected', on);
    }
    for (const entry of tiles) {
      const on = entry.item.id === selection.id;
      entry.tile.setAttribute('aria-pressed', String(on));
      entry.tile.classList.toggle('is-selected', on);
      if (entry.item.sky) {
        entry.thumb.dataset.time = effective;
        continue;
      }
      const found = resolveVariant(entry.item, effective);
      if (found) entry.thumb.src = found.url;
      else if (entry.thumb.removeAttribute) entry.thumb.removeAttribute('src');
      else entry.thumb.src = '';
    }
  }

  function apply(next) {
    selection = normalizeBackgroundSelection(next);
    saveSettings?.({ advBackground: selection.id, advBgTime: selection.time });
    onPreview?.(selection);
    refresh();
  }

  listen(dialog, 'keydown', event => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = dialog.querySelectorAll('button').filter(node => !node.disabled && node.tabIndex >= 0);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable.at(-1);
    if (!dialog.contains(doc.activeElement) ||
      (event.shiftKey && doc.activeElement === first) ||
      (!event.shiftKey && doc.activeElement === last)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    }
  });

  function focusFirstControl() {
    const selected = tiles.find(entry => entry.item.id === selection.id)?.tile;
    (selected || tiles[0]?.tile || closeButton).focus?.();
  }

  function open() {
    if (isOpen) return;
    isOpen = true;
    savedFocus = doc.activeElement;
    selection = normalizeBackgroundSelection(getSelection?.());
    buildTiles();
    refresh();
    overlay.hidden = false;
    overlay.removeAttribute('hidden');
    onOpen?.();
    focusFirstControl();
  }

  function close() {
    if (!isOpen) return;
    isOpen = false;
    overlay.hidden = true;
    overlay.setAttribute('hidden', '');
    onClose?.();
    if (savedFocus?.isConnected && typeof savedFocus.focus === 'function') savedFocus.focus();
  }

  if (container) container.appendChild(overlay);

  return {
    element: overlay,
    dialog,
    open,
    close,
    refresh,
    get isOpen() { return isOpen; },
    get selection() { return { ...selection }; },
    destroy() {
      for (const off of disposers.splice(0)) off();
      isOpen = false;
      tiles.length = 0;
      for (const tile of [...grid.querySelectorAll('button')]) tile.onclick = null;
      overlay.remove();
    },
  };
}
