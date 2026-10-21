/**
 * title.js - ADV 标题画面（占位方案 B，极简现代）。
 *
 * createTitleScreen(root, { actions, documentObj, matchMedia }) 在舞台内挂载
 * 标题层：菜单、两层漂移云带与读取存档页。全部几何以舞台单位 --u 表达
 * （title.css），模块自身不做缩放或 letterbox。样式文件 title.css 由后续
 * 接线负责引入，本模块不 import 其它 adv 文件，方便并行改动时整体替换。
 */

const PAGE_SIZE = 6;
const MENU_ARROWS = {
  ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1,
};
const GRID_ARROWS = {
  ArrowRight: 1, ArrowLeft: -1, ArrowDown: 3, ArrowUp: -3,
};
const GRID_COLUMNS = 3;

const MENU = [
  { key: 'start', label: '开始', en: 'NEW' },
  { key: 'continue', label: '继续', en: 'CONTINUE' },
  { key: 'load', label: '读取', en: 'LOAD' },
  { key: 'cast', label: '角色', en: 'CAST' },
  { key: 'config', label: '设置', en: 'CONFIG' },
];

const CLOUDS = {
  far: [
    [60, 150, 260, 30], [520, 96, 190, 22], [880, 210, 300, 26],
    [1300, 120, 220, 24], [1640, 250, 180, 20],
  ],
  near: [
    [-40, 470, 420, 46], [430, 560, 300, 34], [900, 430, 520, 54],
    [1400, 540, 360, 40], [1700, 450, 300, 30],
  ],
};

const pad2 = value => String(value).padStart(2, '0');

function toDate(value) {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === 'number') {
    const fromNumber = new Date(value);
    return Number.isNaN(fromNumber.getTime()) ? null : fromNumber;
  }
  if (typeof value === 'string' && value) {
    const iso = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(value);
    if (iso && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(value)) {
      return new Date(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3]),
        Number(iso[4]), Number(iso[5]));
    }
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }
  return null;
}

