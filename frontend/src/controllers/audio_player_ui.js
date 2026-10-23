// Audio Player & STT UI Controller
import {
  state,
  saveState,
  getSession,
  getActive,
  DEFAULT_SESSION_SETTINGS,
} from '../store.js';
import {
  startListening,
  stopListening,
  isListening,
  voiceSupported,
  recorderSupported,
  plainSpeechText,
  audioStore,
} from '../voice.js';
import {
  getCachedAudioBlob,
  putCachedAudioBlob,
  loadAudioBlob,
} from '../cache.js';
import { withAudioDeadline } from '../audio_request.js';
import { streamAudioController } from '../audio_player.js';
import { showToast } from '../ui.js';

let _dom = {};
let _callbacks = {};
const inputDom = () => _callbacks.getInput?.() || _dom.input;
const micDom = () => _callbacks.getMicButton?.() || _dom.micBtn;

let currentVoice = null; // { msgId, paused, pause, resume, setPlaying, setProgress, stop }
let voiceRevision = 0;
let pendingPreparation = null;
let pendingSynthesis = null;
const loadingOwners = new WeakMap();
let micTimer = null;
let micSecs = 0;
let micBase = '';
let pendingVoice = { text: null, audio: null, dur: 0, blob: null };
let micFinalizeTimers = [];
let micRevision = 0;

export function cancelMic() {
  ++micRevision;
  clearInterval(micTimer); micTimer = null;
  clearMicTimers();
  if (pendingVoice.audio) { try { URL.revokeObjectURL(pendingVoice.audio); } catch {} }
  pendingVoice = { text: null, audio: null, dur: 0, blob: null };
  stopListening(); updateMicUI(false);
}

export function getCurrentVoice() {
  return currentVoice;
}

export function setCurrentVoice(v) {
  ++voiceRevision;
  pendingPreparation?.abort();
  pendingPreparation = null;
  pendingSynthesis?.abort();
  pendingSynthesis = null;
  currentVoice = v;
}

export function initAudioPlayerUi(dom, callbacks = {}) {
  _dom = dom;
  _callbacks = callbacks;

  streamAudioController.onQueueEmpty = () => {
    if (currentVoice?.msgId === streamAudioController.currentMsgId) {
      try {
        currentVoice.setPlaying(false);
      } catch (_) {}
      currentVoice = null;
    }
  };

  streamAudioController.onError = (error) => {
    const isBrowserBlock = Boolean(error.message?.includes('浏览器'));
    showToast(isBrowserBlock ? error.message
      : '部分语音暂时无法播放，已跳过并保留文字。可稍后在 LOG 里再点「语音」重试；若反复失败，请打开「运行状态」检查服务。', 'info', (!isBrowserBlock && _callbacks.onOpenEngineStatus) ? {
        actionLabel: '检查运行状态',
        onAction: () => _callbacks.onOpenEngineStatus(),
      } : null);
  };

}

export function clearMicTimers() {
  micFinalizeTimers.forEach((t) => clearTimeout(t));
  micFinalizeTimers = [];
}

export function stopCurrentVoice() {
  ++voiceRevision;
  pendingPreparation?.abort();
  pendingPreparation = null;
  pendingSynthesis?.abort();
  pendingSynthesis = null;
  try {
    streamAudioController.interrupt(40);
  } catch (_) {}
  if (currentVoice) {
    const v = currentVoice;
    currentVoice = null;
    try {
      v.stop();
    } catch (_) {}
    try {
      v.setPlaying(false);
    } catch (_) {}
  }
}

