import { createStage } from './stage.js';
import { createSpriteTransition } from './transitions.js';
import { createTextbox } from './textbox.js';
import { createQuickMenu } from './quick_menu.js';
import { createShortcuts } from './shortcuts.js';
import { createLinePlayer } from './line_player.js';
import { createStaging } from './staging.js';
import { createSpriteView } from './sprite.js';
import { createBacklog } from './backlog.js';
import { createConfigPanel } from './config_panel.js';
import { createBackgroundView, effectiveBackgroundKey, normalizeBackgroundSelection, selectionUrl } from './background.js';
import { createBgPanel } from './bg_panel.js';
import { normalizePortrait, resolveSpriteState } from '../sprite_state.js';
import { DEFAULT_SESSION_SETTINGS } from '../store.js';
import { pickSettings } from '../session_data.js';
import { requestJson } from '../request.js';
import { needsModelSettings } from '../ui.js';
export { advActions } from './actions.js';

/** Mount one disposable renderer. Controller remains the sole history writer.
 * dom: { root, sidebar? }
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
  // The controller stores a finished reply under a new id; LOG must still know it is this turn.
  let storedTurnId = null;
  let turnLines = [];
  let pendingOutfit = null;
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
  const spriteTransition = createSpriteTransition(stage.sprite, { reducedMotion: () => reducedMotion });
  // The day part is wall-clock time, so the view keeps its own 60 s re-check
  // outside the player's timer set instead of the injected clock.
  const background = createBackgroundView({
    layer: stage.background, matchMedia, ImageClass: deps.ImageClass || globalThis.Image,
  });
  const playerSettings = () => ({
    primary: settings.advPrimaryLanguage, subtitle: settings.advSubtitleLanguage,
    playerName: settings.advPlayerName, advWindowOpacity: settings.advWindowOpacity,
    revealMsPerGrapheme: settings.advTextIntervalMs, interruptVoice: settings.advInterruptVoice,
    autoBaseMs: settings.advAutoBaseMs, autoPerCharMs: settings.advAutoPerCharMs,
    autoMode: settings.advAutoMode, voiceEnabled: Boolean(deps.voiceEnabled?.()), reducedMotion,
  });
  settings = pickSettings(deps.getSession()?.settings, DEFAULT_SESSION_SETTINGS);
  const textbox = createTextbox(stage.textbox, {
    clock, matchMedia, getSettings: playerSettings,
    isOverlayOpen: () => Boolean(stage.stage.classList.contains('is-home') || drawer || backlog || externalOverlay || configPanel?.isOpen || bgPanel?.isOpen),
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
    now: () => clock.now(), clock, raf, cancelRaf, random: deps.random,
    onStatus: () => {
      if (destroyed) return;
      status.hidden = false; status.textContent = '立绘暂时无法显示，点击重试';
      if (!refreshAttempted) { refreshAttempted = true; loadPortrait(true); }
    },
  });
  const voice = {
    supportsStartNotification: audio.supportsStartNotification === true,
    async play(index, chunks, { onStart } = {}) {
      deps.stopOtherVoice?.();
      const key = turnId;
      const sequence = ++voiceSequence;
      textbox.setVoice({ active: true });
      const lipSync = sprite.startLipSync({ audioPlayer: audio, characterVoice: true });
      let result;
      // playLine resumes a suspended context itself and gives up after a bounded wait.
      try { result = await audio.playLine(index, chunks, { msgId: key, onStart }); }
      catch { result = 'failed'; }
      if (!destroyed && key === turnId && sequence === voiceSequence) textbox.setVoice({ active: false });
      // A later line may own the mouth by now; this line's token cannot stop it.
      sprite.stopLipSync(lipSync);
      return result;
    },
    stop(options) { ++voiceSequence; audio.stopLine(destroyed ? { fadeMs: 0 } : options); textbox.setVoice({ active: false }); sprite.stopLipSync(); },
  };
  // 去设置 on a failed reply, in the textbox and in LOG: the same choice as the error toasts.
  const openErrorSettings = error => (needsModelSettings(error?.message, error?.code)
    ? (deps.onModelSettings || deps.onSettings) : deps.onSettings)?.();
  const staging = createStaging({ random: deps.random });
  const render = {
    ...textbox,
    activate(line, options) {
      currentLine = line;
      sprite.setStaging(staging.next({ move: line.act?.move, emotion: line.emotion }), { instant: reducedMotion || player.skip });
      textbox.setSpeaker({ name: deps.getCharacter?.()?.name || '角色', mode: 'character' });
      textbox.activate(line, options);
      paint(line, options)?.then(() => { if (!destroyed) preloadNextLine(); });
      preloadNextLine();
    },
    enterInput(options) { textbox.enterInput(options); sprite.suspend(); },
    showError(error) {
      sprite.suspend();
      textbox.showError({ message: String(error?.message || error || '回复失败'), actions: [
        ...(controller.canRetry(failedUser) ? [{ label: '重新发送', run: () => controller.retry(failedUser) }] : []),
        { label: '去设置', run: () => openErrorSettings(error) },
      ] });
    },
  };
  const player = createLinePlayer({ clock, voice, render,
    paginate: (line, primary) => textbox.paginate(line, primary), settings: playerSettings() });
  textbox.setPlayer(player);
  const quickMenu = createQuickMenu(stage.quickmenu, { onAction: action });
  const tools = doc.createElement('div'); tools.className = 'adv-app-tools';
  let voiceModeButton;
  stage.stage.appendChild(tools);
  listen(tools, 'click', event => event.stopPropagation());
  for (const [label, name] of [['会话', 'sessions'], ['输入', 'input'], ['语音开关', 'voice_mode'], ['停止', 'stop']]) {
    const button = doc.createElement('button'); button.type = 'button'; button.textContent = label;
    button.setAttribute('aria-label', label); button.dataset.action = name;
    if (name === 'voice_mode') voiceModeButton = button;
    listen(button, 'click', () => action(name)); tools.appendChild(button);
  }

  const configPanel = createConfigPanel(stage.overlay, {
    getSession: () => deps.getSession?.(),
    getCharacter: () => deps.getCharacter?.(),
    getCostumes: () => model?.costumes || sprite.getModel?.()?.costumes || deps.getCostumes?.() || [],
    saveSettings: patch => {
      deps.saveSettings?.(patch);
      update();
    },
    voiceEnabled: () => Boolean(deps.voiceEnabled?.()),
    toggleVoice: () => {
      deps.toggleVoice?.();
      update();
    },
    onModelSettings: () => deps.onModelSettings?.(),
    onSessionSettings: () => (deps.onSessionSettings || deps.onSettings)?.(),
    onPreview: patch => {
      if (patch.advWindowOpacity !== undefined) {
        const band = stage.textbox.querySelector('.adv-band');
        if (band) band.style.setProperty('--adv-window-opacity', String(patch.advWindowOpacity));
        stage.stage.style.setProperty('--adv-window-opacity', String(patch.advWindowOpacity));
      }
      if (patch.advOutfit !== undefined && model) {
        paint(currentLine, { costume: patch.advOutfit || model.defaultCostume });
      }
      if (patch.advBlink !== undefined || patch.advLipSync !== undefined) sprite.setAnimationOptions(patch);
      if (patch.advPrimaryLanguage !== undefined || patch.advSubtitleLanguage !== undefined) {
        if (currentLine && player.state !== 'input' && player.state !== 'error') {
          player.repaginate();
          textbox.reflow({ pageIndex: player.page });
        }
      }
    },
    onRevert: baselineSettings => {
      if (baselineSettings?.advWindowOpacity !== undefined) {
        const band = stage.textbox.querySelector('.adv-band');
        if (band) band.style.setProperty('--adv-window-opacity', String(baselineSettings.advWindowOpacity));
        stage.stage.style.setProperty('--adv-window-opacity', String(baselineSettings.advWindowOpacity));
      }
      if (model) {
        paint(currentLine, { costume: baselineSettings?.advOutfit || model.defaultCostume });
      }
      sprite.setAnimationOptions(animationOptions(baselineSettings || settings));
      if (currentLine && player.state !== 'input' && player.state !== 'error') {
        player.repaginate();
        textbox.reflow({ pageIndex: player.page });
      }
    },
    onOpen: () => {
      player.suspend('dialog');
      sprite.suspend();
      textbox.completeReveal();
      textbox.glyph(false);
      stage.textbox.inert = true;
      stage.quickmenu.inert = true;
      tools.inert = true;
      syncControls();
    },
    onClose: () => {
      stage.textbox.inert = false;
      stage.quickmenu.inert = false;
      tools.inert = false;
      player.resume('dialog');
      if (!drawer && !backlog && !externalOverlay) restoreSprite();
      syncControls();
    },
    clock,
    matchMedia,
    documentObj: doc,
  });

  let backgrounds = [];
  let works = [];
  let appliedBackground = null;
  const storedBackground = () => normalizeBackgroundSelection({
    id: settings.advBackground, time: settings.advBgTime,
  });
  // What the stage shows for a stored choice: auto follows the current character's work.
  const backgroundKey = (stored, work = deps.getCharacter?.()?.work || null) =>
    effectiveBackgroundKey(stored, { list: backgrounds, works, work });
  const shownBackground = selection => ({ ...selection, id: backgroundKey(selection.id) });
  function syncBackground() {
    const next = shownBackground(storedBackground());
    if (appliedBackground?.id === next.id && appliedBackground?.time === next.time) return;
    appliedBackground = next;
    background.setSelection(next);
  }
  const bgPanel = createBgPanel(stage.overlay, {
    getList: () => backgrounds,
    getWorks: () => works,
    getWork: () => deps.getCharacter?.()?.work || null,
    getSelection: storedBackground,
    saveSettings: patch => deps.saveSettings?.(patch),
    onPreview: selection => {
      appliedBackground = shownBackground(selection);
      background.setSelection(appliedBackground);
    },
    onOpen: () => {
      player.suspend('dialog');
      sprite.suspend();
      textbox.completeReveal();
      textbox.glyph(false);
      stage.textbox.inert = true;
      stage.quickmenu.inert = true;
      tools.inert = true;
      syncControls();
    },
    onClose: () => {
      stage.textbox.inert = false;
      stage.quickmenu.inert = false;
      tools.inert = false;
      player.resume('dialog');
      if (!drawer && !backlog && !externalOverlay && !configPanel?.isOpen) restoreSprite();
      syncControls();
    },
    documentObj: doc,
  });
  function openBgPanel() {
    closeDrawer();
    closeLog();
    if (configPanel?.isOpen) configPanel.close({ revert: true });
    bgPanel.open();
  }
  function closeBgPanel() {
    if (bgPanel?.isOpen) bgPanel.close();
  }
  async function loadBackgrounds() {
    let list = [];
    let workList = [];
    try {
      const data = await (deps.fetchBackgrounds || (() => requestJson('/api/backgrounds')))();
      list = Array.isArray(data?.backgrounds) ? data.backgrounds : [];
      workList = Array.isArray(data?.works) ? data.works : [];
    } catch {
      list = [];
      workList = [];
    }
    if (destroyed) return;
    backgrounds = list;
    works = workList;
    background.setList(list);
    // Auto resolves against the list, so re-check once it arrives.
    if (settings) syncBackground();
  }

  function paint(line = currentLine || { emotion: 'gentle', act: {} }, options = {}) {
    if (!line || !model) return;
    const costume = options.costume !== undefined ? options.costume : (settings.advOutfit || model.defaultCostume);
    const next = resolveSpriteState(model, resolved, line, { costume });
    if (pendingOutfit && pendingOutfit !== next?.state.outfit) {
      spriteTransition.abort(); pendingOutfit = null;
    }
    const changed = resolved && next && resolved.state.outfit !== next.state.outfit;
    const apply = () => {
      pendingOutfit = null;
      resolved = next;
      if (resolved) return sprite.apply(resolved, {
        ...options, reducedMotion, skipping: player.skip,
        motion: line.act?.motion || null,
      });
    };
    if (changed) { pendingOutfit = next.state.outfit; return spriteTransition.change(apply); }
    return apply();
  }
  function preloadNextLine() {
    if (!model || !currentLine) return;
    const next = turnLines.filter(line => line.index > currentLine.index)
      .sort((a, b) => a.index - b.index)[0];
    if (!next) return;
    const preview = resolveSpriteState(model, resolved, next, { costume: settings.advOutfit || model.defaultCostume });
    sprite.preloadState(preview);
  }
  function restoreSprite() {
    if (!resolved || !currentLine || ['input', 'error', 'idle'].includes(player.state)) return;
    // Resume the already resolved face. Overlay and OS changes never act again.
    sprite.apply({ ...resolved, state: { ...resolved.state, fx: null } }, { reducedMotion, skipping: player.skip });
  }
  function animationOptions(values) {
    return { advBlink: values.advBlink !== false, advLipSync: values.advLipSync !== false };
  }
  function syncControls() {
    if (!player.skip) temporarySkip = false;
    textbox.setCanSend(!controller.busy);
    const input = player.state === 'input' || player.state === 'error';
    const overlayActive = Boolean(drawer || backlog || externalOverlay || configPanel?.isOpen || bgPanel?.isOpen);
    for (const name of ['auto', 'skip', 'replay_voice', 'hide']) quickMenu.setEnabled(name, !input && !overlayActive);
    quickMenu.setToggle('auto', player.auto); quickMenu.setToggle('skip', player.skip);
    voiceModeButton.setAttribute('aria-pressed', String(Boolean(deps.voiceEnabled?.())));
    voiceModeButton.title = deps.voiceEnabled?.() ? '关闭自动播放语音回复' : '自动播放语音回复';
    quickMenu.setHidden(player.hidden); textbox.setHidden(player.hidden);
    tools.hidden = player.hidden;
    stage.stage.classList.toggle('is-window-hidden', player.hidden);
    stage.stage.classList.toggle('reduced-motion', reducedMotion);
    quickMenu.setModelStatus(deps.getModelStatus?.() || { label: '对话模型', state: controller.busy ? 'busy' : 'ok' });
    if (configPanel?.isOpen && !configPanel.element.contains(doc.activeElement)) {
      configPanel.focusFirstControl?.();
    }
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
    if (!externalOverlay && !backlog && !configPanel?.isOpen) restoreSprite();
    if (savedFocus?.isConnected) savedFocus.focus();
    syncControls();
  }
  function openDrawer(kind) {
    closeBgPanel();
    closeDrawer();
    closeLog();
    if (configPanel?.isOpen) configPanel.close({ revert: true });
    const node = dom.sidebar;
    if (!node?.parentNode) return;
    savedFocus = doc.activeElement;
    player.suspend('overlay'); deps.stopOtherVoice?.(); sprite.suspend(); textbox.completeReveal(); textbox.glyph(false);
    const element = doc.createElement('div'); element.className = `adv-drawer adv-drawer-${kind}`;
    element.setAttribute('role', 'dialog'); element.setAttribute('aria-modal', 'true');
    element.setAttribute('aria-label', '会话列表');
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
    closeBgPanel();
    closeDrawer();
    closeLog();
    if (configPanel?.isOpen) configPanel.close({ revert: true });
    savedFocus = doc.activeElement;
    temporarySkip = false;
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
      getHiddenFrom: () => (turnId && player.isPresenting()
        ? { messageIds: [turnId, storedTurnId].filter(Boolean), after: currentLine?.index ?? -1 } : null),
      voiceEnabled: () => Boolean(deps.voiceEnabled?.()),
      loadOlderHistory: session => (deps.loadOlderHistory || deps.loadOlderSessionHistory)?.(session),
      playUserVoice: (msg, ctl) => (deps.playUserVoice || deps.playVoice)?.(msg, ctl),
      stopOtherVoice: () => deps.stopOtherVoice?.(),
      playLegacyVoice: (msg, ctl) => deps.playLegacyVoice?.(msg, ctl),
      wallNow: () => deps.wallNow?.() ?? Date.now(),
      onSettings: openErrorSettings,
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
      case 'skip':
        if (!['input', 'error', 'suspended'].includes(player.state)) {
          temporarySkip = false;
          const next = !player.skip;
          player.setSkip(next);
          if (next) sprite.suspend();
          else restoreSprite();
        }
        break;
      case 'skip-hold-start':
        if (!['input', 'error', 'suspended'].includes(player.state) && !player.skip) {
          temporarySkip = true;
          player.setSkip(true);
          sprite.suspend();
        }
        break;
      case 'skip-hold-end':
        if (temporarySkip) {
          temporarySkip = false;
          if (player.skip) {
            player.setSkip(false);
            restoreSprite();
          }
        }
        break;
      case 'toggle_hide':
      case 'hide':
        if (player.hidden) unhide();
        else { player.hide(); player.suspend('hidden'); sprite.suspend(); }
        break;
      case 'unhide': unhide(); break;
      case 'log': openLog(); break;
      case 'background': openBgPanel(); break;
      case 'sessions': openDrawer('sessions'); break;
      case 'close_overlay':
        if (backlog) closeLog();
        else if (configPanel?.isOpen) configPanel.close({ revert: true });
        else if (bgPanel?.isOpen) bgPanel.close();
        else if (drawer) closeDrawer();
        else deps.closeOverlay?.();
        break;
      case 'settings':
      case 'config':
        closeBgPanel();
        closeLog();
        configPanel.open();
        break;
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
    restoreSprite();
  }
  const shortcuts = createShortcuts(stage.stage, {
    clock,
    getContext: () => ({ stage: stage.stage, hidden: player.hidden,
      overlayOpen: Boolean(stage.stage.classList.contains('is-home') || drawer || backlog || externalOverlay || configPanel?.isOpen || bgPanel?.isOpen),
      subtitleNone: settings.advSubtitleLanguage === 'none',
      playerInput: player.state === 'input',
      composing: textbox.composing }),
    onAction: action,
  });
  const unlockAudio = () => {
    try {
      const ctx = audio?.ensureContext?.();
      ctx?.resume?.()?.catch?.(() => {});
    } catch (_) {}
  };
  listen(stage.stage, 'pointerdown', unlockAudio);
  listen(stage.stage, 'keydown', unlockAudio);
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
    if (reducedMotion) spriteTransition.cancel();
    player.setSettings({ reducedMotion });
    // Blink and lip sync stop or resume at once, in either direction.
    sprite.setAnimationOptions({ reducedMotion });
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
    else { player.resume('dialog'); if (!drawer && !backlog && !configPanel?.isOpen) restoreSprite(); }
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
      let painted;
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
      } else if (model) {
        painted = paint();
        preloadNextLine();
      }
      // Signed image URLs are transient; renew once, a minute before expiry.
      const spriteFiles = Object.values(model?.sprites || {}).flatMap(faces =>
        faces && typeof faces === 'object' ? Object.values(faces) : []
      );
      const layerFiles = Object.values(model?.layers || {});
      const minExpiry = [...spriteFiles, ...layerFiles].reduce((min, item) => {
        try {
          const exp = Number(new URL(item?.file, 'http://local').searchParams.get('exp'));
          return exp > 0 ? (min ? Math.min(min, exp) : exp) : min;
        } catch {
          return min;
        }
      }, 0);
      if (minExpiry > 0) {
        const delay = Math.max(1000, minExpiry * 1000 - (deps.wallNow?.() ?? Date.now()) - 60000);
        refreshTimer = clock.setTimeout(() => { refreshTimer = null; loadPortrait(true); }, delay);
      }
      await painted;
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
    const mounted = sessionId !== undefined;
    const character = deps.getCharacter?.();
    const changedCharacter = switched || characterKey !== character?.id;
    const changedOutfit = outfit !== nextSettings.advOutfit;
    const changedLanguage = settings.advPrimaryLanguage !== nextSettings.advPrimaryLanguage || settings.advSubtitleLanguage !== nextSettings.advSubtitleLanguage;
    settings = nextSettings;
    if (switched) {
      sprite.setStaging(staging.enter(), { instant: true });
      if (sessionId !== undefined) drafts.set(sessionId, textbox.draft);
      if (configPanel?.isOpen) configPanel.close({ revert: true });
      closeBgPanel();
      closeDrawer(); closeLog(); resetInput(); turnId = null; currentLine = null; failedUser = null;
      textbox.clearAnnouncement();
      textbox.setDraft(drafts.get(session?.id) || '');
    }
    sessionId = session?.id; characterKey = character?.id; outfit = settings.advOutfit;
    player.setSettings(playerSettings());
    sprite.setAnimationOptions(animationOptions(settings));
    syncBackground();
    if (changedCharacter) {
      pendingOutfit = null;
      if (!switched) {
        controller.stop();
        resetInput(); turnId = null; currentLine = null;
      }
      const swap = () => {
        model = null; resolved = null; sprite.setModel(null); refreshAttempted = false;
        return loadPortrait();
      };
      if (mounted) spriteTransition.change(swap);
      else swap();
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
    if (externalOverlay || configPanel?.isOpen) player.suspend('dialog');
    syncControls();
  }
  function resetInput() {
    deps.cancelMic?.();
    liveMessage = null;
    turnLines = [];
    player.reset();
    const idle = Symbol('session input');
    player.startTurn(idle); player.settle(idle, { ok: true });
    textbox.enterInput({ canSend: !controller.busy });
  }
  disposers.push(controller.on('turnstart', event => {
    update();
    if (event.originId !== sessionId) return;
    liveMessage = { ...event.assistantMessage, lines: [...event.assistantMessage.lines || []] };
    turnLines = liveMessage.lines;
    failedUser = event.userMessage; turnId = event.assistantMessage.id; storedTurnId = null; currentLine = null;
    textbox.setDraft(''); textbox.clearError();
    textbox.setSpeaker({ name: deps.getCharacter?.()?.name || '角色', mode: 'character' });
    player.startTurn(turnId); syncControls();
    // Sending hides the input or removes the retry button that had focus. Focus
    // then falls to the page body, where Space, Enter and Esc never reach the
    // stage's shortcuts, so hand it to the stage unless an overlay holds it.
    const focused = doc.activeElement;
    if (!focused || focused === doc.body || !doc.body.contains(focused) || stage.textbox.contains(focused)) {
      stage.stage.focus?.({ preventScroll: true });
    }
    if (drawer || backlog) player.suspend('overlay');
    if (externalOverlay || configPanel?.isOpen) player.suspend('dialog');
  }));
  disposers.push(controller.on('text', () => { /* Legacy streaming text remains in controller/history. */ }));
  disposers.push(controller.on('line', event => {
    if (event.messageId === turnId && deps.getSession()?.id === sessionId) {
      if (liveMessage) {
        const index = liveMessage.lines.findIndex(l => l.index === event.line.index);
        if (index < 0) liveMessage.lines.push(event.line);
        else liveMessage.lines[index] = event.line;
        liveMessage.lines.sort((a, b) => a.index - b.index);
      }
      player.pushLine(turnId, event.line);
      preloadNextLine();
    }
  }));
  disposers.push(controller.on('line_audio', event => {
    if (event.messageId === turnId && deps.getSession()?.id === sessionId) player.setLineAudio(turnId, event.line, event);
  }));
  for (const kind of ['done', 'error', 'cancelled']) disposers.push(controller.on(kind, event => {
    if (event.originId !== sessionId || deps.getSession()?.id !== sessionId) return;
    if (!turnId) { textbox.enterInput({ canSend: !controller.busy }); syncControls(); return; }
    storedTurnId = event.message?.id ?? null;
    const hasContent = msg => Boolean(msg?.content?.trim() || msg?.japanese?.trim() ||
      msg?.lines?.some(l => l.zh?.trim() || l.ja?.trim()));
    const isEmptyDone = kind === 'done' && !hasContent(event.message) && !hasContent(liveMessage);
    liveMessage = null;
    if (isEmptyDone) {
      if (failedUser && !failedUser.failed) {
        failedUser.failed = { error: '模型这次没有返回任何内容', code: 'empty_reply' };
      }
      player.settle(turnId, { kind: 'error', error: { message: '模型这次没有返回任何内容', code: 'empty_reply' } });
      syncControls();
      return;
    }
    player.settle(turnId, kind === 'done' ? { ok: true } : { kind, error: { message: event.error, code: event.code || event.kind } });
    syncControls();
  }));
  disposers.push(player.on('state', syncControls));
  if (deps.subscribe) disposers.push(deps.subscribe(update));
  listen(deps.viewport || globalThis.window, 'resize', () => {
    if (!currentLine || !['revealing', 'waiting_advance', 'waiting_audio'].includes(player.state)) return;
    const wasRevealing = player.state === 'revealing';
    player.repaginate();
    textbox.reflow({ pageIndex: player.page });
    if (wasRevealing) player.advance();
  });
  loadBackgrounds();
  update();
  return {
    update, player, textbox, sprite, stage, action, configPanel, background, bgPanel, closeDrawer,
    backgroundUrl: (id, time, work) => selectionUrl(backgrounds, { id: backgroundKey(id ?? '', work || null), time }),
    listBackgrounds: () => backgrounds.map(({ key, name, work }) => ({ key, name, work })),
    isConfigOpen: () => Boolean(configPanel?.isOpen),
    get backlog() { return backlog; },
    unmount() {
      if (destroyed) return;
      // Stop callbacks before resetting renderers or restoring borrowed nodes.
      for (const off of disposers.splice(0)) off?.();
      closeLog();
      closeDrawer();
      configPanel.destroy();
      bgPanel.destroy();
      background.destroy();
      spriteTransition.destroy();
      destroyed = true; ++portraitRevision;
      portraitRequest?.abort();
      deps.cancelMic?.();
      if (refreshTimer !== null) clock.clearTimeout(refreshTimer);
      drafts.clear();
      shortcuts.destroy(); player.reset(); audio.stopLine({ fadeMs: 0 });
      sprite.destroy(); quickMenu.destroy(); textbox.destroy(); stage.destroy();
    },
  };
}
