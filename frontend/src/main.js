// 应用入口：事件绑定、渲染调度、设置面板、流式回复
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
import {
  startListening,
  stopListening,
  isListening,
  voiceSupported,
  recorderSupported,
  estimateDuration,
  plainSpeechText,
  audioStore,
} from './voice.js';
import {
  getCachedAudioBlob,
  putCachedAudioBlob,
  fetchAndCacheAudio,
  clearMemAudioCache,
} from './cache.js';
import { streamAudioController } from './audio_player.js';
import {
  renderSessionList,
  renderMessages,
  createMessageEl,
  createTypingEl,
  formatContent,
  showToast,
} from './ui.js';

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
  chatMeta: $('chatMeta'),
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
  gProviderActivate: $('gProviderActivate'),
  gCustomBox: $('gCustomBox'),
  gCustomName: $('gCustomName'),
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
  gProviderTest: $('gProviderTest'),
  gTgChatId: $('gTgChatId'),
  gMemoryEnabled: $('gMemoryEnabled'),
  gUserNickname: $('gUserNickname'),
  gDefaultSystemPrompt: $('gDefaultSystemPrompt'),
  gResetBtn: $('gResetBtn'),
};

let listInner = null;
let busy = false;
let cancelStream = null;
let nearBottom = true;

/* ---------- 语音播放：播放/暂停/续播 ----------
 * currentVoice 记录当前播放会话；同一条消息再次点击 = 暂停/继续切换，
 * 点其他消息 = 停止旧的、播放新的。
 */
let currentVoice = null; // { msgId, paused, pause, resume, setPlaying, setProgress, stop }
let micTimer = null;
let micSecs = 0;
let micBase = '';
let pendingVoice = { text: null, audio: null, dur: 0 };
let micFinalizeTimers = [];
function clearMicTimers() {
  micFinalizeTimers.forEach((t) => clearTimeout(t));
  micFinalizeTimers = [];
}

function stopCurrentVoice() {
  try {
    streamAudioController.interrupt(40);
  } catch (_) {}
  if (currentVoice) {
    const v = currentVoice;
    currentVoice = null;
    try { v.stop(); } catch (_) {}
    try { v.setPlaying(false); } catch (_) {}
  }
}

// 从 URL 播放单个音频，支持暂停/续播与进度上报
function playSingleAudio(getAudio, msgId, ctl, { objectUrl = null } = {}) {
  let audio = getAudio();
  let cancelled = false;
  let paused = false;

  const attach = () => {
    audio.ontimeupdate = () => {
      if (audio.duration) ctl.setProgress(audio.currentTime / audio.duration);
    };
    audio.onended = () => {
      audio.ontimeupdate = null;
      audio.onended = null;
      audio.onerror = null;
      if (!cancelled) {
        ctl.setProgress(1);
        ctl.setPlaying(false);
        if (currentVoice && currentVoice.msgId === msgId) currentVoice = null;
        if (objectUrl) {
          try { URL.revokeObjectURL(objectUrl); } catch (_) {}
          objectUrl = null;
        }
      }
    };
    audio.onerror = () => {
      audio.ontimeupdate = null;
      audio.onended = null;
      audio.onerror = null;
      if (cancelled) return;
      ctl.setPlaying(false);
      if (objectUrl) {
        try { URL.revokeObjectURL(objectUrl); } catch (_) {}
        objectUrl = null;
      }
      showToast('音频播放失败', 'error');
    };
  };
  attach();

  currentVoice = {
    msgId,
    get paused() { return paused; },
    pause() {
      paused = true;
      audio.pause();
      ctl.setPlaying(false);
    },
    resume() {
      paused = false;
      const p = audio.play();
      if (p && typeof p.then === 'function') {
        p.then(() => {
          if (paused || cancelled) {
            audio.pause();
            ctl.setPlaying(false);
          } else {
            ctl.setPlaying(true);
          }
        }).catch((err) => {
          if (err && err.name === 'AbortError') return;
          ctl.setPlaying(false);
        });
      }
    },
    setPlaying: ctl.setPlaying,
    setProgress: ctl.setProgress,
    stop() {
      cancelled = true;
      paused = true;
      audio.pause();
      audio.ontimeupdate = null;
      audio.onended = null;
      audio.onerror = null;
      if (objectUrl) {
        try { URL.revokeObjectURL(objectUrl); } catch (_) {}
        objectUrl = null;
      }
    },
  };
  const p = audio.play();
  if (p && typeof p.then === 'function') {
    p.then(() => {
      if (paused || cancelled) {
        audio.pause();
        ctl.setPlaying(false);
      } else {
        ctl.setPlaying(true);
      }
    }).catch((err) => {
      if (err && err.name === 'AbortError') return;
      ctl.setPlaying(false);
    });
  }
}

// 现场调用后端 GPT-SoVITS 合成角色原声音频（带本地持久缓存与参数继承，绝不调用浏览器机械音）
async function synthesizeAiVoice(msg, ctl) {
  stopCurrentVoice();
  const abortCtrl = new AbortController();
  let cancelled = false;
  let paused = false;
  let isTimeout = false;
  const timeoutTimer = setTimeout(() => {
    isTimeout = true;
    abortCtrl.abort();
  }, 16000);

  currentVoice = {
    msgId: msg.id,
    get paused() { return paused; },
    pause() {
      paused = true;
      clearTimeout(timeoutTimer);
      ctl.setLoading(false);
      ctl.setPlaying(false);
      abortCtrl.abort();
    },
    resume() {
      if (currentVoice && currentVoice.msgId === msg.id) {
        currentVoice = null;
        synthesizeAiVoice(msg, ctl);
      }
    },
    setPlaying: ctl.setPlaying,
    setProgress: ctl.setProgress,
    stop() {
      cancelled = true;
      paused = true;
      clearTimeout(timeoutTimer);
      ctl.setLoading(false);
      ctl.setPlaying(false);
      abortCtrl.abort();
    },
  };
  ctl.setLoading(true);
  try {
    const text = plainSpeechText(msg.content).slice(0, 2000);
    if (!text) {
      ctl.setLoading(false);
      currentVoice = null;
      showToast('该消息没有可朗读的文字内容', 'info');
      return;
    }

    const session = getActive() || getSession(state.activeId) || {};
    const settings = session.settings || DEFAULT_SESSION_SETTINGS;
    const hasJa = /[\u3040-\u30ff\u31f0-\u31ff]/.test(text);
    const textLang = hasJa ? 'ja' : 'zh';

    const isAdaptive = settings.aiAdaptiveVoice !== false;
    const dynSpeed = (isAdaptive && msg.ttsParams && typeof msg.ttsParams.speed === 'number')
      ? msg.ttsParams.speed
      : (settings.ttsSpeed || 1.0);
    const dynTemp = (isAdaptive && msg.ttsParams && typeof msg.ttsParams.temperature === 'number')
      ? msg.ttsParams.temperature
      : (settings.ttsTemperature || 1.0);

    const reqBody = {
      text,
      speed: dynSpeed,
      top_k: settings.ttsTopK || 15,
      top_p: settings.ttsTopP || 1.0,
      temperature: dynTemp,
      text_language: textLang,
      voice_profile_id: settings.voiceProfileId || undefined,
      ai_adaptive_voice: isAdaptive,
      options: {
        voice_profile_id: settings.voiceProfileId || undefined,
        text_lang: textLang,
        text_language: textLang,
        speed: dynSpeed,
        top_k: settings.ttsTopK || 15,
        top_p: settings.ttsTopP || 1.0,
        temperature: dynTemp,
        ai_adaptive_voice: isAdaptive,
      },
    };

    const res = await fetch('/api/voice/synthesize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(reqBody),
      signal: abortCtrl.signal,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      const detail = data.detail || `HTTP ${res.status}`;
      throw new Error(detail);
    }
    const blob = await res.blob();
    ctl.setLoading(false);
    if (cancelled || paused || (currentVoice && currentVoice.msgId !== msg.id)) {
      return;
    }

    // 写入浏览器持久化 Cache Storage，重启后依旧秒播
    const cacheKey = `msg_${msg.id}`;
    await putCachedAudioBlob(cacheKey, blob);
    msg.audioUrls = [cacheKey];
    saveState();

    const url = URL.createObjectURL(blob);
    playSingleAudio(() => new Audio(url), msg.id, ctl, { objectUrl: url });
  } catch (e) {
    ctl.setLoading(false);
    ctl.setPlaying(false);
    if (currentVoice && currentVoice.msgId === msg.id) {
      currentVoice = null;
    }
    if (isTimeout) {
      showToast('语音合成超时（16s），请检查 GPT-SoVITS 服务是否正常运行', 'error');
    } else if (cancelled || (e && e.name === 'AbortError')) {
      return;
    } else {
      const errStr = e.message || String(e);
      showToast(`语音合成失败（${errStr}），请检查 GPT-SoVITS 服务是否就绪`, 'error');
    }
  } finally {
    clearTimeout(timeoutTimer);
    ctl.setLoading(false);
  }
}