export function playSingleAudio(getAudio, msgId, ctl, { objectUrl = null } = {}) {
  let audio = getAudio();
  let cancelled = false;
  let paused = false;

  const attach = () => {
    audio.ontimeupdate = () => {
      if (audio.duration && ctl && ctl.setProgress) ctl.setProgress(audio.currentTime / audio.duration);
    };
    audio.onended = () => {
      audio.ontimeupdate = null;
      audio.onended = null;
      audio.onerror = null;
      if (objectUrl) {
        try {
          URL.revokeObjectURL(objectUrl);
        } catch (_) {}
        objectUrl = null;
      }
      if (!cancelled) {
        if (ctl && ctl.setProgress) ctl.setProgress(1);
        if (ctl && ctl.setPlaying) ctl.setPlaying(false);
        if (currentVoice && currentVoice.msgId === msgId) currentVoice = null;
      }
    };
    audio.onerror = () => {
      if (cancelled) return;
      audio.ontimeupdate = null;
      audio.onended = null;
      audio.onerror = null;
      cancelled = true;
      if (ctl && ctl.setPlaying) ctl.setPlaying(false);
      if (currentVoice?.msgId === msgId) currentVoice = null;
      if (objectUrl) {
        try {
          URL.revokeObjectURL(objectUrl);
        } catch (_) {}
        objectUrl = null;
      }
      showToast('音频暂时无法播放，请在 LOG 里再点一次「语音」或「播放录音」重试；若仍失败，请检查浏览器声音权限。', 'info');
    };
  };
  attach();

  currentVoice = {
    msgId,
    get paused() {
      return paused;
    },
    pause() {
      paused = true;
      audio.pause();
      if (ctl && ctl.setPlaying) ctl.setPlaying(false);
    },
    resume() {
      paused = false;
      const p = audio.play();
      if (p && typeof p.then === 'function') {
        p.then(() => {
          if (paused || cancelled) {
            audio.pause();
            if (ctl && ctl.setPlaying) ctl.setPlaying(false);
          } else {
            if (ctl && ctl.setPlaying) ctl.setPlaying(true);
          }
        }).catch((err) => {
          if (cancelled || paused || err?.name === 'AbortError') return;
          audio.onerror?.();
        });
      }
    },
    setPlaying: ctl?.setPlaying,
    setProgress: ctl?.setProgress,
    stop() {
      cancelled = true;
      paused = true;
      audio.pause();
      audio.ontimeupdate = null;
      audio.onended = null;
      audio.onerror = null;
      if (objectUrl) {
        try {
          URL.revokeObjectURL(objectUrl);
        } catch (_) {}
        objectUrl = null;
      }
    },
  };

  const p = audio.play();
  if (p && typeof p.then === 'function') {
    p.then(() => {
      if (paused || cancelled) {
        audio.pause();
        if (ctl && ctl.setPlaying) ctl.setPlaying(false);
      } else {
        if (ctl && ctl.setPlaying) ctl.setPlaying(true);
      }
    }).catch((err) => {
      if (cancelled || paused || err?.name === 'AbortError') return;
      audio.onerror?.();
    });
  }
}

