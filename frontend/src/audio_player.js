/**
 * StreamAudioController — 单句 Web Audio 播放。
 *
 * 1. AudioContext 惰性初始化。单句播放自己做有时限的 resume，不在这里空等。
 * 2. 采样级排期（currentTime + source.start）。当前排期已经落到现在或更早时，首切片加上 currentBufferLeadSec。
 * 3. 12ms 微渐变包络，减轻切片衔接爆音。
 * 4. stopLine / interrupt 通过单句增益做平滑渐出。
 * 5. 一句里坏掉的切片会被跳过，后面的切片继续排期。
 */

import { loadAudioBuffer } from './cache.js';
import { withAudioDeadline } from './audio_request.js';

// 整句没有开声时，只有确认丢失或损坏才返回 'failed'。超时和连接问题下次还能再点。
function loadFailureOutcome(errors) {
  const confirmed = errors.some(error =>
    error?.code === 'AUDIO_DECODE_FAILED' || error?.status === 404 || error?.status === 410);
  if (confirmed || errors.length === 0) return 'failed';
  if (errors.every(error => error?.code === 'AUDIO_TIMEOUT')) return 'timeout';
  return 'network';
}

export class StreamAudioController {
  constructor(options = {}) {
    this.crossFadeMs = options.crossFadeMs || 0.012; // 12ms micro-fade 消除瞬态爆音
    this.fetchTimeoutMs = options.fetchTimeoutMs || 15000;

    this.ctx = null;
    this.masterGain = null;
    this.analyser = null;

    this.nextStartTime = 0;
    this.isPaused = false;
    this._reportedError = false;
    this._linePlayState = null;
    this.supportsStartNotification = true;

    this.currentBufferLeadSec = 0.02;

    this.onError = options.onError || null;

    this._onVisibilityChange = () => {
      if (typeof document !== 'undefined' && !document.hidden) {
        if (this.ctx && this.ctx.state === 'running') {
          if (this.nextStartTime < this.ctx.currentTime) {
            this.nextStartTime = this.ctx.currentTime + this.currentBufferLeadSec;
          }
        }
      }
    };
    if (typeof document !== 'undefined' && document.addEventListener) {
      document.addEventListener('visibilitychange', this._onVisibilityChange);
    }
  }

