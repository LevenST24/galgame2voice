/**
 * StreamingAudioPlayer — 低延迟 Web Audio 流式播放器 (v2 重写)
 *
 * v2 修复的核心缺陷:
 *  1. 队列死锁: 任一句子 fetch/解码失败会永久卡死整个播放队列。
 *     现在: 失败的 chunk 被移出队列并触发 onError, 后续句子继续无缝播放。
 *  2. AudioContext suspended 恢复不完整: resume() 现在 await + 失败重试,
 *     suspended 状态下调度器自动延迟重试, 不再用冻结的 currentTime 排期。
 *  3. HTML5 回退乱序: 回退路径不再立即旁路播放, 失败句子直接跳过并上报,
 *     保证队列顺序与 onQueueEmpty 语义正确。
 *  4. fetch 增加超时(15s)与一次重试, 网络抖动不再直接断流。
 */

class StreamingAudioPlayer {
    constructor(options = {}) {
        this.crossFadeDuration = options.crossFadeDuration || 0.025;
        this.fetchTimeoutMs = options.fetchTimeoutMs || 15000;
        this.maxFetchRetries = 1; // 1 retry on transient network errors

        this.audioCtx = null;
        this.masterGain = null;
        this.analyser = null;
        this.freqData = null;

        /** @type {Array<{id:number,index:number,url:string,sentence:string,audioBuffer:AudioBuffer|null,status:string,sessionId:number}>} */
        this.queue = [];
        this.activeSources = [];
        this.nextStartTime = 0;
        this.isPlaying = false;
        this.isMuted = false;
        this.volume = 1.0;
        this.currentSessionId = 0;
        this._scheduleRetryTimer = null;
        this._resumePromise = null;

        this.equalizerElement = options.equalizerElement || null;
        this.animFrameId = null;

        this.onStatusChange = options.onStatusChange || null;
        this.onChunkStart = options.onChunkStart || null;
        this.onChunkEnd = options.onChunkEnd || null;
        this.onQueueEmpty = options.onQueueEmpty || null;
        this.onError = options.onError || null;
    }

