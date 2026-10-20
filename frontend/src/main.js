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
import { updateBadge as renderModelBadge } from './model_badge.js';
import { voiceSupported, recorderSupported, audioStore } from './voice.js';
import { fetchAndCacheAudio, putCachedAudioBlob } from './cache.js';
import { streamAudioController } from './audio_player.js';
import { portraitStage } from './portrait.js';
import {
  renderSessionList, renderMessages, createMessageEl, createTypingEl, showToast,
  keepToastsAboveComposer, needsModelSettings,
} from './ui.js';
import { initAuthInterceptor } from './auth.js';
import { initSessionBackup } from './controllers/session_backup.js';
import { requestJson } from './api.js';
import { persistSession, deletePersistedSession, isSessionDeleting, fetchAllSessions, fetchSessionHistoryPage, earliestServerMessageId } from './session_api.js';
import { serverTimestamp, normalizeLines } from './session_data.js';
import { openModal, closeModal, hasOpenModal, initSettingsUi } from './settings_ui.js';

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
  updateVoiceModeUI,
  getCurrentVoice,
  setCurrentVoice,
  clearMicTimers,
} from './controllers/audio_player_ui.js';

const appDependencies = {
  state, getActive, getSession, addMessage, uid,
  saveState, persistSession, isSessionDeleting, audioStore, putCachedAudioBlob,
  fetchAndCacheAudio, streamAudioController, getCurrentVoice, stopCurrentVoice, setCurrentVoice,
  clearMicTimers, renderSessionList, renderMessages, createMessageEl, createTypingEl,
  showToast, autoTitle, fetchSessionHistoryPage, earliestServerMessageId, portraitStage,
  getCurrentActiveProvider, getVoiceProfiles, haveProvidersLoaded,
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
    fetchAndCacheAudio, streamAudioController, getCurrentVoice, stopCurrentVoice, setCurrentVoice,
    clearMicTimers, renderSessionList, renderMessages, createMessageEl, createTypingEl,
    showToast, autoTitle, fetchSessionHistoryPage, earliestServerMessageId, portraitStage,
    getCurrentActiveProvider, getVoiceProfiles, haveProvidersLoaded,
  } = { ...appDependencies, ...callbacks };
  if (initialize) {
    initSettingsUi();
    initAuthInterceptor();
  }
  const $ = (id) => document.getElementById(id);
  const dom = injectedDom || {
    sidebar: $('sidebar'),
    backdrop: $('backdrop'),
    menuBtn: $('menuBtn'),
    sidebarClose: $('sidebarClose'),
    newChatBtn: $('newChatBtn'),
    sessionList: $('sessionList'),
    sessionCount: $('sessionCount'),
    chatTitle: $('chatTitle'),
    messages: $('messages'),
    composer: $('composer'),
    input: $('input'),
    sendBtn: $('sendBtn'),
    stopBtn: $('stopBtn'),
    micBtn: $('micBtn'),
    voiceModeBtn: $('voiceModeBtn'),
    voiceModeIcon: $('voiceModeIcon'),
    badge: $('modelBadge'),
    badgeDot: $('badgeDot'),
    sessionSettingsBtn: $('sessionSettingsBtn'),
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

  let listInner = null;
  let nearBottom = true;
  let _syncSessionsGen = 0;
  let getStreamAudioControl = () => null;
  const conversation = createConversation({
    state, getActive, getSession, addMessage, uid, saveState, persistSession,
    isSessionDeleting, audioStore, putCachedAudioBlob, fetchAndCacheAudio,
    audioPlayer: streamAudioController, getCurrentVoice, stopCurrentVoice, setCurrentVoice,
    clearMicTimers, beforeSend: () => { ++_syncSessionsGen; },
    onBlocked: (message) => showToast(message, 'info'),
    getAudioControl: () => getStreamAudioControl(),
    getAudioMode: callbacks.getAudioMode || (() => 'sequential'),
    fetch: callbacks.fetch || ((...args) => globalThis.fetch(...args)),
  });

  /* ---------- 抽屉操作 ---------- */
  function openDrawer() {
    if (dom.sidebar) { dom.sidebar.inert = false; dom.sidebar.classList.add('open'); }
    if (dom.backdrop) dom.backdrop.classList.add('show');
  }

  function closeDrawer() {
    if (dom.sidebar) { dom.sidebar.classList.remove('open'); dom.sidebar.inert = window.innerWidth <= 820; }
    if (dom.backdrop) dom.backdrop.classList.remove('show');
  }

  /* ---------- 渲染与状态栏 ---------- */
  function updateBadge() {
    renderModelBadge(dom, { getCurrentActiveProvider, getActive, getVoiceProfiles, haveProvidersLoaded });
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

  function renderHeader() {
    const s = getActive();
    if (dom.chatTitle) {
      dom.chatTitle.textContent = s ? s.title : '新对话';
      dom.chatTitle.title = dom.chatTitle.textContent;
    }
    if (s && dom.sessionSettingsBtn) {
      const st = s.settings;
      // 会话是否"已配置"看有没有绑定角色：人设提示词属于角色包，不再是会话属性
      dom.sessionSettingsBtn.classList.toggle('configured', Boolean(st?.voiceProfileId));
    }
    updateBadge();
  }

  async function resolveJapanese(msg) {
    if (msg.japanese) return msg.japanese;
    try {
      const data = await requestJson('/api/chat/japanese', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text: msg.content,
          session_id: state.activeId,
        }),
      }, { timeoutMs: 60000 });
      if (data && data.japanese) {
        msg.japanese = data.japanese;
        saveState();
        return data.japanese;
      }
    } catch (e) {
      console.warn('获取日文配音原文失败', e);
    }
    return '';
  }

  function fullRender(animate = false, { scrollToBottom = true } = {}) {
    renderSidebar();
    renderHeader();
    if (dom.messages) {
      listInner = renderMessages(dom.messages, getActive(), {
        animate,
        onPick: sendMessage,
        onPlayVoice: playVoice,
        onResolveJapanese: resolveJapanese,
        onLoadOlder: loadOlderSessionHistory,
        historyBlocked: conversation.busy,
        onOpenModels: () => openGlobalSettings('models'),
        onOpenVoice: openSessionSettings,
        onRetry: retryFailedMessage,
        autoTranslate: Boolean(state.global.autoTranslate),
      });
      if (scrollToBottom) {
        dom.messages.scrollTop = dom.messages.scrollHeight;
        nearBottom = true;
      }
    }
  }

  function updateComposer() {
    if (dom.sendBtn && dom.input) {
      dom.sendBtn.disabled = conversation.busy || Boolean(getActive()?.loadingOlder)
        || isSessionDeleting(state.activeId) || !dom.input.value.trim();
    }
    if (dom.stopBtn) {
      dom.stopBtn.classList.toggle('hidden', !conversation.busy);
    }
    const moreBtn = dom.messages?.querySelector('.history-more-btn');
    if (moreBtn) {
      const loading = Boolean(getActive()?.loadingOlder);
      moreBtn.disabled = conversation.busy || loading;
      moreBtn.textContent = loading ? '正在加载更早的聊天记录…'
        : conversation.busy ? '回复完成后可查看更早的聊天记录' : '↑ 查看更早的聊天记录';
    }
  }

  function autoGrow() {
    if (!dom.input) return;
    dom.input.style.height = 'auto';
    dom.input.style.height = `${Math.min(dom.input.scrollHeight, 160)}px`;
  }

  function scrollBottom(smooth = false) {
    if (!dom.messages) return;
    dom.messages.scrollTo({ top: dom.messages.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
    nearBottom = true;
  }

  function maybeScroll() {
    if (nearBottom) scrollBottom(false);
  }

  /* ---------- 流式回复与打断 ---------- */
  function stopStream() {
    conversation.stop();
  }

  function interruptAll() {
    stopStream();
    stopCurrentVoice();
  }

  function sendMessage(rawText, voiceMeta) {
    conversation.send(typeof rawText === 'string' ? rawText : dom.input.value, voiceMeta);
  }

  function retryFailedMessage(msg) {
    conversation.retry(msg);
  }

  conversation.on('turnstart', ({ userMessage: userMsg, assistantMessage, originId, retryOf }) => {
    if (retryOf) {
      fullRender(false);
    } else {
      dom.input.value = '';
      autoGrow();
      if (!listInner || !listInner.isConnected) fullRender(false);
      const { el: userEl } = createMessageEl(userMsg, { onPlayVoice: playVoice });
      userEl.classList.add('animate-in');
      listInner.classList.remove('is-empty');
      if (listInner.querySelector('.empty')) listInner.innerHTML = '';
      listInner.appendChild(userEl);
    }
    scrollBottom(true);
    renderSidebar();
    renderHeader();
    updateComposer();
    const typing = createTypingEl();
    listInner.appendChild(typing);
    scrollBottom(false);
    let streamEl = null;
    let streamContentEl = null;
    let streamBarCtl = null;
    const ensureStreamVoiceBar = () => {
      if (!streamEl) return null;
      let actions = streamEl.querySelector('.msg-actions');
      if (!actions) {
        actions = document.createElement('div');
        actions.className = 'msg-actions stream-actions';
        const bubble = streamEl.querySelector('.msg-bubble');
        if (bubble) bubble.appendChild(actions);
      }
      let bar = actions.querySelector('.voice-bar');
      if (!bar) {
        bar = document.createElement('div');
        bar.className = 'voice-bar kind-ai playing';
        bar.setAttribute('role', 'button');
        bar.setAttribute('tabindex', '0');
        bar.setAttribute('aria-label', '播放 / 暂停语音');
        bar.style.setProperty('--vb-w', '140px');

        const btn = document.createElement('span');
        btn.className = 'vb-play';
        btn.setAttribute('aria-hidden', 'true');
        btn.innerHTML =
          '<svg class="icon vb-ico-play"><use href="#i-play"></use></svg><svg class="icon vb-ico-pause"><use href="#i-pause"></use></svg>';

        const waves = document.createElement('span');
        waves.className = 'vb-waves';
        const count = 20;
        for (let i = 0; i < count; i++) {
          const barH = 0.2 + (Math.sin(i * 0.7) + 1) * 0.38;
          const ii = document.createElement('i');
          ii.style.height = `${Math.round(barH * 100)}%`;
          waves.appendChild(ii);
        }

        const durEl = document.createElement('span');
        durEl.className = 'vb-dur';
        durEl.textContent = '播放中…';

        bar.append(btn, waves, durEl);
        actions.prepend(bar);

        let isPlaying = true;
        let progress = 0;
        const paint = () => {
          bar.classList.toggle('playing', isPlaying);
          const lit = Math.round(progress * count);
          [...waves.children].forEach((el, idx) => el.classList.toggle('on', idx < lit));
          durEl.textContent = isPlaying ? '播放中…' : '已暂停';
        };

        streamBarCtl = {
          setPlaying: (v) => {
            isPlaying = Boolean(v);
            paint();
          },
          setProgress: (p) => {
            progress = Math.min(1, Math.max(0, p || 0));
            paint();
          },
        };

        const toggle = (e) => {
          if (e) {
            e.preventDefault();
            e.stopPropagation();
          }
          if (streamAudioController.isPlaying) {
            if (streamAudioController.paused) {
              streamAudioController.resume();
              streamBarCtl.setPlaying(true);
            } else {
              streamAudioController.pause();
              streamBarCtl.setPlaying(false);
            }
          }
        };
        bar.addEventListener('click', toggle);
        bar.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') toggle(e);
        });
      }
      return streamBarCtl;
    };

    const onChunk = (full) => {
      typing.remove();
      if (!streamEl) {
        const pendingMsg = { ...assistantMessage, content: full, ts: Date.now() };
        const built = createMessageEl(pendingMsg, { streaming: true });
        streamEl = built.el;
        streamContentEl = built.contentEl;
        streamEl.classList.add('animate-in', 'streaming');
        listInner.appendChild(streamEl);
        if (state.global.voiceMode && originId === state.activeId) {
          ensureStreamVoiceBar();
        }
      } else {
        streamContentEl.textContent = full;
      }
      maybeScroll();
    };

    const onEnd = ({ message: stored, error, kind: errorCode, failedInline, meta = {}, hasStartedStreamAudio }) => {
      const { cancelled = false } = meta;
      typing.remove();
      if (streamEl) {
        if (stored) {
          streamEl.classList.remove('streaming');
          const { el: finalEl, voiceCtl } = createMessageEl(stored, {
            onPlayVoice: playVoice,
            onResolveJapanese: resolveJapanese,
            autoTranslate: Boolean(state.global.autoTranslate),
          });
          streamEl.replaceWith(finalEl);
          streamEl = finalEl;
          conversation.attachVoiceControl(voiceCtl, stored);
        } else {
          streamEl.remove();
        }
      }
      if (failedInline && originId === state.activeId) fullRender(false);
      updateComposer();
      if (error && !(failedInline && originId === state.activeId)) {
        showToast(error, 'error', needsModelSettings(error, errorCode) ? {
          actionLabel: '配置对话模型',
          onAction: () => openGlobalSettings('models'),
        } : null);
      } else if (meta.audioError && !cancelled) {
        showToast(meta.audioError, 'info', {
          actionLabel: '检查运行状态',
          onAction: () => openGlobalSettings('dashboard'),
        });
      }
      renderSidebar();
      renderHeader();
      if (!cancelled) maybeScroll();
      if (stored && !error && !cancelled && originId === state.activeId && !streamAudioController.isPlaying) {
        const quote = stored.japanese || stored.content;
        portraitStage.updateDialogue(quote);
        if (meta.emotion) {
          try {
            portraitStage.setEmotion(meta.emotion, quote);
          } catch (_) {}
        } else {
          try {
            portraitStage.handleMessageEmotion(stored.content);
          } catch (_) {}
        }
      }
      // Refresh unlocks even if dialogue is still following paused/live audio.
      if (stored && !error && !cancelled && originId === state.activeId) portraitStage.refreshAffection();
      if (stored && !error && !cancelled && state.global.voiceMode && originId === state.activeId) {
        if (!hasStartedStreamAudio && !streamAudioController.isPlaying) {
          const bar = streamEl && (streamEl.querySelector('.voice-bar') || streamEl.querySelector('.vb-play'));
          if (bar) bar.click();
        }
      }
    };

    getStreamAudioControl = ensureStreamVoiceBar;
    const offText = conversation.on('text', ({ text }) => onChunk(text));
    const finish = (event) => {
      offText();
      offDone();
      offError();
      offCancelled();
      onEnd(event);
    };
    const offDone = conversation.on('done', finish);
    const offError = conversation.on('error', finish);
    const offCancelled = conversation.on('cancelled', finish);
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
      if (getActive() === session) fullRender(false, { scrollToBottom: false });
      return;
    }
    session.loadingOlder = true;
    updateComposer();
    if (getActive() === session) fullRender(false, { scrollToBottom: false });
    const prevScrollHeight = dom.messages ? dom.messages.scrollHeight : 0;
    const prevScrollTop = dom.messages ? dom.messages.scrollTop : 0;
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
      updateComposer();
      if (getActive() === session) {
        fullRender(false, { scrollToBottom: false });
        if (dom.messages) {
          const delta = dom.messages.scrollHeight - prevScrollHeight;
          dom.messages.scrollTop = Math.max(0, prevScrollTop + delta);
        }
      }
    }
  }

  async function switchSession(id) {
    closeDrawer();
    if (id === state.activeId) {
      const current = getActive();
      if (current && !current.messages?.length) {
        await loadSessionHistory(current);
        if (getActive() === current) fullRender(false);
      }
      return;
    }
    interruptAll();
    state.activeId = id;
    revokeAllCachedAudioUrls();
    const s = getActive();
    saveState();
    fullRender(true);
    ensureSessionVoice(s);
    if (s && (!s.messages || s.messages.length === 0)) {
      await loadSessionHistory(s);
      if (getActive() !== s) return;
      fullRender(false);
    }
    if (dom.input) dom.input.focus();
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
      updateComposer();
      await removal;
    } catch (error) {
      showToast(`尚未确认删除，对话保留在列表中。${error.message} 可以稍后重试。`, 'error');
      updateComposer();
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
    fullRender(true);
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
    fullRender(true);
    ensureSessionVoice(s);
    if (dom.input) dom.input.focus();
    persistSession(s).catch(() => {});
  }

  const app = { sendMessage, retryFailedMessage, loadOlderSessionHistory, fullRender, updateComposer, conversation };
  if (!initialize) return app;

  /* ---------- 核心事件绑定 ---------- */
  if (dom.newChatBtn) dom.newChatBtn.addEventListener('click', newChat);
  if (dom.menuBtn) dom.menuBtn.addEventListener('click', openDrawer);
  if (dom.sidebarClose) dom.sidebarClose.addEventListener('click', closeDrawer);
  if (dom.backdrop) dom.backdrop.addEventListener('click', closeDrawer);

  if (dom.composer) {
    dom.composer.addEventListener('submit', (e) => {
      e.preventDefault();
      sendMessage();
    });
    keepToastsAboveComposer(dom.composer.closest('.composer-wrap'));
  }
  if (dom.input) {
    dom.input.addEventListener('input', () => {
      autoGrow();
      updateComposer();
    });
    dom.input.addEventListener('keydown', (e) => {
      if ((e.key === 'Enter' && !e.shiftKey && !e.isComposing) || ((e.ctrlKey || e.metaKey) && e.key === 'Enter')) {
        e.preventDefault();
        sendMessage();
      }
    });
  }
  if (dom.stopBtn) dom.stopBtn.addEventListener('click', interruptAll);

  if (dom.messages) {
    dom.messages.addEventListener(
      'scroll',
      () => {
        const d = dom.messages;
        nearBottom = d.scrollHeight - d.scrollTop - d.clientHeight < 160;
      },
      { passive: true }
    );
  }

  document.addEventListener('keydown', (e) => {
    if (hasOpenModal()) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      newChat();
    }
    if (e.key === 'Escape') {
      if (portraitStage.dismissMobile()) return;
      closeDrawer();
      if (typeof streamAudioController !== 'undefined' && streamAudioController.isPlaying) {
        streamAudioController.interrupt(40);
      }
      if (conversation.busy) {
        interruptAll();
      }
    }
  });

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
    sendMessage,
    autoGrow,
    isBusy: () => conversation.busy,
    resolveJapanese,
    onOpenEngineStatus: () => openGlobalSettings('dashboard'),
  });

  if (dom.sessionSettingsBtn) {
    dom.sessionSettingsBtn.addEventListener('click', openSessionSettings);
  }
  if (dom.globalSettingsBtn) {
    dom.globalSettingsBtn.addEventListener('click', () => openGlobalSettings());
  }
  if (dom.badge) {
    dom.badge.style.cursor = 'pointer';
    dom.badge.addEventListener('click', () => openGlobalSettings('models'));
  }

  /* ---------- 启动同步与渲染 ---------- */
  loadState();
  initSessionBackup({
    isBusy: () => conversation.busy,
    onOpenBackupTab: () => openGlobalSettings('update'),
    onImport: () => {
      ++_syncSessionsGen;
      interruptAll();
      fullRender(false);
      ensureSessionVoice(getActive(), { silent: true });
    },
  });
  fullRender(true);
  autoGrow();
  updateComposer();
  updateVoiceModeUI();
  ensureSessionVoice(getActive());
  if (!voiceSupported() && !recorderSupported() && dom.micBtn) dom.micBtn.classList.add('hidden');
  if (window.innerWidth > 820 && dom.input) dom.input.focus();

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
            if (bs.title && (local.title === '新对话' || !local.title)) local.title = bs.title;
            local.updatedAt = serverTimestamp(bs.updated_at, local.updatedAt);
            if (bs.voice_profile_id && !local.settings.voiceProfileId) local.settings.voiceProfileId = bs.voice_profile_id;
            if (bs.settings && Object.keys(bs.settings).length > 0) local.settings = { ...local.settings, ...bs.settings };
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
              settings: {
                ...DEFAULT_SESSION_SETTINGS,
                ...(bs.settings || {}),
                voiceProfileId: bs.voice_profile_id || null,
                systemPrompt: '',
              },
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
      fullRender(false);
      ensureSessionVoice(getActive(), { silent: true });
      if (window.innerWidth > 820 && dom.input) dom.input.focus();
      loadProviders().catch(() => {});
    } catch (err) {
      console.debug('Failed to sync sessions from backend:', err);
    }
  }

  syncSessionsFromBackend().catch(() => {});

  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get('settings') === '1' || window.location.hash === '#settings') {
    openGlobalSettings();
  }

  try {
    portraitStage.init({ profileId: getActive()?.settings?.voiceProfileId });
  } catch (e) {
    console.warn('Failed to initialize portrait stage:', e);
  }

  return app;
}

if (typeof document !== 'undefined') initApp();
