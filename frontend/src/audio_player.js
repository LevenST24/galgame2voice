/**
 * StreamAudioController — 现代 Web Audio API 流式音频控制器
 *
 * 核心特性:
 * 1. AudioContext 惰性初始化与用户手势自动唤醒 (resume)。
 * 2. 采样级高精度排期调度 (currentTime + source.start(nextScheduledTime))，实现切片间零死寂缝隙 (Gapless)。
 * 3. 12ms 微渐变包络 (Micro-fade Gain Envelope: 12ms attack & decay)，消除 PCM 直流偏置切片衔接爆音。
 * 4. 打断默认 40ms 线性渐出 (Linear Fade-Out Interrupt)，抑制常规打断的硬截断爆音；
 *    渐出时长由调用方 fadeMs 决定，fadeMs <= 0 时改为立即硬停（startSession 即以 0 调用，
 *    10ms 也有一处调用方），跨会话串音则由 currentSessionId 递增与清空队列阻断。
 * 5. 健壮队列自愈：单切片网络/解码异常自动跳过并推进后续切片，杜绝队列死锁。
 * 6. 与 CacheStorage / memory blob 缓存无缝衔接，极速秒开。
 */

import { getCachedAudioBlob } from './cache.js';

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
    this.currentSessionId = 0;
    this.currentMsgId = null;
    this.abortController = null;
    this._currentCtl = null;
    this._progressTimer = null;
    this._scheduleRetryTimer = null;

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
    if (this.ctx.state === 'suspended') {
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
    if (this.ctx && this.ctx.state === 'suspended') {
      this.ctx.resume().then(() => {
        if (this._currentCtl && this._currentCtl.setPlaying) {
          this._currentCtl.setPlaying(true);
        }
        this._scheduleNext();
      }).catch(() => {});
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
  }

  /**
   * 开始一个新的流式/批量播放会话
   * @param {string} msgId
   * @param {Object} [ctl]
   */
  startSession(msgId, ctl = null) {
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
    this.nextStartTime = 0;
    this.consecutiveSmoothChunks = 0;
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
   * @param {string} [options.sentence] - 切片对应的日文台词文本
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

    // 重复去重：同一会话已排队或正在播放的切片跳过
    if (
      this.queue.some((q) => q.sessionId === sessionId && q.url === url) ||
      this.activeSources.some((s) => s.item && s.item.sessionId === sessionId && s.item.url === url)
    ) {
      return;
    }

    this.queue.push(item);

    try {
      let arrayBuffer;
      if (blob) {
        arrayBuffer = await blob.arrayBuffer();
      } else {
        // 先检查本地 CacheStorage，有则直接复用
        const cachedBlob = await getCachedAudioBlob(url);
        if (cachedBlob && cachedBlob.size > 0) {
          arrayBuffer = await cachedBlob.arrayBuffer();
        } else {
          const signal = this.abortController ? this.abortController.signal : undefined;
          const res = await fetch(url, { signal });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          arrayBuffer = await res.arrayBuffer();
        }
      }

      if (sessionId !== this.currentSessionId) return; // 会话已中断

      const audioBuffer = await this.ctx.decodeAudioData(arrayBuffer);
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

    if (this.ctx.state !== 'running') {
      this.ctx.resume().catch(() => {});
      if (this._scheduleRetryTimer) clearTimeout(this._scheduleRetryTimer);
      this._scheduleRetryTimer = setTimeout(() => {
        this._scheduleRetryTimer = null;
        this._scheduleNext();
      }, 100);
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

      // 独立切片增益节点：负责头尾微渐变 (Micro-fade)，抑制切片衔接处的直流偏置阶跃。
      // 渐变时长为 min(crossFadeMs, duration/4)，切片短于 48ms 时不足 12ms；且只覆盖正常
      // 排期衔接：interrupt(fadeMs <= 0)（startSession 即如此调用）会立即 stop 并 disconnect
      // 活动源，此刻波形相位任意，仍会产生阶跃爆音（见下方 interrupt 注释）。
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
      const entry = { source, chunkGain, item: nextItem, startTime, duration };
      this.activeSources.push(entry);

      this.isPlaying = true;
      if (this._currentCtl && this._currentCtl.setPlaying) {
        this._currentCtl.setPlaying(true);
      }

      // 采样级无缝步进：下一切片起始时刻精确衔接当前切片尾部减去交叉渐变量
      this.nextStartTime = startTime + duration - (fadeDur / 2);

      const delayMs = Math.max(0, (startTime - now) * 1000);
      setTimeout(() => {
        if (nextItem.sessionId === this.currentSessionId && this.isPlaying) {
          if (this.onChunkStart) this.onChunkStart(nextItem);
          // 计算并上报估算进度
          if (this._currentCtl && this._currentCtl.setProgress && nextItem.totalExpected > 0) {
            const frac = nextItem.index / nextItem.totalExpected;
            this._currentCtl.setProgress(Math.min(1, frac));
          }
        }
      }, delayMs);

      source.onended = () => {
        this.activeSources = this.activeSources.filter((s) => s.source !== source);
        try {
          source.disconnect();
          chunkGain.disconnect();
        } catch (_) {}

        if (nextItem.sessionId === this.currentSessionId) {
          if (this.onChunkEnd) this.onChunkEnd(nextItem);
        }
        this._maybeQueueEmpty();
      };
    }
  }

  /**
   * 检查队列与活动源是否全部结束
   */
  _maybeQueueEmpty() {
    if (this.activeSources.length === 0 && this.queue.length === 0) {
      this.isPlaying = false;
      this.isPaused = false;
      this.nextStartTime = 0;
      if (this._currentCtl) {
        if (this._currentCtl.setPlaying) this._currentCtl.setPlaying(false);
        if (this._currentCtl.setProgress) this._currentCtl.setProgress(1);
      }
      if (this.onQueueEmpty) this.onQueueEmpty();
    }
  }

  /**
   * 线性渐出打断 (Smooth Interrupt)
   * 中止网络拉取并清空播放队列。fadeMs > 0 时先线性渐出 masterGain 再停止活动源；
   * fadeMs <= 0 时跳过渐出、立即 stop 所有活动源并把音量复位（即硬截断，仍可能产生瞬态爆音）。
   * @param {number} fadeMs 渐出毫秒数，默认 40ms；startSession() 以 0 调用
   */
  interrupt(fadeMs = 40) {
    this.currentSessionId += 1;
    this.currentMsgId = null;

    if (this.abortController) {
      try {
        this.abortController.abort();
      } catch (_) {}
    }
    this.abortController = new AbortController();
    this.queue = [];

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
