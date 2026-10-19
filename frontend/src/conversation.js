// 会话逻辑不依赖 DOM；聊天气泡和后续 ADV 舞台订阅同一组事件。
import { streamChat } from './ai.js';

export function createConversation({
  state, getActive, getSession, addMessage, uid, saveState, persistSession,
  isSessionDeleting, audioStore, putCachedAudioBlob, fetchAndCacheAudio,
  audioPlayer, getCurrentVoice, stopCurrentVoice, setCurrentVoice,
  clearMicTimers, beforeSend, onBlocked, getAudioControl,
  fetch: fetchRequest = (...args) => globalThis.fetch(...args),
}) {
  const listeners = new Map();
  let busy = false;
  let cancelStream = null;
  let turn = null;
  const emit = (type, event) => {
    for (const fn of [...(listeners.get(type) || [])]) fn(event);
  };
  const on = (type, fn) => {
    if (!listeners.has(type)) listeners.set(type, new Set());
    listeners.get(type).add(fn);
    return () => listeners.get(type)?.delete(fn);
  };

  function attachVoiceControl(voiceCtl, message) {
    if (!turn?.hasStartedStreamAudio || audioPlayer.currentMsgId !== turn.streamMsgId) return;
    audioPlayer.attachControl(voiceCtl, message.id);
    if (audioPlayer.isPlaying) {
      setCurrentVoice({
        msgId: message.id,
        get paused() { return audioPlayer.paused; },
        pause() { audioPlayer.pause(); },
        resume() { audioPlayer.resume(); },
        stop() { audioPlayer.interrupt(40); },
        setPlaying: voiceCtl?.setPlaying,
        setProgress: voiceCtl?.setProgress,
      });
    }
  }

  function send(rawText = '', voiceMeta, { retryOf = null } = {}) {
    clearMicTimers?.();
    const text = rawText.trim();
    if (!text || busy) return;
    if (audioPlayer.isPlaying || getCurrentVoice()) stopCurrentVoice();
    const session = getActive();
    if (!session) return;
    if (session.loadingOlder) {
      onBlocked?.('正在加载更早的聊天记录，完成后即可发送。');
      return;
    }
    if (isSessionDeleting(session.id)) {
      onBlocked?.('正在删除这个对话，请稍候或切换到其他对话。');
      return;
    }
    const originId = session.id;
    beforeSend?.();
    let userMessage = retryOf;
    if (userMessage) {
      // 重试保留消息的位置和录音，只清除失败说明。
      delete userMessage.failed;
      saveState();
    } else {
      const prevTitle = session.title;
      userMessage = addMessage('user', text);
      if (session.title !== prevTitle) persistSession(session).catch(() => {});
      if (voiceMeta && userMessage) {
        audioStore.set(userMessage.id, { url: voiceMeta.url, dur: voiceMeta.dur });
        userMessage.dur = voiceMeta.dur;
        if (voiceMeta.blob) {
          const voiceKey = `user_${userMessage.id}`;
          putCachedAudioBlob(voiceKey, voiceMeta.blob);
          userMessage.voiceKey = voiceKey;
          saveState();
        }
      }
    }
    const assistantMessage = { id: uid('m_stream'), role: 'assistant', content: '', ts: Date.now() };
    const current = { originId, streamMsgId: assistantMessage.id, hasStartedStreamAudio: false };
    turn = current;
    busy = true;
    let lastFull = '';
    cancelStream = () => finish('', { cancelled: true });
    emit('turnstart', { userMessage, assistantMessage, originId, retryOf });
    // A listener may stop the turn before the request starts.
    if (!busy || turn !== current) return;
    const cancel = streamChat({
      prompt: text, sessionId: session.id, settings: session.settings,
      preset: state.global.ttsPreset || '', fetch: fetchRequest,
      onChunk: (full, delta) => {
        lastFull = full;
        assistantMessage.content = full;
        emit('text', { delta, text: full });
      },
      onEvent: (type, payload) => emit(type, payload),
      onAudio: (url, idx, sentence, meta = {}) => {
        if ((!state.global.voiceMode || originId !== state.activeId) && url && typeof url === 'string' && url.startsWith('/audio/')) {
          fetchAndCacheAudio(url).catch(() => {});
        }
        if (state.global.voiceMode && originId === state.activeId && url) {
          if (idx === 0 || !current.hasStartedStreamAudio) {
            current.hasStartedStreamAudio = true;
            current.audioControl = getAudioControl?.() || null;
            const ctl = current.audioControl;
            audioPlayer.startSession(current.streamMsgId, ctl, { streaming: true });
            setCurrentVoice({
              msgId: current.streamMsgId,
              get paused() { return audioPlayer.paused; },
              pause() { audioPlayer.pause(); if (ctl) ctl.setPlaying(false); },
              resume() { audioPlayer.resume(); if (ctl) ctl.setPlaying(true); },
              setPlaying: (p) => { if (ctl) ctl.setPlaying(p); },
              setProgress: (pct) => { if (ctl) ctl.setProgress(pct); },
              stop() { audioPlayer.interrupt(40); },
            });
          } else {
            current.audioControl = getAudioControl?.() || null;
          }
          audioPlayer.enqueueChunk({ url, index: idx, sentence, ctl: current.audioControl, emotion: meta.emotion || '' });
        }
      },
      onEnd: (finalText, meta = {}) => finish(finalText, meta),
    });
    if (busy && turn === current) cancelStream = cancel;

    function finish(finalText, meta = {}) {
      if (current.settled) return;
      current.settled = true;
      const { cancelled = false, error = null, errorCode = null, audioUrls = [] } = meta;
      cancelStream = null;
      const textToSave = (finalText || lastFull || '').trim();
      if (audioPlayer.currentMsgId === current.streamMsgId) audioPlayer.finishInput();
      const message = {
        id: uid('m'), role: 'assistant', content: textToSave,
        japanese: meta.japanese || '', audioUrls, audioChunks: meta.chunks || [],
        ts: Date.now(), ttsParams: meta.ttsParams || null,
      };
      // Keep only reply text delivered by the stream, including final corrections.
      const stored = Boolean(lastFull && textToSave);
      const origin = getSession(originId);
      if (origin && stored) {
        origin.messages.push(message);
        origin.updatedAt = Date.now();
      }
      const failedInline = Boolean(error && !stored && origin?.messages.includes(userMessage));
      if (failedInline) userMessage.failed = { error, code: errorCode };
      saveState();
      busy = false;
      emit(error ? 'error' : cancelled ? 'cancelled' : 'done', {
        message: stored ? message : null, error, kind: errorCode,
        originId, failedInline, meta, hasStartedStreamAudio: current.hasStartedStreamAudio,
      });
    }
  }

  function stop() {
    if (cancelStream) {
      const cancel = cancelStream;
      cancelStream = null;
      cancel();
    }
  }

  function retry(message) {
    const session = getActive();
    if (!session || !message?.failed || session.messages[session.messages.length - 1] !== message) return;
    if (busy) {
      onBlocked?.('当前回复完成后即可重新发送。');
      return;
    }
    send(message.content, null, { retryOf: message });
  }

  return { on, send, stop, retry, attachVoiceControl, get busy() { return busy; } };
}