async function playAiVoice(msg, ctl) {
  // 同一条消息：切换暂停 / 继续
  if (currentVoice && currentVoice.msgId === msg.id) {
    if (currentVoice.paused) currentVoice.resume();
    else currentVoice.pause();
    return;
  }
  stopCurrentVoice();

  // 1. 优先从本地 Cache Storage 读取整句持久缓存（毫秒级秒开秒播，不惧后端清理/重启）
  const fullCacheKey = `msg_${msg.id}`;
  const localCachedBlob = await getCachedAudioBlob(fullCacheKey);
  if (localCachedBlob && localCachedBlob.size > 0) {
    ctl.setProgress(0);
    ctl.setPlaying(true);
    currentVoice = {
      msgId: msg.id,
      get paused() {
        return streamAudioController.paused;
      },
      pause() {
        streamAudioController.pause();
        ctl.setPlaying(false);
      },
      resume() {
        streamAudioController.resume();
        ctl.setPlaying(true);
      },
      setPlaying: ctl.setPlaying,
      setProgress: ctl.setProgress,
      stop() {
        streamAudioController.interrupt(40);
      },
    };
    streamAudioController.startSession(msg.id, ctl);
    streamAudioController.enqueueChunk({
      url: fullCacheKey,
      index: 0,
      ctl,
      blob: localCachedBlob,
      totalExpected: 1,
    });
    return;
  }

  const urls = (msg.audioUrls || []).filter(Boolean);
  if (!urls.length) {
    // 缺失音频：自动调用模型现场重合成角色原声
    synthesizeAiVoice(msg, ctl);
    return;
  }

  // 2. 有分句音频链接：Web Audio API 无缝高保真排队播放 (Gapless + 12ms micro-fade)
  ctl.setProgress(0);
  ctl.setPlaying(true);

  currentVoice = {
    msgId: msg.id,
    get paused() {
      return streamAudioController.paused;
    },
    pause() {
      streamAudioController.pause();
      ctl.setPlaying(false);
    },
    resume() {
      streamAudioController.resume();
      ctl.setPlaying(true);
    },
    setPlaying: ctl.setPlaying,
    setProgress: ctl.setProgress,
    stop() {
      streamAudioController.interrupt(40);
    },
  };

  streamAudioController.playChunks(urls, ctl, msg.id).catch((err) => {
    console.warn('[playAiVoice] 播放失败，回退重合成:', err);
    currentVoice = null;
    synthesizeAiVoice(msg, ctl);
  });
}

async function playUserVoice(msg, ctl) {
  let rec = audioStore.get(msg.id);
  if (!rec && (msg.voiceKey || msg.audioUrl)) {
    const key = msg.voiceKey || msg.audioUrl;
    const blob = await getCachedAudioBlob(key);
    if (blob) {
      rec = { url: URL.createObjectURL(blob), dur: msg.dur || 3 };
      audioStore.set(msg.id, rec);
    }
  }
  if (!rec) {
    showToast('该条录音缓存已清理，无法播放', 'info');
    return;
  }
  // 同一条录音：切换暂停 / 继续
  if (currentVoice && currentVoice.msgId === msg.id) {
    if (currentVoice.paused) currentVoice.resume();
    else currentVoice.pause();
    return;
  }
  stopCurrentVoice();
  playSingleAudio(() => new Audio(rec.url), msg.id, ctl);
}

function playVoice(msg, ctl) {
  if (msg.role === 'user') playUserVoice(msg, ctl);
  else playAiVoice(msg, ctl);
}

function updateVoiceModeUI() {
  const on = state.global.voiceMode;
  dom.voiceModeBtn.classList.toggle('active', on);
  dom.voiceModeBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  dom.voiceModeBtn.title = on ? '关闭自动播放语音回复' : '自动播放语音回复';
  dom.voiceModeIcon.querySelector('use').setAttribute('href', on ? '#i-volume' : '#i-volume-off');
  if (!on) stopCurrentVoice();
}

function updateMicUI(listening) {
  dom.micBtn.classList.toggle('listening', Boolean(listening));
  dom.micBtn.setAttribute('aria-pressed', listening ? 'true' : 'false');
}

function finalizeVoice() {
  clearMicTimers();
  const { text, audio, dur, blob } = pendingVoice;
  pendingVoice = { text: null, audio: null, dur: 0, blob: null };
  if (!text && !audio) return;
  if (text) {
    sendMessage(text, audio ? { url: audio, dur, blob } : undefined);
  } else {
    showToast('没有识别到语音内容，请靠近麦克风再试', 'error');
  }
}

function stopMic() {
  clearInterval(micTimer);
  micTimer = null;
  clearMicTimers();
  updateMicUI(false);
  dom.input.placeholder = '说点什么，或点麦克风用语音输入…';
  stopListening();
  // 转写结束回调与录音停止回调都会更新 pendingVoice，这里给停止流程一个兜底窗口
  micFinalizeTimers.push(
    setTimeout(() => {
      if (!isListening() && pendingVoice.text !== null) finalizeVoice();
    }, 600)
  );
  micFinalizeTimers.push(
    setTimeout(() => {
      if (pendingVoice.text !== null || pendingVoice.audio) finalizeVoice();
    }, 1400)
  );
}

function toggleMic() {
  clearMicTimers();
  if (isListening()) {
    stopMic();
    return;
  }
  if (busy) return;
  const handle = startListening({
    onInterim: (t) => {
      dom.input.value = `${micBase}${micBase ? ' ' : ''}${t}`;
      autoGrow();
    },
    onText: (finalText) => {
      pendingVoice.text = `${micBase ? micBase + ' ' : ''}${finalText}`.trim();
      dom.input.value = pendingVoice.text;
      autoGrow();
    },
    onRecorded: (blob, dur) => {
      pendingVoice.audio = URL.createObjectURL(blob);
      pendingVoice.dur = dur;
      pendingVoice.blob = blob;
    },
    onError: (msg) => {
      showToast(msg, 'error');
    },
  });
  if (!handle.ok) {
    showToast(handle.reason, 'error');
    return;
  }
  micBase = dom.input.value;
  pendingVoice = { text: null, audio: null, dur: 0, blob: null };
  micSecs = 0;
  updateMicUI(true);
  micTimer = setInterval(() => {
    micSecs += 1;
    dom.input.placeholder = `正在聆听… ${micSecs}s（再次点击麦克风结束）`;
  }, 1000);
  dom.input.placeholder = '正在聆听…（再次点击麦克风结束）';
  if (!recorderSupported() && !voiceSupported()) {
    showToast('当前浏览器不支持语音输入，建议使用 Chrome / Edge', 'error');
  }
}

/* ---------- 渲染 ---------- */
function renderSidebar() {
  renderSessionList(dom.sessionList, {
    sessions: state.sessions,
    activeId: state.activeId,
    onSelect: switchSession,
    onDelete: handleDelete,
  });
  dom.sessionCount.textContent = state.sessions.length;
}

function renderHeader() {
  const s = getActive();
  dom.chatTitle.textContent = s ? s.title : '新对话';
  if (s) {
    const st = s.settings;
    dom.chatMeta.textContent = `${st.systemPrompt ? '已设人设' : '未设人设'} · 温度 ${st.temperature} · 上下文 ${st.maxContext} 条`;
    dom.sessionSettingsBtn.classList.toggle('configured', Boolean(st.systemPrompt));
  } else {
    dom.chatMeta.textContent = '';
  }
  updateBadge();
}