export async function synthesizeAiVoice(msg, ctl) {
  stopCurrentVoice();
  const revision = voiceRevision;
  const session = getActive() || getSession(state.activeId) || {};
  const settings = { ...(session.settings || DEFAULT_SESSION_SETTINGS) };
  const abortCtrl = new AbortController();
  pendingSynthesis = abortCtrl;
  abortCtrl.signal.addEventListener('abort', () => ctl?.setLoading?.(false), { once: true });
  let cancelled = false;
  let paused = false;
  let isTimeout = false;
  const timeoutTimer = setTimeout(() => {
    isTimeout = true;
    abortCtrl.abort();
  }, 16000);

  currentVoice = {
    msgId: msg.id,
    get paused() {
      return paused;
    },
    pause() {
      paused = true;
      clearTimeout(timeoutTimer);
      if (ctl) {
        ctl.setLoading(false);
        ctl.setPlaying(false);
      }
      abortCtrl.abort();
    },
    resume() {
      if (currentVoice && currentVoice.msgId === msg.id) {
        currentVoice = null;
        synthesizeAiVoice(msg, ctl);
      }
    },
    setPlaying: ctl?.setPlaying,
    setProgress: ctl?.setProgress,
    stop() {
      cancelled = true;
      paused = true;
      clearTimeout(timeoutTimer);
      if (ctl) {
        ctl.setLoading(false);
        ctl.setPlaying(false);
      }
      abortCtrl.abort();
    },
  };

  if (ctl) ctl.setLoading(true);
  try {
    // 配音只能用日文台词：中文气泡文本喂给日文音色模型会"音色对、说的却是中文"，
    // 所以缺日文时先补取，补不到就明确报错，绝不退回中文。
    let text = plainSpeechText(msg.japanese || '').slice(0, 2000);
    if (!text && _callbacks.resolveJapanese) {
      text = plainSpeechText(await withAudioDeadline(() => _callbacks.resolveJapanese(msg), {
        signal: abortCtrl.signal, timeoutMs: 16000,
      }) || '').slice(0, 2000);
    }
    if (cancelled || paused || abortCtrl.signal.aborted || revision !== voiceRevision) return;
    if (!text) {
      if (ctl) {
        ctl.setLoading(false);
        ctl.setPlaying(false);
      }
      currentVoice = null;
      showToast('该消息缺少日文配音原文，点消息下的「翻译」取到日文后再播', 'info');
      return;
    }

    const hasJa = /[\u3040-\u30ff\u31f0-\u31ff]/.test(text);
    const textLang = hasJa ? 'ja' : 'zh';

    const isAdaptive = settings.aiAdaptiveVoice !== false;
    const dynSpeed =
      isAdaptive && msg.ttsParams && typeof msg.ttsParams.speed === 'number'
        ? msg.ttsParams.speed
        : settings.ttsSpeed || 1.0;
    const dynTemp =
      isAdaptive && msg.ttsParams && typeof msg.ttsParams.temperature === 'number'
        ? msg.ttsParams.temperature
        : settings.ttsTemperature || 1.0;

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

    const blob = await withAudioDeadline(async signal => {
      const res = await fetch('/api/voice/synthesize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(reqBody),
        signal,
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        const detail = data.detail || `HTTP ${res.status}`;
        throw new Error(detail);
      }
      return res.blob();
    }, { signal: abortCtrl.signal, timeoutMs: 16000 });
    if (cancelled || paused || abortCtrl.signal.aborted || revision !== voiceRevision) {
      return;
    }
    if (!blob?.size) throw new Error('生成的音频为空，请重新生成');
    if (ctl) ctl.setLoading(false);

    const cacheKey = `msg_${msg.id}`;
    void putCachedAudioBlob(cacheKey, blob);
    msg.audioUrls = [cacheKey];
    saveState();

    const url = URL.createObjectURL(blob);
    playSingleAudio(() => new Audio(url), msg.id, ctl, { objectUrl: url });
  } catch (e) {
    if (revision !== voiceRevision || cancelled || paused) return;
    if (ctl) {
      ctl.setLoading(false);
      ctl.setPlaying(false);
    }
    if (currentVoice && currentVoice.msgId === msg.id) {
      currentVoice = null;
    }
    const engineAction = _callbacks.onOpenEngineStatus ? {
      actionLabel: '检查运行状态',
      onAction: () => _callbacks.onOpenEngineStatus(),
    } : null;
    if (isTimeout || e.code === 'AUDIO_TIMEOUT') {
      showToast('语音合成超时（16s），请检查 GPT-SoVITS 服务是否正常运行', 'error', engineAction);
    } else if (cancelled || (e && e.name === 'AbortError')) {
      return;
    } else {
      const errStr = e.message || String(e);
      showToast(`语音合成失败（${errStr}），请检查 GPT-SoVITS 服务是否就绪`, 'error', engineAction);
    }
  } finally {
    clearTimeout(timeoutTimer);
    if (pendingSynthesis === abortCtrl) pendingSynthesis = null;
    if (revision === voiceRevision && ctl) ctl.setLoading(false);
  }
}

// A loading request belongs to the click that created it. Switching sessions,
// clicking another message or starting a stream invalidates every late result.
function beginPlaybackLoad(msg, ctl) {
  stopCurrentVoice();
  const revision = voiceRevision;
  const controller = new AbortController();
  pendingPreparation = { controller, msgId: msg.id, abort: () => controller.abort() };
  ctl?.setLoading?.(true);
  if (ctl) loadingOwners.set(ctl, controller);
  const stopLoading = () => {
    if (ctl && loadingOwners.get(ctl) === controller) {
      loadingOwners.delete(ctl);
      ctl.setLoading?.(false);
    }
  };
  controller.signal.addEventListener('abort', stopLoading, { once: true });
  return {
    signal: controller.signal,
    current: () => revision === voiceRevision && !controller.signal.aborted,
    finish() {
      if (pendingPreparation?.controller === controller) pendingPreparation = null;
      stopLoading();
    },
  };
}

