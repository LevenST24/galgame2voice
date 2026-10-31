// 应用入口：事件绑定、渲染调度、设置协调、流式回复
import {
  state,
  loadState,
  saveState,
  createSession,
  deleteSession,
  getSession,
  getActive,
  addMessage,
  autoTitle,
  uid,
  DEFAULT_SESSION_SETTINGS,
} from './store.js';
import { createConversation } from './conversation.js';
import { createLineAudioRecovery } from './controllers/line_audio_recovery.js';
import { mountAdv } from './adv/adv_app.js';
import { getModelStatus } from './model_badge.js';
import { audioStore } from './voice.js';
import { fetchAndCacheAudio, putCachedAudioBlob } from './cache.js';
import { streamAudioController } from './audio_player.js';
import { renderSessionList, showToast, needsModelSettings } from './ui.js';
import { advActions, createTitleActions } from './adv/actions.js';
import { showTitle } from './adv/title_hook.js';
import { createPortraitCache } from './adv/portrait_cache.js';

import { initAuthInterceptor } from './auth.js';
import { createSettingsCenter } from './controllers/settings_center.js';
import { initSessionBackup } from './controllers/session_backup.js';
import { requestJson } from './api.js';
import { persistSession, deletePersistedSession, isSessionDeleting, fetchAllSessions, fetchSessionHistoryPage, earliestServerMessageId } from './session_api.js';
import { serverTimestamp, normalizeLines, pickSettings } from './session_data.js';
import { openModal, closeModal, closeAllModals, hasOpenModal, initSettingsUi, onModalChange } from './settings_ui.js';

// Modular controllers
import { initUpdateManager, loadSystemVersionInfo } from './controllers/update_manager.js';
import {
  initVoiceSettings,
  openSessionSettings,
  cancelSessionSettingsLoad,
  fetchVoiceProfiles,
  ensureSessionVoice,
  getVoiceProfiles,
  getActiveProfileId,
  invalidateScannedModels,
} from './controllers/voice_settings.js';
import {
  initGlobalSettings,
  openGlobalSettings,
  loadProviders,
  getCurrentActiveProvider,
  haveProvidersLoaded,
  hasProviderReadError,
} from './controllers/global_settings.js';
import {
  initAudioPlayerUi,
  playVoice,
  stopCurrentVoice,
  revokeAllCachedAudioUrls,
  toggleVoiceMode,
  getCurrentVoice,
  clearMicTimers,
  toggleMic,
  cancelMic,
} from './controllers/audio_player_ui.js';

// Stable integration point for the separately implemented title screen.
export { advActions };

const appDependencies = {
  state, getActive, getSession, addMessage, uid,
  saveState, persistSession, isSessionDeleting, audioStore, putCachedAudioBlob,
  fetchAndCacheAudio, streamAudioController, getCurrentVoice, stopCurrentVoice,
  clearMicTimers, renderSessionList,
  showToast, autoTitle, fetchSessionHistoryPage, earliestServerMessageId,
  getCurrentActiveProvider, getVoiceProfiles, haveProvidersLoaded, hasProviderReadError,
  showTitle,
};

export function mapBackendMessage(bm) {
  const msg = {
    id: `m_${bm.id}`,
    ...(Number.isSafeInteger(bm.id) && bm.id > 0 ? { serverMessageId: bm.id } : {}),
    role: bm.role,
    content: bm.content_chinese,
    japanese: bm.content_japanese || '',
    audioUrls: bm.audio_url ? [bm.audio_url] : [],
    ts: serverTimestamp(bm.created_at),
  };
  if (Array.isArray(bm.lines)) {
    msg.lines = normalizeLines(bm.lines);
  }
  return msg;
}

export function extractServerId(msg) {
  if (!msg) return null;
  if (Number.isSafeInteger(msg.serverMessageId) && msg.serverMessageId > 0) {
    return msg.serverMessageId;
  }
  if (typeof msg.id === 'string') {
    const match = /^m_(\d+)$/.exec(msg.id);
    if (match) {
      const parsed = Number(match[1]);
      if (Number.isSafeInteger(parsed) && parsed > 0) return parsed;
    }
  }
  return null;
}