function updateBadge() {
  dom.badgeDot.className = 'badge-dot dot-meoo';
  dom.badge.lastChild.textContent = '本地引擎 · GPT-SoVITS';
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
  listInner = renderMessages(dom.messages, getActive(), {
    animate,
    onPick: sendMessage,
    onPlayVoice: playVoice,
    onResolveJapanese: resolveJapanese,
    autoTranslate: Boolean(state.global.autoTranslate),
  });
  dom.messages.scrollTop = dom.messages.scrollHeight;
  nearBottom = true;
}

function updateComposer() {
  dom.sendBtn.disabled = busy || !dom.input.value.trim();
  dom.stopBtn.classList.toggle('hidden', !busy);
}

function autoGrow() {
  dom.input.style.height = 'auto';
  dom.input.style.height = `${Math.min(dom.input.scrollHeight, 160)}px`;
}

function scrollBottom(smooth = false) {
  dom.messages.scrollTo({ top: dom.messages.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
  nearBottom = true;
}

function maybeScroll() {
  if (nearBottom) scrollBottom(false);
}

/* ---------- 抽屉 ---------- */
function openDrawer() {
  dom.sidebar.classList.add('open');
  dom.backdrop.classList.add('show');
}
function closeDrawer() {
  dom.sidebar.classList.remove('open');
  dom.backdrop.classList.remove('show');
}

/* ---------- 模态框 ---------- */
function openModal(el) {
  el.classList.remove('hidden');
  const m = el.querySelector('.modal');
  if (m) m.scrollTop = 0;
  const b = el.querySelector('.modal-body');
  if (b) b.scrollTop = 0;
}
function closeModal(el) {
  el.classList.add('hidden');
}

function openSessionSettings() {
  const s = getActive();
  if (!s) return;
  dom.smTitle.textContent = `· ${s.title}`;
  dom.sSystem.value = s.settings.systemPrompt;
  dom.sTemp.value = s.settings.temperature;
  dom.sTopP.value = s.settings.topP;
  dom.sFreq.value = s.settings.freqPenalty;
  dom.sPres.value = s.settings.presPenalty;
  dom.sMaxTokens.value = s.settings.maxTokens;
  dom.sCtx.value = s.settings.maxContext;
  dom.sAiAdaptiveVoice.value = String(s.settings.aiAdaptiveVoice !== false);
  dom.sTtsSpeed.value = s.settings.ttsSpeed;
  dom.sTtsTopK.value = s.settings.ttsTopK;
  dom.sTtsTopP.value = s.settings.ttsTopP;
  dom.sTtsTemp.value = s.settings.ttsTemperature;
  syncRangeLabels();
  loadSessionVoiceSelect(s);
  openModal(dom.sessionModal);
}

/* ---------- 角色音色（按会话绑定：切换会话 = 换角色） ---------- */
let voiceProfiles = [];
let activeProfileId = null;

async function fetchVoiceProfiles() {
  const res = await fetch('/api/voice/profiles');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  voiceProfiles = data.profiles || [];
  activeProfileId = data.active_profile_id;
  return data;
}

// 会话绑定了音色且与当前加载的不一致时，自动切换模型权重
async function ensureSessionVoice(session, { silent = false } = {}) {
  const want = session?.settings?.voiceProfileId;
  if (!want) return;
  try {
    if (activeProfileId === null) await fetchVoiceProfiles();
    let res = await fetch('/api/voice/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile_id: want }),
    });
    let data = await res.json().catch(() => ({}));
    if (!res.ok && res.status === 503) {
      // 内存严格阈值预警时，自动以安全 force 模式重试（加载 ~330MB 权重无需苛求 2GB 绝对空闲）
      res = await fetch('/api/voice/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile_id: want, force: true }),
      });
      data = await res.json().catch(() => ({}));
    }
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    activeProfileId = want;
    if (!silent) showToast(`已切换音色：${data.profile || want}`, 'success');
  } catch (e) {
    showToast(`音色切换失败: ${e.message || e}`, 'error');
  }
}

async function loadSessionVoiceSelect(session) {
  dom.sVoice.innerHTML = '<option value="">跟随后端当前音色</option>';
  dom.sVoiceActive.textContent = '';
  try {
    await fetchVoiceProfiles();
    const active = voiceProfiles.find((p) => p.id === activeProfileId);
    dom.sVoiceActive.textContent = active ? `当前加载：${active.name}` : '';
    // 当前加载的音色排最前
    const sortedProfiles = active ? [active, ...voiceProfiles.filter((p) => p !== active)] : voiceProfiles;
    for (const p of sortedProfiles) {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = `${p.name}${p.id === activeProfileId ? '（当前加载）' : ''}`;
      dom.sVoice.appendChild(opt);
    }
    const customOpt = document.createElement('option');
    customOpt.value = '__custom__';
    customOpt.textContent = '＋ 新建自定义音色（选择 ckpt / pth / 参考音频）…';
    dom.sVoice.appendChild(customOpt);
    if (session.settings.voiceProfileId) dom.sVoice.value = String(session.settings.voiceProfileId);
  } catch (e) {
    dom.sVoice.innerHTML = '<option value="">加载失败（后端未启动？）</option>';
  }
  syncCustomVoiceBox();
}

function syncCustomVoiceBox() {
  const show = dom.sVoice.value === '__custom__';
  dom.sCustomVoiceBox.classList.toggle('hidden', !show);
  if (show) populateScanOptions();
}

let scannedModels = null;
async function populateScanOptions() {
  const fill = (sel, list, emptyHint) => {
    sel.innerHTML = list.length
      ? list.map((f) => `<option value="${f.path}">${f.name}</option>`).join('')
      : `<option value="">${emptyHint}</option>`;
  };
  try {
    if (!scannedModels) {
      const res = await fetch('/api/voice/scan-models');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      scannedModels = await res.json();
    }
    fill(dom.sCvGpt, scannedModels.gpt_weights || [], '（未扫描到 .ckpt 文件）');
    fill(dom.sCvSovits, scannedModels.sovits_weights || [], '（未扫描到 .pth 文件）');
    fill(dom.sCvRef, scannedModels.audio_files || [], '（未扫描到音频文件，可留空音色但质量差）');
  } catch (e) {
    [dom.sCvGpt, dom.sCvSovits, dom.sCvRef].forEach((sel) => {
      sel.innerHTML = `<option value="">扫描失败（${e.message || e}）</option>`;
    });
  }
}