function streamVoiceHandle(msg, ctl) {
  return {
    msgId: msg.id,
    get paused() { return streamAudioController.paused; },
    pause() { streamAudioController.pause(); },
    resume() { streamAudioController.resume(); },
    setPlaying: ctl?.setPlaying,
    setProgress: ctl?.setProgress,
    stop() { streamAudioController.interrupt(40); },
  };
}

export async function playAiVoice(msg, ctl) {
  if (pendingPreparation?.msgId === msg.id) { stopCurrentVoice(); return; }
  if (currentVoice?.msgId === msg.id) {
    if (currentVoice.paused) currentVoice.resume();
    else currentVoice.pause();
    return;
  }
  const request = beginPlaybackLoad(msg, ctl);
  try {
    const fullCacheKey = `msg_${msg.id}`;
    const chunks = (msg.audioChunks || []).filter(c => c?.url).map(c => ({ ...c }));
    const cached = chunks.length ? null : await getCachedAudioBlob(fullCacheKey);
    if (!request.current()) return;
    if (cached?.size) {
      currentVoice = streamVoiceHandle(msg, ctl);
      streamAudioController.startSession(msg.id, ctl);
      await streamAudioController.enqueueChunk({ url: fullCacheKey, index: 0, ctl, blob: cached, totalExpected: 1 });
      return;
    }
    if (!chunks.length) {
      chunks.push(...(msg.audioUrls || []).filter(Boolean).map(url => ({ url })));
    }
    if (!chunks.length) {
      request.finish();
      return synthesizeAiVoice(msg, ctl);
    }
    try {
      // Reuse the downloaded body instead of probing and fetching it twice.
      chunks[0].blob = await loadAudioBlob(chunks[0].url, { signal: request.signal });
    } catch (error) {
      if (!request.current()) return;
      // A timeout or an offline service does not prove cached URLs have expired.
      if (error.status !== 404 && error.status !== 410) throw error;
      msg.audioUrls = [];
      msg.audioChunks = [];
      saveState();
      request.finish();
      return synthesizeAiVoice(msg, ctl);
    }
    if (!request.current()) return;
    currentVoice = streamVoiceHandle(msg, ctl);
    await streamAudioController.playChunks(chunks, ctl, msg.id);
  } catch (error) {
    if (!request.current() || error.name === 'AbortError') return;
    ctl?.setPlaying?.(false);
    showToast('语音暂时无法加载，原有音频记录已保留。请稍后在 LOG 里点「语音」重试，或打开「运行状态」检查服务。', 'info', _callbacks.onOpenEngineStatus ? {
      actionLabel: '检查运行状态',
      onAction: () => _callbacks.onOpenEngineStatus(),
    } : null);
  } finally {
    request.finish();
  }
}

export async function playUserVoice(msg, ctl) {
  if (pendingPreparation?.msgId === msg.id) { stopCurrentVoice(); return; }
  if (currentVoice?.msgId === msg.id) {
    if (currentVoice.paused) currentVoice.resume();
    else currentVoice.pause();
    return;
  }
  const request = beginPlaybackLoad(msg, ctl);
  try {
    let rec = audioStore.get(msg.id);
    if (!rec && (msg.voiceKey || msg.audioUrl)) {
      const blob = await getCachedAudioBlob(msg.voiceKey || msg.audioUrl);
      if (!request.current()) return;
      if (blob) {
        rec = { url: URL.createObjectURL(blob), dur: msg.dur || 3 };
        audioStore.set(msg.id, rec);
      }
    }
    if (!request.current()) return;
    if (!rec) {
      showToast('该条录音缓存已清理，无法播放', 'info');
      return;
    }
    playSingleAudio(() => new Audio(rec.url), msg.id, ctl);
  } finally {
    request.finish();
  }
}

