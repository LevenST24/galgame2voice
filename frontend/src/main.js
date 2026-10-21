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
import { mountAdv } from './adv/adv_app.js';
import { getModelStatus } from './model_badge.js';
import { audioStore } from './voice.js';
import { fetchAndCacheAudio, putCachedAudioBlob } from './cache.js';
import { streamAudioController } from './audio_player.js';
import { renderSessionList, showToast, needsModelSettings } from './ui.js';
import { advActions, createTitleActions } from './adv/actions.js';
import { showTitle } from './adv/title_hook.js';

import { initAuthInterceptor } from './auth.js';
import { initSessionBackup } from './controllers/session_backup.js';
import { requestJson } from './api.js';
import { persistSession, deletePersistedSession, isSessionDeleting, fetchAllSessions, fetchSessionHistoryPage, earliestServerMessageId } from './session_api.js';
import { serverTimestamp, normalizeLines, pickSettings } from './session_data.js';
import { openModal, closeModal, hasOpenModal, initSettingsUi, onModalChange } from './settings_ui.js';

// Modular controllers
import { initUpdateManager, loadSystemVersionInfo } from './controllers/update_manager.js';
import {
  initVoiceSettings,
  openSessionSettings,
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
  getCurrentActiveProvider, getVoiceProfiles, haveProvidersLoaded,
  showTitle,
};

