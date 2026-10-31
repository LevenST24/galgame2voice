/** Home is a transparent overlay on the active session's real ADV stage. */
import { canReplayLine } from './replay.js';

const PAGE_SIZE = 6;
export function formatHomeTime(value, now = new Date()) {
  const date = new Date(value);
  if (value == null || Number.isNaN(date.getTime())) return '';
  const day = d => Date.UTC(d.getFullYear(), d.getMonth(), d.getDate());
  const days = (day(now) - day(date)) / 86400000;
  const clock = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  if (days === 0) return `今天 ${clock}`;
  if (days === 1) return `昨天 ${clock}`;
  if (days > 1 && days <= 6) return `周${'日一二三四五六'[date.getDay()]}`;
  return `${date.getFullYear() === now.getFullYear() ? '' : `${date.getFullYear()}年`}${date.getMonth() + 1}月${date.getDate()}日`;
}

export function createTitleScreen(root, { actions = {}, documentObj = globalThis.document,
  matchMedia = globalThis.matchMedia, clock = globalThis, now = () => new Date() } = {}) {
  const doc = documentObj || root.ownerDocument;
  const stage = root.closest?.('.adv-stage');
  const screen = el('section', 'adv-title-screen');
  screen.setAttribute('aria-label', '首页'); screen.hidden = true;
  const top = el('header', 'home-topbar');
  const brand = el('div', 'home-wordmark');
  add(brand, el('span', '', 'Gal'), el('span', 'home-accent', '2'), el('span', '', 'Voice'));
  top.appendChild(brand);
  const tools = el('div', 'home-tools'); top.appendChild(tools);
  const pills = {};
  for (const kind of ['chat', 'voice']) {
    const wrap = el('div', 'home-pill-wrap');
    const button = btn('home-pill', '', () => togglePopover(kind));
    const dot = el('i', 'home-dot'); dot.setAttribute('aria-hidden', 'true');
    const text = el('span', 'home-pill-text');
    const short = el('span', 'home-pill-short', kind === 'chat' ? '对话' : '语音'); short.setAttribute('aria-hidden', 'true');
    button.replaceChildren(dot, text, short);
    button.setAttribute('aria-expanded', 'false');
    wrap.appendChild(button); tools.appendChild(wrap); pills[kind] = { wrap, button, dot, text };
  }
  tools.appendChild(btn('home-gear', '⚙', () => actions.openSettings?.(), '设置中心'));
  screen.appendChild(top);
  const pane = el('section', 'home-excerpt'); screen.appendChild(pane);
  const recents = el('section', 'home-recents'); screen.appendChild(recents);
  const load = el('section', 'adv-title-load'); load.hidden = true;
  load.setAttribute('aria-label', '全部会话');
  const head = el('header', 'adv-title-load-head');
  add(head, el('h2', '', '全部会话'), btn('adv-title-back', '← 返回', returnHome));
  const grid = el('div', 'adv-title-grid');
  const pager = el('div', 'adv-title-pager');
  const prev = btn('adv-title-page-prev', '上一页', () => setPage(page - 1));
  const info = el('span', 'adv-title-page-info');
  const next = btn('adv-title-page-next', '下一页', () => setPage(page + 1));
  add(pager, prev, info, next); add(load, head, grid, pager); screen.appendChild(load);
  root.appendChild(screen);
  let opened = false, destroyed = false, mode = 'home', page = 0, sessions = [], mainId = null;
  let recentOrder = [];
  let status = { modelLoaded: false, engine: {} }, textOnly = false, draft = null;
  let popup = null, popupButton = null, dropdown = null, dropdownButton = null;
  let timer = null, unsubscribe = null, revision = 0, previewRevision = 0, polling = null;
  let playing = false, playRevision = 0, listenButton = null, rendered = '';
  const failedAudio = new Set();
  const disposers = [];
  const stageControls = new Map();

  function add(node, ...children) { children.forEach(child => node.appendChild(child)); }
  function el(tag, cls = '', text) {
    const node = doc.createElement(tag); node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function btn(cls, text, action, label) {
    const node = el('button', cls); node.type = 'button'; node.appendChild(el('span', '', text));
    if (label) node.setAttribute('aria-label', label);
    node.onclick = event => {
      event.stopPropagation();
      if (!opened || node.disabled) return;
      if (popup && !popup.contains(node) && node !== popupButton) closePopup();
      if (dropdown && !dropdown.contains(node) && node !== dropdownButton) closeDropdown();
      action();
    };
    return node;
  }
  function focus(node) { node?.focus?.({ preventScroll: true }); }
  function main() { return sessions.find(s => s.id === mainId); }
  function chatOK() { return status.modelLoaded && status.hasModel !== false && status.model?.state === 'ok'; }
  function readSessions() {
    sessions = (actions.listSessions?.() || []).slice().sort((a, b) => new Date(b.updatedAt || 0) - new Date(a.updatedAt || 0));
    const active = actions.getActiveId?.();
    if (sessions.some(s => s.id === active)) mainId = active;
    else if (!sessions.some(s => s.id === mainId)) mainId = sessions[0]?.id ?? null;
  }
  function closePopup(restore = false) {
    popup?.remove(); popup = null; popupButton?.setAttribute('aria-expanded', 'false');
    if (restore) focus(popupButton); popupButton = null;
  }
  function closeDropdown(restore = false) {
    dropdown?.remove(); dropdown = null; dropdownButton?.setAttribute('aria-expanded', 'false');
    if (restore) focus(dropdownButton); dropdownButton = null;
  }
  function togglePopover(kind) {
    const was = popupButton === pills[kind].button;
    closePopup(); closeDropdown(); if (was) return;
    popupButton = pills[kind].button; popupButton.setAttribute('aria-expanded', 'true');
    popup = el('div', 'home-popover'); popup.setAttribute('role', 'group');
    add(popup, el('p', '', explanation(kind)), btn('home-link', '去设置', () => {
      closePopup(); actions.openSettings?.(kind === 'chat' ? 'models' : 'inference');
    }));
    pills[kind].wrap.appendChild(popup); focus(popup.querySelector('button'));
  }
  function explanation(kind) {
    if (kind === 'chat') return status.model?.label || '正在读取模型配置…';
    const engine = status.engine || {};
    return engine.state === 'ready' ? `语音服务已连接${engine.base_url || engine.address ? ` · ${engine.base_url || engine.address}` : ''}`
      : engine.message || '语音服务未连接，可以先用文字聊天';
  }
  function renderStatus() {
    const states = { chat: !status.modelLoaded ? ['warn', '对话 检查中'] : status.model?.readFailed ? ['warn', '对话 状态未确认'] : chatOK() ? ['ok', '对话 可用'] : ['bad', '对话 未设置'],
      voice: status.engine?.state === 'ready' ? ['ok', '语音 可用']
        : status.engine?.state === 'starting' ? ['warn', '语音 启动中']
        : status.engine?.state === 'loading_slow' ? ['warn', '语音 加载较慢']
        : status.engine?.state === 'checking' ? ['warn', '语音 待确认']
        : ['bad', '语音 未连接'] };
    for (const [kind, [color, text]] of Object.entries(states)) {
      pills[kind].dot.className = `home-dot ${color}`; pills[kind].text.textContent = text;
      pills[kind].button.setAttribute('aria-label', text);
    }
    if (popup) popup.querySelector('p').textContent = explanation(popupButton === pills.chat.button ? 'chat' : 'voice');
  }
  function enter(id = mainId) {
    close(); if (id != null) actions.loadSession?.(id); else actions.startNew?.();
  }
  function stopListen() {
    ++playRevision;
    if (playing) actions.stopExcerpt?.(); playing = false;
    if (listenButton) { listenButton.children[0].textContent = '▶ 试听这句'; listenButton.setAttribute('aria-pressed', 'false'); }
  }
  async function toggleListen(excerpt) {
    if (playing) { stopListen(); return; }
    if (!canReplayLine(excerpt.line, excerpt, actions.voiceEnabled?.() ?? true, now().getTime())) {
      listenButton.hidden = true; return;
    }
    playing = true; const token = ++playRevision;
    listenButton.children[0].textContent = '■ 停止'; listenButton.setAttribute('aria-pressed', 'true');
    let result;
    try { result = await actions.playExcerpt?.(excerpt); } catch { result = 'failed'; }
    if (!opened || token !== playRevision) return;
    playing = false; listenButton.children[0].textContent = '▶ 试听这句'; listenButton.setAttribute('aria-pressed', 'false');
    if (result === 'failed') { failedAudio.add(JSON.stringify(excerpt)); listenButton.hidden = true; }
  }
  function renderPane(force = false) {
    const session = main();
    const firstUse = status.modelLoaded && !status.model?.readFailed && !chatOK();
    const signature = JSON.stringify([mode, session, firstUse, status.engine?.state, textOnly,
      canReplayLine(session?.excerpt?.line, session?.excerpt, actions.voiceEnabled?.() ?? true, now().getTime())]);
    if (!force && rendered === signature) return;
    rendered = signature;
    pane.classList.toggle('is-reading', mode === 'home' && !firstUse);
    const hadFocus = pane.contains(doc.activeElement);
    stopListen(); closeDropdown(); pane.replaceChildren(); listenButton = null;
    if (mode === 'new') renderDraft();
    else if (firstUse) renderFirst();
    else {
      const engineState = status.engine?.state;
      const notice = engineState === 'ready' ? ''
        : engineState === 'starting' ? '语音引擎正在启动，可以先用文字聊天'
        : engineState === 'loading_slow' ? '语音引擎加载较慢，可以先用文字聊天'
        : engineState === 'checking' ? '正在核对语音引擎，可以先用文字聊天'
        : '语音引擎未连接，可以先用文字聊天';
      if (notice) pane.appendChild(el('p', 'home-notice', notice));
      pane.appendChild(el('p', 'home-label', '继续这一页'));
      const meta = el('p', 'home-meta');
      add(meta, el('b', '', session?.characterName || session?.title || '新对话'),
        el('span', 'home-time', session ? ` · ${formatHomeTime(session.updatedAt, now())}` : ''));
      pane.appendChild(meta);
      const excerpt = session?.excerpt?.zh ? session.excerpt : { zh: '从这一刻，开始你们的故事。' };
      if (excerpt.user) pane.appendChild(el('p', 'home-you', `你：${excerpt.user}`));
      pane.appendChild(el('blockquote', 'home-quote', `「${String(excerpt.zh || '').replace(/^「|」$/g, '')}」`));
      if (excerpt.ja) pane.appendChild(el('p', 'home-ja', excerpt.ja));
      const row = el('div', 'home-actions');
      row.appendChild(btn('home-primary home-continue', session?.hasUserMessages ? '接着聊 →' : '开始聊天', () => enter()));
      if (canReplayLine(excerpt.line, excerpt, actions.voiceEnabled?.() ?? true, now().getTime()) && !failedAudio.has(JSON.stringify(excerpt))) {
        listenButton = btn('home-link home-listen', '▶ 试听这句', () => toggleListen(excerpt));
        listenButton.setAttribute('aria-pressed', 'false'); row.appendChild(listenButton);
      }
      row.appendChild(btn('home-link home-new', '另开一段对话', openNew)); pane.appendChild(row);
    }
    if (hadFocus) focusPrimary();
  }
  function focusPrimary() { focus(pane.querySelector('.home-continue') || pane.querySelector('.home-step-pending') || pane.querySelector('button')); }
  function renderFirst() {
    // Keep the actual session's greeting available in the DOM while onboarding
    // replaces its visual excerpt (also used by portable initialization smoke).
    const excerpt = main()?.excerpt;
    if (excerpt?.zh) { const greeting = el('blockquote', 'home-greeting', excerpt.zh); greeting.hidden = true; pane.appendChild(greeting); }
    const heading = el('div', 'home-first-heading');
    heading.appendChild(el('p', 'home-label', '第一次使用'));
    if (main()?.hasUserMessages) {
      heading.appendChild(btn('home-link home-existing', '查看已有对话 →', () => enter()));
    }
    add(pane, heading, el('h1', 'home-first-title', '先做三件小事，就能开始聊天'));
    const done = [chatOK(), status.engine?.state === 'ready' || textOnly, Boolean(main()?.settings?.voiceProfileId)];
    const labels = ['连接对话模型', '连接语音引擎', '选一个角色'];
    const descriptions = ['让她能用文字回答你', '让她用自己的声音念出日语台词', '从已安装的角色里挑一位'];
    labels.forEach((label, i) => {
      const row = el('div', `home-step${done[i] ? ' is-done' : ''}`);
      const copy = el('div', 'home-step-copy');
      add(copy, el('b', '', label), el('p', '', descriptions[i]));
      add(row, el('span', 'home-step-mark', done[i] ? '✓' : ''), copy);
      if (i === 1) row.appendChild(btn('home-link home-text-only', textOnly ? '✓ 暂时只用文字' : '暂时只用文字', () => { textOnly = true; renderPane(true); }));
      row.appendChild(btn(`home-step-button${done[i] ? '' : ' home-step-pending'}`, i === 2 ? '选择' : '去设置', () => {
        if (i === 2) actions.openCharacters?.(); else actions.openSettings?.(i === 0 ? 'models' : 'inference');
      }));
      pane.appendChild(row);
    });
    // Optional voice/character steps never block entry once the model is ready.
  }
  function openNew() {
    closePopup(); stopListen(); mode = 'new';
    const s = main()?.settings || {};
    draft = { voiceProfileId: s.voiceProfileId ?? null, advBackground: s.advBackground || '', advBgTime: s.advBgTime || 'auto' };
    renderPane(true); focus(pane.querySelector('button'));
  }
  function renderDraft() {
    pane.appendChild(el('p', 'home-label', '另开一段对话'));
    const sentence = el('div', 'home-sentence');
    const characters = actions.listCharacters?.() || [];
    const chars = characters.map(p => [p.id, p.name]);
    const backgrounds = actions.getBackgrounds?.() || [];
    const work = characters.find(p => p.id === draft.voiceProfileId)?.work;
    const own = work ? backgrounds.filter(b => b.work === work) : [];
    const places = [['', '默认背景'], ...(own.length ? own : backgrounds).map(b => [b.key, b.name || b.key])];
    // A place the list cannot show (another work, the plain sky) would start a different scene than the label says.
    if (backgrounds.length && !places.some(([key]) => key === draft.advBackground)) draft.advBackground = '';
    const times = [['auto', '跟随时间'], ['day', '白天'], ['evening', '傍晚'], ['night', '夜晚']];
    for (const [prefix, key, choices] of [['和 ', 'voiceProfileId', chars], ['，在 ', 'advBackground', places], ['，', 'advBgTime', times]]) {
      sentence.appendChild(el('span', '', prefix));
      const wrap = el('span', 'home-pick-wrap');
      const fallback = key === 'voiceProfileId' ? '选择角色' : key === 'advBackground' ? '默认背景' : '跟随时间';
      const button = btn(`home-pick home-pick-${key}`, `${choices.find(c => c[0] === draft[key])?.[1] || fallback} ▾`, () => {
        const same = dropdownButton === button; closePopup(); closeDropdown(); if (same) return;
        dropdownButton = button; button.setAttribute('aria-expanded', 'true');
        dropdown = el('div', 'home-dropdown'); dropdown.setAttribute('role', 'group');
        for (const [value, label] of choices) {
          const option = btn('home-option', label, () => {
            draft[key] = value;
            if (key === 'voiceProfileId') {
              const nextWork = characters.find(p => p.id === value)?.work;
              const place = backgrounds.find(b => b.key === draft.advBackground);
              if (nextWork && place?.work !== nextWork) draft.advBackground = '';
            }
            renderPane(true); focus(pane.querySelector(`.home-pick-${key}`));
          });
          option.setAttribute('aria-pressed', String(value === draft[key])); dropdown.appendChild(option);
        }
        if (!choices.length) dropdown.appendChild(el('p', '', '暂无角色，请在设置中添加'));
        wrap.appendChild(dropdown); focus(dropdown.querySelector('button'));
      });
      button.setAttribute('aria-expanded', 'false'); wrap.appendChild(button); sentence.appendChild(wrap);
    }
    pane.appendChild(sentence);
    const row = el('div', 'home-actions');
    add(row, btn('home-primary home-start', '开始新对话', () => { const settings = { ...draft }; close(); actions.startNewWith?.(settings); }),
      btn('home-link home-cancel', '取消', returnHome)); pane.appendChild(row);
  }
  function renderRecents() {
    recents.replaceChildren();
    const others = [...new Set([...recentOrder, ...sessions.map(s => s.id)])]
      .filter(id => id !== mainId).map(id => sessions.find(s => s.id === id)).filter(Boolean);
    recents.hidden = !others.length || mode === 'all';
    if (!others.length) return;
    recents.appendChild(el('p', 'home-recent-label', '最近还聊过'));
    for (const session of others.slice(0, 3)) {
      const button = btn('home-recent', '', () => preview(session.id));
      button.setAttribute('aria-label', `切换到${session.characterName || session.title}`);
      button.dataset.session = session.id;
      const avatar = el('span', 'home-avatar', (session.characterName || session.title).slice(0, 1));
      const text = el('span', 'home-recent-text');
      add(text, el('b', '', session.characterName || session.title), el('span', '', formatHomeTime(session.updatedAt, now())));
      button.replaceChildren(avatar, text); recents.appendChild(button);
      updateAvatar(avatar, session);
    }
    recents.appendChild(btn('home-link home-all', '全部会话 →', () => {
      stopListen(); closePopup(); closeDropdown(); mode = 'all'; page = 0;
      pane.hidden = true; recents.hidden = true; load.hidden = false; renderGrid(); focus(grid.querySelector('button'));
    }));
  }
  function updateAvatar(avatar, session) {
    Promise.resolve(actions.getAvatar?.(session)).then(data => {
      if (!opened || !avatar.isConnected || !data) return;
      const key = JSON.stringify(data);
      if (avatar.dataset.art === key) return;
      const { rect, layers } = data;
      const images = layers.map(layer => {
        const image = el('img'); image.alt = ''; image.src = layer.file;
        const r = layer.rect || { x: 0, y: 0, width: layer.width, height: layer.height };
        const crop = data.crop || { left: 0, top: 0 };
        image.style.left = `${(r.x - crop.left - rect.x) / rect.width * 100}%`;
        image.style.top = `${(r.y - crop.top - rect.y) / rect.width * 100}%`;
        image.style.width = r.width ? `${r.width / rect.width * 100}%` : 'auto';
        image.style.height = r.height ? `${r.height / rect.width * 100}%` : 'auto';
        if (!r.width) { image.style.height = 'auto'; image.style.width = 'auto'; image.onload = () => { image.style.width = `${image.naturalWidth / rect.width * 100}%`; }; }
        image.style.opacity = String(layer.opacity ?? 1);
        image.onerror = () => { delete avatar.dataset.art; avatar.replaceChildren(); avatar.textContent = (session.characterName || session.title).slice(0, 1); };
        return image;
      });
      avatar.dataset.art = key; avatar.textContent = ''; avatar.replaceChildren(...images);
    }).catch(() => {});
  }
  async function preview(id) {
    const token = ++previewRevision, previous = mainId; stopListen(); closePopup();
    screen.setAttribute('aria-busy', 'true');
    try {
      const result = await actions.previewSession?.(id);
      if (!opened || token !== previewRevision || result === false) return;
      const order = recentOrder.length ? [...recentOrder] : sessions.filter(s => s.id !== previous).map(s => s.id);
      const slot = order.indexOf(id);
      if (slot >= 0) order[slot] = previous; else order.unshift(previous);
      readSessions(); mainId = id; mode = 'home';
      recentOrder = order.filter(other => other != null && other !== mainId);
      setStageHome(true); renderPane(true); renderRecents(); focusPrimary();
    } finally { if (token === previewRevision) screen.removeAttribute('aria-busy'); }
  }
  function returnHome() {
    closePopup(); closeDropdown(); mode = 'home'; load.hidden = true; pane.hidden = false;
    renderPane(true); renderRecents(); focusPrimary();
  }
  function setPage(value) { page = Math.max(0, Math.min(value, Math.ceil(sessions.length / PAGE_SIZE) - 1)); renderGrid(); }
  function renderGrid() {
    grid.replaceChildren();
    for (const session of sessions.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)) {
      const slot = btn('adv-title-slot', '', () => enter(session.id)); slot.setAttribute('data-session', session.id);
      if (session.background) { const image = el('img', 'adv-title-thumb-image'); image.src = session.background; image.alt = ''; slot.appendChild(image); }
      const meta = el('span', 'adv-title-slot-meta');
      meta.appendChild(el('b', '', session.title));
      if (session.characterName) meta.appendChild(el('span', 'adv-title-slot-title', session.characterName));
      add(meta, el('span', 'adv-title-slot-line', session.lastLine), el('span', 'adv-title-slot-date', formatHomeTime(session.updatedAt, now())));
      slot.appendChild(meta); grid.appendChild(slot);
    }
    if (!sessions.length) grid.appendChild(el('p', '', '还没有会话'));
    const count = Math.max(1, Math.ceil(sessions.length / PAGE_SIZE));
    info.textContent = `第 ${page + 1} / ${count} 页 · 共 ${sessions.length} 个会话`;
    prev.disabled = page === 0; next.disabled = page >= count - 1;
  }
  async function refresh() {
    if (!opened || polling?.revision === revision) return;
    const token = revision, request = { revision: token };
    polling = request;
    try {
      const result = await actions.getStatus?.();
      if (!opened || token !== revision) return;
      status = result || status; readSessions(); renderStatus(); renderPane();
      if (mode === 'all') renderGrid();
      // Keep recent buttons stable during polling so keyboard focus is preserved.
      const recentKey = JSON.stringify(sessions.map(s => [s.id, s.characterName, s.title, s.updatedAt, s.settings?.voiceProfileId]));
      if (recents.dataset.key !== recentKey) { recents.dataset.key = recentKey; renderRecents(); }
      else for (const button of recents.querySelectorAll('.home-recent')) {
        const session = sessions.find(s => s.id === button.dataset.session);
        if (session) updateAvatar(button.querySelector('.home-avatar'), session);
      }
      setStageHome(true);
    } finally { if (polling === request) polling = null; }
  }
  function schedule() {
    timer = clock.setTimeout(() => { timer = null; refresh().catch(() => {}); if (opened) schedule(); }, 5000);
    timer?.unref?.();
  }
  function setStageHome(on) {
    stage?.classList.toggle('is-home', on);
    const controls = [...(stage?.querySelectorAll('.adv-layer-textbox, .adv-layer-quickmenu, .adv-app-tools, .adv-art-status') || []),
      ...[...root.children].filter(node => node !== screen && !node.classList.contains('adv-config-overlay'))];
    for (const layer of controls) {
      if (on) { if (!stageControls.has(layer)) stageControls.set(layer, layer.inert); layer.inert = true; }
      else layer.inert = stageControls.get(layer) ?? false;
    }
    if (!on) stageControls.clear();
  }
  function open() {
    if (opened || destroyed) return;
    opened = true; ++revision; screen.hidden = false; screen.removeAttribute('hidden');
    mode = 'home'; pane.hidden = false; load.hidden = true; textOnly = false;
    status = { modelLoaded: false, engine: {} };
    setStageHome(true); readSessions(); recentOrder = sessions.filter(s => s.id !== mainId).map(s => s.id);
    renderStatus(); renderPane(true); renderRecents(); focusPrimary();
    // Reconcile even a matching id: startup sync may have changed its settings
    // or the character list since the stage last rendered.
    if (mainId != null) preview(mainId);
    refresh().catch(() => {}); schedule();
    unsubscribe = actions.subscribeSettings?.(() => { refresh().catch(() => {}); });
  }
  function close() {
    if (!opened) return;
    opened = false; ++revision; ++previewRevision; closePopup(); closeDropdown(); stopListen();
    clock.clearTimeout(timer); timer = null; unsubscribe?.(); unsubscribe = null;
    screen.hidden = true; setStageHome(false);
  }
  function keydown(event) {
    if (!opened || event.isComposing || event.keyCode === 229) return;
    // Keep home keystrokes away from stage shortcuts and its hidden input.
    event.stopPropagation();
    if (event.key !== 'Escape') return;
    if (popup) closePopup(true);
    else if (dropdown) closeDropdown(true);
    else if (mode !== 'home') returnHome();
    event.preventDefault();
  }
  function outside(event) { if (!screen.contains(event.target) || !event.target.closest?.('.home-pill-wrap')) closePopup(); if (!event.target.closest?.('.home-pick-wrap')) closeDropdown(); }
  screen.addEventListener('keydown', keydown); doc.addEventListener('click', outside);
  disposers.push(() => screen.removeEventListener('keydown', keydown), () => doc.removeEventListener('click', outside));
  return { element: screen, menuScreen: screen, loadScreen: load, open, close, refresh,
    get isOpen() { return opened; }, destroy() { close(); destroyed = true; for (const off of disposers) off(); screen.remove(); } };
}