// 可注入存储、流和渲染器，测试无需启动鉴权或麦克风。
export function initApp({ dom: injectedDom, initialize = true, ...callbacks } = {}) {
  const {
    state, getActive, getSession, addMessage, uid,
    saveState, persistSession, isSessionDeleting, audioStore, putCachedAudioBlob,
    fetchAndCacheAudio, streamAudioController, getCurrentVoice, stopCurrentVoice,
    clearMicTimers, renderSessionList,
    showToast, autoTitle, fetchSessionHistoryPage, earliestServerMessageId,
    getCurrentActiveProvider, getVoiceProfiles, haveProvidersLoaded, hasProviderReadError,
    showTitle: showTitleScreen,
  } = { ...appDependencies, ...callbacks };
  if (initialize) {
    initSettingsUi();
    initAuthInterceptor();
  }
  const $ = (id) => document.getElementById(id);
  const dom = injectedDom || {
    sidebar: $('sidebar'),
    sidebarClose: $('sidebarClose'),
    newChatBtn: $('newChatBtn'),
    sessionList: $('sessionList'),
    sessionCount: $('sessionCount'),
    globalSettingsBtn: $('globalSettingsBtn'),
    globalModal: $('globalModal'),
    sessionModal: $('sessionModal'),
    sSystem: $('sSystem'),
    sVoice: $('sVoice'),
    sVoiceActive: $('sVoiceActive'),
    sVoiceRescan: $('sVoiceRescan'),
    sVoiceDelete: $('sVoiceDelete'),
    sCustomVoiceBox: $('sCustomVoiceBox'),
    sCvName: $('sCvName'),
    sCvGpt: $('sCvGpt'),
    sCvSovits: $('sCvSovits'),
    sCvRef: $('sCvRef'),
    sCvPromptText: $('sCvPromptText'),
    sCvLang: $('sCvLang'),
    sCvCreate: $('sCvCreate'),
    sTemp: $('sTemp'),
    sTempVal: $('sTempVal'),
    sTopP: $('sTopP'),
    sTopPVal: $('sTopPVal'),
    sFreq: $('sFreq'),
    sFreqVal: $('sFreqVal'),
    sPres: $('sPres'),
    sPresVal: $('sPresVal'),
    sMaxTokens: $('sMaxTokens'),
    sCtx: $('sCtx'),
    sCtxVal: $('sCtxVal'),
    sAiAdaptiveVoice: $('sAiAdaptiveVoice'),
    sAdvReplyFormat: $('sAdvReplyFormat'),
    sTtsSpeed: $('sTtsSpeed'),
    sTtsSpeedVal: $('sTtsSpeedVal'),
    sTtsTopK: $('sTtsTopK'),
    sTtsTopKVal: $('sTtsTopKVal'),
    sTtsTopP: $('sTtsTopP'),
    sTtsTopPVal: $('sTtsTopPVal'),
    sTtsTemp: $('sTtsTemp'),
    sTtsTempVal: $('sTtsTempVal'),
    sSave: $('sSave'),
    sReset: $('sReset'),
    smTitle: $('smTitle'),
    gProvider: $('gProvider'),
    gProviderActive: $('gProviderActive'),
    gProviderActiveBadge: $('gProviderActiveBadge'),
    gAllowPrivateEndpoints: $('gAllowPrivateEndpoints'),
    gPrivateEndpointsStatus: $('gPrivateEndpointsStatus'),
    gProviderCard: $('gProviderCard'),
    gProviderDescBanner: $('gProviderDescBanner'),
    gProviderDesc: $('gProviderDesc'),
    gProviderKeyTag: $('gProviderKeyTag'),
    gKeyStatusDot: $('gKeyStatusDot'),
    gKeyStatusText: $('gKeyStatusText'),
    gCustomNameField: $('gCustomNameField'),
    gCustomName: $('gCustomName'),
    gApiKey: $('gApiKey'),
    gBtnToggleKeyVisibility: $('gBtnToggleKeyVisibility'),
    gIconKeyEye: $('gIconKeyEye'),
    gBtnToggleKeyText: $('gBtnToggleKeyText'),
    gApiKeyTip: $('gApiKeyTip'),
    gBaseUrl: $('gBaseUrl'),
    gBtnResetBaseUrl: $('gBtnResetBaseUrl'),
    gChatModelSelect: $('gChatModelSelect'),
    gChatModel: $('gChatModel'),
    gReasoningEffort: $('gReasoningEffort'),
    gProviderTestResult: $('gProviderTestResult'),
    gTestResultTitle: $('gTestResultTitle'),
    gTestResultLatency: $('gTestResultLatency'),
    gTestResultBody: $('gTestResultBody'),
    gProviderTest: $('gProviderTest'),
    gProviderActivate: $('gProviderActivate'),
    gCustomBox: $('gCustomBox'),
    gCustomBaseUrl: $('gCustomBaseUrl'),
    gCustomApiKey: $('gCustomApiKey'),
    gCustomModel: $('gCustomModel'),
    gPreset: $('gPreset'),
    gPrecision: $('gPrecision'),
    gAutoTranslate: $('gAutoTranslate'),
    gStatus: $('gStatus'),
    gAudioRetention: $('gAudioRetention'),
    gTgEnabled: $('gTgEnabled'),
    gTgFieldsGroup: $('gTgFieldsGroup'),
    gTgToken: $('gTgToken'),
    gTgProxyHost: $('gTgProxyHost'),
    gTgProxyPort: $('gTgProxyPort'),
    gTgProxyEnabled: $('gTgProxyEnabled'),
    gTgTest: $('gTgTest'),
    gTgSave: $('gTgSave'),
    gTabs: $('gTabs'),
    gDashSovitsBadge: $('gDashSovitsBadge'),
    gDashSovitsStatus: $('gDashSovitsStatus'),
    gDashSovitsUrl: $('gDashSovitsUrl'),
    gBtnRestartSovits: $('gBtnRestartSovits'),
    gBtnTogglePrecision: $('gBtnTogglePrecision'),
    gBtnTogglePrecisionText: $('gBtnTogglePrecisionText'),
    gDashPrecisionBadge: $('gDashPrecisionBadge'),
    gDashPrecisionVal: $('gDashPrecisionVal'),
    gDashDeviceVal: $('gDashDeviceVal'),
    gDashHardwareVal: $('gDashHardwareVal'),
    gDashUptimeVal: $('gDashUptimeVal'),
    gDashCacheVal: $('gDashCacheVal'),
    gDashRetentionVal: $('gDashRetentionVal'),
    gBtnClearCache: $('gBtnClearCache'),
    gBtnRefreshStatus: $('gBtnRefreshStatus'),
    gParamSovitsUrl: $('gParamSovitsUrl'),
    gSovitsDirectory: $('gSovitsDirectory'),
    gSovitsDirectoryStatus: $('gSovitsDirectoryStatus'),
    gBrowseSovitsDirectory: $('gBrowseSovitsDirectory'),
    gSaveSovitsDirectory: $('gSaveSovitsDirectory'),
    gParamSliceMethod: $('gParamSliceMethod'),
    gParamFragmentInterval: $('gParamFragmentInterval'),
    gParamSpeed: $('gParamSpeed'),
    gParamSpeedVal: $('gParamSpeedVal'),
    gParamTopK: $('gParamTopK'),
    gParamTopKVal: $('gParamTopKVal'),
    gParamTopP: $('gParamTopP'),
    gParamTopPVal: $('gParamTopPVal'),
    gParamTemp: $('gParamTemp'),
    gParamTempVal: $('gParamTempVal'),
    gTgChatId: $('gTgChatId'),
    gResetBtn: $('gResetBtn'),
    // 系统版本与更新
    gDashVersionBadge: $('gDashVersionBadge'),
    gDashVersionCommit: $('gDashVersionCommit'),
    gDashVersionBranch: $('gDashVersionBranch'),
    gBtnGoUpdateTab: $('gBtnGoUpdateTab'),
    gCurrentCommit: $('gCurrentCommit'),
    gCurrentBranch: $('gCurrentBranch'),
    gCurrentDate: $('gCurrentDate'),
    gRemoteUrl: $('gRemoteUrl'),
    gRemoteStatusTag: $('gRemoteStatusTag'),
    gCurrentCommitMsg: $('gCurrentCommitMsg'),
    gCurrentMsgText: $('gCurrentMsgText'),
    gBtnCheckUpdate: $('gBtnCheckUpdate'),
    gBtnCheckUpdateText: $('gBtnCheckUpdateText'),
    gIconCheckUpdate: $('gIconCheckUpdate'),
    gBtnApplyUpdate: $('gBtnApplyUpdate'),
    gBtnApplyUpdateText: $('gBtnApplyUpdateText'),
    gIconApplyUpdate: $('gIconApplyUpdate'),
    gUpdateNoticeBox: $('gUpdateNoticeBox'),
    gUpdateNoticeIcon: $('gUpdateNoticeIcon'),
    gUpdateNoticeTitle: $('gUpdateNoticeTitle'),
    gUpdateNoticeDesc: $('gUpdateNoticeDesc'),
    gCommitsLogContainer: $('gCommitsLogContainer'),
    gCommitsLogList: $('gCommitsLogList'),
    gUpdateProgressBox: $('gUpdateProgressBox'),
    gUpdateProgressTitle: $('gUpdateProgressTitle'),
    gUpdateSpinner: $('gUpdateSpinner'),
    gBtnReloadPage: $('gBtnReloadPage'),
    gUpdateLogOutput: $('gUpdateLogOutput'),
  };

  let _syncSessionsGen = 0;
  const sessionsBehindServer = new Set();
  let advApp = null;
  let syncingAdv = false;
  // Home opens after the startup sync; a voice result that arrives earlier belongs to it too.
  let homeStarting = true;
  const portraits = createPortraitCache(id => requestJson(`/api/characters/${encodeURIComponent(id)}/portrait`));
  const conversation = createConversation({
    state, getActive, getSession, addMessage, uid, saveState, persistSession,
    isSessionDeleting, audioStore, putCachedAudioBlob, fetchAndCacheAudio,
    audioPlayer: streamAudioController, getCurrentVoice, stopCurrentVoice,
    clearMicTimers, beforeSend: () => { ++_syncSessionsGen; },
    onBlocked: (message) => showToast(message, 'info'),
    fetch: callbacks.fetch || ((...args) => globalThis.fetch(...args)),
  });

  const closeDrawer = () => advApp?.closeDrawer();
  const regenerateLineAudio = createLineAudioRecovery({
    getSession: getActive, getVoiceProfiles, saveState, isSessionDeleting,
  });

  /* ---------- 渲染与状态栏 ---------- */
  function updateBadge() {
    syncAdv();
  }

  function syncAdv() {
    const root = dom.advRoot || (!injectedDom && $('advRoot'));
    if (!root || syncingAdv) return;
    syncingAdv = true;
    try {
      root.hidden = false;
      if (advApp) { advApp.update(); return; }
      advApp = mountAdv({ ...dom, root }, {
        controller: conversation, audio: streamAudioController, getSession: getActive,
        voiceEnabled: () => state.global.voiceMode,
        getCharacter: () => {
          // An unbound imported session has no character of its own. On home,
          // do not present the engine's globally loaded voice as that session.
          const homeOpen = advApp?.stage.stage.classList.contains('is-home');
          const id = getActive()?.settings?.voiceProfileId || (homeOpen ? null : getActiveProfileId());
          const profile = getVoiceProfiles().find(p => p.id === id);
          return { id, name: profile?.name || '角色', work: profile?.work ?? null };
        },
        getModelStatus: () => {
          const model = getModelStatus({ getCurrentActiveProvider, getActive, getVoiceProfiles, haveProvidersLoaded, hasProviderReadError });
          return { ...model, state: conversation.busy ? 'busy' : model.state };
        },
        loadPortrait: (id, options) => portraits.get(id, options),
        saveSettings: (patch, { confirm = false } = {}) => {
          const session = getActive();
          if (!session) throw new Error('当前没有可保存的会话。');
          ++_syncSessionsGen;
          session.settings = pickSettings({ ...session.settings, ...patch }, DEFAULT_SESSION_SETTINGS);
          saveState();
          const confirmation = persistSession(session);
          if (confirm) return confirmation;
          confirmation.catch(error => showToast(`当前页面已应用显示设置，但本机服务尚未确认保存：${error.message}`, 'error'));
        },
        onSettingsSaveError: (error, origin) => showToast(`会话「${origin?.title || '原会话'}」的聊天体验尚未确认保存：${error?.message || error}。请重新打开聊天体验并保存。`, 'error'),
        toggleVoice: () => toggleVoiceMode(),
        stopOtherVoice: stopCurrentVoice,
        onMic: () => toggleMic(),
        cancelMic,
        onSettings: openSessionSettings,
        ...(document.getElementById('gp-experience') ? {
          readingSettingsContainer: document.getElementById('gp-experience'),
          onOpenReadingSettings: () => openGlobalSettings('experience'),
          onReadingSettingsClose: () => settingsCenter?.experienceClosed(),
        } : {}),
        onModelSettings: () => openGlobalSettings('models'),
        onOpenSettings: () => openGlobalSettings(),
        onHome: () => returnHome(),
        onNewSession: newChat,
        loadOlderHistory: session => loadOlderSessionHistory(session),
        playUserVoice: (msg, ctl) => playVoice(msg, ctl),
        playLegacyVoice: (msg, ctl) => playVoice(msg, ctl),
        regenerateLineAudio,
        getRecoveryVoice: () => getVoiceProfiles().find(profile => profile.id === getActive()?.settings?.voiceProfileId) || null,
        subscribeOverlay: onModalChange,
        closeOverlay: () => document.querySelector('.overlay.active [data-close]')?.click(),
      });
      if (hasOpenModal()) root.inert = true;
    } finally { syncingAdv = false; }
  }

  function renderSidebar() {
    if (dom.sessionList) {
      renderSessionList(dom.sessionList, {
        sessions: state.sessions,
        activeId: state.activeId,
        onSelect: switchSession,
        onDelete: handleDelete,
      });
    }
    if (dom.sessionCount) {
      dom.sessionCount.textContent = state.sessions.length;
    }
  }

  function renderHeader() { updateBadge(); }

  async function resolveJapanese(msg) {
    if (msg.japanese) return msg.japanese;
    try {
      const data = await requestJson('/api/chat/japanese', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: msg.content, session_id: state.activeId }),
      }, { timeoutMs: 60000 });
      if (data.japanese) { msg.japanese = data.japanese; saveState(); return data.japanese; }
    } catch (error) { console.debug('Japanese voice text unavailable:', error); }
    return '';
  }

  function fullRender() {
    renderSidebar();
    renderHeader();
  }

  function updateControls() { advApp?.update(); }

  /* ---------- 流式回复与打断 ---------- */
  function stopStream() {
    conversation.stop();
  }

  function interruptAll() {
    stopStream();
    stopCurrentVoice();
  }

  function sendMessage(rawText, voiceMeta) {
    return conversation.send(typeof rawText === 'string' ? rawText : advApp?.textbox.draft || '', voiceMeta);
  }

  function retryFailedMessage(msg) {
    conversation.retry(msg);
  }

  conversation.on('turnstart', () => { renderSidebar(); renderHeader(); });
  for (const kind of ['done', 'error', 'cancelled']) conversation.on(kind, event => {
    const { error, kind: code, failedInline, originId, meta = {} } = event;
    if (error && !(failedInline && originId === state.activeId)) {
      showToast(error, 'error', needsModelSettings(error, code) ? {
        actionLabel: '配置对话模型', onAction: () => openGlobalSettings('models'),
      } : null);
    } else if (meta.audioError && !meta.cancelled) {
      showToast(meta.audioError, 'info', {
        actionLabel: '检查运行状态', onAction: () => openGlobalSettings('dashboard'),
      });
    }
    renderSidebar(); renderHeader();
  });

  /* ---------- 会话操作 ---------- */
  async function loadSessionHistory(session) {
    if (!session) return;
    const isBehind = sessionsBehindServer.has(session.id);
    if (!isBehind && session.messages && session.messages.length > 0) return;
    const initialCount = session.messages ? session.messages.length : 0;
    // A retry already in flight cleared `failed` without adding a message. Another
    // load, including the home preview after startup, must not replace it.
    if (conversation.busy && getActive() === session) return;
    // beforeSend bumps this when a message is sent or retried during the request.
    const historyGen = _syncSessionsGen;
    try {
      const data = await fetchSessionHistoryPage(session.id, { limit: 100 });
      // That attempt, a reply typed while history was loading, or a deleted session
      // wins. The session stays behind so the next open refetches.
      if (historyGen !== _syncSessionsGen || getSession(session.id) !== session || (session.messages ? session.messages.length : 0) !== initialCount) return;
      if (Array.isArray(data.messages) && data.messages.length > 0) {
        const incoming = data.messages.map(mapBackendMessage);
        if (session.messages && session.messages.length > 0) {
          const usedIndices = new Set();
          const pairedLocal = new Set();

          // 1. 本地消息有已知服务端 ID（serverMessageId 或 m_<数字> 形式的 id）时，只和同 ID 的服务端消息配对。
          const incomingById = new Map();
          for (let j = 0; j < incoming.length; j++) {
            const sid = extractServerId(incoming[j]);
            if (sid !== null) {
              incomingById.set(sid, j);
            }
          }

          for (let i = 0; i < session.messages.length; i++) {
            const localMsg = session.messages[i];
            const sid = extractServerId(localMsg);
            if (sid !== null && incomingById.has(sid)) {
              const j = incomingById.get(sid);
              if (!usedIndices.has(j)) {
                const serverMsg = incoming[j];
                if (serverMsg.role === localMsg.role) {
                  usedIndices.add(j);
                  pairedLocal.add(localMsg);
                  if (localMsg.voiceKey) {
                    serverMsg.voiceKey = localMsg.voiceKey;
                    if (localMsg.dur !== undefined) {
                      serverMsg.dur = localMsg.dur;
                    }
                    if (audioStore && audioStore.has(localMsg.id)) {
                      audioStore.set(serverMsg.id, audioStore.get(localMsg.id));
                    }
                  }
                }
              }
            }
          }

          // 2. 没有已知 ID 的本地用户消息才按正文配对，而且只配给没被 ID 占用的服务端用户消息，保持从后往前的顺序。
          for (let i = session.messages.length - 1; i >= 0; i--) {
            const localMsg = session.messages[i];
            if (localMsg && localMsg.role === 'user' && extractServerId(localMsg) === null) {
              for (let j = incoming.length - 1; j >= 0; j--) {
                if (usedIndices.has(j)) continue;
                const serverMsg = incoming[j];
                if (serverMsg.role === 'user' && serverMsg.content === localMsg.content) {
                  usedIndices.add(j);
                  pairedLocal.add(localMsg);
                  if (localMsg.voiceKey) {
                    serverMsg.voiceKey = localMsg.voiceKey;
                    if (localMsg.dur !== undefined) {
                      serverMsg.dur = localMsg.dur;
                    }
                    if (audioStore && audioStore.has(localMsg.id)) {
                      audioStore.set(serverMsg.id, audioStore.get(localMsg.id));
                    }
                  }
                  break;
                }
              }
            }
          }

          // 3. 本地最后一条是带 failed 的用户消息，且没有配上任何服务端消息时，把它原样（保留 failed）接到新列表末尾。
          const lastLocalMsg = session.messages[session.messages.length - 1];
          if (lastLocalMsg && lastLocalMsg.role === 'user' && lastLocalMsg.failed && !pairedLocal.has(lastLocalMsg)) {
            incoming.push(lastLocalMsg);
          }
        }
        session.messages = incoming;
        session.hasMoreHistory = Boolean(data.has_more);
        session.oldestMessageId = Number.isInteger(data.oldest_id) ? data.oldest_id : earliestServerMessageId(session.messages);
        autoTitle(session);
        sessionsBehindServer.delete(session.id);
        saveState();
      } else {
        session.hasMoreHistory = false;
        session.oldestMessageId = null;
        sessionsBehindServer.delete(session.id);
      }
    } catch (e) {
      console.debug('Failed to load session history:', e);
      showToast(`历史记录暂时无法读取：${e.message} 恢复连接后点击当前对话可重试。`, 'error');
    }
  }

  async function loadOlderSessionHistory(session) {
    if (!session || session.loadingOlder) return;
    if (conversation.busy) {
      showToast('回复完成后即可查看更早的聊天记录。', 'info');
      return;
    }
    const beforeId = Number.isInteger(session.oldestMessageId)
      ? session.oldestMessageId
      : earliestServerMessageId(session.messages);
    if (!beforeId || beforeId <= 1) {
      session.hasMoreHistory = false;
      if (getActive() === session) fullRender();
      return;
    }
    session.loadingOlder = true;
    updateControls();
    if (getActive() === session) fullRender();
    try {
      const data = await fetchSessionHistoryPage(session.id, { limit: 100, beforeId });
      if (getSession(session.id) !== session) return;
      const incoming = Array.isArray(data.messages) ? data.messages.map(mapBackendMessage) : [];
      const existingIds = new Set((session.messages || []).map((m) => m.id));
      const uniqueOlder = incoming.filter((m) => !existingIds.has(m.id));
      if (uniqueOlder.length > 0) {
        session.messages = [...uniqueOlder, ...(session.messages || [])];
      }
      session.hasMoreHistory = Boolean(data.has_more);
      session.oldestMessageId = Number.isInteger(data.oldest_id)
        ? data.oldest_id
        : earliestServerMessageId(session.messages);
      autoTitle(session);
      saveState();
    } catch (e) {
      showToast(`加载更早消息失败：${e.message}`, 'error');
    } finally {
      session.loadingOlder = false;
      updateControls();
      if (getActive() === session) {
        fullRender();
      }
    }
  }

  function ensureHomeEntryVoice(session) {
    const want = session?.settings?.voiceProfileId;
    // The saved active ID does not prove that weights finished loading (or
    // survived an engine restart). The switch endpoint checks actual readiness.
    if (want) ensureSessionVoice(session, { silent: getActiveProfileId() === want });
  }

  async function switchSession(id, { focusInput = true, enterFromHome = false } = {}) {
    if (!getSession(id)) return false;
    closeDrawer();
    if (id === state.activeId) {
      const current = getActive();
      if (current && (!current.messages?.length || sessionsBehindServer.has(current.id))) {
        await loadSessionHistory(current);
      }
      if (getActive() === current) {
        fullRender();
        if (enterFromHome) ensureHomeEntryVoice(current);
      }
      if (focusInput) advApp?.textbox.inputElement?.focus();
      return;
    }
    interruptAll();
    state.activeId = id;
    revokeAllCachedAudioUrls();
    const s = getActive();
    saveState();
    fullRender();
    if (enterFromHome) ensureHomeEntryVoice(s);
    else ensureSessionVoice(s);
    if (s && (!s.messages || s.messages.length === 0 || sessionsBehindServer.has(s.id))) {
      await loadSessionHistory(s);
      if (getActive() !== s) return;
      fullRender();
    }
    if (focusInput) advApp?.textbox.inputElement?.focus();
  }

  async function handleDelete(id) {
    if (isSessionDeleting(id)) return;
    ++_syncSessionsGen;
    if (id === state.activeId || conversation.busy) {
      interruptAll();
    } else {
      stopCurrentVoice();
    }
    try {
      const removal = deletePersistedSession(id);
      updateControls();
      await removal;
    } catch (error) {
      showToast(`尚未确认删除，对话保留在列表中。${error.message} 可以稍后重试。`, 'error');
      updateControls();
      return;
    }
    sessionsBehindServer.delete(id);
    const sess = getSession(id);
    if (sess) {
      for (const m of sess.messages) {
        if (audioStore.has(m.id)) {
          audioStore.delete(m.id);
        }
      }
    }
    deleteSession(id);
    fullRender();
    ensureSessionVoice(getActive());
    showToast('对话已删除');
  }

  function newChat(selection, { enterFromHome = false } = {}) {
    if (!selection || !Object.hasOwn(selection, 'voiceProfileId')) selection = null;
    ++_syncSessionsGen;
    interruptAll();
    closeDrawer();
    revokeAllCachedAudioUrls();
    const prevSession = getActive();
    const s = createSession();
    const activeProfileId = getActiveProfileId();
    const voiceProfiles = getVoiceProfiles();

    if (prevSession && prevSession.settings && prevSession.settings.voiceProfileId) {
      // 只继承角色绑定与语速；人设提示词属于角色包，由后端按绑定角色解析，
      // 不再复制会话副本（复制正是同一角色在不同会话设定不一致的来源）
      s.settings.voiceProfileId = prevSession.settings.voiceProfileId;
      s.settings.ttsSpeed = prevSession.settings.ttsSpeed;
    }
    if (selection && Object.hasOwn(selection, 'voiceProfileId')) {
      s.settings.voiceProfileId = selection.voiceProfileId;
      s.settings.advBackground = selection.advBackground;
      s.settings.advBgTime = selection.advBgTime;
      s.settings.ttsSpeed = DEFAULT_SESSION_SETTINGS.ttsSpeed;
    }
    if (selection || !s.settings.voiceProfileId) {
      const active = voiceProfiles.find((p) => p.id === (selection ? selection.voiceProfileId : activeProfileId));
      if (active) {
        s.settings.voiceProfileId = active.id;
        if (active.name === '高楯欧丽叶') s.settings.ttsSpeed = 0.88;
        else if (active.name === '常陆茉子') s.settings.ttsSpeed = 0.90;
        else if (active.name === '白雪乃爱') s.settings.ttsSpeed = 1.05;
      }
    }
    saveState();
    fullRender();
    if (enterFromHome) ensureHomeEntryVoice(s);
    else ensureSessionVoice(s);
    advApp?.textbox.inputElement?.focus();
    persistSession(s).catch(() => {});
    return s;
  }

  const actions = createTitleActions({
    getSessions: () => state.sessions, getProfiles: getVoiceProfiles,
    getActiveId: () => state.activeId,
    isBehindServer: id => sessionsBehindServer.has(id),
    startNew: selection => newChat(selection, { enterFromHome: true }),
    loadSession: id => switchSession(id, { enterFromHome: true }),
    previewSession: id => switchSession(id, { focusInput: false }),
    openCharacters: openSessionSettings, openSettings: page => openGlobalSettings(page),
    getBackgroundUrl: (id, time, work) => advApp?.backgroundUrl(id, time, work) || '',
    getBackgrounds: () => advApp?.listBackgrounds() || [],
    getAvatar: session => portraits.avatar(session),
    getStatus: async () => {
      let engine;
      try { engine = await requestJson('/api/system/engine-status', {}, { timeoutMs: 4500 }); }
      catch (error) { engine = { state: 'unavailable', message: error.message }; }
      // Sample all model fields together after the engine request. Providers may
      // finish loading while it is pending; never mix that flag with an old model.
      syncAdv();
      const model = getModelStatus({ getCurrentActiveProvider, getActive, getVoiceProfiles, haveProvidersLoaded, hasProviderReadError });
      return { model, modelLoaded: haveProvidersLoaded(), hasModel: Boolean(getCurrentActiveProvider()), engine };
    },
    voiceEnabled: () => state.global.voiceMode,
    playExcerpt: excerpt => {
      stopCurrentVoice();
      return streamAudioController.playLine(excerpt.line.index, excerpt.line.audio.chunks, { msgId: excerpt.messageId });
    },
    stopExcerpt: () => streamAudioController.stopLine({ fadeMs: 0 }),
    subscribeSettings: listener => onModalChange(open => { if (!open) listener(); }),
  });
  Object.assign(advActions, actions);
  function returnHome() {
    interruptAll();
    cancelSessionSettingsLoad();
    closeAllModals();
    advApp?.prepareHome();
    return showTitleScreen(actions);
  }
  const app = { sendMessage, retryFailedMessage, loadOlderSessionHistory, fullRender, updateControls, conversation,
    switchSession, syncAdv, actions, returnHome, get adv() { return advApp; } };
  let settingsCenter = null;
  if (!initialize) return app;

  dom.newChatBtn?.addEventListener('click', newChat);

  /* ---------- 初始化控制器 ---------- */
  initUpdateManager(dom);
  settingsCenter = createSettingsCenter({ dom, openCenter: openGlobalSettings,
    getExperience: () => advApp?.configPanel, getSession: getActive, loadSession: openSessionSettings,
    cancelSession: cancelSessionSettingsLoad });
  const settingsOpen = el => settingsCenter ? settingsCenter.open(el) : openModal(el);
  const settingsClose = el => settingsCenter ? settingsCenter.close(el) : closeModal(el);
  initVoiceSettings(dom, {
    openModal: settingsOpen,
    closeModal: settingsClose,
    renderSidebar,
    renderHeader,
    onOpenSessionSettings: openSessionSettings,
    onOpenEngineStatus: () => openGlobalSettings('dashboard'),
    isHomeOpen: () => homeStarting || Boolean(advApp?.stage.stage.classList.contains('is-home')),
    invalidateSessionSync: () => { ++_syncSessionsGen; },
  });
  initGlobalSettings(dom, {
    ...(settingsCenter ? { onSettingsTabChange: name => settingsCenter.select(name) } : {}),
    onOpenSessionSettings: openSessionSettings,
    onOpenReadingSettings: () => advApp?.action('reading_settings'),
    onSovitsDirectorySaved: invalidateScannedModels,
    openModal: settingsOpen,
    closeModal: settingsClose,
    loadSystemVersionInfo,
    fetchVoiceProfiles,
    getVoiceProfiles,
    getActiveProfileId,
    updateBadge,
    revokeAllCachedAudioUrls,
  });
  initAudioPlayerUi(dom, {
    getInput: () => advApp?.textbox.inputElement,
    getMicButton: () => advApp?.textbox.micButton,
    sendMessage,
    isBusy: () => conversation.busy,
    resolveJapanese,
    onOpenSessionSettings: openSessionSettings,
    onOpenEngineStatus: () => openGlobalSettings('dashboard'),
  });

  if (dom.globalSettingsBtn) {
    dom.globalSettingsBtn.addEventListener('click', () => openGlobalSettings());
  }
  document.querySelectorAll('[data-home]').forEach(button => button.addEventListener('click', returnHome));
  document.querySelectorAll('[data-settings-center]').forEach(button => button.addEventListener('click', () => {
    closeAllModals(); openGlobalSettings();
  }));
  /* ---------- 启动同步与渲染 ---------- */
  loadState();
  initSessionBackup({
    isBusy: () => conversation.busy,
    onOpenBackupTab: () => openGlobalSettings('update'),
    onImport: () => {
      ++_syncSessionsGen;
      interruptAll();
      fullRender();
      ensureSessionVoice(getActive(), { silent: true });
    },
  });
  fullRender();
  updateControls();
  ensureSessionVoice(getActive());
  if (window.innerWidth > 820) advApp?.textbox.inputElement?.focus();

  // 同步后端 SQLite 会话列表及历史消息（generation counter 防止过期结果覆盖用户新建会话）
  async function syncSessionsFromBackend() {
    const gen = ++_syncSessionsGen;
    const isStale = () => gen !== _syncSessionsGen;
    try {
      const backendSessions = await fetchAllSessions();
      if (isStale()) return;
      // 保存失败时本机已经留下更新的一份。刷新后不要用更旧的服务端设置盖掉它。
      const newerLocalSessions = [];
      if (backendSessions.length > 0) {
        const existingMap = new Map(state.sessions.map((s) => [s.id, s]));
        const merged = [];
        for (const bs of backendSessions) {
          let local = existingMap.get(bs.id);
          if (local) {
            local.lastLine = bs.last_message || '';
            if (bs.title && (local.title === '新对话' || !local.title)) local.title = bs.title;
            const serverUpdatedAt = serverTimestamp(bs.updated_at, local.updatedAt);
            if (local.updatedAt > serverUpdatedAt) {
              newerLocalSessions.push(local);
            } else {
              local.updatedAt = serverUpdatedAt;
              if (bs.voice_profile_id && !local.settings.voiceProfileId) local.settings.voiceProfileId = bs.voice_profile_id;
              if (bs.settings && Object.keys(bs.settings).length > 0) local.settings = pickSettings({ ...local.settings, ...bs.settings }, DEFAULT_SESSION_SETTINGS);
            }
            const lastMsg = local.messages ? local.messages[local.messages.length - 1] : null;
            const hasUnsentFailed = Boolean(
              lastMsg &&
              lastMsg.role === 'user' &&
              lastMsg.failed &&
              extractServerId(lastMsg) === null
            );
            const localCount = local.messages
              ? (local.messages.length - (hasUnsentFailed ? 1 : 0))
              : 0;
            if (typeof bs.message_count === 'number' && bs.message_count > localCount) {
              sessionsBehindServer.add(local.id);
            } else {
              sessionsBehindServer.delete(local.id);
            }
            // 人设提示词是角色级数据，会话里残留的旧副本一律丢弃
            delete local.settings.systemPrompt;
            delete local.settings.promptProfileId;
            merged.push(local);
            existingMap.delete(bs.id);
          } else {
            merged.push({
              id: bs.id,
              title: bs.title || '新对话',
              createdAt: serverTimestamp(bs.created_at),
              updatedAt: serverTimestamp(bs.updated_at),
              messages: [],
              lastLine: bs.last_message || '',
              settings: pickSettings({
                ...DEFAULT_SESSION_SETTINGS,
                ...(bs.settings || {}),
                voiceProfileId: bs.voice_profile_id || null,
                systemPrompt: '',
              }, DEFAULT_SESSION_SETTINGS),
            });
          }
        }
        for (const [_, rem] of existingMap) {
          if (rem.messages && rem.messages.some((m) => m.role === 'user')) {
            merged.push(rem);
          }
        }
        state.sessions = merged;
        if (!state.activeId || !state.sessions.some((s) => s.id === state.activeId)) {
          state.activeId = state.sessions[0]?.id || null;
        }
      } else if (state.sessions.length === 0) {
        const s = createSession('新对话');
        persistSession(s).catch(() => {});
      }
      const curActive = getActive();
      if (curActive && (!curActive.messages || curActive.messages.length === 0 || sessionsBehindServer.has(curActive.id))) {
        await loadSessionHistory(curActive);
      }
      if (isStale()) return;
      saveState();
      for (const session of newerLocalSessions) persistSession(session).catch(() => {});
      fullRender();
      ensureSessionVoice(getActive(), { silent: true });
      if (window.innerWidth > 820) advApp?.textbox.inputElement?.focus();
      loadProviders().catch(() => {});
    } catch (err) {
      console.debug('Failed to sync sessions from backend:', err);
      throw err;
    }
  }

  // The first protected request completes token authentication before title UI.
  app.ready = syncSessionsFromBackend().then(() => showTitleScreen(actions)).catch(error => {
    console.debug('Title startup deferred:', error);
  }).finally(() => { homeStarting = false; });

  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get('settings') === '1' || window.location.hash === '#settings') {
    openGlobalSettings();
  }

  return app;
}

if (typeof document !== 'undefined') initApp();
