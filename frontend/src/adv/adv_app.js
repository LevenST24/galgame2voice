import { createStage } from './stage.js';
import { createTextbox } from './textbox.js';
import { createQuickMenu } from './quick_menu.js';
import { createShortcuts } from './shortcuts.js';
import { createLinePlayer } from './line_player.js';
import { createSpriteView } from './sprite.js';
import { createBacklog } from './backlog.js';
import { normalizePortrait, resolveSpriteState } from '../sprite_state.js';
import { DEFAULT_SESSION_SETTINGS } from '../store.js';
import { pickSettings } from '../session_data.js';

/** Mount one disposable renderer. Controller remains the sole history writer.
 * dom: { root, sidebar?, messages?, input?, micBtn?, voiceModeBtn? }
 * deps: { controller, audio, getSession, voiceEnabled, loadPortrait, getCharacter,
 *   clock?, raf?, cancelRaf?, matchMedia?, subscribe?, subscribeOverlay?, ...actions }
 */
export function mountAdv(dom, deps) {
  const { controller, audio } = deps;
  const doc = dom.root.ownerDocument || globalThis.document;
  const clock = deps.clock || {
    now: () => performance.now(), setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: id => clearTimeout(id),
  };
  const raf = deps.raf || (fn => requestAnimationFrame(fn));
  const cancelRaf = deps.cancelRaf || (id => cancelAnimationFrame(id));
  const stage = createStage(dom.root);
  const disposers = [];
  let destroyed = false, sessionId, characterKey, outfit, turnId = null;
  let settings, model = null, resolved = null, currentLine = null, failedUser = null, liveMessage = null;
  let portraitRevision = 0, portraitRequest = null, refreshTimer = null, refreshAttempted = false;
  let drawer = null, backlog = null, externalOverlay = false, savedFocus = null, temporarySkip = false;
  let voiceSequence = 0;
  const moved = new Map();
  const drafts = new Map();
  let replayToken = 0;
  const listen = (el, type, fn, options) => {
    if (!el?.addEventListener) return;
    el.addEventListener(type, fn, options);
    disposers.push(() => el.removeEventListener(type, fn, options));
  };
  const matchMedia = deps.matchMedia || (query => globalThis.matchMedia?.(query));
  const mql = matchMedia('(prefers-reduced-motion: reduce)');
  let reducedMotion = Boolean(mql?.matches);
  const playerSettings = () => ({
    primary: settings.advPrimaryLanguage, subtitle: settings.advSubtitleLanguage,
    playerName: settings.advPlayerName, advWindowOpacity: settings.advWindowOpacity,
    revealMsPerGrapheme: settings.advTextIntervalMs, interruptVoice: settings.advInterruptVoice,
    autoBaseMs: settings.advAutoBaseMs, autoPerCharMs: settings.advAutoPerCharMs,
    autoMode: settings.advAutoMode, voiceEnabled: Boolean(deps.voiceEnabled?.()), reducedMotion,
  });
  settings = pickSettings(deps.getSession()?.settings, DEFAULT_SESSION_SETTINGS);
  const textbox = createTextbox(stage.textbox, {
    clock, matchMedia: () => ({ matches: false }), getSettings: playerSettings,
    onAdvance: () => player.advance(),
    onSend: text => !controller.busy && controller.send(text),
    onMic: () => {
      player.beginPlayerEdit();
      deps.onMic?.(textbox.inputElement, textbox.micButton);
    },
    onAction: (action, data) => {
      if (action === 'player_edit') { player.beginPlayerEdit(); sprite.suspend(); syncControls(); }
      if (action === 'swap_languages') {
        deps.saveSettings?.({ advPrimaryLanguage: data.primary, advSubtitleLanguage: data.subtitle });
        update();
      }
    },
  });
  const status = doc.createElement('button');
  status.type = 'button'; status.className = 'adv-art-status'; status.hidden = true;
  stage.stage.appendChild(status);
  const sprite = createSpriteView({
    spriteLayer: stage.sprite, fxLayer: stage.fx, faceSlot: textbox.faceSlot,
    documentObj: doc, ImageClass: deps.ImageClass || globalThis.Image,
    now: () => clock.now(), clock, raf, cancelRaf,
    onStatus: () => {
      if (destroyed) return;
      status.hidden = false; status.textContent = '立绘暂时无法显示，点击重试';
      if (!refreshAttempted) { refreshAttempted = true; loadPortrait(true); }
    },
  });
  const voice = {
    async play(index, chunks) {
      deps.stopOtherVoice?.();
      const key = turnId;
      const sequence = ++voiceSequence;
      textbox.setVoice({ active: true });
      let result;
      try { result = await audio.playLine(index, chunks, { msgId: key }); }
      catch { result = 'failed'; }
      if (!destroyed && key === turnId && sequence === voiceSequence) textbox.setVoice({ active: false });
      return result;
    },
    stop(options) { ++voiceSequence; audio.stopLine(destroyed ? { fadeMs: 0 } : options); textbox.setVoice({ active: false }); },
  };
  const render = {
    ...textbox,
    activate(line, options) {
      currentLine = line;
      textbox.setSpeaker({ name: deps.getCharacter?.()?.name || '角色', mode: 'character' });
      textbox.activate(line, options);
      paint(line, options);
    },
    enterInput(options) { textbox.enterInput(options); sprite.suspend(); },
    showError(error) {
      sprite.suspend();
      textbox.showError({ message: String(error?.message || error || '回复失败'), actions: [
        ...(controller.canRetry(failedUser) ? [{ label: '重新发送', run: () => controller.retry(failedUser) }] : []),
        { label: '去设置', run: () => deps.onSettings?.() },
      ] });
    },
  };
  const player = createLinePlayer({ clock, voice, render,
    paginate: (line, primary) => textbox.paginate(line, primary), settings: playerSettings() });
  textbox.setPlayer(player);
  const quickMenu = createQuickMenu(stage.quickmenu, { onAction: action });
  const tools = doc.createElement('div'); tools.className = 'adv-app-tools';
  stage.stage.appendChild(tools);
  listen(tools, 'click', event => event.stopPropagation());
  for (const [label, name] of [['会话', 'sessions'], ['输入', 'input'], ['语音开关', 'voice_mode'], ['停止', 'stop']]) {
    const button = doc.createElement('button'); button.type = 'button'; button.textContent = label;
    button.setAttribute('aria-label', label); button.dataset.action = name;
    listen(button, 'click', () => action(name)); tools.appendChild(button);
  }
  function paint(line = currentLine || { emotion: 'gentle', act: {} }, options = {}) {
    if (!line || !model) return;
    resolved = resolveSpriteState(model, resolved, line, { costume: settings.advOutfit || model.defaultCostume });
    if (resolved) sprite.apply(resolved, {
      ...options, reducedMotion, skipping: player.skip,
      motion: line.act?.motion || null,
    });
  }
  function restoreSprite() {
    if (!resolved || !currentLine || ['input', 'error', 'idle'].includes(player.state)) return;
    // Resume the already resolved face. Overlay and OS changes never act again.
    sprite.apply({ ...resolved, state: { ...resolved.state, fx: null } }, { reducedMotion, skipping: player.skip });
  }
  function syncControls() {
    textbox.setCanSend(!controller.busy);
    const input = player.state === 'input' || player.state === 'error';
    for (const name of ['auto', 'skip', 'replay_voice', 'hide']) quickMenu.setEnabled(name, !input && !drawer && !backlog && !externalOverlay);
    quickMenu.setToggle('auto', player.auto); quickMenu.setToggle('skip', player.skip);
    quickMenu.setHidden(player.hidden); textbox.setHidden(player.hidden);
    tools.hidden = player.hidden;
    stage.stage.classList.toggle('is-window-hidden', player.hidden);
    stage.stage.classList.toggle('reduced-motion', reducedMotion);
    quickMenu.setModelStatus(deps.getModelStatus?.() || { label: '对话模型', state: controller.busy ? 'busy' : 'ok' });
  }
  function restore(node) {
    const position = moved.get(node);
    if (!position) return;
    position.parent.insertBefore(node, position.next?.parentNode === position.parent ? position.next : null);
    node.inert = position.inert;
    moved.delete(node);
  }
  function closeDrawer() {
    if (!drawer) return;
    const node = drawer.node;
    restore(node); drawer.element.remove(); drawer = null;
    stage.textbox.inert = false; stage.quickmenu.inert = false; tools.inert = false;
    player.resume('overlay');
    if (!externalOverlay && !backlog) restoreSprite();
    if (savedFocus?.isConnected) savedFocus.focus();
    syncControls();
  }
  function openDrawer(kind) {
    closeDrawer();
    closeLog();
    const node = kind === 'sessions' ? dom.sidebar : dom.messages;
    if (!node?.parentNode) return;
    savedFocus = doc.activeElement;
    player.suspend('overlay'); deps.stopOtherVoice?.(); sprite.suspend(); textbox.completeReveal(); textbox.glyph(false);
    const element = doc.createElement('div'); element.className = `adv-drawer adv-drawer-${kind}`;
    element.setAttribute('role', 'dialog'); element.setAttribute('aria-modal', 'true');
    element.setAttribute('aria-label', kind === 'log' ? 'LOG 聊天历史' : '会话列表');
    const close = doc.createElement('button'); close.type = 'button'; close.textContent = '关闭';
    // Drawer nodes are disposed on close; don't retain their handlers in the app.
    close.onclick = closeDrawer;
    moved.set(node, { parent: node.parentNode, next: node.nextSibling, inert: node.inert });
    element.appendChild(close); element.appendChild(node); node.inert = false;
    stage.overlay.appendChild(element); drawer = { element, node, kind };
    stage.textbox.inert = true; stage.quickmenu.inert = true; tools.inert = true;
    syncControls(); close.focus();
  }
  function closeLog() {
    if (!backlog) return;
    backlog.destroy();
    backlog = null;
    stage.textbox.inert = false; stage.quickmenu.inert = false; tools.inert = false;
    player.resume('overlay');
    if (!externalOverlay && !drawer) restoreSprite();
    if (savedFocus?.isConnected) savedFocus.focus();
    syncControls();
  }
  function openLog() {
    closeDrawer();
    closeLog();
    savedFocus = doc.activeElement;
    player.setSkip(false);
    player.suspend('overlay');
    deps.stopOtherVoice?.();
    audio.stopLine?.({ fadeMs: 0 });
    sprite.suspend();
    textbox.glyph(false);
    backlog = createBacklog(stage.overlay, {
      controller,
      audio,
      clock,
      getSession: () => deps.getSession?.(),
      getLiveMessage: () => liveMessage,
      getSettings: () => settings,
      getCharacter: () => deps.getCharacter?.(),
      voiceEnabled: () => Boolean(deps.voiceEnabled?.()),
      loadOlderHistory: session => (deps.loadOlderHistory || deps.loadOlderSessionHistory)?.(session),
      playUserVoice: (msg, ctl) => (deps.playUserVoice || deps.playVoice)?.(msg, ctl),
      stopOtherVoice: () => deps.stopOtherVoice?.(),
      wallNow: () => deps.wallNow?.() ?? Date.now(),
      onClose: closeLog,
    });
    stage.textbox.inert = true; stage.quickmenu.inert = true; tools.inert = true;
    syncControls();
  }
  function action(name) {
    if (destroyed) return;
    switch (name) {
      case 'advance': player.advance(); break;
      case 'toggle_auto':
      case 'auto': if (!['input', 'error', 'suspended'].includes(player.state)) player.setAuto(!player.auto); break;
      case 'toggle_skip':
      case 'skip': if (!['input', 'error', 'suspended'].includes(player.state)) player.setSkip(!player.skip); sprite.suspend(); break;
      case 'skip-hold-start': temporarySkip = !player.skip; if (temporarySkip) player.setSkip(true); sprite.suspend(); break;
      case 'skip-hold-end': if (temporarySkip) player.setSkip(false); temporarySkip = false; break;
      case 'toggle_hide':
      case 'hide':
        if (player.hidden) unhide();
        else { player.hide(); player.suspend('hidden'); sprite.suspend(); }
        break;
      case 'unhide': unhide(); break;
      case 'log': openLog(); break;
      case 'sessions': openDrawer('sessions'); break;
      case 'close_overlay':
        if (backlog) closeLog();
        else if (drawer) closeDrawer();
        else deps.closeOverlay?.();
        break;
      case 'settings':
      case 'config': deps.onSettings?.(); break;
      case 'model_status': deps.onModelSettings?.(); break;
      case 'swap_languages': textbox.swapLanguages(); break;
      case 'new_session': deps.onNewSession?.(); break;
      case 'stop': controller.stop(); break;
      case 'input': textbox.prepareInput({ canSend: !controller.busy }); break;
      case 'voice_mode': deps.toggleVoice?.(); backlog?.stopReplay(); update(); break;
      case 'replay_voice': {
        const line = currentLine;
        if (line?.audio?.chunks?.length && !controller.busy && player.state !== 'input') {
          const key = turnId, token = ++replayToken;
          player.suspend('replay');
          voice.play(line.index, line.audio.chunks).then(() => {
            if (!destroyed && key === turnId && token === replayToken) { player.resume('replay'); syncControls(); }
          });
        }
        break;
      }
    }
    syncControls();
  }
  function unhide() {
    player.resume('hidden');
    player.unhide();
  }
  const shortcuts = createShortcuts(stage.stage, {
    getContext: () => ({ stage: stage.stage, hidden: player.hidden,
      overlayOpen: Boolean(drawer || backlog || externalOverlay), subtitleNone: settings.advSubtitleLanguage === 'none',
      playerInput: player.state === 'input',
      composing: textbox.composing }),
    onAction: action,
  });
  // Trap focus in the temporary drawer, including its existing playback controls.
  listen(stage.stage, 'keydown', event => {
    if (!drawer || event.key !== 'Tab') return;
    const nodes = [...drawer.element.querySelectorAll('button, input, select, textarea, [tabindex]')]
      .filter(node => !node.disabled && node.tabIndex >= 0);
    const first = nodes[0], last = nodes.at(-1);
    if (first && (!drawer.element.contains(doc.activeElement) ||
      (event.shiftKey && doc.activeElement === first) || (!event.shiftKey && doc.activeElement === last))) {
      event.preventDefault(); (event.shiftKey ? last : first).focus();
    }
  });
  listen(dom.sidebar, 'click', event => {
    if (event.target.closest?.('.session-item, #sidebarClose, #newChatBtn')) { event.stopPropagation(); closeDrawer(); }
  });
  listen(status, 'click', event => { event.stopPropagation(); refreshAttempted = false; loadPortrait(true); });
  const motionChange = event => {
    reducedMotion = Boolean(event.matches);
    player.setSettings({ reducedMotion });
    if (reducedMotion) {
      sprite.suspend();
      restoreSprite();
    }
    syncControls();
  };
  if (mql?.addEventListener) { mql.addEventListener('change', motionChange); disposers.push(() => mql.removeEventListener('change', motionChange)); }
  else if (mql?.addListener) { mql.addListener(motionChange); disposers.push(() => mql.removeListener(motionChange)); }
  function setOverlay(open) {
    externalOverlay = open;
    if (open) { player.suspend('dialog'); sprite.suspend(); textbox.completeReveal(); textbox.glyph(false); }
    else { player.resume('dialog'); if (!drawer && !backlog) restoreSprite(); }
    syncControls();
  }
  if (deps.subscribeOverlay) disposers.push(deps.subscribeOverlay(setOverlay));
  async function loadPortrait(refresh = false) {
    const revision = ++portraitRevision;
    portraitRequest?.abort(); portraitRequest = new AbortController();
    if (refreshTimer !== null) { clock.clearTimeout(refreshTimer); refreshTimer = null; }
    const character = deps.getCharacter?.();
    if (!character?.id || !deps.loadPortrait) {
      model = null; sprite.setModel(null); status.hidden = false; status.textContent = '此角色暂无立绘'; return;
    }
    try {
      const payload = await deps.loadPortrait(character.id, { signal: portraitRequest.signal, refresh });
      if (destroyed || revision !== portraitRevision) return;
      const nextModel = normalizePortrait(payload);
      if (!nextModel && refresh && model) throw new Error('立绘刷新失败');
      const previous = resolved;
      model = nextModel; resolved = null; sprite.setModel(model, { preserveCurrent: refresh });
      status.hidden = Boolean(model); status.textContent = model ? '' : '此角色暂无立绘';
      if (model && refresh && previous) {
        // Renewal changes signed URLs, not the current facial choice or acting.
        resolved = resolveSpriteState(model, previous, { emotion: currentLine?.emotion || 'gentle', act: {
          face: previous.state.face, blush: previous.state.blush, fx: null,
        } }, { costume: previous.state.outfit });
        if (resolved) sprite.apply(resolved, { reducedMotion, skipping: true });
      } else if (model) paint();
      // Signed image URLs are transient; renew once, a minute before expiry.
      const expiries = Object.values(model?.sprites || {}).flatMap(faces => Object.values(faces))
        .map(item => { try { return Number(new URL(item.file, 'http://local').searchParams.get('exp')); } catch { return 0; } })
        .filter(exp => exp > 0);
      if (expiries.length) {
        const delay = Math.max(1000, Math.min(...expiries) * 1000 - (deps.wallNow?.() ?? Date.now()) - 60000);
        refreshTimer = clock.setTimeout(() => { refreshTimer = null; loadPortrait(true); }, delay);
      }
    } catch (error) {
      if (destroyed || revision !== portraitRevision || error.name === 'AbortError') return;
      status.hidden = false; status.textContent = '立绘暂时无法加载，点击重试';
    }
  }
  function update() {
    if (destroyed) return;
    const session = deps.getSession();
    const nextSettings = pickSettings(session?.settings, DEFAULT_SESSION_SETTINGS);
    const switched = sessionId !== session?.id;
    const character = deps.getCharacter?.();
    const changedCharacter = switched || characterKey !== character?.id;
    const changedOutfit = outfit !== nextSettings.advOutfit;
    const changedLanguage = settings.advPrimaryLanguage !== nextSettings.advPrimaryLanguage || settings.advSubtitleLanguage !== nextSettings.advSubtitleLanguage;
    settings = nextSettings;
    if (switched) {
      if (sessionId !== undefined) drafts.set(sessionId, textbox.draft);
      closeDrawer(); closeLog(); resetInput(); turnId = null; currentLine = null; failedUser = null;
      textbox.setDraft(drafts.get(session?.id) || '');
    }
    sessionId = session?.id; characterKey = character?.id; outfit = settings.advOutfit;
    player.setSettings(playerSettings());
    if (changedCharacter) {
      if (!switched) {
        controller.stop();
        resetInput(); turnId = null; currentLine = null;
      }
      model = null; resolved = null; sprite.setModel(null); refreshAttempted = false;
      loadPortrait();
    } else if (changedOutfit) paint();
    if (changedLanguage && currentLine && player.state !== 'input' && player.state !== 'error') {
      // Reflow presentation only; no activate/live announcement/acting/voice replay.
      player.repaginate();
      textbox.reflow({ pageIndex: player.page });
    }
    textbox.refreshSettings();
    if (backlog) {
      const top = backlog.listElement.scrollTop;
      backlog.renderAll();
      backlog.listElement.scrollTop = top;
    }
    if (drawer || backlog) player.suspend('overlay');
    if (externalOverlay) player.suspend('dialog');
    syncControls();
  }
  function resetInput() {
    deps.cancelMic?.();
    liveMessage = null;
    player.reset();
    const idle = Symbol('session input');
    player.startTurn(idle); player.settle(idle, { ok: true });
    textbox.enterInput({ canSend: !controller.busy });
  }
  disposers.push(controller.on('turnstart', event => {
    update();
    if (event.originId !== sessionId) return;
    liveMessage = { ...event.assistantMessage, lines: [...event.assistantMessage.lines || []] };
    failedUser = event.userMessage; turnId = event.assistantMessage.id; currentLine = null;
    textbox.setDraft(''); textbox.clearError();
    textbox.setSpeaker({ name: deps.getCharacter?.()?.name || '角色', mode: 'character' });
    player.startTurn(turnId); syncControls();
    if (drawer || backlog) player.suspend('overlay');
    if (externalOverlay) player.suspend('dialog');
  }));
  disposers.push(controller.on('text', () => { /* Legacy streaming text remains in controller/history. */ }));
  disposers.push(controller.on('line', event => {
    if (event.messageId === turnId && deps.getSession()?.id === sessionId) {
      if (liveMessage) {
        const index = liveMessage.lines.findIndex(l => l.index === event.line.index);
        if (index < 0) liveMessage.lines.push(event.line);
        else liveMessage.lines[index] = event.line;
      }
      player.pushLine(turnId, event.line);
    }
  }));
  disposers.push(controller.on('line_audio', event => {
    if (event.messageId === turnId && deps.getSession()?.id === sessionId) player.setLineAudio(turnId, event.line, event);
  }));
  for (const kind of ['done', 'error', 'cancelled']) disposers.push(controller.on(kind, event => {
    if (event.originId !== sessionId || deps.getSession()?.id !== sessionId) return;
    if (!turnId) { textbox.enterInput({ canSend: !controller.busy }); syncControls(); return; }
    const hasContent = msg => Boolean(msg?.content?.trim() || msg?.japanese?.trim() ||
      msg?.lines?.some(l => l.zh?.trim() || l.ja?.trim()));
    const isEmptyDone = kind === 'done' && !hasContent(event.message) && !hasContent(liveMessage);
    liveMessage = null;
    if (isEmptyDone) {
      if (failedUser && !failedUser.failed) {
        failedUser.failed = { error: '模型这次没有返回任何内容', code: 'empty_reply' };
      }
      player.settle(turnId, { kind: 'error', error: { message: '模型这次没有返回任何内容' } });
      syncControls();
      return;
    }
    player.settle(turnId, kind === 'done' ? { ok: true } : { kind, error: { message: event.error } });
    syncControls();
  }));
  disposers.push(player.on('state', syncControls));
  if (deps.subscribe) disposers.push(deps.subscribe(update));
  listen(deps.viewport || globalThis.window, 'resize', () => {
    if (!currentLine || !['revealing', 'waiting_advance', 'waiting_audio'].includes(player.state)) return;
    player.repaginate(); textbox.reflow({ pageIndex: player.page });
  });
  update();
  return {
    update, player, textbox, sprite, stage, action,
    get backlog() { return backlog; },
    unmount() {
      if (destroyed) return;
      // Stop callbacks before resetting renderers or restoring borrowed nodes.
      for (const off of disposers.splice(0)) off?.();
      closeLog();
      closeDrawer(); destroyed = true; ++portraitRevision;
      portraitRequest?.abort();
      deps.cancelMic?.();
      if (refreshTimer !== null) clock.clearTimeout(refreshTimer);
      drafts.clear();
      shortcuts.destroy(); player.reset(); audio.stopLine({ fadeMs: 0 });
      sprite.destroy(); quickMenu.destroy(); textbox.destroy(); stage.destroy();
    },
  };
}