async function createCustomVoice() {
  const name = dom.sCvName.value.trim();
  const gptPath = dom.sCvGpt.value;
  const sovitsPath = dom.sCvSovits.value;
  const refPath = dom.sCvRef.value;
  const promptText = dom.sCvPromptText.value.trim();
  if (!name || !gptPath || !sovitsPath) {
    showToast('请填写音色名称并选择 GPT / SoVITS 模型文件', 'error');
    return;
  }
  if (!refPath) {
    showToast('必须选择参考音频：没有声音样本，合成音色会严重失真', 'error');
    return;
  }
  if (!promptText) {
    showToast('必须填写参考音频里说的话：SoVITS V3/V4 模型留空会直接导致合成失败（400）', 'error');
    return;
  }
  dom.sCvCreate.disabled = true;
  try {
    const payload = {
      name,
      gpt_weights_path: gptPath,
      sovits_weights_path: sovitsPath,
      ref_audio_path: refPath,
      prompt_text: promptText,
      prompt_lang: dom.sCvLang.value,
      text_lang: dom.sCvLang.value,
    };
    const res = await fetch('/api/voice/profiles', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const profileId = data.id;
    const s = getActive();
    if (s) {
      s.settings.voiceProfileId = profileId;
      saveState();
    }
    scannedModels = null; // 下次打开重新扫描，让新档案出现在列表
    await loadSessionVoiceSelect(s);
    dom.sVoice.value = String(profileId);
    syncCustomVoiceBox();
    showToast(`音色「${data.name}」已创建并绑定到当前会话`, 'success');
    ensureSessionVoice(getActive());
  } catch (e) {
    showToast(`音色创建失败: ${e.message || e}`, 'error');
  } finally {
    dom.sCvCreate.disabled = false;
  }
}

async function deleteVoiceProfile() {
  const id = Number(dom.sVoice.value);
  if (!id) {
    showToast('请先在下拉框中选择要删除的音色', 'error');
    return;
  }
  if (id === activeProfileId) {
    showToast('该音色正在被引擎加载，请先把会话切换到其他音色再删除', 'error');
    return;
  }
  const p = voiceProfiles.find((x) => x.id === id);
  if (!window.confirm(`确定删除音色「${p ? p.name : id}」？此操作不可恢复。`)) return;
  dom.sVoiceDelete.disabled = true;
  try {
    const res = await fetch(`/api/voice/profiles/${id}`, { method: 'DELETE' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    // 清除所有会话对该音色的绑定
    let cleared = 0;
    for (const sess of state.sessions) {
      if (sess.settings?.voiceProfileId === id) {
        sess.settings.voiceProfileId = null;
        cleared += 1;
      }
    }
    if (cleared) saveState();
    await loadSessionVoiceSelect(getActive());
    showToast(`音色「${p ? p.name : id}」已删除${cleared ? `，并解除了 ${cleared} 个会话的绑定` : ''}`, 'success');
  } catch (e) {
    showToast(`删除失败: ${e.message || e}`, 'error');
  } finally {
    dom.sVoiceDelete.disabled = false;
  }
}

/* ---------- 对话模型（LLM 提供商，后端全局生效） ---------- */
async function loadProviders() {
  dom.gProvider.innerHTML = '<option value="">加载中…</option>';
  dom.gProviderActive.textContent = '';
  try {
    const res = await fetch('/api/providers');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const list = data.providers || [];
    const active = list.find((p) => p.is_active);
    // 当前启用的排最前并默认选中
    const sorted = active ? [active, ...list.filter((p) => p !== active)] : list;
    dom.gProviderActive.textContent = active ? `当前：${active.name} · ${active.chat_model || ''}` : '';
    if (!sorted.length) {
      dom.gProvider.innerHTML = '<option value="__custom__">＋ 自定义模型 / 接口…</option>';
    } else {
      dom.gProvider.innerHTML =
        sorted.map((p) => `<option value="${p.id}">${p.name} · ${p.chat_model || '-'}${p.is_active ? '（当前）' : ''}</option>`).join('') +
        '<option value="__custom__">＋ 自定义模型 / 接口…</option>';
    }
    syncCustomBox();
  } catch (e) {
    dom.gProvider.innerHTML = '<option value="__custom__">＋ 自定义模型 / 接口…</option>';
    showToast(`模型列表加载失败: ${e.message || e}`, 'error');
    syncCustomBox();
  }
}

function syncCustomBox() {
  dom.gCustomBox.classList.toggle('hidden', dom.gProvider.value !== '__custom__');
}

async function activateProvider() {
  const selected = dom.gProvider.value;
  dom.gProviderActivate.disabled = true;
  try {
    if (selected === '__custom__') {
      const baseUrl = dom.gCustomBaseUrl.value.trim();
      const model = dom.gCustomModel.value.trim();
      if (!baseUrl || !model) {
        showToast('请填写 Base URL 和模型名', 'error');
        return;
      }
      const payload = {
        id: 'custom',
        name: dom.gCustomName.value.trim() || '自定义模型',
        api_base_url: baseUrl,
        chat_model: model,
        is_active: true,
      };
      const key = dom.gCustomApiKey.value.trim();
      if (key) payload.api_key = key;
      const res = await fetch('/api/providers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      const act = await fetch('/api/providers/custom/activate', { method: 'POST' });
      if (!act.ok) {
        const ad = await act.json().catch(() => ({}));
        throw new Error(ad.detail || `HTTP ${act.status}`);
      }
      showToast(`已启用自定义模型：${payload.name} · ${model}`, 'success');
      dom.gProviderActive.textContent = `当前：${payload.name} · ${model}`;
    } else {
      const res = await fetch(`/api/providers/${encodeURIComponent(selected)}/activate`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      const p = data.active_provider;
      showToast(`已启用：${p ? p.name : selected}${p && p.chat_model ? ' · ' + p.chat_model : ''}`, 'success');
      if (p) dom.gProviderActive.textContent = `当前：${p.name} · ${p.chat_model || ''}`;
    }
    await loadProviders();
  } catch (e) {
    showToast(`模型启用失败: ${e.message || e}`, 'error');
  } finally {
    dom.gProviderActivate.disabled = false;
  }
}

function syncRangeLabels() {
  dom.sTempVal.textContent = Number(dom.sTemp.value).toFixed(2);
  dom.sTopPVal.textContent = Number(dom.sTopP.value).toFixed(2);
  dom.sFreqVal.textContent = Number(dom.sFreq.value).toFixed(1);
  dom.sPresVal.textContent = Number(dom.sPres.value).toFixed(1);
  dom.sCtxVal.textContent = dom.sCtx.value;
  dom.sTtsSpeedVal.textContent = Number(dom.sTtsSpeed.value).toFixed(2);
  dom.sTtsTopKVal.textContent = dom.sTtsTopK.value;
  dom.sTtsTopPVal.textContent = Number(dom.sTtsTopP.value).toFixed(2);
  dom.sTtsTempVal.textContent = Number(dom.sTtsTemp.value).toFixed(2);
  for (const r of [dom.sTemp, dom.sTopP, dom.sFreq, dom.sPres, dom.sCtx, dom.sTtsSpeed, dom.sTtsTopK, dom.sTtsTopP, dom.sTtsTemp]) {
    const pct = ((r.value - r.min) / (r.max - r.min)) * 100;
    r.style.setProperty('--fill', `${pct}%`);
  }
}

/* ---------- 流式回复 ---------- */
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
  // 发送新消息时，平滑打断旧声音 (40ms fade-out)，避免新老语音混杂
  if (streamAudioController.isPlaying || currentVoice) {
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
        if (streamAudioController.paused) {
          streamAudioController.resume();
          streamBarCtl.setPlaying(true);
        } else {
          streamAudioController.pause();
          streamBarCtl.setPlaying(false);
        }
      };
      bar.addEventListener('click', toggle);
      bar.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') toggle(e);
      });
      paint();
    }
    return streamBarCtl;
  };

  const onChunk = (full) => {
    lastFull = full;
    if (!streamEl) {
      typing.remove();
      const built = createMessageEl(
        { role: 'assistant', content: '', ts: Date.now() },
        { streaming: true }
      );
      streamEl = built.el;
      streamContentEl = built.contentEl;
      listInner.appendChild(streamEl);
    }
    streamContentEl.innerHTML = formatContent(full);
    maybeScroll();
  };

  const onEnd = (full, cancelled, error, audioUrls, meta) => {
    cancelStream = null;
    if (typing.isConnected) typing.remove();
    const finalText = full || lastFull;
    let stored = finalText;
    if (error) {
      // 保留已生成的部分内容，错误信息附加在后面
      const errNote = `（流式传输中断：${error}）`;
      stored = finalText ? `${finalText}\n\n${errNote}` : `（请求失败）${error}\n— 请确认 Galgame2Voice 后端已启动，或查看后端日志排查。`;
      showToast(error, 'error');
    }
    const cleanUrls = (audioUrls && audioUrls.length) ? [...audioUrls] : [];
    for (const u of cleanUrls) {
      if (typeof u === 'string' && u.startsWith('/audio/')) {
        fetchAndCacheAudio(u).catch(() => {});
      }
    }
    const finalMsgId = uid('m');
    const msgPayload = {
      id: finalMsgId,
      role: 'assistant',
      content: stored,
      japanese: (meta && meta.japanese) || '',
      ts: Date.now(),
      audioUrls: cleanUrls,
      ttsParams: (meta && meta.ttsParams) || null,
      emotion: (meta && meta.emotion) || null,
    };
    if (streamEl) {
      if (stored) {
        const fresh = createMessageEl(
          msgPayload,
          {
            onPlayVoice: playVoice,
            onResolveJapanese: resolveJapanese,
            autoTranslate: Boolean(state.global.autoTranslate),
          }
        );
        streamEl.replaceWith(fresh.el);
        streamEl = fresh.el;
        streamContentEl = fresh.contentEl;

        // 若流式音频仍在播放中，将 currentVoice 的控制器顺滑接力至新生成的语音条
        if (streamAudioController.isPlaying && currentVoice && currentVoice.msgId === streamMsgId) {
          currentVoice.msgId = finalMsgId;
          const freshBar = fresh.el.querySelector('.voice-bar');
          if (freshBar) {
            freshBar.classList.add('playing');
            const freshWaves = freshBar.querySelector('.vb-waves');
            const waveCount = freshWaves ? freshWaves.children.length : 20;
            const updatedCtl = {
              setPlaying: (p) => {
                freshBar.classList.toggle('playing', Boolean(p));
              },
              setProgress: (pct) => {
                if (freshWaves) {
                  const lit = Math.round(pct * waveCount);
                  [...freshWaves.children].forEach((el, i) => el.classList.toggle('on', i < lit));
                }
              },
            };
            streamAudioController.attachControl(updatedCtl, finalMsgId);
            currentVoice.setPlaying = updatedCtl.setPlaying;
            currentVoice.setProgress = updatedCtl.setProgress;
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
    // 全局朗读开启时：若尚未在流中播放（例如无分块音频或降级重试），才触发 bar.click()
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
      // 全局朗读开启且仍处于当前会话：首句切片到达立即秒级开播，后续切片无缝微渐变追加排队
      if (state.global.voiceMode && originId === state.activeId && url) {
        if (idx === 0 || !hasStartedStreamAudio) {
          hasStartedStreamAudio = true;
          const ctl = ensureStreamVoiceBar();
          streamAudioController.startSession(streamMsgId, ctl);
          currentVoice = {
            msgId: streamMsgId,
            get paused() { return streamAudioController.paused; },
            pause() { streamAudioController.pause(); if (ctl) ctl.setPlaying(false); },
            resume() { streamAudioController.resume(); if (ctl) ctl.setPlaying(true); },
            setPlaying: (p) => { if (ctl) ctl.setPlaying(p); },
            setProgress: (pct) => { if (ctl) ctl.setProgress(pct); },
            stop() { streamAudioController.interrupt(40); },
          };
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
  const s = getActive();
  if (s && (!s.messages || s.messages.length === 0)) {
    await loadSessionHistory(s);
  }
  saveState();
  fullRender(true);
  ensureSessionVoice(getActive());
  dom.input.focus();
}

function handleDelete(id) {
  if (id === state.activeId || busy) {
    interruptAll();
  } else {
    stopCurrentVoice();
  }
  // 释放该会话用户录音的 blob URL，避免内存泄漏
  const sess = getSession(id);
  if (sess) {
    for (const m of sess.messages) {
      if (audioStore.has(m.id)) {
        audioStore.delete(m.id);
      }
    }
  }
  deleteSession(id);
  // 同步清理后端 SQLite 中该会话及其消息
  fetch(`/api/chat/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => {});
  fetch(`/api/chat/history?session_id=${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => {});
  fullRender(true);
  ensureSessionVoice(getActive());
  showToast('对话已删除');
}

function newChat() {
  interruptAll();
  closeDrawer();
  const s = createSession();
  fullRender(true);
  ensureSessionVoice(s);
  dom.input.focus();
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

/* ---------- 事件绑定 ---------- */
dom.newChatBtn.addEventListener('click', newChat);
dom.menuBtn.addEventListener('click', openDrawer);
dom.sidebarClose.addEventListener('click', closeDrawer);
dom.backdrop.addEventListener('click', closeDrawer);

dom.composer.addEventListener('submit', (e) => {
  e.preventDefault();
  sendMessage();
});
dom.input.addEventListener('input', () => {
  autoGrow();
  if (!busy) dom.sendBtn.disabled = !dom.input.value.trim();
});
dom.input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    sendMessage();
  }
});
dom.stopBtn.addEventListener('click', interruptAll);
dom.micBtn.addEventListener('click', toggleMic);
dom.voiceModeBtn.addEventListener('click', () => {
  state.global.voiceMode = !state.global.voiceMode;
  saveState();
  updateVoiceModeUI();
  showToast(state.global.voiceMode ? '已开启朗读回复：AI 回复将自动播放语音' : '已关闭朗读回复');
});

dom.messages.addEventListener('scroll', () => {
  const d = dom.messages;
  nearBottom = d.scrollHeight - d.scrollTop - d.clientHeight < 160;
});

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    newChat();
  }
  if (e.key === 'Escape') {
    closeModal(dom.globalModal);
    closeModal(dom.sessionModal);
    closeDrawer();
  }
});

/* 全局设置（引擎层：状态 / 语音推理 / 大模型 / Telegram / 记忆与人设） */
function openGlobalSettings() {
  openModal(dom.globalModal);
  loadProviders();
  loadGlobalConfig();
  fetchSystemTelemetry();
  if (dom.gPreset) {
    dom.gPreset.value = state.global.ttsPreset || '';
  }
  if (dom.gAutoTranslate) {
    dom.gAutoTranslate.value = state.global.autoTranslate ? 'auto' : 'manual';
  }
}

async function fetchSystemTelemetry() {
  try {
    const [statusRes, cacheRes] = await Promise.all([
      fetch('/api/system/status').catch(() => null),
      fetch('/api/cache/stats').catch(() => null),
    ]);
    if (statusRes && statusRes.ok) {
      const data = await statusRes.json();
      // 1. GPT-SoVITS
      if (data.gpt_sovits && dom.gDashSovitsStatus) {
        const isOnline = data.gpt_sovits.status === 'reachable';
        dom.gDashSovitsBadge.className = `badge-status-pill ${isOnline ? 'badge-pill-green' : 'badge-pill-red'}`;
        dom.gDashSovitsBadge.textContent = isOnline ? '在线' : '离线';
        dom.gDashSovitsStatus.textContent = isOnline ? `运行正常 (${data.gpt_sovits.latency_ms || 0}ms)` : '服务未响应';
        dom.gDashSovitsUrl.textContent = data.gpt_sovits.base_url || 'http://127.0.0.1:9880';
      }
      // 2. Precision & GPU
      if (data.hardware) {
        const prec = (data.hardware.inference_precision || '').toUpperCase();
        const isCpu = prec === 'CPU';
        const isFp16 = prec === 'FP16';
        if (dom.gDashPrecisionBadge) {
          if (isCpu) {
            dom.gDashPrecisionBadge.className = 'badge-status-pill badge-pill-yellow';
            dom.gDashPrecisionBadge.textContent = 'CPU';
          } else if (isFp16) {
            dom.gDashPrecisionBadge.className = 'badge-status-pill badge-pill-indigo';
            dom.gDashPrecisionBadge.textContent = 'FP16';
          } else {
            dom.gDashPrecisionBadge.className = 'badge-status-pill badge-pill-green';
            dom.gDashPrecisionBadge.textContent = 'FP32';
          }
        }
        if (dom.gDashPrecisionVal) {
          if (isCpu) {
            dom.gDashPrecisionVal.textContent = '🛡️ CPU 稳定模式';
          } else if (isFp16) {
            dom.gDashPrecisionVal.textContent = '⚡ FP16 半精度';
          } else {
            dom.gDashPrecisionVal.textContent = '🛡️ FP32 单精度';
          }
        }
        if (dom.gBtnTogglePrecisionText) {
          if (isCpu) {
            dom.gBtnTogglePrecisionText.textContent = '切为 GPU(FP16)';
          } else if (isFp16) {
            dom.gBtnTogglePrecisionText.textContent = '切为 FP32 重启';
          } else {
            dom.gBtnTogglePrecisionText.textContent = '切为 CPU 模式';
          }
        }
        if (dom.gDashDeviceVal) {
          if (isCpu) {
            dom.gDashDeviceVal.textContent = '免显存占用 · 依托物理大内存';
          } else {
            dom.gDashDeviceVal.textContent = data.hardware.gpu_name ? data.hardware.gpu_name : '硬件加速中';
          }
        }
        if (dom.gDashHardwareVal) {
          const procMem = (data.app && data.app.memory_usage_mb !== undefined && data.app.memory_usage_mb !== null)
            ? `${Math.round(data.app.memory_usage_mb)} MB`
            : '正常';
          dom.gDashHardwareVal.textContent = `服务内存: ${procMem}`;
        }
        const hostRamEl = $('gDashHostRam');
        if (hostRamEl) {
          if (data.hardware.system_memory_gb && data.hardware.system_memory_avail_gb) {
            hostRamEl.textContent = `系统可用: ${data.hardware.system_memory_avail_gb.toFixed(1)}G / ${data.hardware.system_memory_gb.toFixed(1)}G`;
          } else {
            hostRamEl.textContent = '系统内存: 良好';
          }
        }
      }
      // 3. Uptime & PID
      if (data.app && dom.gDashUptimeVal) {
        const sec = Math.round(data.app.uptime_seconds || 0);
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        const s = sec % 60;
        dom.gDashUptimeVal.textContent = `PID: ${data.app.pid || '-'} · 运行: ${h}h ${m}m ${s}s`;
      }
    }
    if (cacheRes && cacheRes.ok && dom.gDashCacheVal) {
      const cData = await cacheRes.json();
      dom.gDashCacheVal.textContent = `${cData.total_entries || 0} 个文件 · ${(cData.total_size_mb || 0).toFixed(1)} MB`;
    }
  } catch (e) {
    console.warn('Failed to fetch telemetry:', e);
  }
}

function updateTelegramFieldsVisibility() {
  const enabled = dom.gTgEnabled ? dom.gTgEnabled.checked : false;
  if (dom.gTgFieldsGroup) {
    dom.gTgFieldsGroup.style.opacity = enabled ? '1' : '0.45';
    dom.gTgFieldsGroup.style.pointerEvents = enabled ? 'auto' : 'none';
  }
}

async function loadGlobalConfig() {
  try {
    const [cfgRes, voiceErr] = await Promise.all([
      fetch('/api/config'),
      fetchVoiceProfiles().catch(() => null),
    ]);
    const cfg = await cfgRes.json();
    const s = cfg.settings || {};
    const provider = cfg.active_provider;
    const activeVoice = voiceProfiles.find((p) => p.id === activeProfileId);

    const isOk = cfg.status === 'ok';
    const providerStr = provider ? `${provider.name} · ${provider.chat_model || '-'}` : '未配置';
    const voiceStr = activeVoice ? activeVoice.name : '未加载';

    const diagBackend = $('gDiagBackend');
    const diagBackendDot = $('gDiagBackendDot');
    const diagModel = $('gDiagModel');
    const diagModelDot = $('gDiagModelDot');
    const diagVoice = $('gDiagVoice');
    const diagVoiceDot = $('gDiagVoiceDot');

    if (diagBackend) diagBackend.textContent = isOk ? '运行正常' : String(cfg.status);
    if (diagBackendDot) diagBackendDot.className = `diag-dot ${isOk ? 'ok' : ''}`;
    if (diagModel) {
      diagModel.textContent = providerStr;
      diagModel.title = providerStr;
    }
    if (diagModelDot) diagModelDot.className = `diag-dot ${provider ? 'ok' : ''}`;
    if (diagVoice) {
      diagVoice.textContent = voiceStr;
      diagVoice.title = voiceStr;
    }
    if (diagVoiceDot) diagVoiceDot.className = `diag-dot ${activeVoice ? 'ok' : ''}`;

    // 语音与推理参数
    if (dom.gParamSovitsUrl) dom.gParamSovitsUrl.value = s.gpt_sovits_url || 'http://127.0.0.1:9880';
    if (dom.gPrecision) dom.gPrecision.value = s.inference_precision || 'auto';
    if (dom.gParamSliceMethod) dom.gParamSliceMethod.value = s.text_split_method || 'cut5';
    if (dom.gParamSpeed) {
      dom.gParamSpeed.value = s.speed_factor !== undefined ? s.speed_factor : 1.0;
      if (dom.gParamSpeedVal) dom.gParamSpeedVal.textContent = dom.gParamSpeed.value;
    }
    if (dom.gParamTopK) {
      dom.gParamTopK.value = s.top_k || 15;
      if (dom.gParamTopKVal) dom.gParamTopKVal.textContent = dom.gParamTopK.value;
    }
    if (dom.gParamTopP) {
      dom.gParamTopP.value = s.top_p !== undefined ? s.top_p : 1.0;
      if (dom.gParamTopPVal) dom.gParamTopPVal.textContent = dom.gParamTopP.value;
    }
    if (dom.gParamTemp) {
      dom.gParamTemp.value = s.temperature !== undefined ? s.temperature : 1.0;
      if (dom.gParamTempVal) dom.gParamTempVal.textContent = dom.gParamTemp.value;
    }
    if (dom.gParamFragmentInterval) dom.gParamFragmentInterval.value = s.fragment_interval !== undefined ? s.fragment_interval : 0.3;
    if (dom.gAudioRetention) dom.gAudioRetention.value = s.audio_retention_minutes || 30;
    if (dom.gDashRetentionVal) dom.gDashRetentionVal.textContent = `保留时长: ${s.audio_retention_minutes || 30} 分钟`;

    // STT 与 Telegram
    if (dom.gSttEngine) dom.gSttEngine.value = s.stt_engine || 'browser';
    if (dom.gTgEnabled) {
      dom.gTgEnabled.checked = Boolean(s.telegram_enabled);
      updateTelegramFieldsVisibility();
    }
    const token = s.telegram_bot_token || '';
    const masked = token.includes('****');
    dom.gTgToken.value = '';
    dom.gTgToken.placeholder = masked
      ? '已保存（输入新 Token 可覆盖）'
      : '未配置，如 123456:ABC-DEF…';
    if (dom.gTgChatId) dom.gTgChatId.value = s.telegram_chat_id || '';
    dom.gTgProxyHost.value = s.telegram_proxy_host || '';
    dom.gTgProxyPort.value = s.telegram_proxy_port || '';
    dom.gTgProxyEnabled.checked = Boolean(s.telegram_proxy_enabled);

    // 记忆与人设
    if (dom.gMemoryEnabled) dom.gMemoryEnabled.checked = s.memory_enabled !== false;
    if (dom.gUserNickname) dom.gUserNickname.value = s.user_nickname || '';
    if (dom.gDefaultSystemPrompt) dom.gDefaultSystemPrompt.value = s.system_prompt || '';
  } catch (e) {
    dom.gStatus.textContent = '状态加载失败（后端未启动？）';
  }
}

async function saveGlobalConfig() {
  dom.gTgSave.disabled = true;
  try {
    const payload = {
      gpt_sovits_url: dom.gParamSovitsUrl ? dom.gParamSovitsUrl.value.trim() || 'http://127.0.0.1:9880' : undefined,
      inference_precision: dom.gPrecision ? dom.gPrecision.value : undefined,
      text_split_method: dom.gParamSliceMethod ? dom.gParamSliceMethod.value : undefined,
      speed_factor: dom.gParamSpeed ? Number(dom.gParamSpeed.value) : undefined,
      top_k: dom.gParamTopK ? Number(dom.gParamTopK.value) : undefined,
      top_p: dom.gParamTopP ? Number(dom.gParamTopP.value) : undefined,
      temperature: dom.gParamTemp ? Number(dom.gParamTemp.value) : undefined,
      fragment_interval: dom.gParamFragmentInterval ? Number(dom.gParamFragmentInterval.value) || 0.3 : undefined,
      audio_retention_minutes: dom.gAudioRetention ? Math.max(1, Number(dom.gAudioRetention.value) || 30) : 30,
      stt_engine: dom.gSttEngine ? dom.gSttEngine.value : undefined,
      telegram_enabled: dom.gTgEnabled ? dom.gTgEnabled.checked : false,
      telegram_chat_id: dom.gTgChatId ? dom.gTgChatId.value.trim() || undefined : undefined,
      telegram_proxy_enabled: dom.gTgProxyEnabled.checked,
      telegram_proxy_host: dom.gTgProxyHost.value.trim() || '127.0.0.1',
      telegram_proxy_port: Number(dom.gTgProxyPort.value) || 10809,
      memory_enabled: dom.gMemoryEnabled ? dom.gMemoryEnabled.checked : true,
      user_nickname: dom.gUserNickname ? dom.gUserNickname.value.trim() : undefined,
      system_prompt: dom.gDefaultSystemPrompt ? dom.gDefaultSystemPrompt.value.trim() || undefined : undefined,
    };
    const token = dom.gTgToken.value.trim();
    if (token) payload.telegram_bot_token = token;

    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);

    if (dom.gPreset) {
      saveGlobal({ ttsPreset: dom.gPreset.value });
    }
    if (dom.gAutoTranslate) {
      saveGlobal({ autoTranslate: dom.gAutoTranslate.value === 'auto' });
    }

    showToast('全局配置已成功保存并实时生效', 'success');
    loadGlobalConfig();
    fetchSystemTelemetry();
  } catch (e) {
    showToast(`保存失败: ${e.message || e}`, 'error');
  } finally {
    dom.gTgSave.disabled = false;
  }
}

async function testTelegram() {
  dom.gTgTest.disabled = true;
  try {
    const payload = {
      proxy_enabled: dom.gTgProxyEnabled.checked,
      proxy_host: dom.gTgProxyHost.value.trim() || '127.0.0.1',
      proxy_port: Number(dom.gTgProxyPort.value) || 10809,
    };
    const token = dom.gTgToken.value.trim();
    if (token) payload.token = token;
    const res = await fetch('/api/telegram/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    showToast(data.message || (data.success ? '连接成功' : '连接失败'), data.success ? 'success' : 'error');
  } catch (e) {
    showToast(`测试失败: ${e.message || e}`, 'error');
  } finally {
    dom.gTgTest.disabled = false;
  }
}

// 选项卡切换
if (dom.gTabs) {
  dom.gTabs.querySelectorAll('.modal-tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      dom.gTabs.querySelectorAll('.modal-tab-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      const targetTab = btn.getAttribute('data-gtab');
      dom.globalModal.querySelectorAll('.gtab-panel').forEach((p) => p.classList.add('hidden'));
      const targetPanel = $(`gp-${targetTab}`);
      if (targetPanel) targetPanel.classList.remove('hidden');
      const modalBody = dom.globalModal ? dom.globalModal.querySelector('.modal-body') : null;
      if (modalBody) modalBody.scrollTop = 0;
    });
  });
}