  /**
   * 初始化 AudioContext 与 MasterGainNode。可安全重复调用。
   * 单句播放禁用隐式 resume，由已登记的播放任务负责限时唤醒与取消。
   * @returns {AudioContext|null}
   */
  ensureContext({ resumeIfSuspended = true } = {}) {
    if (!this.ctx) {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) {
        console.error('[StreamAudioController] 当前浏览器不支持 Web Audio API');
        return null;
      }
      this.ctx = new AudioContextClass();
      this.masterGain = this.ctx.createGain();
      this.masterGain.gain.setValueAtTime(1.0, this.ctx.currentTime);
      this.analyser = null;
      if (typeof this.ctx.createAnalyser === 'function') {
        try {
          const analyser = this.ctx.createAnalyser();
          analyser.fftSize = 256;
          analyser.smoothingTimeConstant = 0.6;
          this.masterGain.connect(analyser);
          analyser.connect(this.ctx.destination);
          this.analyser = analyser;
        } catch (_) {
          this.analyser = null;
          this.masterGain.connect(this.ctx.destination);
        }
      } else {
        this.masterGain.connect(this.ctx.destination);
      }
    }
    if (resumeIfSuspended && this.ctx.state === 'suspended' && !this.isPaused) {
      this.ctx.resume().catch(() => {});
    }
    return this.ctx;
  }

  /**
   * 当前是否处于暂停状态
   */
  get paused() {
    return this.isPaused || (this.ctx && this.ctx.state === 'suspended');
  }

  get isPlaying() {
    return Boolean(this._linePlayState && !this._linePlayState.settled);
  }

  /**
   * 返回当前时域振幅均方根 (RMS)，用于驱动口型同步 (Lip Sync)。
   * 采样缓冲区复用，口型轮询每秒最多 30 次，不必每次分配。
   * @returns {number}
   */
  getRms() {
    const analyser = this.analyser;
    if (!analyser) return 0;
    const size = analyser.fftSize || 256;
    if (this._rmsBuffer?.length !== size) this._rmsBuffer = new Float32Array(size);
    const buffer = this._rmsBuffer;
    if (typeof analyser.getFloatTimeDomainData === 'function') {
      analyser.getFloatTimeDomainData(buffer);
    } else if (typeof analyser.getByteTimeDomainData === 'function') {
      if (this._rmsBytes?.length !== size) this._rmsBytes = new Uint8Array(size);
      analyser.getByteTimeDomainData(this._rmsBytes);
      for (let i = 0; i < size; i++) buffer[i] = (this._rmsBytes[i] - 128) / 128;
    } else {
      return 0;
    }
    let sum = 0;
    for (let i = 0; i < size; i++) sum += buffer[i] * buffer[i];
    return Math.sqrt(sum / size);
  }

  /**
   * 暂停当前播放
   */
  pause() {
    this.isPaused = true;
    if (this.ctx && this.ctx.state === 'running') {
      this.ctx.suspend().catch(() => {});
    }
  }

  /**
   * 恢复当前播放
   */
  resume() {
    this.isPaused = false;
    if (this.ctx && this.ctx.state === 'suspended') {
      withAudioDeadline(() => this.ctx.resume(), { timeoutMs: 1500 }).catch(() => {});
    }
  }

  /**
   * 按单句台词播放切片列表
   * @param {number} lineIndex
   * @param {Array<string|Object>} chunks
   * @param {Object} [options]
   * @param {string} [options.msgId]
   * @param {() => void} [options.onStart] 首个切片进入排定播放时刻时触发一次
   * @returns {Promise<'ended'|'interrupted'|'failed'|'timeout'|'network'|'blocked'>}
   * `'timeout'` 与 `'network'` 表示这次没有播出声音，且还不能确定文件已损坏或丢失。
   */
  async playLine(lineIndex, chunks, { msgId = null, onStart = null } = {}) {
    // 上一句的解码错误不能把后面单句的提示一起吞掉。
    this._reportedError = false;
    if (this._linePlayState) {
      this.stopLine({ fadeMs: 0 });
    }

    if (!Array.isArray(chunks) || chunks.length === 0) {
      return 'failed';
    }

    const ctx = this.ensureContext({ resumeIfSuspended: false });
    if (!ctx) return 'failed';
    const abortController = new AbortController();
    const lineGain = ctx.createGain();
    lineGain.gain.setValueAtTime(1.0, ctx.currentTime);
    lineGain.connect(this.masterGain);

    let lineResolve;
    const linePromise = new Promise((resolve) => {
      lineResolve = resolve;
    });

    const self = this;
    const lineState = {
      lineIndex,
      msgId,
      abortController,
      lineGain,
      settled: false,
      startTimer: null,
      started: false,
      resolve(outcome, { fadeMs = 0 } = {}) {
        if (lineState.settled) return;
        lineState.settled = true;
        clearTimeout(lineState.startTimer);
        abortController.abort();
        const sources = lineState.activeSources.splice(0);
        lineState.queue = [];
        const cleanup = () => {
          for (const { source, chunkGain } of sources) {
            source.onended = null;
            try { source.stop(); } catch (_) {}
            try { source.disconnect(); } catch (_) {}
            try { chunkGain.disconnect(); } catch (_) {}
          }
          try { lineGain.disconnect(); } catch (_) {}
        };
        if (fadeMs > 0 && sources.length) {
          const now = ctx.currentTime;
          try {
            lineGain.gain.cancelScheduledValues(now);
            lineGain.gain.setValueAtTime(lineGain.gain.value, now);
            lineGain.gain.linearRampToValueAtTime(0.0001, now + fadeMs / 1000);
          } catch (_) {}
          setTimeout(cleanup, fadeMs + 10);
        } else cleanup();
        if (self._linePlayState === lineState) {
          self._linePlayState = null;
        }
        lineResolve(outcome);
      },
      queue: [],
      activeSources: [],
      errors: [],
      playedCount: 0,
      totalChunks: chunks.length,
    };
    this._linePlayState = lineState;

    // Register ownership before the first await. A stop/session change must
    // cancel preparation even while the browser is waiting for an audio gesture.
    if (ctx.state === 'suspended' && !this.isPaused) {
      try {
        await withAudioDeadline(() => ctx.resume(), { signal: abortController.signal, timeoutMs: 1500 });
      } catch (_) {}
      if (lineState.settled) return linePromise;
      if (ctx.state !== 'running') {
        lineState.resolve(ctx.state === 'suspended' ? 'blocked' : 'failed');
        if (ctx.state === 'suspended') {
          this._reportError(new Error('浏览器尚未允许播放语音，请在 LOG 里再点一次「语音」。文字聊天可继续使用。'));
        }
        return linePromise;
      }
    }

    for (let i = 0; i < chunks.length; i++) {
      const c = chunks[i];
      const url = typeof c === 'string' ? c : (c?.audio_url || c?.url || '');
      const sentence = typeof c === 'object' ? (c?.sentence || '') : '';
      const emotion = typeof c === 'object' ? (c?.emotion || '') : '';
      const blob = typeof c === 'object' ? (c?.blob || null) : null;
      lineState.queue.push({
        index: i,
        url,
        sentence,
        emotion,
        blob,
        status: 'fetching',
        audioBuffer: null,
      });
    }

    function checkCompletion() {
      if (lineState.settled) return;
      if (lineState.queue.length === 0 && lineState.activeSources.length === 0) {
        if (lineState.playedCount > 0) {
          lineState.resolve('ended');
        } else {
          lineState.resolve(loadFailureOutcome(lineState.errors));
        }
      }
    }

    function scheduleLineNext() {
      if (lineState.settled) return;

      const now = ctx.currentTime;
      if (self.nextStartTime <= now) {
        self.nextStartTime = now + self.currentBufferLeadSec;
      }

      while (lineState.queue.length > 0) {
        const head = lineState.queue[0];
        if (head.status === 'fetching') {
          break;
        }
        lineState.queue.shift();
        if (head.status === 'failed' || !head.audioBuffer) {
          continue;
        }

        const buffer = head.audioBuffer;
        const startTime = self.nextStartTime;
        const duration = buffer.duration || 0;
        const fadeDur = Math.min(self.crossFadeMs, duration / 4);

        const source = ctx.createBufferSource();
        source.buffer = buffer;

        const chunkGain = ctx.createGain();
        chunkGain.gain.setValueAtTime(0.0001, startTime);
        chunkGain.gain.linearRampToValueAtTime(1.0, startTime + fadeDur);

        const fadeOutStart = Math.max(startTime + fadeDur, startTime + duration - fadeDur);
        chunkGain.gain.setValueAtTime(1.0, fadeOutStart);
        chunkGain.gain.linearRampToValueAtTime(0.0001, startTime + duration);

        source.connect(chunkGain);
        chunkGain.connect(lineGain);

        source.start(startTime);
        if (typeof onStart === 'function' && !lineState.started && lineState.startTimer === null) {
          const notifyStart = () => {
            lineState.startTimer = null;
            if (lineState.settled || self._linePlayState !== lineState) return;
            // Browser timers keep advancing while an interrupted/suspended
            // AudioContext does not. Only its running clock can release text.
            const remainingMs = (startTime - ctx.currentTime) * 1000;
            if (ctx.state !== 'running' || self.isPaused || remainingMs > 0) {
              const delay = ctx.state === 'running' && !self.isPaused
                ? Math.max(10, Math.min(50, remainingMs)) : 50;
              lineState.startTimer = setTimeout(notifyStart, delay);
              return;
            }
            lineState.started = true;
            try { onStart(); } catch (error) { console.error('Audio start notification failed:', error); }
          };
          lineState.startTimer = setTimeout(notifyStart, Math.max(0, (startTime - ctx.currentTime) * 1000));
        }
        self.nextStartTime = startTime + duration - (fadeDur / 2);

        const entry = { source, chunkGain, startTime, duration };
        lineState.activeSources.push(entry);
        lineState.playedCount++;

        source.onended = () => {
          lineState.activeSources = lineState.activeSources.filter((s) => s !== entry);
          try {
            source.disconnect();
            chunkGain.disconnect();
          } catch (_) {}
          checkCompletion();
        };
      }

      checkCompletion();
    }

    for (const item of [...lineState.queue]) {
      if (!item.url && !item.blob) {
        item.status = 'failed';
        scheduleLineNext();
        continue;
      }
      (async () => {
        try {
          if (!ctx) throw new Error('Web Audio not supported');
          const audioBuffer = await loadAudioBuffer(buffer => ctx.decodeAudioData(buffer), {
            url: item.url, blob: item.blob, signal: abortController.signal, timeoutMs: self.fetchTimeoutMs,
          });

          if (lineState.settled) return;
          item.audioBuffer = audioBuffer;
          item.status = 'ready';
          scheduleLineNext();
        } catch (err) {
          if (lineState.settled) return;
          if (err.code === 'AUDIO_DECODE_FAILED') self._reportError(err);
          lineState.errors.push(err);
          item.status = 'failed';
          scheduleLineNext();
        }
      })();
    }

    return linePromise;
  }

  /**
   * 停止当前句的播放
   * @param {Object} [options]
   * @param {number} [options.fadeMs=40]
   */
  stopLine({ fadeMs = 40 } = {}) {
    const state = this._linePlayState;
    if (!state || state.settled) return;

    if (this.ctx) {
      this.nextStartTime = this.ctx.currentTime;
    }
    state.resolve('interrupted', { fadeMs });
  }

  /**
   * 停掉当前单句播放并重置暂停、排期与错误状态。
   * @param {number} [fadeMs=40]
   */
  interrupt(fadeMs = 40) {
    this.stopLine({ fadeMs });
    this.isPaused = false;
    this._reportedError = false;
    this.nextStartTime = 0;
  }

  _reportError(error) {
    if (this._reportedError) return;
    this._reportedError = true;
    this.onError?.(error);
  }

  /**
   * 释放所有资源
   */
  async close() {
    if (typeof document !== 'undefined' && document.removeEventListener && this._onVisibilityChange) {
      document.removeEventListener('visibilitychange', this._onVisibilityChange);
      this._onVisibilityChange = null;
    }
    this.interrupt(10);
    if (this.ctx) {
      try {
        await this.ctx.close();
      } catch (_) {}
      this.ctx = null;
      this.masterGain = null;
      this.analyser = null;
    }
  }
}

export const streamAudioController = new StreamAudioController();
export default streamAudioController;

// Vite HMR：模块热更新/失效时释放单例，移除 visibilitychange 监听避免重复注册与泄漏
if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    streamAudioController.close().catch(() => {});
  });
}