export function mapBackendMessage(bm) {
  const msg = {
    id: `m_${bm.id}`,
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

// 可注入存储、流和渲染器，测试无需启动鉴权或麦克风。
export function initApp({ dom: injectedDom, initialize = true, ...callbacks } = {}) {
  const {
    state, getActive, getSession, addMessage, uid,
    saveState, persistSession, isSessionDeleting, audioStore, putCachedAudioBlob,
    fetchAndCacheAudio, streamAudioController, getCurrentVoice, stopCurrentVoice,
    clearMicTimers, renderSessionList,
    showToast, autoTitle, fetchSessionHistoryPage, earliestServerMessageId,
    getCurrentActiveProvider, getVoiceProfiles, haveProvidersLoaded,
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
    gMemoryEnabled: $('gMemoryEnabled'),
    gUserNickname: $('gUserNickname'),
    gDefaultSystemPrompt: $('gDefaultSystemPrompt'),
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
  let advApp = null;
  let syncingAdv = false;
  const conversation = createConversation({
    state, getActive, getSession, addMessage, uid, saveState, persistSession,
    isSessionDeleting, audioStore, putCachedAudioBlob, fetchAndCacheAudio,
    audioPlayer: streamAudioController, getCurrentVoice, stopCurrentVoice,
    clearMicTimers, beforeSend: () => { ++_syncSessionsGen; },
    onBlocked: (message) => showToast(message, 'info'),
    fetch: callbacks.fetch || ((...args) => globalThis.fetch(...args)),
  });

  const closeDrawer = () => advApp?.closeDrawer();

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
          const id = getActive()?.settings?.voiceProfileId || getActiveProfileId();
          return { id, name: getVoiceProfiles().find(profile => profile.id === id)?.name || '角色' };
        },
        getModelStatus: () => {
          const model = getModelStatus({ getCurrentActiveProvider, getActive, getVoiceProfiles, haveProvidersLoaded });
          return { ...model, state: conversation.busy ? 'busy' : model.state };
        },
        loadPortrait: (id, { signal }) => requestJson(`/api/characters/${encodeURIComponent(id)}/portrait`, { signal }),
        saveSettings: patch => {
          const session = getActive();
          session.settings = pickSettings({ ...session.settings, ...patch }, DEFAULT_SESSION_SETTINGS);
          saveState(); persistSession(session).catch(() => {});
        },
        toggleVoice: () => toggleVoiceMode(),
        stopOtherVoice: stopCurrentVoice,
        onMic: () => toggleMic(),
        cancelMic,
        onSettings: openSessionSettings,
        onModelSettings: () => openGlobalSettings('models'),
        onNewSession: newChat,
        loadOlderHistory: session => loadOlderSessionHistory(session),
        playUserVoice: (msg, ctl) => playVoice(msg, ctl),
        playLegacyVoice: (msg, ctl) => playVoice(msg, ctl),
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
    if (!session || (session.messages && session.messages.length > 0)) return;
    try {
      const data = await fetchSessionHistoryPage(session.id, { limit: 100 });
      // A reply typed while history was loading, or a deleted session, wins over the old response.
      if (getSession(session.id) !== session || session.messages?.length) return;
      if (Array.isArray(data.messages) && data.messages.length > 0) {
        session.messages = data.messages.map(mapBackendMessage);
        session.hasMoreHistory = Boolean(data.has_more);
        session.oldestMessageId = Number.isInteger(data.oldest_id) ? data.oldest_id : earliestServerMessageId(session.messages);
        autoTitle(session);
        saveState();
      } else {
        session.hasMoreHistory = false;
        session.oldestMessageId = null;
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

  async function switchSession(id) {
    if (!getSession(id)) return false;
    closeDrawer();
    if (id === state.activeId) {
      const current = getActive();
      if (current && !current.messages?.length) {
        await loadSessionHistory(current);
        if (getActive() === current) fullRender();
      }
      return;
    }
    interruptAll();
    state.activeId = id;
    revokeAllCachedAudioUrls();
    const s = getActive();
    saveState();
    fullRender();
    ensureSessionVoice(s);
    if (s && (!s.messages || s.messages.length === 0)) {
      await loadSessionHistory(s);
      if (getActive() !== s) return;
      fullRender();
    }
    advApp?.textbox.inputElement?.focus();
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

  function newChat() {
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
    } else if (activeProfileId) {
      const active = voiceProfiles.find((p) => p.id === activeProfileId);
      if (active) {
        s.settings.voiceProfileId = active.id;
        if (active.name === '高楯欧丽叶') s.settings.ttsSpeed = 0.88;
        else if (active.name === '常陆茉子') s.settings.ttsSpeed = 0.90;
        else if (active.name === '白雪乃爱') s.settings.ttsSpeed = 1.05;
      }
    }
    saveState();
    fullRender();
    ensureSessionVoice(s);
    advApp?.textbox.inputElement?.focus();
    persistSession(s).catch(() => {});
    return s;
  }

  const actions = createTitleActions({
    getSessions: () => state.sessions, getProfiles: getVoiceProfiles,
    startNew: newChat, loadSession: switchSession,
    openCharacters: openSessionSettings, openSettings: () => openGlobalSettings(),
  });
  Object.assign(advActions, actions);
  const app = { sendMessage, retryFailedMessage, loadOlderSessionHistory, fullRender, updateControls, conversation,
    switchSession, syncAdv, actions, get adv() { return advApp; } };
  if (!initialize) return app;

  dom.newChatBtn?.addEventListener('click', newChat);

  /* ---------- 初始化控制器 ---------- */
  initUpdateManager(dom);
  initVoiceSettings(dom, {
    openModal,
    closeModal,
    renderSidebar,
    renderHeader,
    onOpenSessionSettings: openSessionSettings,
    onOpenEngineStatus: () => openGlobalSettings('dashboard'),
    invalidateSessionSync: () => { ++_syncSessionsGen; },
  });
  initGlobalSettings(dom, {
    onOpenSessionSettings: openSessionSettings,
    onSovitsDirectorySaved: invalidateScannedModels,
    openModal,
    closeModal,
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
    onOpenEngineStatus: () => openGlobalSettings('dashboard'),
  });

  if (dom.globalSettingsBtn) {
    dom.globalSettingsBtn.addEventListener('click', () => openGlobalSettings());
  }
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
      if (backendSessions.length > 0) {
        const existingMap = new Map(state.sessions.map((s) => [s.id, s]));
        const merged = [];
        for (const bs of backendSessions) {
          let local = existingMap.get(bs.id);
          if (local) {
            local.lastLine = bs.last_message || '';
            if (bs.title && (local.title === '新对话' || !local.title)) local.title = bs.title;
            local.updatedAt = serverTimestamp(bs.updated_at, local.updatedAt);
            if (bs.voice_profile_id && !local.settings.voiceProfileId) local.settings.voiceProfileId = bs.voice_profile_id;
            if (bs.settings && Object.keys(bs.settings).length > 0) local.settings = pickSettings({ ...local.settings, ...bs.settings }, DEFAULT_SESSION_SETTINGS);
            if (typeof bs.message_count === 'number' && local.messages && local.messages.length > 0 && bs.message_count > local.messages.length) {
              const oldest = earliestServerMessageId(local.messages);
              if (oldest && oldest > 1) {
                local.hasMoreHistory = true;
                local.oldestMessageId = oldest;
              }
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
      if (curActive && (!curActive.messages || curActive.messages.length === 0)) {
        await loadSessionHistory(curActive);
      }
      if (isStale()) return;
      saveState();
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
  });

  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get('settings') === '1' || window.location.hash === '#settings') {
    openGlobalSettings();
  }

  return app;
}

if (typeof document !== 'undefined') initApp();
