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
} from '../cache.js';
import { streamAudioController } from '../audio_player.js';
import { portraitStage } from '../portrait.js';
import { showToast } from '../ui.js';

let _dom = {};
let _callbacks = {};

let currentVoice = null; // { msgId, paused, pause, resume, setPlaying, setProgress, stop }
let micTimer = null;
let micSecs = 0;
let micBase = '';
let pendingVoice = { text: null, audio: null, dur: 0, blob: null };
let micFinalizeTimers = [];

export function getCurrentVoice() {
  return currentVoice;
}

export function setCurrentVoice(v) {
  currentVoice = v;
}

export function initAudioPlayerUi(dom, callbacks = {}) {
  _dom = dom;
  _callbacks = callbacks;

  // Stream audio controller hookups with portrait stage
  streamAudioController.onStatusChange = (isPlaying) => {
    try {
      portraitStage.setSpeaking(isPlaying);
    } catch (_) {}
  };

  streamAudioController.onChunkStart = (chunk) => {
    if (chunk && chunk.sentence) {
      try {
        portraitStage.updateDialogue(chunk.sentence);
        // 后端随音频切片下发逐句情绪，立绘据此在同情绪内做细微漂移
        if (chunk.emotion) {
          portraitStage.setEmotion(chunk.emotion, chunk.sentence);
        } else {
          portraitStage.handleMessageEmotion(chunk.sentence);
        }
      } catch (_) {}
    }
  };

  streamAudioController.onQueueEmpty = () => {
    try {
      portraitStage.setSpeaking(false);
    } catch (_) {}
    if (currentVoice) {
      try {
        currentVoice.setPlaying(false);
      } catch (_) {}
      currentVoice = null;
    }
  };

  if (_dom.voiceModeBtn) {
    _dom.voiceModeBtn.addEventListener('click', () => {
      state.global.voiceMode = !state.global.voiceMode;
      updateVoiceModeUI();
    });
  }

  if (_dom.micBtn) {
    _dom.micBtn.addEventListener('click', toggleMic);
  }
}

export function clearMicTimers() {
  micFinalizeTimers.forEach((t) => clearTimeout(t));
  micFinalizeTimers = [];
}

export function stopCurrentVoice() {
  try {
    streamAudioController.interrupt(40);
  } catch (_) {}
  try {
    portraitStage.setSpeaking(false);
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
    try {
      portraitStage.setSpeaking(true);
    } catch (_) {}
    audio.ontimeupdate = () => {
      if (audio.duration && ctl && ctl.setProgress) ctl.setProgress(audio.currentTime / audio.duration);
    };
    audio.onended = () => {
      try {
        portraitStage.setSpeaking(false);
      } catch (_) {}
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
      try {
        portraitStage.setSpeaking(false);
      } catch (_) {}
      audio.ontimeupdate = null;
      audio.onended = null;
      audio.onerror = null;
      if (cancelled) return;
      if (ctl && ctl.setPlaying) ctl.setPlaying(false);
      if (objectUrl) {
        try {
          URL.revokeObjectURL(objectUrl);
        } catch (_) {}
        objectUrl = null;
      }
      showToast('音频播放失败', 'error');
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
          if (err && err.name === 'AbortError') return;
          if (ctl && ctl.setPlaying) ctl.setPlaying(false);
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
      if (err && err.name === 'AbortError') return;
      if (ctl && ctl.setPlaying) ctl.setPlaying(false);
    });
  }
}

export async function synthesizeAiVoice(msg, ctl) {
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
      text = plainSpeechText(await _callbacks.resolveJapanese(msg) || '').slice(0, 2000);
    }
    if (!text) {
      if (ctl) {
        ctl.setLoading(false);
        ctl.setPlaying(false);
      }
      currentVoice = null;
      showToast('该消息缺少日文配音原文，点消息下的「翻译」取到日文后再播', 'info');
      return;
    }

    const session = getActive() || getSession(state.activeId) || {};
    const settings = session.settings || DEFAULT_SESSION_SETTINGS;
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
    if (ctl) ctl.setLoading(false);
    if (cancelled || paused || (currentVoice && currentVoice.msgId !== msg.id)) {
      return;
    }

    const cacheKey = `msg_${msg.id}`;
    await putCachedAudioBlob(cacheKey, blob);
    msg.audioUrls = [cacheKey];
    saveState();

    const url = URL.createObjectURL(blob);
    playSingleAudio(() => new Audio(url), msg.id, ctl, { objectUrl: url });
  } catch (e) {
    if (ctl) {
      ctl.setLoading(false);
      ctl.setPlaying(false);
    }
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
    if (ctl) ctl.setLoading(false);
  }
}

/**
 * 切片是否还取得到：先看 Cache Storage，再探一次 HTTP。
 * 只探最早那条作抽样 —— 分句 wav 由 audio_cleaner 按时间清理（缓存文件按最后访问
 * 7 天 LRU 淘汰，临时文件按保留期 30 分钟过期），同批生成的分句通常一起到期。
 */
async function firstChunkStillExists(url) {
  try {
    const cached = await getCachedAudioBlob(url);
    if (cached && cached.size > 0) return true;
    const res = await fetch(url, { cache: 'no-store' });
    return res.ok;
  } catch (_) {
    return false;
  }
}

