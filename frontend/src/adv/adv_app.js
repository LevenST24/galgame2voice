// @ts-check
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

const MAX_TIMELINE_TURNS = 5;

export function formatTurnTimelineEntry(entry) {
  const fmt = v => (v != null && Number.isFinite(v) ? `${Math.round(v)}ms` : '--');
  return `#${entry.turn_seq} 首字: ${fmt(entry.llm_first_token_ms)} | 首句切出: ${fmt(entry.first_sentence_parsed_ms)} | 首音就绪: ${fmt(entry.first_tts_ready_ms)} | 首音播放: ${fmt(entry.first_audio_play_ms)}`;
}

function validAffectionLevel(value) {
  return Number.isInteger(value) && value >= 1 && value <= 5 ? value : null;
}

/** Mount one disposable renderer. Controller remains the sole history writer.
 * dom: { root, sidebar? }
 * deps: { controller, audio, getSession, voiceEnabled, loadPortrait, loadAffection?, getCharacter,
 *   clock?, raf?, cancelRaf?, matchMedia?, subscribe?, subscribeOverlay?, ...actions }
 */
// The earliest `exp` (seconds) among signed media URLs, or 0 when none is signed.
function earliestExpiry(urls) {
  return urls.reduce((min, url) => {
    try {
      const exp = Number(new URL(url, 'http://local').searchParams.get('exp'));
      return exp > 0 ? (min ? Math.min(min, exp) : exp) : min;
    } catch {
      return min;
    }
  }, 0);
}

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
  let destroyed = false, sessionId, characterKey, boundProfileId, outfit, turnId = null;
  let settings, model = null, resolved = null, currentLine = null, failedUser = null, liveMessage = null;
  // The controller stores a finished reply under a new id; LOG must still know it is this turn.
  let storedTurnId = null;
  let turnLines = [];
  let pendingOutfit = null;
  let portraitRevision = 0, portraitRequest = null, refreshTimer = null, refreshAttempted = false;
  // The shown character's affection level (1-5). Higher levels let lines blush
  // without an explicit act.blush, as the resolver's affection fallback says.
  let affectionLevel = 1, affectionRevision = 0, affectionRequest = null;
  let drawer = null, backlog = null, externalOverlay = false, savedFocus = null, temporarySkip = false;
  /** @type {string | null} */
  let backlogRenderKey = null;
  let voiceSequence = 0;
  let turnSeq = 0, activeTurn = null;
  const recentTurnTimelines = [];
  function ensureActiveTurnEntry(turn) {
    if (!turn.entry) {
      turn.entry = {
        turn_seq: turn.turn_seq,
        request_start_ms: 0,
        llm_first_token_ms: null,
        first_sentence_parsed_ms: null,
        first_tts_ready_ms: null,
        first_audio_play_ms: null,
        total_ms: null,
      };
      recentTurnTimelines.push(turn.entry);
      if (recentTurnTimelines.length > MAX_TIMELINE_TURNS) {
        recentTurnTimelines.shift();
      }
    }
    return turn.entry;
  }
  function syncTelemetryPerfSummary() {
    if (!dom.gDashPerfSummary) return;
    if (recentTurnTimelines.length === 0) {
      dom.gDashPerfSummary.textContent = '等待对话数据...';
      return;
    }
    dom.gDashPerfSummary.textContent = [...recentTurnTimelines]
      .reverse()
      .map(formatTurnTimelineEntry)
      .join('\n');
  }
  const moved = new Map();
  const drafts = new Map();
  let replayToken = 0, stageReplaying = false;
  let lastVoiceEnabled = Boolean(deps.voiceEnabled?.());
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
    // An image that will not load may only have an expired signed URL.
    onLoadFailed: () => { renewBackgroundsIfExpiring(); },
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
    onAdvance: () => action('advance'),
    onSend: text => !controller.busy && controller.send(text),
    onMic: () => {
      player.beginPlayerEdit();
      stopStageReplay();
      deps.onMic?.(textbox.inputElement, textbox.micButton);
    },
    onAction: (actionName, data) => {
      if (actionName === 'player_edit') { deps.disarmMicAutoSend?.(); player.beginPlayerEdit(); stopStageReplay(); sprite.suspend(); syncControls(); }
      if (actionName === 'swap_languages') {
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
    /**
     * @param {number} index
     * @param {any} chunks
     * @param {{ onStart?: () => void }} [opts]
     */
    async play(index, chunks, { onStart } = {}) {
      deps.stopOtherVoice?.();
      const key = turnId;
      const sequence = ++voiceSequence;
      const turnForLine = activeTurn;
      const handleStart = () => {
        if (turnForLine && turnForLine.first_audio_play_ms === null) {
          const playMs = Math.max(0, Math.round(clock.now() - turnForLine.startedAtMs));
          turnForLine.first_audio_play_ms = playMs;
          const entry = ensureActiveTurnEntry(turnForLine);
          entry.first_audio_play_ms = playMs;
          syncTelemetryPerfSummary();
        }
        onStart?.();
      };
      textbox.setVoice({ active: true });
      const lipSync = sprite.startLipSync({ audioPlayer: audio, characterVoice: true });
      let result;
      // playLine resumes a suspended context itself and gives up after a bounded wait.
      try { result = await audio.playLine(index, chunks, { msgId: key, onStart: handleStart }); }
      catch { result = 'failed'; }
      if (!destroyed && key === turnId && sequence === voiceSequence) textbox.setVoice({ active: false });
      // A later line may own the mouth by now; this line's token cannot stop it.
      sprite.stopLipSync(lipSync);
      return result;
    },
    stop(options) { ++voiceSequence; audio.stopLine(destroyed ? { fadeMs: 0 } : options); textbox.setVoice({ active: false }); sprite.stopLipSync(); },
    progress: () => audio.lineProgress?.() ?? null,
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
      // Saved partial lines are not resent from here. 继续 only leaves the error.
      const retry = controller.canRetry(failedUser)
        ? [{ label: '重新发送', run: () => controller.retry(failedUser) }]
        : [{ label: '继续', run: () => player.dismissError() }];
      textbox.showError({ message: String(error?.message || error || '回复失败'), actions: [
        ...retry,
        { label: '去设置', run: () => openErrorSettings(error) },
      ] });
    },
  };
  const player = createLinePlayer({ clock, voice, render,
    paginate: (line, primary) => textbox.paginate(line, primary), settings: playerSettings() });
  textbox.setPlayer(player);
  /** @param {{ fadeMs?: number }} [options] */
  function stopStageReplay(options = { fadeMs: 40 }) {
    if (!stageReplaying) return false;
    stageReplaying = false;
    ++replayToken;
    voice.stop(options);
    player.resume('replay');
    return true;
  }
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
  if (deps.onHome) {
    const button = doc.createElement('button'); button.type = 'button'; button.textContent = '首页';
    button.dataset.action = 'home'; button.setAttribute('aria-label', '返回首页');
    listen(button, 'click', () => action('home')); tools.appendChild(button);
  }

  const configPanel = createConfigPanel(deps.readingSettingsContainer || stage.overlay, {
    embedded: Boolean(deps.readingSettingsContainer),
    getSession: () => deps.getSession?.(),
    getCharacter: () => deps.getCharacter?.(),
    getCostumes: () => model?.costumes || sprite.getModel?.()?.costumes || deps.getCostumes?.() || [],
    saveSettings: patch => {
      const confirmation = deps.saveSettings?.(patch, { confirm: true });
      update();
      return confirmation;
    },
    onSaveError: (error, origin) => deps.onSettingsSaveError?.(error, origin),
    voiceEnabled: () => Boolean(deps.voiceEnabled?.()),
    toggleVoice: () => {
      deps.toggleVoice?.();
      update();
    },
    onModelSettings: () => deps.onModelSettings?.(),
    onSessionSettings: () => (deps.onSessionSettings || deps.onSettings)?.(),
    onOpenSettings: deps.onOpenSettings,
    onReturnHome: deps.onHome ? () => action('home') : undefined,
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
        if (currentLine && player.isPresenting()) {
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
      if (currentLine && player.isPresenting()) {
        player.repaginate();
        textbox.reflow({ pageIndex: player.page });
      }
    },
    onOpen: () => {
      deps.cancelMic?.();
      player.suspend('dialog');
      stopStageReplay();
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
      if (!externalOverlay) player.resume('dialog');
      if (!drawer && !backlog && !externalOverlay) restoreSprite();
      syncControls();
      deps.onReadingSettingsClose?.();
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
    saveSettings: patch => {
      deps.saveSettings?.(patch);
      // main.js sends no session update for its own save, so reread it: reopening the panel
      // or picking a time must start from this choice, not the one loaded at mount.
      update();
    },
    onPreview: selection => {
      appliedBackground = shownBackground(selection);
      background.setSelection(appliedBackground);
    },
    onOpen: () => {
      deps.cancelMic?.();
      player.suspend('dialog');
      stopStageReplay();
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
  let bgPanelOpenToken = 0;
  function openBgPanel() {
    closeDrawer();
    closeLog();
    if (configPanel?.isOpen) configPanel.close({ revert: true });
    const token = ++bgPanelOpenToken;
    // The panel's thumbnails are built from the list, so renew expiring URLs first.
    const renewal = renewBackgroundsIfExpiring();
    if (renewal) renewal.then(() => {
      if (destroyed || token !== bgPanelOpenToken) return;
      if (stage.stage.classList.contains('is-home') || drawer || backlog || externalOverlay || configPanel?.isOpen) return;
      bgPanel.open();
    });
    else bgPanel.open();
  }
  function closeBgPanel() {
    ++bgPanelOpenToken;
    if (bgPanel?.isOpen) bgPanel.close();
  }
  async function loadBackgrounds({ keepOnError = false } = {}) {
    let list = [];
    let workList = [];
    try {
      const data = await (deps.fetchBackgrounds || (() => requestJson('/api/backgrounds')))();
      list = Array.isArray(data?.backgrounds) ? data.backgrounds : [];
      workList = Array.isArray(data?.works) ? data.works : [];
    } catch {
      if (keepOnError) return;
      list = [];
      workList = [];
    }
    if (destroyed) return;
    backgrounds = list;
    works = workList;
    backgroundsExpireAt = earliestExpiry(list.flatMap(item =>
      item?.variants && typeof item.variants === 'object' ? Object.values(item.variants) : []));
    background.setList(list);
    // Auto resolves against the list, so re-check once it arrives.
    if (settings) syncBackground();
    if (bgPanel?.isOpen) bgPanel.refresh();
  }
  // Background image URLs are signed for about 12 hours. Check the wall clock when
  // they are about to be used (a timer would not run while the computer sleeps),
  // and read the list again if they expire within a minute.
  let backgroundsExpireAt = 0;
  /** @type {Promise<void> | null} */
  let backgroundRenewal = null;
  function renewBackgroundsIfExpiring() {
    if (backgroundRenewal) return backgroundRenewal;
    if (destroyed || !backgroundsExpireAt) return null;
    if ((deps.wallNow?.() ?? Date.now()) < backgroundsExpireAt * 1000 - 60000) return null;
    backgroundRenewal = loadBackgrounds({ keepOnError: true }).finally(() => { backgroundRenewal = null; });
    return backgroundRenewal;
  }
  listen(doc, 'visibilitychange', () => { if (doc.visibilityState === 'visible') renewBackgroundsIfExpiring(); });

  function paint(line = currentLine || { emotion: 'gentle', act: {} }, options = {}) {
    if (!line || !model) return;
    const costume = options.costume !== undefined ? options.costume : (settings.advOutfit || model.defaultCostume);
    const next = resolveSpriteState(model, resolved, line, { costume, affectionLevel });
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
    const preview = resolveSpriteState(model, resolved, next, { costume: settings.advOutfit || model.defaultCostume, affectionLevel });
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
    // Embedded in the settings center, the dialog owns focus; pulling it back would yank the user off its tabs and buttons.
    if (configPanel?.isOpen && !deps.readingSettingsContainer && !configPanel.element.contains(doc.activeElement)) {
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
    deps.cancelMic?.();
    player.suspend('overlay'); stopStageReplay(); deps.stopOtherVoice?.(); sprite.suspend(); textbox.completeReveal(); textbox.glyph(false);
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
  function computeBacklogRenderKey(session = deps.getSession?.(), character = deps.getCharacter?.()) {
    const recoveryVoice = deps.getRecoveryVoice?.();
    return [
      session?.id ?? '',
      settings.advPrimaryLanguage ?? '',
      settings.advSubtitleLanguage ?? '',
      settings.advPlayerName ?? '',
      character?.id ?? '',
      character?.name ?? '',
      deps.voiceEnabled?.() ? '1' : '0',
      recoveryVoice ? '1' : '0',
      recoveryVoice?.id ?? '',
      recoveryVoice?.name ?? '',
    ].map(value => {
      const part = String(value);
      return `${part.length}:${part}`;
    }).join('|');
  }
  function closeLog() {
    if (!backlog) return;
    backlog.destroy();
    backlog = null;
    backlogRenderKey = null;
    stage.textbox.inert = false; stage.quickmenu.inert = false; tools.inert = false;
    player.resume('overlay');
    if (!externalOverlay && !drawer) restoreSprite();
    if (savedFocus?.isConnected) savedFocus.focus();
    syncControls();
  }
  // A finished reply stores new line objects. Repair writes those, while this
  // turn's stage line and the player's audio table still hold the earlier ones.
  function adoptRepairedLineAudio(messageId, lineIndex, audio) {
    const sameTurn = messageId === turnId || messageId === storedTurnId
      || (liveMessage != null && messageId === liveMessage.id);
    if (!sameTurn || !audio || !Number.isInteger(lineIndex) || turnId == null) return;
    const stageLine = turnLines.find(line => line.index === lineIndex);
    if (stageLine) stageLine.audio = audio;
    if (currentLine?.index === lineIndex) currentLine.audio = audio;
    player.setLineAudio(turnId, lineIndex, audio);
  }
  const regenerateLineAudio = deps.regenerateLineAudio && (async request => {
    const result = await deps.regenerateLineAudio(request);
    adoptRepairedLineAudio(request.messageId, request.lineIndex, result.audio);
    return result;
  });
  function openLog() {
    closeBgPanel();
    closeDrawer();
    closeLog();
    if (configPanel?.isOpen) configPanel.close({ revert: true });
    savedFocus = doc.activeElement;
    temporarySkip = false;
    player.setSkip(false);
    player.suspend('overlay');
    deps.cancelMic?.();
    const stoppedReplay = stopStageReplay({ fadeMs: 0 });
    deps.stopOtherVoice?.();
    if (!stoppedReplay) audio.stopLine?.({ fadeMs: 0 });
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
      regenerateLineAudio,
      getRecoveryVoice: () => deps.getRecoveryVoice?.(),
      onChooseVoice: () => { closeLog(); deps.onSettings?.(); },
      wallNow: () => deps.wallNow?.() ?? Date.now(),
      onSettings: openErrorSettings,
      onClose: closeLog,
    });
    backlogRenderKey = computeBacklogRenderKey();
    stage.textbox.inert = true; stage.quickmenu.inert = true; tools.inert = true;
    syncControls();
  }
  function action(name) {
    if (destroyed) return;
    switch (name) {
      case 'advance': stopStageReplay(); flushRelayout(); player.advance(); break;
      case 'toggle_auto':
      case 'auto': if (!player.hidden && (stageReplaying || !['input', 'error', 'suspended'].includes(player.state))) player.setAuto(!player.auto); break;
      case 'toggle_skip':
      case 'skip':
        if (player.hidden) break;
        stopStageReplay();
        if (!['input', 'error', 'suspended'].includes(player.state)) {
          temporarySkip = false;
          const next = !player.skip;
          player.setSkip(next);
          if (next) sprite.suspend();
          else restoreSprite();
        }
        break;
      case 'skip-hold-start':
        if (player.hidden) break;
        stopStageReplay();
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
        // Hiding pauses AUTO and the next line, but a voice already playing (or replaying) finishes.
        else { player.hide(); sprite.suspend(); }
        break;
      case 'unhide': unhide(); break;
      case 'log': openLog(); break;
      case 'background': openBgPanel(); break;
      case 'sessions': openDrawer('sessions'); break;
      case 'close_overlay':
        ++bgPanelOpenToken;
        if (backlog) closeLog();
        else if (configPanel?.isOpen) configPanel.close({ revert: true });
        else if (bgPanel?.isOpen) bgPanel.close();
        else if (drawer) closeDrawer();
        else deps.closeOverlay?.();
        break;
      case 'settings':
      case 'config':
        if (deps.onOpenSettings) {
          closeBgPanel(); closeLog(); closeDrawer();
          configPanel.close({ revert: true });
          deps.onOpenSettings();
          break;
        }
        // A standalone stage can still host its local experience editor.
      case 'reading_settings':
        closeBgPanel();
        closeLog();
        if (deps.onOpenReadingSettings) deps.onOpenReadingSettings();
        else configPanel.open();
        break;
      case 'home': deps.onHome?.(); break;
      case 'model_status': deps.onModelSettings?.(); break;
      case 'swap_languages':
        // Only swap while presenting a visible line; hidden, input, error and their suspended states have no line on stage.
        if (!player.isPresenting() || player.hidden) break;
        textbox.swapLanguages(); break;
      case 'new_session': deps.onNewSession?.(); break;
      case 'stop':
        ++bgPanelOpenToken;
        stopStageReplay();
        controller.stop();
        if (player.isPresenting()) player.beginPlayerEdit();
        break;
      case 'input':
        // AUTO or SKIP would advance to the next line and hide the box the player just opened.
        // Lines stay queued; they are only dropped once the player actually starts typing.
        temporarySkip = false;
        if (player.skip) { player.setSkip(false); restoreSprite(); }
        player.setAuto(false);
        textbox.prepareInput({ canSend: !controller.busy });
        syncControls();
        break;
      case 'voice_mode': deps.toggleVoice?.(); if (!deps.voiceEnabled?.()) stopStageReplay(); backlog?.stopReplay(); update(); break;
      case 'replay_voice': {
        const line = currentLine;
        const overlayActive = Boolean(drawer || backlog || externalOverlay || configPanel?.isOpen || bgPanel?.isOpen);
        if (line?.audio?.chunks?.length && !controller.busy && !player.hidden && !overlayActive && !['input', 'error'].includes(player.state)) {
          const key = turnId, token = ++replayToken;
          stageReplaying = true;
          player.suspend('replay');
          voice.play(line.index, line.audio.chunks).then(() => {
            if (!destroyed && key === turnId && token === replayToken) {
              stageReplaying = false;
              player.resume('replay');
              syncControls();
            }
          });
        }
        break;
      }
    }
    syncControls();
  }
  function unhide() {
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
    if (open) { ++bgPanelOpenToken; deps.cancelMic?.(); player.suspend('dialog'); stopStageReplay(); sprite.suspend(); textbox.completeReveal(); textbox.glyph(false); }
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
      ++affectionRevision; affectionRequest?.abort(); affectionRequest = null; affectionLevel = 1;
      model = null; sprite.setModel(null); status.hidden = false; status.textContent = '此角色暂无立绘'; return;
    }
    if (!refresh) {
      // Read the saved level beside the art; a slow or failed read never holds the portrait back.
      const affRevision = ++affectionRevision;
      affectionRequest?.abort(); affectionRequest = new AbortController();
      const affSignal = affectionRequest.signal;
      affectionLevel = 1;
      Promise.resolve().then(() => deps.loadAffection?.(character.id, { signal: affSignal })).then(level => {
        if (destroyed || affRevision !== affectionRevision) return;
        const nextLevel = validAffectionLevel(level) ?? 1;
        if (nextLevel === affectionLevel) return;
        affectionLevel = nextLevel;
        if (model && resolved && currentLine?.act?.blush == null) {
          const next = resolveSpriteState(model, resolved, {
            emotion: currentLine?.emotion || 'gentle',
            act: { face: resolved.state.face, pose: resolved.state.pose, fx: null },
          }, { costume: resolved.state.outfit, affectionLevel });
          if (next && next.state.blush !== resolved.state.blush) {
            resolved = next;
            sprite.apply(resolved, { reducedMotion, skipping: player.skip });
          }
        }
        preloadNextLine();
      }, () => {});
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
      const minExpiry = earliestExpiry([...spriteFiles, ...layerFiles].map(item => item?.file));
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
    const nextVoiceEnabled = Boolean(deps.voiceEnabled?.());
    if (lastVoiceEnabled && !nextVoiceEnabled) stopStageReplay();
    lastVoiceEnabled = nextVoiceEnabled;
    const session = deps.getSession();
    const nextSettings = pickSettings(session?.settings, DEFAULT_SESSION_SETTINGS);
    const switched = sessionId !== session?.id;
    const mounted = sessionId !== undefined;
    const character = deps.getCharacter?.();
    const changedCharacter = switched || characterKey !== character?.id;
    const changedBinding = switched || boundProfileId !== nextSettings.voiceProfileId;
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
    sessionId = session?.id; characterKey = character?.id; boundProfileId = nextSettings.voiceProfileId; outfit = settings.advOutfit;
    player.setSettings(playerSettings());
    sprite.setAnimationOptions(animationOptions(settings));
    syncBackground();
    if (changedCharacter) {
      pendingOutfit = null;
      // A character imported after the list loaded can bring its own work's backgrounds.
      if (mounted && character?.work && !works.some(entry => entry?.id === character.work)) loadBackgrounds();
      if (!switched) {
        const presentingTurn = turnId !== null && player.state !== 'input' && player.state !== 'error';
        if (!changedBinding && (controller.busy || presentingTurn)) {
          if (presentingTurn) {
            textbox.setSpeaker({ name: character?.name || '角色', mode: 'character' });
          }
        } else {
          controller.stop();
          const idle = resetInput(); turnId = null; currentLine = null;
          if (/^VOICE_PROFILE_/.test(failedUser?.failed?.code || '')
            && controller.canRetry(failedUser) && !textbox.draft.trim()) {
            player.settle(idle, { kind: 'error', error: {
              message: '已重新选择角色音色，可以重新发送上一条问题。', code: 'VOICE_PROFILE_CHANGED',
            } });
          }
        }
      }
      const swap = () => {
        model = null; resolved = null; sprite.setModel(null); refreshAttempted = false;
        return loadPortrait();
      };
      if (mounted) spriteTransition.change(swap);
      else swap();
    } else if (changedOutfit) paint();
    if (changedLanguage && currentLine && player.isPresenting()) {
      // Reflow presentation only; no activate/live announcement/acting/voice replay.
      player.repaginate();
      textbox.reflow({ pageIndex: player.page });
    }
    textbox.refreshSettings();
    if (backlog) {
      const nextBacklogKey = computeBacklogRenderKey(session, character);
      if (nextBacklogKey !== backlogRenderKey || !backlog.rendersMessages(session?.messages)) {
        backlogRenderKey = nextBacklogKey;
        const top = backlog.listElement.scrollTop;
        backlog.renderAll();
        backlog.listElement.scrollTop = top;
      }
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
    stopStageReplay({ fadeMs: 0 });
    const idle = Symbol('session input');
    player.startTurn(idle); player.settle(idle, { ok: true });
    textbox.enterInput({ canSend: !controller.busy });
    return idle;
  }
  disposers.push(controller.on('turnstart', event => {
    update();
    if (event.originId !== sessionId) return;
    deps.cancelMic?.();
    liveMessage = { ...event.assistantMessage, lines: [...event.assistantMessage.lines || []] };
    turnLines = liveMessage.lines;
    failedUser = event.userMessage; turnId = event.assistantMessage.id; storedTurnId = null; currentLine = null;
    turnSeq += 1;
    activeTurn = { turn_seq: turnSeq, startedAtMs: clock.now(), first_audio_play_ms: null, entry: null };
    textbox.setDraft(''); textbox.clearError();
    textbox.setSpeaker({ name: deps.getCharacter?.()?.name || '角色', mode: 'character' });
    player.startTurn(turnId); stopStageReplay({ fadeMs: 0 }); syncControls();
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
  disposers.push(controller.on('text_complete', event => {
    if (event.messageId === turnId && deps.getSession()?.id === sessionId) player.textComplete(turnId);
  }));
  disposers.push(controller.on('line_audio', event => {
    if (event.messageId === turnId && deps.getSession()?.id === sessionId) player.setLineAudio(turnId, event.line, event);
  }));
  for (const kind of ['done', 'error', 'cancelled']) disposers.push(controller.on(kind, event => {
    if (event.originId !== sessionId || deps.getSession()?.id !== sessionId) return;
    if (kind === 'done') {
      // The turn's affection result is the level the next reply plays at. A failed
      // update reports a neutral level 1 with nothing earned; keep the known level then.
      const affection = event.meta?.affection;
      const level = validAffectionLevel(affection?.level);
      if (level !== null && !(level === 1 && !affection.score && !affection.points_earned)) {
        ++affectionRevision; affectionRequest?.abort(); affectionLevel = level;
      }
    }
    if (!turnId) { textbox.enterInput({ canSend: !controller.busy }); syncControls(); return; }
    storedTurnId = event.message?.id ?? null;
    const hasContent = msg => Boolean(msg?.content?.trim() || msg?.japanese?.trim() ||
      msg?.lines?.some(l => l.zh?.trim() || l.ja?.trim()));
    const isEmptyDone = kind === 'done' && !hasContent(event.message) && !hasContent(liveMessage);
    liveMessage = null;
    if (isEmptyDone) {
      if (failedUser && !failedUser.failed) {
        failedUser.failed = { error: '模型这次没有返回内容。请点「重新发送」再试；仍失败请检查「对话模型」设置。', code: 'empty_reply' };
      }
      player.settle(turnId, { kind: 'error', error: { message: '模型这次没有返回内容。请点「重新发送」再试；仍失败请检查「对话模型」设置。', code: 'empty_reply' } });
      syncControls();
      return;
    }
    if (kind === 'done' && activeTurn) {
      const telemetry = event.meta?.telemetry;
      if (telemetry) {
        const marks = telemetry.marks || telemetry;
        const entry = ensureActiveTurnEntry(activeTurn);
        const ttft = marks.llm_first_token ?? telemetry.t1_ttft_ms;
        const firstSentence = marks.first_sentence_parsed ?? telemetry.t2_first_sentence_ms;
        const firstTtsReady = marks.first_tts_ready ?? telemetry.t5_app_ttfa_ms;
        const total = telemetry.total_ms ?? event.meta.latencyMs;
        if (ttft != null) entry.llm_first_token_ms = Math.round(Number(ttft));
        if (firstSentence != null) entry.first_sentence_parsed_ms = Math.round(Number(firstSentence));
        if (firstTtsReady != null) entry.first_tts_ready_ms = Math.round(Number(firstTtsReady));
        if (total != null) entry.total_ms = Math.round(Number(total));
        syncTelemetryPerfSummary();
      }
    }
    player.settle(turnId, kind === 'done' ? { ok: true } : { kind, error: { message: event.error, code: event.code || event.kind } });
    syncControls();
  }));
  // A resize while an overlay or replay holds the player is applied once it resumes,
  // so the page laid out for the old window size does not stay on screen.
  let relayoutLine = null, activeState = player.state;
  const RELAYOUT_STATES = ['revealing', 'waiting_advance', 'waiting_audio'];
  function relayout() {
    if (!currentLine || !RELAYOUT_STATES.includes(player.state)) return;
    relayoutLine = null;
    const wasRevealing = player.state === 'revealing';
    player.repaginate();
    textbox.reflow({ pageIndex: player.page });
    if (wasRevealing) player.advance();
  }
  function flushRelayout() {
    if (relayoutLine && relayoutLine === currentLine) relayout();
  }
  disposers.push(player.on('state', syncControls));
  disposers.push(player.on('state', state => {
    if (state === 'suspended') return;
    activeState = state;
    if (!relayoutLine) return;
    // Let resume() finish restoring the line before the page is laid out again.
    if (relayoutLine === currentLine && RELAYOUT_STATES.includes(state)) queueMicrotask(flushRelayout);
    else relayoutLine = null;
  }));
  if (deps.subscribe) disposers.push(deps.subscribe(update));
  listen(deps.viewport || globalThis.window, 'resize', () => {
    if (currentLine && player.state === 'suspended' && RELAYOUT_STATES.includes(activeState)) relayoutLine = currentLine;
    else relayout();
  });
  loadBackgrounds();
  update();
  syncTelemetryPerfSummary();
  return {
    update, player, textbox, sprite, stage, action, configPanel, background, bgPanel, closeDrawer,
    getRecentTurnTimelines: () => recentTurnTimelines.map(item => ({ ...item })),
    prepareHome() {
      configPanel.close({ revert: true }); closeBgPanel(); closeLog(); closeDrawer();
      renewBackgroundsIfExpiring();
      temporarySkip = false;
      resetInput(); ++replayToken; turnId = null; currentLine = null;
      sprite.suspend(); syncControls();
    },
    backgroundUrl: (id, time, work) => {
      renewBackgroundsIfExpiring();
      return selectionUrl(backgrounds, { id: backgroundKey(id ?? '', work || null), time });
    },
    listBackgrounds: () => backgrounds.map(({ key, name, work }) => ({ key, name, work })),
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
      destroyed = true; ++portraitRevision; ++affectionRevision;
      portraitRequest?.abort(); affectionRequest?.abort();
      deps.cancelMic?.();
      if (refreshTimer !== null) clock.clearTimeout(refreshTimer);
      drafts.clear();
      shortcuts.destroy(); player.reset(); audio.stopLine({ fadeMs: 0 });
      sprite.destroy(); quickMenu.destroy(); textbox.destroy(); stage.destroy();
    },
  };
}
