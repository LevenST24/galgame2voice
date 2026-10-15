/**
 * StreamAudioController — 现代 Web Audio API 流式音频控制器
 *
 * 核心特性:
 * 1. AudioContext 惰性初始化与用户手势自动唤醒 (resume)。
 * 2. 采样级高精度排期调度 (currentTime + source.start(nextScheduledTime))，实现切片间零死寂缝隙 (Gapless)。
 * 3. 12ms 微渐变包络 (Micro-fade Gain Envelope: 12ms attack & decay)，消除 PCM 直流偏置切片衔接爆音。
 * 4. 40ms 平滑线性渐出打断 (40ms Linear Fade-Out Interrupt)，彻底杜绝硬截断爆音与跨会话串音。
 * 5. 健壮队列自愈：单切片网络/解码异常自动跳过并推进后续切片，杜绝队列死锁。
 * 6. 与 CacheStorage / memory blob 缓存无缝衔接，极速秒开。
 */

import { loadAudioBlob } from './cache.js';
import { withAudioDeadline } from './audio_request.js';

export class StreamAudioController {
  constructor(options = {}) {
    this.crossFadeMs = options.crossFadeMs || 0.012; // 12ms micro-fade 消除瞬态爆音
    this.fetchTimeoutMs = options.fetchTimeoutMs || 15000;

    this.ctx = null;
    this.masterGain = null;
    this.masterGainNode = null;
    this.analyser = null;

    /** @type {Array<{id: string, sessionId: number, index: number, url: string, sentence: string, audioBuffer: AudioBuffer|null, status: string, duration?: number}>} */
    this.queue = [];
    this.activeSources = [];
    this.nextStartTime = 0;
    this.isPlaying = false;
    this.isPaused = false;
    this.isStreaming = false;
    this.currentSessionId = 0;
    this.currentMsgId = null;
    this.abortController = null;
    this._currentCtl = null;
    this._progressTimer = null;
    this._scheduleRetryTimer = null;
    this._seenChunkIndexes = new Set();
    this._reportedError = false;
    this._speaking = false;
    this._lastProgress = 0;

    this.underrunCount = 0;
    this.chunksPlayed = 0;
    this.minBufferLeadSec = 0.02; // 20ms baseline ultra-low latency
    this.maxBufferLeadSec = 0.12; // 120ms max jitter cushion
    this.currentBufferLeadSec = 0.02;
    this.consecutiveSmoothChunks = 0;

    this.onStatusChange = options.onStatusChange || null;
    this.onChunkStart = options.onChunkStart || null;
    this.onChunkEnd = options.onChunkEnd || null;
    this.onQueueEmpty = options.onQueueEmpty || null;
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
   * @returns {AudioContext|null}
   */
  ensureContext() {
    if (!this.ctx) {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) {
        console.error('[StreamAudioController] 当前浏览器不支持 Web Audio API');
        return null;
      }
      this.ctx = new AudioContextClass();
      this.masterGain = this.ctx.createGain();
      this.masterGainNode = this.masterGain;
      this.masterGain.gain.setValueAtTime(1.0, this.ctx.currentTime);
      this.masterGain.connect(this.ctx.destination);
    }
    if (this.ctx.state === 'suspended' && !this.isPaused) {
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

  /**
   * 暂停当前播放
   */
  pause() {
    this.isPaused = true;
    this._setSpeaking(false);
    if (this.ctx && this.ctx.state === 'running') {
      this.ctx.suspend().catch(() => {});
    }
    if (this._currentCtl && this._currentCtl.setPlaying) {
      this._currentCtl.setPlaying(false);
    }
  }

  /**
   * 恢复当前播放
   */
  resume() {
    this.isPaused = false;
    const sessionId = this.currentSessionId;
    if (this.ctx && this.ctx.state === 'suspended') {
      withAudioDeadline(() => this.ctx.resume(), { signal: this.abortController?.signal, timeoutMs: 1500 }).then(() => {
        if (sessionId !== this.currentSessionId || this.isPaused) return;
        if (this._currentCtl && this._currentCtl.setPlaying) {
          this._currentCtl.setPlaying(true);
        }
        this._scheduleNext();
      }).catch(() => {
        if (sessionId === this.currentSessionId && !this.isPaused) this._playbackBlocked();
      });
    } else {
      if (this._currentCtl && this._currentCtl.setPlaying) {
        this._currentCtl.setPlaying(true);
      }
      this._scheduleNext();
    }
  }

  /**
   * 绑定或更新当前播放的 UI 控件回调
   * @param {Object} ctl - { setPlaying, setProgress, setLoading }
   * @param {string} [msgId]
   */
  attachControl(ctl, msgId = null) {
    this._currentCtl = ctl;
    if (msgId) this.currentMsgId = msgId;
    if (ctl && ctl.setPlaying) {
      ctl.setPlaying(this.isPlaying && !this.paused);
    }
    ctl?.setProgress?.(this._lastProgress);
  }

  /**
   * 开始一个新的流式/批量播放会话
   * @param {string} msgId
   * @param {Object} [ctl]
   */
  startSession(msgId, ctl = null, { streaming = false } = {}) {
    this.currentSessionId += 1; // 使旧 interrupt 的延迟回调失效，避免打断新会话开头
    this.interrupt(0);
    this.ensureContext();
    const gainNode = this.masterGainNode || this.masterGain;
    if (gainNode && gainNode.gain && this.ctx) {
      try {
        gainNode.gain.cancelScheduledValues(this.ctx.currentTime);
        gainNode.gain.setValueAtTime(1.0, this.ctx.currentTime);
      } catch (_) {}
    }
    this.currentMsgId = msgId;
    this._currentCtl = ctl;
    this.isPaused = false;
    this.isPlaying = true;
    this.isStreaming = streaming;
    this.nextStartTime = 0;
    this.consecutiveSmoothChunks = 0;
    this._lastProgress = 0;
    if (this.underrunCount === 0) {
      this.currentBufferLeadSec = this.minBufferLeadSec;
    }
    if (ctl && ctl.setPlaying) ctl.setPlaying(true);
    if (ctl && ctl.setProgress) ctl.setProgress(0);
  }

  /**
   * 追加待播放切片（支持流式推送或静态批量列表）
   * @param {Object} options
   * @param {string} options.url - 音频切片 URL
   * @param {number} options.index - 切片索引
   * @param {string} [options.sentence] - 切片日文/中文文本
   * @param {Object} [options.ctl] - UI 控制句柄 { setPlaying, setProgress }
   * @param {Blob} [options.blob] - 本地预加载的 Blob（若有）
   * @param {number} [options.totalExpected] - 预期总切片数（用于计算进度）
   */
  async enqueueChunk({ url, index, sentence = '', emotion = '', ctl = null, blob = null, totalExpected = 0 }) {
    this.ensureContext();
    if (ctl) this._currentCtl = ctl;

    const sessionId = this.currentSessionId;
    const item = {
      id: `chunk_${sessionId}_${index}_${Date.now()}`,
      sessionId,
      index,
      url,
      sentence,
      emotion,
      audioBuffer: null,
      status: 'fetching',
      totalExpected: totalExpected || (index + 1),
    };

    // Two identical sentences can legitimately share a cached URL.
    // Deduplicate events by their position, including already finished chunks.
    if (this._seenChunkIndexes.has(index)) return;
    this._seenChunkIndexes.add(index);

    this.queue.push(item);

    try {
      const ctx = this.ctx;
      const audioBuffer = await withAudioDeadline(async signal => {
        if (!ctx) throw new Error('当前浏览器无法播放语音，请使用新版 Chrome 或 Edge。');
        const audioBlob = blob || await loadAudioBlob(url, { signal, timeoutMs: this.fetchTimeoutMs });
        const arrayBuffer = await audioBlob.arrayBuffer();
        if (signal.aborted) throw new DOMException('播放已取消', 'AbortError');
        return ctx.decodeAudioData(arrayBuffer);
      }, { signal: this.abortController?.signal, timeoutMs: this.fetchTimeoutMs });
      if (sessionId !== this.currentSessionId) return;

      item.audioBuffer = audioBuffer;
      item.duration = audioBuffer.duration;
      item.status = 'ready';
      this._scheduleNext();
    } catch (err) {
      if (err.name === 'AbortError' || sessionId !== this.currentSessionId) return;
      console.warn('[StreamAudioController] 音频切片加载/解码失败，平滑跳过:', url, err);
      // 容错降级：移出失败切片，排期后续就绪切片，避免死锁
      const idx = this.queue.indexOf(item);
      if (idx !== -1) this.queue.splice(idx, 1);
      this._reportError(err);
      this._scheduleNext();
      this._maybeQueueEmpty();
    }
  }

  /**
   * 批量播放已有完整消息的分句切片列表
   * @param {Array<string|{url:string,sentence?:string,emotion?:string}>} chunks
   *   纯 URL 数组，或带句子/情绪元数据的对象数组（后者可让立绘逐句切换）
   * @param {Object} [ctl] - UI 控制器
   * @param {string} [msgId]
   */
  async playChunks(chunks, ctl = null, msgId = null) {
    if (!chunks || chunks.length === 0) return;
    this.startSession(msgId, ctl);
    const sessionId = this.currentSessionId;
    const total = chunks.length;
    for (let i = 0; i < chunks.length; i++) {
      if (sessionId !== this.currentSessionId) break;
      const c = chunks[i];
      const meta = typeof c === 'string' ? { url: c } : (c || {});
      this.enqueueChunk({
        url: meta.url,
        index: i,
        sentence: meta.sentence || '',
        emotion: meta.emotion || '',
        blob: meta.blob || null,
        ctl,
        totalExpected: total,
      });
    }
  }

  /**
   * 调度器：在 Web Audio 时间线上精确排期已就绪切片
   */
  _scheduleNext() {
    if (!this.ctx) return;
    if (this.isPaused) return;
    if (!this.queue.length) return;

    if (this.ctx.state !== 'running') {
      // Autoplay can remain blocked until a gesture: show a paused/retry state
      // instead of polling resume() indefinitely in the background.
      const sessionId = this.currentSessionId;
      withAudioDeadline(() => this.ctx.resume(), { signal: this.abortController?.signal, timeoutMs: 1500 }).then(() => {
        if (sessionId !== this.currentSessionId || this.isPaused) return;
        if (this.ctx?.state === 'running') this._scheduleNext();
        else this._playbackBlocked();
      }).catch(() => {
        if (sessionId === this.currentSessionId) this._playbackBlocked();
      });
      return;
    }

    const now = this.ctx.currentTime;
    if (this.masterGain && this.masterGain.gain) {
      try {
        if (this.masterGain.gain.value < 0.5) {
          this.masterGain.gain.cancelScheduledValues(now);
          this.masterGain.gain.setValueAtTime(1.0, now);
        }
      } catch (_) {}
    }
    if (this.nextStartTime < now) {
      if (this.nextStartTime > 0 && this.isPlaying && this.activeSources.length === 0) {
        this.underrunCount++;
        // 欠载自适应：缓冲裕度递增 20ms（上限 120ms），消除后续切片抖动
        this.currentBufferLeadSec = Math.min(this.maxBufferLeadSec, this.currentBufferLeadSec + 0.02);
        this.consecutiveSmoothChunks = 0;
      }
      // 预留自适应 Jitter 缓冲给音频驱动混音
      this.nextStartTime = now + this.currentBufferLeadSec;
    }

    while (this.queue.length > 0) {
      const nextItem = this.queue[0];
      if (nextItem.status !== 'ready' || !nextItem.audioBuffer) {
        break; // 队头仍在下载或解码，等待后续 enqueueChunk 完成触发
      }

      this.queue.shift();
      const buffer = nextItem.audioBuffer;
      const startTime = this.nextStartTime;
      const duration = buffer.duration;
      // 保护超短切片 (<= 50ms)
      const fadeDur = Math.min(this.crossFadeMs, duration / 4);

      const source = this.ctx.createBufferSource();
      source.buffer = buffer;

      // 独立切片增益节点：负责 12ms 头尾微渐变 (Micro-fade)，彻底消除直流偏置阶跃爆音
      const chunkGain = this.ctx.createGain();
      chunkGain.gain.setValueAtTime(0.0001, startTime);
      chunkGain.gain.linearRampToValueAtTime(1.0, startTime + fadeDur);

      const fadeOutStart = Math.max(startTime + fadeDur, startTime + duration - fadeDur);
      chunkGain.gain.setValueAtTime(1.0, fadeOutStart);
      chunkGain.gain.linearRampToValueAtTime(0.0001, startTime + duration);

      source.connect(chunkGain);
      chunkGain.connect(this.masterGain);

      source.start(startTime);
      this.chunksPlayed++;
      this.consecutiveSmoothChunks++;
      // 连续 5 个切片平稳排期播放无欠载时，平滑递减 5ms 回退向 20ms 基线，恢复极低延迟
      if (this.consecutiveSmoothChunks >= 5 && this.currentBufferLeadSec > this.minBufferLeadSec) {
        this.currentBufferLeadSec = Math.max(this.minBufferLeadSec, this.currentBufferLeadSec - 0.005);
        this.consecutiveSmoothChunks = 0;
      }
      const entry = { source, chunkGain, item: nextItem, startTime, duration, notified: false };
      this.activeSources.push(entry);

      this.isPlaying = true;
      if (this._currentCtl && this._currentCtl.setPlaying) {
        this._currentCtl.setPlaying(true);
      }

      // 采样级无缝步进：下一切片起始时刻精确衔接当前切片尾部减去交叉渐变量
      this.nextStartTime = startTime + duration - (fadeDur / 2);

      this._startProgressTimer();

      source.onended = () => {
        this.activeSources = this.activeSources.filter((s) => s.source !== source);
        try {
          source.disconnect();
          chunkGain.disconnect();
        } catch (_) {}

        if (nextItem.sessionId === this.currentSessionId) {
          if (this.onChunkEnd) this.onChunkEnd(nextItem);
          this._maybeQueueEmpty();
        }
      };
    }
  }

  /**
   * 检查队列与活动源是否全部结束
   */
  _maybeQueueEmpty() {
    if (!this.activeSources.length) this._setSpeaking(false);
    if (this.activeSources.length === 0 && this.queue.length === 0) {
      clearInterval(this._progressTimer);
      this._progressTimer = null;
      if (this.isStreaming) return;
      this.isPlaying = false;
      this.isPaused = false;
      this.nextStartTime = 0;
      this._lastProgress = 1;
      if (this._currentCtl) {
        if (this._currentCtl.setPlaying) this._currentCtl.setPlaying(false);
        if (this._currentCtl.setProgress) this._currentCtl.setProgress(1);
      }
      if (this.onQueueEmpty) this.onQueueEmpty();
    }
  }

  /**
   * 40ms 平滑线性渐出打断 (Smooth Interrupt)
   * 立即中止网络拉取、停止活动源并渐出静音，杜绝瞬间截断造成的硬件爆破杂音
   * @param {number} fadeMs 渐出毫秒数，默认 40ms
   */
  interrupt(fadeMs = 40) {
    this.currentSessionId += 1;
    this.currentMsgId = null;
    this.isStreaming = false;

    if (this.abortController) {
      try {
        this.abortController.abort();
      } catch (_) {}
    }
    this.abortController = new AbortController();
    this.queue = [];
    this._seenChunkIndexes.clear();
    this._reportedError = false;
    clearInterval(this._progressTimer);
    this._progressTimer = null;
    this._setSpeaking(false);

    if (this._scheduleRetryTimer) {
      clearTimeout(this._scheduleRetryTimer);
      this._scheduleRetryTimer = null;
    }

    if (this.ctx && this.masterGain && this.activeSources.length > 0) {
      if (fadeMs <= 0) {
        // 瞬间完全打断，立即停止所有源并重置音量
        const sourcesToStop = [...this.activeSources];
        this.activeSources = [];
        this.nextStartTime = 0;
        this.isPlaying = false;
        this.isPaused = false;
        try {
          this.masterGain.gain.cancelScheduledValues(this.ctx.currentTime);
          this.masterGain.gain.setValueAtTime(1.0, this.ctx.currentTime);
        } catch (_) {}
        sourcesToStop.forEach(({ source, chunkGain }) => {
          try {
            source.stop();
            source.disconnect();
            chunkGain.disconnect();
          } catch (_) {}
        });
      } else {
        const now = this.ctx.currentTime;
        const fadeSec = fadeMs / 1000;

        try {
          this.masterGain.gain.cancelScheduledValues(now);
          this.masterGain.gain.setValueAtTime(this.masterGain.gain.value, now);
          this.masterGain.gain.linearRampToValueAtTime(0.0001, now + fadeSec);
        } catch (_) {}

        const sourcesToStop = [...this.activeSources];
        this.activeSources = [];
        this.nextStartTime = 0;
        this.isPlaying = false;
        this.isPaused = false;

        const sessionAtInterrupt = this.currentSessionId;
        setTimeout(() => {
          sourcesToStop.forEach(({ source, chunkGain }) => {
            try {
              source.stop();
              source.disconnect();
              chunkGain.disconnect();
            } catch (_) {}
          });
          // 恢复 masterGain 为 1.0 备下次播放；
          // 仅当期间没有新会话开始（startSession 会递增 currentSessionId）时才恢复，
          // 否则新会话的增益已由 startSession 自行重置，此处再动会产生爆音
          if (this.currentSessionId === sessionAtInterrupt && this.masterGain && this.ctx) {
            try {
              this.masterGain.gain.cancelScheduledValues(this.ctx.currentTime);
              this.masterGain.gain.setValueAtTime(1.0, this.ctx.currentTime);
            } catch (_) {}
          }
        }, fadeMs + 10);
      }
    } else {
      this.activeSources = [];
      this.nextStartTime = 0;
      this.isPlaying = false;
      this.isPaused = false;
      if (this.masterGain && this.ctx) {
        try {
          this.masterGain.gain.cancelScheduledValues(this.ctx.currentTime);
          this.masterGain.gain.setValueAtTime(1.0, this.ctx.currentTime);
        } catch (_) {}
      }
    }

    if (this._currentCtl && this._currentCtl.setPlaying) {
      this._currentCtl.setPlaying(false);
    }
    this._currentCtl = null;
  }

  finishInput() {
    this.isStreaming = false;
    this._maybeQueueEmpty();
  }

  _reportError(error) {
    if (this._reportedError) return;
    this._reportedError = true;
    this.onError?.(error);
  }

  _playbackBlocked() {
    this.pause();
    this._reportError(new Error('浏览器暂停了语音播放，请点击消息旁的播放按钮继续。'));
  }

  _setSpeaking(value) {
    if (value === this._speaking) return;
    this._speaking = value;
    this.onStatusChange?.(value);
  }

  _startProgressTimer() {
    if (this._progressTimer) return;
    this._progressTimer = setInterval(() => {
      // Audio time stops on pause. Wall-clock timers would advance portraits
      // and dialogue while the corresponding sentence was still inaudible.
      if (!this.ctx || this.isPaused || this.ctx.state !== 'running') return;
      const now = this.ctx.currentTime;
      const audible = this.activeSources.filter(entry =>
        entry.item.sessionId === this.currentSessionId && entry.startTime <= now &&
        now < entry.startTime + entry.duration);
      this._setSpeaking(audible.length > 0);
      for (const entry of audible) {
        if (!entry.notified) {
          entry.notified = true;
          this.onChunkStart?.(entry.item);
        }
        const { item, startTime, duration } = entry;
        if (item.totalExpected > 0) {
          const fraction = (item.index + Math.min(1, (now - startTime) / duration)) / item.totalExpected;
          this._lastProgress = Math.min(1, fraction);
          this._currentCtl?.setProgress?.(this._lastProgress);
        }
      }
    }, 40);
  }

  /**
   * 返回当前播放时延与缓冲健康度指标
   */
  getStats() {
    return {
      isPlaying: this.isPlaying,
      isPaused: this.paused,
      underrunCount: this.underrunCount,
      chunksPlayed: this.chunksPlayed,
      bufferLeadMs: Math.round(this.currentBufferLeadSec * 1000),
      activeSources: this.activeSources.length,
      queueLength: this.queue.length,
      currentTime: this.ctx ? this.ctx.currentTime : 0,
      nextStartTime: this.nextStartTime,
    };
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