export async function playAiVoice(msg, ctl) {
  if (currentVoice && currentVoice.msgId === msg.id) {
    if (streamAudioController.isPlaying) {
      if (currentVoice.paused) currentVoice.resume();
      else currentVoice.pause();
      return;
    }
    currentVoice = null;
  }
  stopCurrentVoice();

  const fullCacheKey = `msg_${msg.id}`;
  // 有分句元数据时优先走分句播放：整块 blob 路径不带句子和情绪，立绘就不会逐句切换
  const chunked = (msg.audioChunks || []).filter((c) => c && c.url);
  const localCachedBlob = chunked.length ? null : await getCachedAudioBlob(fullCacheKey);
  if (localCachedBlob && localCachedBlob.size > 0) {
    if (ctl) {
      ctl.setProgress(0);
      ctl.setPlaying(true);
    }
    currentVoice = {
      msgId: msg.id,
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
      setPlaying: ctl?.setPlaying,
      setProgress: ctl?.setProgress,
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

  const urls = chunked.length ? chunked.map((c) => c.url) : (msg.audioUrls || []).filter(Boolean);
  if (urls.length && !(await firstChunkStillExists(urls[0]))) {
    // 切片 wav 已被 audio_cleaner 清掉（分句通常存于 /audio/cache，按最后访问超 7 天
    // 被 LRU 淘汰；只有写缓存失败落的临时分句才按保留期默认 30 分钟过期）。enqueueChunk
    // 会静默吞掉 404 继续排期，结果是"点了没声音、进度条却满格"，所以这里主动
    // 放弃这些 URL，改走按需合成。
    msg.audioUrls = [];
    msg.audioChunks = [];
    saveState();
    synthesizeAiVoice(msg, ctl);
    return;
  }
  if (!urls.length) {
    synthesizeAiVoice(msg, ctl);
    return;
  }

  if (ctl) {
    ctl.setProgress(0);
    ctl.setPlaying(true);
  }

  currentVoice = {
    msgId: msg.id,
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
    setPlaying: ctl?.setPlaying,
    setProgress: ctl?.setProgress,
    stop() {
      streamAudioController.interrupt(40);
    },
  };

  // 传带元数据的分句对象，重播时立绘才会跟着语境逐句切换
  streamAudioController.playChunks(chunked.length ? chunked : urls, ctl, msg.id).catch((err) => {
    console.warn('[playAiVoice] 播放失败，回退重合成:', err);
    currentVoice = null;
    synthesizeAiVoice(msg, ctl);
  });
}

export async function playUserVoice(msg, ctl) {
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
  if (currentVoice && currentVoice.msgId === msg.id) {
    if (currentVoice.paused) currentVoice.resume();
    else currentVoice.pause();
    return;
  }
  stopCurrentVoice();
  playSingleAudio(() => new Audio(rec.url), msg.id, ctl);
}

export function playVoice(msg, ctl) {
  if (msg.role === 'user') playUserVoice(msg, ctl);
  else playAiVoice(msg, ctl);
}

export function updateVoiceModeUI() {
  const on = state.global.voiceMode;
  if (_dom.voiceModeBtn) {
    _dom.voiceModeBtn.classList.toggle('active', on);
    _dom.voiceModeBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    _dom.voiceModeBtn.title = on ? '关闭自动播放语音回复' : '自动播放语音回复';
  }
  if (_dom.voiceModeIcon) {
    const use = _dom.voiceModeIcon.querySelector('use');
    if (use) use.setAttribute('href', on ? '#i-volume' : '#i-volume-off');
  }
  if (!on) stopCurrentVoice();
}

export function updateMicUI(listening) {
  if (_dom.micBtn) {
    _dom.micBtn.classList.toggle('listening', Boolean(listening));
    _dom.micBtn.setAttribute('aria-pressed', listening ? 'true' : 'false');
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
  if (_dom.input) _dom.input.placeholder = '说点什么，或点麦克风用语音输入…';
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
  const handle = startListening({
    onInterim: (t) => {
      if (_dom.input) {
        _dom.input.value = `${micBase}${micBase ? ' ' : ''}${t}`;
      }
      if (_callbacks.autoGrow) _callbacks.autoGrow();
    },
    onText: (finalText) => {
      pendingVoice.text = `${micBase ? micBase + ' ' : ''}${finalText}`.trim();
      if (_dom.input) {
        _dom.input.value = pendingVoice.text;
      }
      if (_callbacks.autoGrow) _callbacks.autoGrow();
    },
    onRecorded: (blob, dur) => {
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
      showToast(msg, 'error');
    },
  });
  if (!handle.ok) {
    showToast(handle.reason, 'error');
    return;
  }
  micBase = _dom.input ? _dom.input.value : '';
  pendingVoice = { text: null, audio: null, dur: 0, blob: null };
  micSecs = 0;
  updateMicUI(true);
  micTimer = setInterval(() => {
    micSecs += 1;
    if (_dom.input) {
      _dom.input.placeholder = `正在聆听… ${micSecs}s（再次点击麦克风结束）`;
    }
  }, 1000);
  if (_dom.input) _dom.input.placeholder = '正在聆听…（再次点击麦克风结束）';
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