// 推理参数滑块数值联动
if (dom.gParamSpeed) {
  dom.gParamSpeed.addEventListener('input', (e) => { if (dom.gParamSpeedVal) dom.gParamSpeedVal.textContent = e.target.value; });
}
if (dom.gParamTopK) {
  dom.gParamTopK.addEventListener('input', (e) => { if (dom.gParamTopKVal) dom.gParamTopKVal.textContent = e.target.value; });
}
if (dom.gParamTopP) {
  dom.gParamTopP.addEventListener('input', (e) => { if (dom.gParamTopPVal) dom.gParamTopPVal.textContent = e.target.value; });
}
if (dom.gParamTemp) {
  dom.gParamTemp.addEventListener('input', (e) => { if (dom.gParamTempVal) dom.gParamTempVal.textContent = e.target.value; });
}

// 刷新状态诊断
if (dom.gBtnRefreshStatus) {
  dom.gBtnRefreshStatus.addEventListener('click', () => {
    fetchSystemTelemetry();
    loadGlobalConfig();
    showToast('诊断数据已刷新', 'info');
  });
}

// 一键热重启 GPT-SoVITS 引擎
if (dom.gBtnRestartSovits) {
  dom.gBtnRestartSovits.addEventListener('click', async () => {
    dom.gBtnRestartSovits.disabled = true;
    dom.gBtnRestartSovits.innerHTML = '<svg class="icon"><use href="#i-play"></use></svg><span>正在热重启...</span>';
    try {
      const payload = {};
      if (dom.gPrecision && dom.gPrecision.value) {
        payload.precision = dom.gPrecision.value;
      }
      const res = await fetch('/api/system/restart_sovits', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      showToast(data.message || 'GPT-SoVITS 重启指令已发送', 'success');
      setTimeout(() => {
        fetchSystemTelemetry();
        dom.gBtnRestartSovits.disabled = false;
        dom.gBtnRestartSovits.innerHTML = '<svg class="icon"><use href="#i-play"></use></svg><span>重启 SoVITS 引擎</span>';
      }, 2500);
    } catch (err) {
      showToast(`重启失败: ${err.message}`, 'error');
      dom.gBtnRestartSovits.disabled = false;
      dom.gBtnRestartSovits.innerHTML = '<svg class="icon"><use href="#i-play"></use></svg><span>重启 SoVITS 引擎</span>';
    }
  });
}

// 一键切换精度/设备并重启 (FP16 -> FP32 -> CPU 循环切换)
if (dom.gBtnTogglePrecision) {
  dom.gBtnTogglePrecision.addEventListener('click', async () => {
    const badgeText = dom.gDashPrecisionBadge ? dom.gDashPrecisionBadge.textContent.trim().toUpperCase() : 'FP16';
    let targetPrec = 'fp16';
    let targetLabel = 'FP16 半精度';
    if (badgeText === 'FP16') {
      targetPrec = 'fp32';
      targetLabel = 'FP32 单精度';
    } else if (badgeText === 'FP32') {
      targetPrec = 'cpu';
      targetLabel = 'CPU 稳定模式 (免显存)';
    } else {
      targetPrec = 'fp16';
      targetLabel = 'FP16 半精度';
    }

    dom.gBtnTogglePrecision.disabled = true;
    if (dom.gBtnRestartSovits) dom.gBtnRestartSovits.disabled = true;
    if (dom.gBtnTogglePrecisionText) dom.gBtnTogglePrecisionText.textContent = `正在切换为 ${targetPrec.toUpperCase()}...`;
    try {
      if (dom.gPrecision) dom.gPrecision.value = targetPrec;
      const res = await fetch('/api/system/restart_sovits', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ precision: targetPrec }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      showToast(data.message || `已按 ${targetLabel} 重启语音引擎`, 'success');
      setTimeout(() => {
        fetchSystemTelemetry();
        dom.gBtnTogglePrecision.disabled = false;
        if (dom.gBtnRestartSovits) dom.gBtnRestartSovits.disabled = false;
      }, 2500);
    } catch (err) {
      showToast(`切换精度重启失败: ${err.message}`, 'error');
      dom.gBtnTogglePrecision.disabled = false;
      if (dom.gBtnRestartSovits) dom.gBtnRestartSovits.disabled = false;
    }
  });
}

// 精度下拉菜单自动保存
if (dom.gPrecision) {
  dom.gPrecision.addEventListener('change', async () => {
    const val = dom.gPrecision.value;
    try {
      await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ inference_precision: val }),
      });
      const labelMap = {
        auto: '自动判定 (Auto)',
        fp16: 'FP16 半精度',
        fp32: 'FP32 单精度',
        cpu: 'CPU 稳定模式 (免显存)',
      };
      showToast(`已保存推理设置: ${labelMap[val] || val}。请点击【重启 SoVITS 引擎】应用。`, 'info');
      fetchSystemTelemetry();
    } catch (err) {
      console.warn('Auto-save precision failed:', err);
    }
  });
}

