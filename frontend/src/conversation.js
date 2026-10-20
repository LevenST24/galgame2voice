/**
 * 会话逻辑不依赖 DOM；聊天气泡和后续 ADV 舞台订阅同一组事件。
 *
 * 控制器事件 (Controller Events):
 * - turnstart: ({ userMessage, assistantMessage, originId, retryOf })
 * - text: ({ delta, text })
 * - line: ({ messageId, line }) — 新单句台词到达，line 为 { index, zh, ja, emotion, act, audio: { status, chunks } }
 * - line_audio: ({ messageId, line, status, chunks }) — 某句台词的音频结果权威状态与切片
 * - audio_chunk: (chunk) — 逐句音频切片事件
 * - done: ({ message, error, kind, originId, failedInline, meta, hasStartedStreamAudio })
 * - cancelled: ({ message, error, kind, originId, failedInline, meta, hasStartedStreamAudio })
 * - error: ({ message, error, kind, originId, failedInline, meta, hasStartedStreamAudio })
 */
import { streamChat } from './ai.js';
import { normalizeLines } from './session_data.js';

export function createConversation({
  state, getActive, getSession, addMessage, uid, saveState, persistSession,
  isSessionDeleting, audioStore, putCachedAudioBlob, fetchAndCacheAudio,
  audioPlayer, getCurrentVoice, stopCurrentVoice, setCurrentVoice,
  clearMicTimers, beforeSend, onBlocked, getAudioControl,
  getAudioMode = () => 'sequential',
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
  const isBusy = () => {
    const session = getActive();
    return busy || Boolean(session?.loadingOlder || (session && isSessionDeleting(session.id)));
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
    if (!text || busy) return false;
    if (audioPlayer.isPlaying || getCurrentVoice()) stopCurrentVoice();
    const session = getActive();
    if (!session) return false;
    if (session.loadingOlder) {
      onBlocked?.('正在加载更早的聊天记录，完成后即可发送。');
      return false;
    }
    if (isSessionDeleting(session.id)) {
      onBlocked?.('正在删除这个对话，请稍候或切换到其他对话。');
      return false;
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
    const current = {
      originId,
      streamMsgId: assistantMessage.id,
      hasStartedStreamAudio: false,
      bufferedChunksByLine: new Map(),
      linesByLineIndex: new Map(),
      hasReceivedLineEvents: false,
      settled: false,
    };
    turn = current;
    busy = true;
    let lastFull = '';
    cancelStream = () => finish('', { cancelled: true });
    emit('turnstart', { userMessage, assistantMessage, originId, retryOf });
    // A listener may stop the turn before the request starts.
    if (!busy || turn !== current) return true;
    const cancel = streamChat({
      prompt: text, sessionId: session.id, settings: session.settings,
      preset: state.global.ttsPreset || '', fetch: fetchRequest,
      onChunk: (full, delta) => {
        lastFull = full;
        assistantMessage.content = full;
        emit('text', { delta, text: full });
      },
      onEvent: (type, payload) => {
        if (turn !== current || current.settled) return;
        if (type === 'line') {
          if (!Number.isInteger(payload?.index) || payload.index < 0) return;
          current.hasReceivedLineEvents = true;
          if (!assistantMessage.lines) assistantMessage.lines = [];
          const earlyChunks = current.bufferedChunksByLine.get(payload.index) || [];
          const lineRecord = {
            index: payload.index,
            zh: typeof payload.zh === 'string' ? payload.zh : '',
            ja: typeof payload.ja === 'string' ? payload.ja : '',
            emotion: typeof payload.emotion === 'string' ? payload.emotion : '',
            act: (payload.act && typeof payload.act === 'object' && !Array.isArray(payload.act)) ? { ...payload.act } : {},
            audio: {
              status: 'pending',
              chunks: [...earlyChunks],
            },
          };
          current.linesByLineIndex.set(payload.index, lineRecord);
          const existingIdx = assistantMessage.lines.findIndex((l) => l.index === payload.index);
          if (existingIdx !== -1) {
            assistantMessage.lines[existingIdx] = lineRecord;
          } else {
            assistantMessage.lines.push(lineRecord);
            assistantMessage.lines.sort((a, b) => a.index - b.index);
          }
          emit('line', { messageId: assistantMessage.id, line: lineRecord });
        } else if (type === 'line_audio') {
          const lineRecord = current.linesByLineIndex.get(payload.line);
          const chunks = Array.isArray(payload.chunks) ? payload.chunks : (lineRecord?.audio?.chunks || []);
          if (lineRecord) {
            lineRecord.audio = { status: payload.status, chunks };
          }
          emit('line_audio', {
            messageId: assistantMessage.id,
            line: payload.line,
            status: payload.status,
            chunks,
          });
        } else if (type === 'audio_chunk') {
          if (payload && typeof payload.line === 'number') {
            const lineRecord = current.linesByLineIndex.get(payload.line);
            const chunkObj = {
              index: payload.index,
              line: payload.line,
              audio_url: payload.audio_url,
              sentence: payload.sentence || '',
              emotion: payload.emotion || '',
            };
            if (lineRecord) {
              if (!lineRecord.audio.chunks.some((c) => c.index === payload.index)) {
                lineRecord.audio.chunks.push(chunkObj);
              }
            } else {
              if (!current.bufferedChunksByLine.has(payload.line)) {
                current.bufferedChunksByLine.set(payload.line, []);
              }
              current.bufferedChunksByLine.get(payload.line).push(chunkObj);
            }
          }
          emit('audio_chunk', payload);
        } else {
          emit(type, payload);
        }
      },
      onAudio: (url, idx, sentence, meta = {}) => {
        if (turn !== current || current.settled) return;
        const mode = typeof getAudioMode === 'function' ? getAudioMode() : 'sequential';
        if (mode === 'lines') {
          if (url && typeof url === 'string' && url.startsWith('/audio/')) {
            fetchAndCacheAudio(url).catch(() => {});
          }
          return;
        }
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
    return true;

    function finish(finalText, meta = {}) {
      if (current.settled) return;
      current.settled = true;
      const { cancelled = false, audioUrls = [] } = meta;
      let { error = null, errorCode = null } = meta;
      cancelStream = null;
      const textToSave = (finalText || lastFull || '').trim();
      if (audioPlayer.currentMsgId === current.streamMsgId) audioPlayer.finishInput();
      const message = {
        id: uid('m'), role: 'assistant', content: textToSave,
        japanese: meta.japanese || '', audioUrls, audioChunks: meta.chunks || [],
        ts: Date.now(), ttsParams: meta.ttsParams || null,
      };

      if (Array.isArray(meta.lines)) {
        message.lines = normalizeLines(meta.lines);
        assistantMessage.lines = message.lines;
      } else if (current.hasReceivedLineEvents && assistantMessage.lines) {
        message.lines = assistantMessage.lines;
      }

      // The turn is over, so no line_audio can still arrive.
      for (const lines of [message.lines, assistantMessage.lines]) {
        for (const line of lines || []) {
          if (line.audio && line.audio.status === 'pending') line.audio.status = 'failed';
        }
      }

      // Keep only reply text delivered by the stream, including final corrections.
      const meaningfulLines = message.lines?.some(line => line.zh.trim() || line.ja.trim());
      if (meaningfulLines) {
        if (!message.content) message.content = message.lines.map(line => line.zh).join('');
        if (!message.japanese) message.japanese = message.lines.map(line => line.ja).join('');
      }
      const stored = Boolean((lastFull && textToSave) || meaningfulLines);
      if (!cancelled && !error && !textToSave && !message.japanese.trim() && !meaningfulLines) {
        error = '模型这次没有返回任何内容';
        errorCode = 'empty_reply';
      }
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

  function canRetry(message) {
    const session = getActive();
    return Boolean(session && message?.failed && session.messages.at(-1) === message && !isBusy());
  }

  function retry(message) {
    if (!canRetry(message)) {
      if (isBusy() && message?.failed && getActive()?.messages.at(-1) === message) {
        onBlocked?.('当前回复完成后即可重新发送。');
      }
      return false;
    }
    return send(message.content, null, { retryOf: message });
  }

  return { on, send, stop, retry, canRetry, attachVoiceControl, get busy() { return isBusy(); } };
}
