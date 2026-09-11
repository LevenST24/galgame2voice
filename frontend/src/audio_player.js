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

    this.onStatusChange = options.onStatusChange || null;
    this.onChunkStart = options.onChunkStart || null;
    this.onChunkEnd = options.onChunkEnd || null;
    this.onQueueEmpty = options.onQueueEmpty || null;
    this.onError = options.onError || null;
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
    this.interrupt(40);
    this.ensureContext();
    const gainNode = this.masterGainNode || this.masterGain;
    if (gainNode && gainNode.gain) {
      try {
        gainNode.gain.cancelScheduledValues(0);
        gainNode.gain.setValueAtTime(1.0, 0);
      } catch (_) {}
    }
    this.currentMsgId = msgId;
    this._currentCtl = ctl;
    this.isPaused = false;
    this.isPlaying = true;
    this.nextStartTime = 0;
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
  async enqueueChunk({ url, index, sentence = '', ctl = null, blob = null, totalExpected = 0 }) {
    this.ensureContext();
    if (ctl) this._currentCtl = ctl;

    const sessionId = this.currentSessionId;
    const item = {
      id: `chunk_${sessionId}_${index}_${Date.now()}`,
      sessionId,
      index,
      url,
      sentence,
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
   * @param {string[]} urls - 切片 URL 数组
   * @param {Object} [ctl] - UI 控制器
   * @param {string} [msgId]
   */
  async playChunks(urls, ctl = null, msgId = null) {
    if (!urls || urls.length === 0) return;
    this.startSession(msgId, ctl);
    const sessionId = this.currentSessionId;
    const total = urls.length;
    for (let i = 0; i < urls.length; i++) {
      if (sessionId !== this.currentSessionId) break;
      this.enqueueChunk({
        url: urls[i],
        index: i,
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
    if (this.nextStartTime < now) {
      // 预留 20ms 给音频驱动混音缓冲
      this.nextStartTime = now + 0.02;
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
   * 40ms 平滑线性渐出打断 (Smooth Interrupt)
   * 立即中止网络拉取、停止活动源并渐出静音，杜绝瞬间截断造成的硬件爆破杂音
   * @param {number} fadeMs 渐出毫秒数，默认 40ms
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
      const now = this.ctx.currentTime;
      const fadeSec = fadeMs / 1000;

      // 40ms 线性平滑渐出至 0.0001
      try {
        this.masterGain.gain.setValueAtTime(this.masterGain.gain.value, now);
        this.masterGain.gain.linearRampToValueAtTime(0.0001, now + fadeSec);
      } catch (_) {}

      const sourcesToStop = [...this.activeSources];
      this.activeSources = [];
      this.nextStartTime = 0;
      this.isPlaying = false;
      this.isPaused = false;

      setTimeout(() => {
        sourcesToStop.forEach(({ source, chunkGain }) => {
          try {
            source.stop();
            source.disconnect();
            chunkGain.disconnect();
          } catch (_) {}
        });
        // 恢复 masterGain 为 1.0 备下次播放
        if (this.masterGain && this.ctx) {
          try {
            this.masterGain.gain.setValueAtTime(1.0, this.ctx.currentTime);
          } catch (_) {}
        }
      }, fadeMs + 10);
    } else {
      this.activeSources = [];
      this.nextStartTime = 0;
      this.isPlaying = false;
      this.isPaused = false;
    }

    if (this._currentCtl && this._currentCtl.setPlaying) {
      this._currentCtl.setPlaying(false);
    }
    this._currentCtl = null;
  }

  /**
   * 释放所有资源
   */
  async close() {
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