function formatStamp(value) {
  const date = toDate(value);
  if (!date) return '';
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
    + ` ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

function timeValue(session) {
  const date = toDate(session?.updatedAt);
  return date ? date.getTime() : 0;
}

export function createTitleScreen(root, {
  actions = {},
  documentObj = document,
  matchMedia = globalThis.matchMedia,
} = {}) {
  const doc = documentObj || root?.ownerDocument || globalThis.document;
  const {
    startNew = () => {},
    continueLatest = () => {},
    loadSession = () => {},
    openCharacters = () => {},
    openSettings = () => {},
    listSessions = () => [],
  } = actions || {};

  const disposals = [];
  let mql = null;
  let motionOff = null;
  try {
    mql = typeof matchMedia === 'function'
      ? matchMedia('(prefers-reduced-motion: reduce)')
      : null;
  } catch {
    mql = null;
  }

  const listen = (node, type, handler) => {
    node.addEventListener(type, handler);
    disposals.push(() => node.removeEventListener(type, handler));
  };

  let openState = false;
  let loadOpen = false;
  let destroyed = false;
  let sealed = false;
  let sequence = 0;
  let sessions = [];
  let page = 0;
  let lastActivated = null;
  let currentSlots = [];

  const el = (tag, className, text) => {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const sized = (node, styles) => {
    for (const [property, number] of styles) {
      node.style.setProperty(property, `calc(${number} * var(--u))`);
    }
    return node;
  };

  // ---- 标题菜单 ----

  const screen = el('section', 'adv-title-screen');
  screen.hidden = true;
  screen.setAttribute('hidden', '');

  ['far', 'near'].forEach(which => {
    const layer = el('div', `adv-title-cloud adv-title-cloud-${which}`);
    layer.setAttribute('aria-hidden', 'true');
    const strip = el('div', `adv-title-strip adv-title-strip-${which}`);
    for (const [left, top, width, height] of CLOUDS[which]) {
      for (let copy = 0; copy < 2; copy += 1) {
        strip.appendChild(sized(el('span', 'adv-title-blob'), [
          ['--cl', left + copy * 1920], ['--ct', top], ['--cw', width], ['--ch', height],
        ]));
      }
    }
    layer.appendChild(strip);
    screen.appendChild(layer);
  });

  screen.appendChild(el('div', 'adv-title-horizon'));

  const brand = el('div', 'adv-title-brand');
  brand.appendChild(el('h1', 'adv-title-logo', 'galgame2voice'));
  brand.appendChild(el('p', 'adv-title-sub', '与 AI 角色的对话剧场'));
  screen.appendChild(brand);

  screen.appendChild(el('div', 'adv-title-rule'));

  const menu = el('nav', 'adv-title-menu');
  menu.setAttribute('aria-label', '主菜单');
  const items = MENU.map(spec => {
    const item = el('button', 'adv-title-item');
    item.type = 'button';
    item.dataset.key = spec.key;
    item.appendChild(el('span', 'adv-title-item-label', spec.label));
    item.appendChild(el('span', 'adv-title-item-en', spec.en));
    item.setAttribute('aria-disabled', 'false');
    listen(item, 'click', event => {
      lastActivated = null;
      if (lastActivatedWas(event.target)) return;
      activate(event.target);
    });
    listen(item, 'keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        activate(event.target);
        return;
      }
      const step = MENU_ARROWS[event.key];
      if (step === undefined) return;
      const at = items.indexOf(event.target);
      if (at < 0) return;
      event.preventDefault();
      items[(at + step + items.length) % items.length].focus({ preventScroll: true });
    });
    menu.appendChild(item);
    return item;
  });
  const continueItem = items.find(item => item.dataset.key === 'continue');
  const loadItem = items.find(item => item.dataset.key === 'load');
  screen.appendChild(menu);

  screen.appendChild(el('p', 'adv-title-hint', '← → 选择 · Enter 确认'));

  // ---- 读取存档 ----

  const load = el('section', 'adv-title-load');
  load.setAttribute('role', 'dialog');
  load.setAttribute('aria-modal', 'true');
  load.setAttribute('aria-label', '读取存档');
  load.hidden = true;

  const head = el('header', 'adv-title-load-head');
  const heading = el('h2', 'adv-title-load-title', '读取存档');
  heading.appendChild(el('span', 'adv-title-load-tip', '方向键选择 · Enter 读取 · Esc 返回'));
  head.appendChild(heading);
  const backBtn = el('button', 'adv-title-back', '返回');
  backBtn.type = 'button';
  listen(backBtn, 'click', () => {
    lastActivated = null;
    closeLoad();
  });
  head.appendChild(backBtn);
  load.appendChild(head);

  const grid = el('div', 'adv-title-grid');
  grid.setAttribute('role', 'group');
  grid.setAttribute('aria-label', '存档列表');
  const emptyMessage = el('p', 'adv-title-empty', '还没有存档');
  emptyMessage.hidden = true;
  grid.appendChild(emptyMessage);
  load.appendChild(grid);

  const pager = el('div', 'adv-title-pager');
  const prevBtn = el('button', 'adv-title-page-prev', '上一页');
  const info = el('span', 'adv-title-page-info');
  const nextBtn = el('button', 'adv-title-page-next', '下一页');
  for (const button of [prevBtn, nextBtn]) {
    button.type = 'button';
    listen(button, 'click', () => {
      lastActivated = null;
      if (button.disabled) return;
      setPage(page + (button === nextBtn ? 1 : -1));
    });
  }
  prevBtn.disabled = true;
  prevBtn.setAttribute('aria-disabled', 'true');
  pager.appendChild(prevBtn);
  pager.appendChild(info);
  pager.appendChild(nextBtn);
  load.appendChild(pager);

  screen.appendChild(load);

  if (root && typeof root.appendChild === 'function') root.appendChild(screen);

  // ---- 行为 ----

  function lastActivatedWas(node) {
    if (lastActivated !== node) return false;
    lastActivated = null;
    return true;
  }

  function canActivate(node) {
    return openState && !destroyed && !node.disabled;
  }

  function activate(node) {
    if (!canActivate(node)) return;
    lastActivated = node;
    const key = node.dataset.key;
    if (key === 'start') {
      close();
      startNew();
    } else if (key === 'continue') {
      close();
      continueLatest();
    } else if (key === 'load') {
      openLoad();
    } else if (key === 'cast') {
      openCharacters();
    } else if (key === 'config') {
      openSettings();
    }
  }

  function pageCount() {
    return Math.max(1, Math.ceil(sessions.length / PAGE_SIZE));
  }

  function setPagerButton(button, disabled) {
    button.disabled = disabled;
    button.setAttribute('aria-disabled', String(disabled));
  }

  function setPage(next) {
    page = Math.min(Math.max(next, 0), pageCount() - 1);
    renderGrid();
  }

  function makeSlot(session) {
    const slot = el('button', 'adv-title-slot');
    slot.type = 'button';
    slot.setAttribute('data-session', session.id);
    if (session.background) {
      const image = el('img', 'adv-title-thumb-image');
      image.setAttribute('alt', '');
      image.src = session.background;
      slot.appendChild(image);
    }
    const meta = el('span', 'adv-title-slot-meta');
    const names = el('span', 'adv-title-slot-names');
    names.appendChild(el('span', 'adv-title-slot-character', String(session.characterName ?? '')));
    names.appendChild(el('span', 'adv-title-slot-title', String(session.title ?? '')));
    meta.appendChild(names);
    meta.appendChild(el('span', 'adv-title-slot-line', String(session.lastLine ?? '')));
    meta.appendChild(el('span', 'adv-title-slot-date', formatStamp(session.updatedAt)));
    slot.appendChild(meta);
    slot.onclick = () => {
      lastActivated = null;
      if (lastActivatedWas(slot)) return;
      if (!canActivate(slot)) return;
      lastActivated = slot;
      close();
      loadSession(session.id);
    };
    disposals.push(() => { slot.onclick = null; });
    return slot;
  }

  function renderGrid() {
    grid.replaceChildren(emptyMessage);
    currentSlots = sessions.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE).map(makeSlot);
    for (const slot of currentSlots) grid.appendChild(slot);
    emptyMessage.hidden = sessions.length > 0;
    const total = pageCount();
    info.textContent = `第 ${page + 1} / ${total} 页 · 共 ${sessions.length} 个存档`;
    if (sessions.length) info.setAttribute('aria-current', 'true');
    else info.removeAttribute('aria-current');
    setPagerButton(prevBtn, page <= 0);
    setPagerButton(nextBtn, page >= total - 1);
  }

  function applyList(list, seq) {
    if (destroyed || seq !== sequence) return;
    const rows = (Array.isArray(list) ? list : [])
      .filter(session => session && session.id != null);
    sessions = rows
      .map((session, index) => ({ session, index, time: timeValue(session) }))
      .sort((a, b) => (b.time - a.time) || (a.index - b.index)
        || String(a.session.id).localeCompare(String(b.session.id)))
      .map(entry => entry.session);
    page = 0;
    renderGrid();
    const none = sessions.length === 0;
    continueItem.disabled = none;
    continueItem.setAttribute('aria-disabled', String(none));
  }

  function refresh() {
    const seq = ++sequence;
    let pending;
    try {
      pending = Promise.resolve(listSessions());
    } catch {
      applyList([], seq);
      return;
    }
    pending
      .then(list => applyList(list, seq))
      .catch(() => applyList([], seq));
  }

  function firstEnabledItem() {
    return items.find(item => !item.disabled) || null;
  }

  function focusItem(item) {
    (item || firstEnabledItem())?.focus({ preventScroll: true });
  }

  function openLoad() {
    loadOpen = true;
    load.hidden = false;
    screen.classList.add('is-load');
    renderGrid();
    (currentSlots[0] || backBtn).focus({ preventScroll: true });
  }

  function closeLoad() {
    loadOpen = false;
    load.hidden = true;
    screen.classList.remove('is-load');
    focusItem(loadItem);
  }

  function open() {
    if (openState || destroyed || sealed) return;
    openState = true;
    screen.hidden = false;
    screen.removeAttribute('hidden');
    refresh();
    if (!loadOpen) focusItem(firstEnabledItem());
  }

  function close() {
    if (!openState) return;
    openState = false;
    if (loadOpen) closeLoad();
    screen.hidden = true;
    screen.setAttribute('hidden', '');
  }

  function blurActive() {
    const node = doc.activeElement;
    if (node && node !== doc.body && typeof node.blur === 'function') node.blur();
  }

  listen(screen, 'keydown', event => {
    if (event.key !== 'Escape') return;
    if (loadOpen) {
      event.preventDefault();
      closeLoad();
      return;
    }
    if (openState && items.includes(doc.activeElement)) event.preventDefault();
  });

  listen(screen, 'pointerdown', () => screen.classList.remove('is-kbd'));

  listen(root, 'pointerdown', () => screen.classList.remove('is-kbd'));

  listen(root, 'keydown', event => {
    if (!openState) return;
    if (event.key === 'Enter') {
      const node = event.target;
      if (node === lastActivated) { lastActivated = null; return; }
      if (node?.tagName !== 'BUTTON' || typeof node.onclick !== 'function') return;
      if (screen.contains(node)) event.preventDefault();
      if (!node.isConnected) return;
      node.click();
      return;
    }
    if (event.key === 'Escape' && loadOpen) closeLoad();
  });

  listen(root, 'keydown', event => {
    if (!openState || event.defaultPrevented) return;
    const arrows = loadOpen ? GRID_ARROWS : MENU_ARROWS;
    const step = arrows[event.key];
    if (step === undefined) return;
    if (loadOpen) {
      if (!currentSlots.length) return;
      const at = currentSlots.indexOf(doc.activeElement);
      event.preventDefault();
      if (at < 0) {
        currentSlots[0].focus({ preventScroll: true });
        return;
      }
      currentSlots[(at + step + currentSlots.length) % currentSlots.length]
        .focus({ preventScroll: true });
      return;
    }
    const at = items.indexOf(doc.activeElement);
    if (at < 0) return;
    event.preventDefault();
    items[(at + step + items.length) % items.length].focus({ preventScroll: true });
  });

  listen(root, 'keydown', event => {
    if (!openState) return;
    if (event.key === 'Tab' || event.key.startsWith('Arrow') || event.key === 'Enter') {
      screen.classList.add('is-kbd');
    }
  });

  listen(root, 'click', event => {
    const node = event.target;
    if (lastActivated === node) { lastActivated = null; return; }
    if (lastActivated) return;
    if (!openState || !node || typeof node.onclick !== 'function') return;
    if (!node.isConnected) return;
    node.click();
  });

  function syncReducedMotion(matches) {
    screen.classList.toggle('is-reduced', Boolean(matches));
  }

  syncReducedMotion(mql?.matches);
  if (mql) {
    motionOff = event => syncReducedMotion(event.matches);
    if (typeof mql.addEventListener === 'function') {
      mql.addEventListener('change', motionOff);
      disposals.push(() => mql.removeEventListener('change', motionOff));
    } else if (typeof mql.addListener === 'function') {
      mql.addListener(motionOff);
      disposals.push(() => mql.removeListener(motionOff));
    }
  }

  return {
    element: screen,
    menuScreen: screen,
    loadScreen: load,
    open,
    close,
    get isOpen() { return openState; },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      openState = false;
      loadOpen = false;
      sessions = [];
      currentSlots = [];
      for (const item of items) item.onclick = null;
      for (const button of [backBtn, prevBtn, nextBtn]) button.onclick = null;
      for (const off of disposals.splice(0)) off();
      if (motionOff) {
        if (typeof mql.removeEventListener === 'function') mql.removeEventListener('change', motionOff);
        else if (typeof mql.removeListener === 'function') mql.removeListener(motionOff);
        motionOff = null;
      }
      sequence += 1;
      screen.remove();
    },
  };
}