// 跳转至语音与推理设置
const btnGoPrecision = $('gBtnGoPrecision');
if (btnGoPrecision) {
  btnGoPrecision.addEventListener('click', () => {
    const tabBtn = dom.gTabs ? dom.gTabs.querySelector('[data-gtab="inference"]') : null;
    if (tabBtn) tabBtn.click();
  });
}

// 一键清空音频缓存
if (dom.gBtnClearCache) {
  dom.gBtnClearCache.addEventListener('click', async () => {
    if (!confirm('确定要清空全部 TTS 离线音频缓存吗？清空后新请求将重新合成。')) return;
    dom.gBtnClearCache.disabled = true;
    try {
      clearMemAudioCache();
      if ('caches' in window) {
        await caches.delete('gal2voice-audio-v1').catch(() => {});
      }
      const res = await fetch('/api/cache/clear', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      showToast(`缓存已清空：删除 ${data.deleted_files || 0} 个文件，释放 ${data.freed_mb || 0} MB`, 'success');
      fetchSystemTelemetry();
    } catch (err) {
      showToast(`清空缓存失败: ${err.message}`, 'error');
    } finally {
      dom.gBtnClearCache.disabled = false;
    }
  });
}

// 测试模型连通性
if (dom.gProviderTest) {
  dom.gProviderTest.addEventListener('click', async () => {
    dom.gProviderTest.disabled = true;
    dom.gProviderTest.textContent = '测试中…';
    try {
      const isCustom = dom.gProvider.value === '__custom__';
      const payload = {};
      if (isCustom) {
        payload.api_base_url = dom.gCustomBaseUrl.value.trim();
        payload.api_key = dom.gCustomApiKey.value.trim();
        payload.chat_model = dom.gCustomModel.value.trim();
      } else {
        payload.id = dom.gProvider.value;
      }
      const res = await fetch('/api/providers/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (data.success) {
        showToast(`连接成功: 延迟 ${data.latency_ms || 0}ms`, 'success');
      } else {
        showToast(`连接测试失败: ${data.message || '未知错误'}`, 'error');
      }
    } catch (e) {
      showToast(`测试请求异常: ${e.message}`, 'error');
    } finally {
      dom.gProviderTest.disabled = false;
      dom.gProviderTest.textContent = '测试连通性';
    }
  });
}

// 恢复默认参数
if (dom.gResetBtn) {
  dom.gResetBtn.addEventListener('click', () => {
    if (!confirm('确定要将全局参数恢复为推荐默认值吗？')) return;
    if (dom.gParamSpeed) { dom.gParamSpeed.value = '1.0'; dom.gParamSpeedVal.textContent = '1.0'; }
    if (dom.gParamTopK) { dom.gParamTopK.value = '15'; dom.gParamTopKVal.textContent = '15'; }
    if (dom.gParamTopP) { dom.gParamTopP.value = '1.0'; dom.gParamTopPVal.textContent = '1.0'; }
    if (dom.gParamTemp) { dom.gParamTemp.value = '1.0'; dom.gParamTempVal.textContent = '1.0'; }
    if (dom.gParamSliceMethod) dom.gParamSliceMethod.value = 'cut5';
    if (dom.gPrecision) dom.gPrecision.value = 'auto';
    if (dom.gAudioRetention) dom.gAudioRetention.value = '30';
    showToast('已恢复推荐数值，请点击【保存全局配置】生效', 'info');
  });
}

dom.globalSettingsBtn.addEventListener('click', openGlobalSettings);
dom.globalModal.querySelectorAll('[data-close]').forEach((btn) =>
  btn.addEventListener('click', () => closeModal(dom.globalModal))
);
dom.gProvider.addEventListener('change', syncCustomBox);
dom.gProviderActivate.addEventListener('click', activateProvider);
if (dom.gTgEnabled) dom.gTgEnabled.addEventListener('change', updateTelegramFieldsVisibility);
dom.gTgTest.addEventListener('click', testTelegram);
dom.gTgSave.addEventListener('click', saveGlobalConfig);
if (dom.gPreset) {
  dom.gPreset.addEventListener('change', () => {
    saveGlobal({ ttsPreset: dom.gPreset.value });
    showToast(dom.gPreset.value ? '语音质量预设已保存，对新消息生效' : '语音质量已恢复为后端默认', 'success');
  });
}
if (dom.gAutoTranslate) {
  dom.gAutoTranslate.addEventListener('change', () => {
    const isAuto = dom.gAutoTranslate.value === 'auto';
    saveGlobal({ autoTranslate: isAuto });
    fullRender(false);
    showToast(isAuto ? '已开启翻译自动展开' : '已切换为手动点击翻译', 'success');
  });
}

/* 会话设置 */
dom.sessionSettingsBtn.addEventListener('click', openSessionSettings);
dom.sessionModal.querySelectorAll('[data-close]').forEach((btn) =>
  btn.addEventListener('click', () => closeModal(dom.sessionModal))
);
dom.sTemp.addEventListener('input', syncRangeLabels);
dom.sVoice.addEventListener('change', syncCustomVoiceBox);
dom.sVoiceDelete.addEventListener('click', deleteVoiceProfile);
dom.sCvCreate.addEventListener('click', createCustomVoice);
dom.sTopP.addEventListener('input', syncRangeLabels);
dom.sFreq.addEventListener('input', syncRangeLabels);
dom.sPres.addEventListener('input', syncRangeLabels);
dom.sCtx.addEventListener('input', syncRangeLabels);
dom.sTtsSpeed.addEventListener('input', syncRangeLabels);
dom.sTtsTopK.addEventListener('input', syncRangeLabels);
dom.sTtsTopP.addEventListener('input', syncRangeLabels);
dom.sTtsTemp.addEventListener('input', syncRangeLabels);
dom.sReset.addEventListener('click', () => {
  dom.sSystem.value = DEFAULT_SESSION_SETTINGS.systemPrompt;
  dom.sVoice.value = '';
  dom.sTemp.value = DEFAULT_SESSION_SETTINGS.temperature;
  dom.sTopP.value = DEFAULT_SESSION_SETTINGS.topP;
  dom.sFreq.value = DEFAULT_SESSION_SETTINGS.freqPenalty;
  dom.sPres.value = DEFAULT_SESSION_SETTINGS.presPenalty;
  dom.sMaxTokens.value = DEFAULT_SESSION_SETTINGS.maxTokens;
  dom.sCtx.value = DEFAULT_SESSION_SETTINGS.maxContext;
  dom.sAiAdaptiveVoice.value = String(DEFAULT_SESSION_SETTINGS.aiAdaptiveVoice !== false);
  dom.sTtsSpeed.value = DEFAULT_SESSION_SETTINGS.ttsSpeed;
  dom.sTtsTopK.value = DEFAULT_SESSION_SETTINGS.ttsTopK;
  dom.sTtsTopP.value = DEFAULT_SESSION_SETTINGS.ttsTopP;
  dom.sTtsTemp.value = DEFAULT_SESSION_SETTINGS.ttsTemperature;
  syncRangeLabels();
});
dom.sSave.addEventListener('click', () => {
  const s = getActive();
  if (!s) return;
  const maxTokens = Math.round(Number(dom.sMaxTokens.value)) || DEFAULT_SESSION_SETTINGS.maxTokens;
  s.settings = {
    systemPrompt: dom.sSystem.value,
    voiceProfileId: dom.sVoice.value ? Number(dom.sVoice.value) : null,
    temperature: Number(dom.sTemp.value),
    topP: Number(dom.sTopP.value),
    maxTokens: Math.min(32768, Math.max(16, maxTokens)),
    freqPenalty: Number(dom.sFreq.value),
    presPenalty: Number(dom.sPres.value),
    maxContext: Number(dom.sCtx.value),
    aiAdaptiveVoice: dom.sAiAdaptiveVoice.value === 'true',
    ttsSpeed: Number(dom.sTtsSpeed.value),
    ttsTopK: Math.round(Number(dom.sTtsTopK.value)),
    ttsTopP: Number(dom.sTtsTopP.value),
    ttsTemperature: Number(dom.sTtsTemp.value),
  };
  s.updatedAt = Date.now();
  saveState();
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
  closeModal(dom.sessionModal);
  renderSidebar();
  renderHeader();
  ensureSessionVoice(s);
  showToast('会话参数已保存', 'success');
});

/* ---------- 启动 ---------- */
loadState();
async function restoreCachedAudios() {
  // 惰性加载：启动时不为全量历史消息预建 Blob URL，避免浏览器内存暴涨与句柄泄露。
  // 用户在会话中点击录音时，由 playUserVoice 按需从 CacheStorage 解析，并受控于 BoundedAudioStore (LRU 30 项上限)。
}
restoreCachedAudios().catch(() => {});
fullRender(true);
autoGrow();
updateComposer();
updateVoiceModeUI();
// 启动时同步当前会话绑定的音色（引擎可能还加载着别的权重）
ensureSessionVoice(getActive());
if (!voiceSupported() && !recorderSupported()) dom.micBtn.classList.add('hidden');
if (window.innerWidth > 820) dom.input.focus();

// 同步后端 SQLite 会话列表及历史消息（跨端口、刷新或重启后无缝恢复）
async function syncSessionsFromBackend() {
  try {
    const res = await fetch('/api/chat/sessions?limit=50');
    if (!res.ok) return;
    const data = await res.json();
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
      // 保留本地有用户发言但后端尚未同步的会话
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
    saveState();
    fullRender(false);
    ensureSessionVoice(getActive(), { silent: true });
    if (window.innerWidth > 820) dom.input.focus();
  } catch (err) {
    console.debug('Failed to sync sessions from backend:', err);
  }
}
syncSessionsFromBackend().catch(() => {});

// 启动时检查 URL 是否携带设置参数（例如 /console 或 /settings 重定向过来的请求）
const urlParams = new URLSearchParams(window.location.search);
if (urlParams.get('settings') === '1' || window.location.hash === '#settings') {
  openGlobalSettings();
}