    /**
     * Initializes AudioContext & AnalyserNode. Safe to call repeatedly.
     * Returns the context, or null if Web Audio is unavailable.
     */
    initContext() {
        if (!this.audioCtx) {
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextClass) {
                console.error('[AudioPlayer] Web Audio API 不受当前浏览器支持');
                return null;
            }
            this.audioCtx = new AudioContextClass();
            this.masterGain = this.audioCtx.createGain();
            this.masterGain.gain.setValueAtTime(this.isMuted ? 0 : this.volume, this.audioCtx.currentTime);

            this.analyser = this.audioCtx.createAnalyser();
            this.analyser.fftSize = 64;
            this.analyser.smoothingTimeConstant = 0.8;
            this.freqData = new Uint8Array(this.analyser.frequencyBinCount);

            this.masterGain.connect(this.analyser);
            this.analyser.connect(this.audioCtx.destination);
        }
        if (this.audioCtx.state === 'suspended') {
            this._ensureResumed();
        }
        return this.audioCtx;
    }

    /**
     * Resumes a suspended AudioContext with verification. Concurrent calls share
     * one in-flight resume promise. Failures are logged, never thrown.
     */
    async _ensureResumed() {
        if (!this.audioCtx || this.audioCtx.state !== 'suspended') return true;
        if (this._resumePromise) return this._resumePromise;
        this._resumePromise = this.audioCtx.resume()
            .then(() => {
                const ok = this.audioCtx && this.audioCtx.state === 'running';
                if (!ok) console.warn('[AudioPlayer] resume() 完成但上下文未进入 running 状态');
                return ok;
            })
            .catch((e) => {
                console.warn('[AudioPlayer] AudioContext resume 失败(可能需要用户交互):', e);
                return false;
            })
            .finally(() => {
                this._resumePromise = null;
            });
        return this._resumePromise;
    }

    getFrequencyData() {
        if (this.analyser && this.isPlaying) {
            this.analyser.getByteFrequencyData(this.freqData);
            return this.freqData;
        }
        return null;
    }

    attachEqualizer(el) {
        this.equalizerElement = el;
    }

    _startVisualizerLoop() {
        if (this.animFrameId) return;
        const render = () => {
            if (!this.isPlaying) {
                this._resetEqualizerBars();
                this.animFrameId = null;
                return;
            }
            const data = this.getFrequencyData();
            if (data && this.equalizerElement) {
                const bars = this.equalizerElement.querySelectorAll('.bar');
                if (bars && bars.length > 0) {
                    const step = Math.max(1, Math.floor(data.length / bars.length));
                    bars.forEach((bar, i) => {
                        const val = data[i * step] || 0;
                        const heightPx = Math.max(4, Math.round(4 + (val / 255) * 18));
                        bar.style.height = `${heightPx}px`;
                    });
                }
            }
            this.animFrameId = requestAnimationFrame(render);
        };
        this.animFrameId = requestAnimationFrame(render);
    }

    _stopVisualizerLoop() {
        if (this.animFrameId) {
            cancelAnimationFrame(this.animFrameId);
            this.animFrameId = null;
        }
        this._resetEqualizerBars();
    }

    _resetEqualizerBars() {
        if (this.equalizerElement) {
            const bars = this.equalizerElement.querySelectorAll('.bar');
            bars.forEach(bar => { bar.style.height = '4px'; });
        }
    }

    /**
     * fetch with timeout + bounded retry. Throws on final failure.
     */
    async _fetchWithRetry(url, sessionId) {
        let lastErr = null;
        for (let attempt = 0; attempt <= this.maxFetchRetries; attempt++) {
            if (this.currentSessionId !== sessionId) {
                throw new Error('interrupted');
            }
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), this.fetchTimeoutMs);
            try {
                const response = await fetch(url, { signal: controller.signal });
                if (!response.ok) {
                    throw new Error('HTTP ' + response.status);
                }
                return await response.arrayBuffer();
            } catch (err) {
                lastErr = err;
                if (err.message === 'interrupted') throw err;
                if (attempt < this.maxFetchRetries) {
                    console.warn(`[AudioPlayer] 音频拉取失败 (第${attempt + 1}次): ${err.message}，重试中...`);
                    await new Promise(r => setTimeout(r, 600));
                }
            } finally {
                clearTimeout(timer);
            }
        }
        throw lastErr || new Error('fetch failed');
    }

    /**
     * Enqueues an incoming audio chunk URL for playback.
     * A failed chunk is removed from the queue (after retry), reported via
     * onError, and playback continues with the next chunk.
     * @param {Object} chunk - { index, audio_url, sentence }
     */
    async enqueue(chunk) {
        this.initContext();
        if (!this.audioCtx) return;

        const sessionId = this.currentSessionId;
        const item = {
            id: 'chunk_' + sessionId + '_' + chunk.index + '_' + Date.now(),
            sessionId: sessionId,
            index: chunk.index,
            url: chunk.audio_url,
            sentence: chunk.sentence || '',
            audioBuffer: null,
            status: 'fetching'
        };

        // Dedup: same URL already queued or playing → skip.
        if (this.queue.some(q => q.url === item.url) ||
            this.activeSources.some(s => s.item && s.item.url === item.url)) {
            return;
        }

        this.queue.push(item);
        this._notifyStatus(`缓冲中: 句子 #${chunk.index + 1}`, this.isPlaying);

        try {
            const arrayBuffer = await this._fetchWithRetry(item.url, sessionId);
            if (this.currentSessionId !== sessionId) return;

            try {
                const audioBuffer = await this.audioCtx.decodeAudioData(arrayBuffer);
                if (this.currentSessionId !== sessionId) return;
                item.audioBuffer = audioBuffer;
                item.status = 'ready';
                this._scheduleNext();
            } catch (decodeErr) {
                // 解码失败(音频损坏): 移出队列并跳过, 不再使用乱序的 HTML5 旁路播放。
                console.warn('[AudioPlayer] 音频解码失败, 跳过该句:', item.url, decodeErr);
                this._dropItem(item, `解码失败: ${decodeErr.message || decodeErr}`);
            }
        } catch (err) {
            if (this.currentSessionId === sessionId && err.message !== 'interrupted') {
                console.error('[AudioPlayer] 音频拉取失败, 跳过该句:', item.url, err);
                this._dropItem(item, `拉取失败: ${err.message || err}`);
            }
        }
    }

    /**
     * Removes a failed item from the queue, reports the error, and keeps the
     * queue flowing (this is the core deadlock fix).
     */
    _dropItem(item, reason) {
        const idx = this.queue.indexOf(item);
        if (idx !== -1) this.queue.splice(idx, 1);
        if (this.onError) {
            try { this.onError(new Error(reason), item); } catch (e) { console.error(e); }
        }
        // Queue may now have a ready head — keep playing.
        this._scheduleNext();
        this._maybeQueueEmpty();
    }

    /** Fires onQueueEmpty when nothing is active and nothing is queued. */
    _maybeQueueEmpty() {
        if (this.activeSources.length === 0 && this.queue.length === 0) {
            if (this.isPlaying) {
                this.isPlaying = false;
                this._stopVisualizerLoop();
                this._notifyStatus('就绪', false);
            }
            if (this.onQueueEmpty) this.onQueueEmpty();
        }
    }

    /**
     * Schedules ready audio buffers into the Web Audio timeline.
     * If the context is suspended, retries shortly instead of scheduling
     * against a frozen clock.
     */
    _scheduleNext() {
        if (!this.audioCtx) return;

        if (this.audioCtx.state !== 'running') {
            this._ensureResumed();
            if (this._scheduleRetryTimer) clearTimeout(this._scheduleRetryTimer);
            this._scheduleRetryTimer = setTimeout(() => {
                this._scheduleRetryTimer = null;
                this._scheduleNext();
            }, 120);
            return;
        }

        const now = this.audioCtx.currentTime;
        if (this.nextStartTime < now) {
            this.nextStartTime = now + 0.02;
        }

        while (this.queue.length > 0) {
            const nextItem = this.queue[0];
            if (nextItem.status !== 'ready' || !nextItem.audioBuffer) {
                break; // head still downloading/decoding — enqueue() will re-trigger
            }

            this.queue.shift();
            const buffer = nextItem.audioBuffer;
            const startTime = this.nextStartTime;
            const duration = buffer.duration;
            // Fade duration guarded against very short clips (<50ms).
            const fadeDur = Math.min(this.crossFadeDuration, duration / 4);

            const source = this.audioCtx.createBufferSource();
            source.buffer = buffer;

            const chunkGain = this.audioCtx.createGain();
            chunkGain.gain.setValueAtTime(0.0001, startTime);
            try {
                chunkGain.gain.exponentialRampToValueAtTime(1.0, startTime + fadeDur);
            } catch (e) {
                chunkGain.gain.linearRampToValueAtTime(1.0, startTime + fadeDur);
            }

            const fadeOutStart = Math.max(startTime + fadeDur, startTime + duration - fadeDur);
            chunkGain.gain.setValueAtTime(1.0, fadeOutStart);
            try {
                chunkGain.gain.exponentialRampToValueAtTime(0.0001, startTime + duration);
            } catch (e) {
                chunkGain.gain.linearRampToValueAtTime(0.0001, startTime + duration);
            }

            source.connect(chunkGain);
            chunkGain.connect(this.masterGain);

            source.start(startTime);
            this.activeSources.push({ source, chunkGain, item: nextItem, startTime, duration });
            this.isPlaying = true;
            this._startVisualizerLoop();

            this.nextStartTime = startTime + duration - (fadeDur / 2);

            const delayMs = Math.max(0, (startTime - now) * 1000);
            setTimeout(() => {
                if (nextItem.sessionId === this.currentSessionId) {
                    const desc = nextItem.sentence ? nextItem.sentence : ('段落 #' + (nextItem.index + 1));
                    this._notifyStatus('播放中: ' + desc, true);
                    if (this.onChunkStart) this.onChunkStart(nextItem);
                }
            }, delayMs);

            source.onended = () => {
                this.activeSources = this.activeSources.filter(s => s.source !== source);
                if (nextItem.sessionId === this.currentSessionId) {
                    if (this.onChunkEnd) this.onChunkEnd(nextItem);
                }
                this._maybeQueueEmpty();
            };
        }
    }

    /**
     * Immediately stops active playback and clears queued audio chunks.
     */
    interrupt() {
        this.currentSessionId += 1;
        this.queue = [];
        if (this._scheduleRetryTimer) {
            clearTimeout(this._scheduleRetryTimer);
            this._scheduleRetryTimer = null;
        }

        for (const s of this.activeSources) {
            try {
                if (s.chunkGain && this.audioCtx) {
                    s.chunkGain.gain.setValueAtTime(s.chunkGain.gain.value, this.audioCtx.currentTime);
                    s.chunkGain.gain.linearRampToValueAtTime(0.0, this.audioCtx.currentTime + 0.015);
                }
                s.source.stop(this.audioCtx ? this.audioCtx.currentTime + 0.015 : 0);
                s.source.disconnect();
                s.chunkGain.disconnect();
            } catch (e) {
                // Ignore already stopped sources
            }
        }
        this.activeSources = [];
        this.nextStartTime = 0;
        this.isPlaying = false;
        this._stopVisualizerLoop();
        this._notifyStatus('已重置', false);
    }

    setVolume(value) {
        this.volume = Math.max(0, Math.min(1.0, value));
        if (this.masterGain && this.audioCtx && !this.isMuted) {
            this.masterGain.gain.setValueAtTime(this.volume, this.audioCtx.currentTime);
        }
    }

    setMuted(isMuted) {
        this.isMuted = !!isMuted;
        if (this.masterGain && this.audioCtx) {
            this.masterGain.gain.setValueAtTime(this.isMuted ? 0 : this.volume, this.audioCtx.currentTime);
        }
        return this.isMuted;
    }

    _notifyStatus(statusText, isPlaying) {
        if (this.onStatusChange) {
            this.onStatusChange(statusText, isPlaying);
        }
    }
}

// Export for browser and module environments
if (typeof window !== 'undefined') {
    window.StreamingAudioPlayer = StreamingAudioPlayer;
}
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { StreamingAudioPlayer };
}
