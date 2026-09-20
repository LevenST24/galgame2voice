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
import { streamChat } from './ai.js';
import { voiceSupported, recorderSupported, audioStore } from './voice.js';
import { fetchAndCacheAudio, putCachedAudioBlob } from './cache.js';
import { streamAudioController } from './audio_player.js';
import { portraitStage } from './portrait.js';
import { renderSessionList, renderMessages, createMessageEl, createTypingEl, showToast } from './ui.js';
import { initAuthInterceptor } from './auth.js';

// Initialize console token authentication interceptor
initAuthInterceptor();

// Modular controllers
import { initUpdateManager, loadSystemVersionInfo } from './controllers/update_manager.js';
import {
  initVoiceSettings,
  openSessionSettings,
  fetchVoiceProfiles,
  ensureSessionVoice,
  getVoiceProfiles,
  getActiveProfileId,
} from './controllers/voice_settings.js';
import {
  initGlobalSettings,
  openGlobalSettings,
  loadProviders,
  getCurrentActiveProvider,
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

const $ = (id) => document.getElementById(id);
const dom = {
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
  gSttEngine: $('gSttEngine'),
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
let busy = false;
let cancelStream = null;
let nearBottom = true;

/* ---------- 模态框通用操作 ---------- */
function openModal(el) {
  if (!el) return;
  el.classList.remove('hidden');
  const m = el.querySelector('.modal');
  if (m) m.scrollTop = 0;
  const b = el.querySelector('.modal-body');
  if (b) b.scrollTop = 0;
}

function closeModal(el) {
  if (el) el.classList.add('hidden');
}

/* ---------- 抽屉操作 ---------- */
function openDrawer() {
  if (dom.sidebar) dom.sidebar.classList.add('open');
  if (dom.backdrop) dom.backdrop.classList.add('show');
}

function closeDrawer() {
  if (dom.sidebar) dom.sidebar.classList.remove('open');
  if (dom.backdrop) dom.backdrop.classList.remove('show');
}

/* ---------- 渲染与状态栏 ---------- */
function updateBadge() {
  if (dom.badgeDot) dom.badgeDot.className = 'badge-dot dot-meoo';
  if (dom.badge) {
    const label = dom.badge.querySelector('.badge-label') || dom.badge.lastChild;
    if (label) {
      const activeProvider = getCurrentActiveProvider();
      if (activeProvider && (activeProvider.name || activeProvider.chat_model)) {
        const pName = activeProvider.name || '活跃模型';
        const mName = activeProvider.chat_model || '';
        label.textContent = mName ? `${pName} · ${mName}` : pName;
      } else {
        label.textContent = '本地引擎 · GPT-SoVITS';
      }
    }
  }
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
  }
  if (s && dom.sessionSettingsBtn) {
    const st = s.settings;
    dom.sessionSettingsBtn.classList.toggle('configured', Boolean(st?.systemPrompt));
  }
  updateBadge();
}

async function resolveJapanese(msg) {
  if (msg.japanese) return msg.japanese;
  try {
    const res = await fetch('/api/chat/japanese', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: msg.content,
        session_id: state.activeId,
      }),
    });
    if (!res.ok) return '';
    const data = await res.json();
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

function fullRender(animate = false) {
  renderSidebar();
  renderHeader();
  if (dom.messages) {
    listInner = renderMessages(dom.messages, getActive(), {
      animate,
      onPick: sendMessage,
      onPlayVoice: playVoice,
      onResolveJapanese: resolveJapanese,
      autoTranslate: Boolean(state.global.autoTranslate),
    });
    dom.messages.scrollTop = dom.messages.scrollHeight;
  }
  nearBottom = true;
}