export function playVoice(msg, ctl) {
  if (msg.role === 'user') playUserVoice(msg, ctl);
  else playAiVoice(msg, ctl);
}

export function updateVoiceModeUI() {
  if (!state.global.voiceMode) stopCurrentVoice();
}

export function toggleVoiceMode() {
  state.global.voiceMode = !state.global.voiceMode;
  saveState();
  updateVoiceModeUI();
}

export function updateMicUI(listening) {
  if (micDom()) {
    micDom().classList.toggle('listening', Boolean(listening));
    micDom().setAttribute('aria-pressed', listening ? 'true' : 'false');
  }
}

export function finalizeVoice() {
  clearMicTimers();
  const { text, audio, dur, blob } = pendingVoice;
  pendingVoice = { text: null, audio: null, dur: 0, blob: null };
  if (!text && !audio) return;
  if (!text && audio) {
    // 有录音但未识别出文字 → 不会发送，释放 Blob URL 避免泄漏
    try {
      URL.revokeObjectURL(audio);
    } catch (_) {}
  }
  if (text) {
    if (_callbacks.sendMessage) {
      _callbacks.sendMessage(text, audio ? { url: audio, dur, blob } : undefined);
    }
  } else {
    showToast('没有识别到语音内容，请靠近麦克风再试', 'error');
  }
}

export function stopMic() {
  clearInterval(micTimer);
  micTimer = null;
  clearMicTimers();
  updateMicUI(false);
  if (inputDom()) inputDom().placeholder = '说点什么，或点麦克风用语音输入…';
  stopListening();
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

export function toggleMic() {
  clearMicTimers();
  if (isListening()) {
    stopMic();
    return;
  }
  if (_callbacks.isBusy && _callbacks.isBusy()) return;
  const revision = ++micRevision;
  const handle = startListening({
    onInterim: (t) => {
      if (revision !== micRevision) return;
      if (inputDom()) {
        inputDom().value = `${micBase}${micBase ? ' ' : ''}${t}`;
      }
      if (_callbacks.autoGrow) _callbacks.autoGrow();
    },
    onText: (finalText) => {
      if (revision !== micRevision) return;
      pendingVoice.text = `${micBase ? micBase + ' ' : ''}${finalText}`.trim();
      if (inputDom()) {
        inputDom().value = pendingVoice.text;
      }
      if (_callbacks.autoGrow) _callbacks.autoGrow();
    },
    onRecorded: (blob, dur) => {
      if (revision !== micRevision) return;
      if (pendingVoice.audio) {
        try {
          URL.revokeObjectURL(pendingVoice.audio);
        } catch (_) {}
      }
      pendingVoice.audio = URL.createObjectURL(blob);
      pendingVoice.dur = dur;
      pendingVoice.blob = blob;
    },
    onError: (msg) => {
      if (revision !== micRevision) return;
      showToast(msg, 'error');
    },
  });
  if (!handle.ok) {
    showToast(handle.reason, 'error');
    return;
  }
  micBase = inputDom() ? inputDom().value : '';
  if (pendingVoice?.audio) {
    try {
      URL.revokeObjectURL(pendingVoice.audio);
    } catch (_) {}
  }
  pendingVoice = { text: null, audio: null, dur: 0, blob: null };
  micSecs = 0;
  updateMicUI(true);
  micTimer = setInterval(() => {
    micSecs += 1;
    if (inputDom()) {
      inputDom().placeholder = `正在聆听… ${micSecs}s（再次点击麦克风结束）`;
    }
  }, 1000);
  if (inputDom()) inputDom().placeholder = '正在聆听…（再次点击麦克风结束）';
  if (!recorderSupported() && !voiceSupported()) {
    showToast('当前浏览器不支持语音输入，建议使用 Chrome / Edge', 'error');
  }
}

export function revokeAllCachedAudioUrls() {
  audioStore.clear();
  if (pendingVoice && pendingVoice.audio) {
    try {
      URL.revokeObjectURL(pendingVoice.audio);
    } catch (_) {}
    pendingVoice.audio = null;
  }
}