function updateComposer() {
  if (dom.sendBtn && dom.input) {
    dom.sendBtn.disabled = busy || !dom.input.value.trim();
  }
  if (dom.stopBtn) {
    dom.stopBtn.classList.toggle('hidden', !busy);
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
  if (cancelStream) {
    const c = cancelStream;
    cancelStream = null;
    c();
  }
}

function interruptAll() {
  stopStream();
  stopCurrentVoice();
}

function sendMessage(rawText, voiceMeta) {
  clearMicTimers();
  const text = (typeof rawText === 'string' ? rawText : dom.input.value).trim();
  if (!text || busy) return;

  if (streamAudioController.isPlaying || getCurrentVoice()) {
    stopCurrentVoice();
  }
  const session = getActive();
  if (!session) return;
  const originId = session.id;

  const prevTitle = session.title;
  const userMsg = addMessage('user', text);
  if (session.title !== prevTitle) {
    fetch('/api/chat/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: session.id,
        title: session.title,
        voice_profile_id: session.settings?.voiceProfileId,
        custom_system_prompt: session.settings?.systemPrompt,
        settings: session.settings,
      }),
    }).catch(() => {});
  }
  if (voiceMeta && userMsg) {
    audioStore.set(userMsg.id, { url: voiceMeta.url, dur: voiceMeta.dur });
    userMsg.dur = voiceMeta.dur;
    if (voiceMeta.blob) {
      const voiceKey = `user_${userMsg.id}`;
      putCachedAudioBlob(voiceKey, voiceMeta.blob);
      userMsg.voiceKey = voiceKey;
      saveState();
    }
  }
  dom.input.value = '';
  autoGrow();

  if (!listInner || !listInner.isConnected) fullRender(false);
  const { el: userEl } = createMessageEl(userMsg, { onPlayVoice: playVoice });
  userEl.classList.add('animate-in');
  listInner.classList.remove('is-empty');
  if (listInner.querySelector('.empty')) listInner.innerHTML = '';
  listInner.appendChild(userEl);
  scrollBottom(true);

  renderSidebar();
  renderHeader();
  busy = true;
  updateComposer();

  const typing = createTypingEl();
  listInner.appendChild(typing);
  scrollBottom(false);

  let streamEl = null;
  let streamContentEl = null;
  let lastFull = '';
  const streamMsgId = uid('m_stream');
  let streamBarCtl = null;
  let hasStartedStreamAudio = false;

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

  const onChunk = (chunk, full) => {
    typing.remove();
    lastFull = full;
    if (!streamEl) {
      const pendingMsg = {
        id: streamMsgId,
        role: 'assistant',
        content: full,
        ts: Date.now(),
      };
      const built = createMessageEl(pendingMsg, { onPlayVoice: playVoice });
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

  const onEnd = (finalText, meta, error, cancelled) => {
    typing.remove();
    cancelStream = null;
    let stored = null;
    const textToSave = (finalText || lastFull || '').trim();

    const msgPayload = {
      id: uid('m'),
      role: 'assistant',
      content: textToSave,
      japanese: meta?.japanese || '',
      audioUrls: meta?.audio_urls || (meta?.audio_url ? [meta.audio_url] : []),
      dur: meta?.audio_duration || 0,
      ts: Date.now(),
      ttsParams: meta?.tts_params || null,
      ttsPlan: meta?.tts_plan || null,
    };

    if (streamEl) {
      if (textToSave) {
        streamEl.classList.remove('streaming');
        stored = msgPayload;
        const { el: finalEl } = createMessageEl(stored, {
          onPlayVoice: playVoice,
          onResolveJapanese: resolveJapanese,
          autoTranslate: Boolean(state.global.autoTranslate),
        });
        streamEl.replaceWith(finalEl);
        streamEl = finalEl;
        if (hasStartedStreamAudio && streamBarCtl) {
          const newBar = finalEl.querySelector('.voice-bar');
          if (newBar) {
            newBar.classList.add('playing');
          }
        }
      } else {
        streamEl.remove();
      }
    }
    const origin = getSession(originId);
    if (origin && stored) {
      origin.messages.push(msgPayload);
      origin.updatedAt = Date.now();
    }
    saveState();
    busy = false;
    updateComposer();
    renderSidebar();
    renderHeader();
    if (!cancelled) maybeScroll();
    if (stored && !error && !cancelled) {
      if (meta && meta.emotion) {
        try {
          portraitStage.setEmotion(meta.emotion, stored);
        } catch (_) {}
      } else {
        try {
          portraitStage.handleMessageEmotion(stored);
        } catch (_) {}
      }
    }
    if (stored && !error && !cancelled && state.global.voiceMode && originId === state.activeId) {
      if (!streamAudioController.isPlaying) {
        const bar = streamEl && (streamEl.querySelector('.voice-bar') || streamEl.querySelector('.vb-play'));
        if (bar) bar.click();
      }
    }
  };

  cancelStream = streamChat({
    prompt: text,
    sessionId: session.id,
    settings: session.settings,
    preset: state.global.ttsPreset || '',
    onChunk,
    onAudio: (url, idx, sentence) => {
      if (url && typeof url === 'string' && url.startsWith('/audio/')) {
        fetchAndCacheAudio(url).catch(() => {});
      }
      if (state.global.voiceMode && originId === state.activeId && url) {
        if (idx === 0 || !hasStartedStreamAudio) {
          hasStartedStreamAudio = true;
          const ctl = ensureStreamVoiceBar();
          streamAudioController.startSession(streamMsgId, ctl);
          setCurrentVoice({
            msgId: streamMsgId,
            get paused() {
              return streamAudioController.paused;
            },
            pause() {
              streamAudioController.pause();
              if (ctl) ctl.setPlaying(false);
            },
            resume() {
              streamAudioController.resume();
              if (ctl) ctl.setPlaying(true);
            },
            setPlaying: (p) => {
              if (ctl) ctl.setPlaying(p);
            },
            setProgress: (pct) => {
              if (ctl) ctl.setProgress(pct);
            },
            stop() {
              streamAudioController.interrupt(40);
            },
          });
          streamAudioController.enqueueChunk({ url, index: idx, sentence, ctl });
        } else {
          streamAudioController.enqueueChunk({ url, index: idx, sentence, ctl: streamBarCtl });
        }
      }
    },
    onEnd,
  });
}

/* ---------- 会话操作 ---------- */
async function loadSessionHistory(session) {
  if (!session || (session.messages && session.messages.length > 0)) return;
  try {
    const res = await fetch(`/api/chat/history?session_id=${encodeURIComponent(session.id)}&limit=100`);
    if (!res.ok) return;
    const data = await res.json();
    if (Array.isArray(data.messages) && data.messages.length > 0) {
      session.messages = data.messages.map((bm) => ({
        id: `m_${bm.id}`,
        role: bm.role,
        content: bm.content_chinese,
        japanese: bm.content_japanese || '',
        audioUrls: bm.audio_url ? [bm.audio_url] : [],
        ts: bm.created_at ? new Date(bm.created_at).getTime() : Date.now(),
      }));
      autoTitle(session);
      saveState();
    }
  } catch (e) {
    console.debug('Failed to load session history:', e);
  }
}

async function switchSession(id) {
  closeDrawer();
  if (id === state.activeId) return;
  interruptAll();
  state.activeId = id;
  revokeAllCachedAudioUrls();
  const s = getActive();
  if (s && (!s.messages || s.messages.length === 0)) {
    await loadSessionHistory(s);
  }
  saveState();
  fullRender(true);
  ensureSessionVoice(getActive());
  if (dom.input) dom.input.focus();
}

function handleDelete(id) {
  if (id === state.activeId || busy) {
    interruptAll();
  } else {
    stopCurrentVoice();
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
  fetch(`/api/chat/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => {});
  fetch(`/api/chat/history?session_id=${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => {});
  fullRender(true);
  ensureSessionVoice(getActive());
  showToast('对话已删除');
}

function newChat() {
  interruptAll();
  closeDrawer();
  revokeAllCachedAudioUrls();
  const prevSession = getActive();
  const s = createSession();
  const activeProfileId = getActiveProfileId();
  const voiceProfiles = getVoiceProfiles();

  if (prevSession && prevSession.settings && prevSession.settings.voiceProfileId) {
    s.settings.voiceProfileId = prevSession.settings.voiceProfileId;
    s.settings.systemPrompt = prevSession.settings.systemPrompt;
    s.settings.ttsSpeed = prevSession.settings.ttsSpeed;
  } else if (activeProfileId) {
    const active = voiceProfiles.find((p) => p.id === activeProfileId);
    if (active) {
      s.settings.voiceProfileId = active.id;
      if (active.system_prompt) s.settings.systemPrompt = active.system_prompt;
      if (active.name === '高楯欧丽叶') s.settings.ttsSpeed = 0.88;
      else if (active.name === '常陆茉子') s.settings.ttsSpeed = 0.90;
      else if (active.name === '白雪乃爱') s.settings.ttsSpeed = 1.05;
    }
  }
  saveState();
  fullRender(true);
  ensureSessionVoice(s);
  if (dom.input) dom.input.focus();
  fetch('/api/chat/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      id: s.id,
      title: s.title,
      voice_profile_id: s?.settings?.voiceProfileId,
      custom_system_prompt: s.settings?.systemPrompt,
      settings: s.settings,
    }),
  }).catch(() => {});
}

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
}
if (dom.input) {
  dom.input.addEventListener('input', () => {
    autoGrow();
    if (!busy && dom.sendBtn) dom.sendBtn.disabled = !dom.input.value.trim();
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
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    newChat();
  }
  if (e.key === 'Escape') {
    const isModalOpen =
      (dom.globalModal && dom.globalModal.classList.contains('active')) ||
      (dom.sessionModal && dom.sessionModal.classList.contains('active'));
    closeModal(dom.globalModal);
    closeModal(dom.sessionModal);
    closeDrawer();
    if (!isModalOpen) {
      if (typeof streamAudioController !== 'undefined' && streamAudioController.isPlaying) {
        streamAudioController.interrupt(40);
      }
      if (busy) {
        interruptAll();
      }
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
});
initGlobalSettings(dom, {
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
  isBusy: () => busy,
});

if (dom.sessionSettingsBtn) {
  dom.sessionSettingsBtn.addEventListener('click', openSessionSettings);
}
if (dom.globalSettingsBtn) {
  dom.globalSettingsBtn.addEventListener('click', openGlobalSettings);
}

/* ---------- 启动同步与渲染 ---------- */
loadState();
fullRender(true);
autoGrow();
updateComposer();
updateVoiceModeUI();
ensureSessionVoice(getActive());
if (!voiceSupported() && !recorderSupported() && dom.micBtn) dom.micBtn.classList.add('hidden');
if (window.innerWidth > 820 && dom.input) dom.input.focus();

// 同步后端 SQLite 会话列表及历史消息（generation counter 防止过期结果覆盖用户新建会话）
let _syncSessionsGen = 0;
async function syncSessionsFromBackend() {
  const gen = ++_syncSessionsGen;
  const isStale = () => gen !== _syncSessionsGen;
  try {
    const res = await fetch('/api/chat/sessions?limit=50');
    if (!res.ok) return;
    const data = await res.json();
    if (isStale()) return;
    const backendSessions = data.sessions || [];
    if (backendSessions.length > 0) {
      const existingMap = new Map(state.sessions.map((s) => [s.id, s]));
      const merged = [];
      for (const bs of backendSessions) {
        let local = existingMap.get(bs.id);
        if (local) {
          if (bs.title && (local.title === '新对话' || !local.title)) local.title = bs.title;
          local.updatedAt = bs.updated_at ? new Date(bs.updated_at).getTime() : local.updatedAt;
          if (bs.voice_profile_id && !local.settings.voiceProfileId) local.settings.voiceProfileId = bs.voice_profile_id;
          if (bs.custom_system_prompt && !local.settings.systemPrompt) local.settings.systemPrompt = bs.custom_system_prompt;
          if (bs.settings && Object.keys(bs.settings).length > 0) local.settings = { ...local.settings, ...bs.settings };
          merged.push(local);
          existingMap.delete(bs.id);
        } else {
          merged.push({
            id: bs.id,
            title: bs.title || '新对话',
            createdAt: bs.created_at ? new Date(bs.created_at).getTime() : Date.now(),
            updatedAt: bs.updated_at ? new Date(bs.updated_at).getTime() : Date.now(),
            messages: [],
            settings: {
              ...DEFAULT_SESSION_SETTINGS,
              ...(bs.settings || {}),
              voiceProfileId: bs.voice_profile_id || null,
              systemPrompt: bs.custom_system_prompt || '',
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
      fetch('/api/chat/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: s.id,
          title: s.title,
          voice_profile_id: s.settings?.voiceProfileId,
          custom_system_prompt: s.settings?.systemPrompt,
          settings: s.settings,
        }),
      }).catch(() => {});
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
  portraitStage.init();
} catch (e) {
  console.warn('Failed to initialize portrait stage:', e);
}
