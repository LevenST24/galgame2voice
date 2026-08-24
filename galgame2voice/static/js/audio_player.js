/**
 * StreamingAudioPlayer - Low-Latency Web Audio API Streaming Player
 * for galgame2voice.
 *
 * Features:
 *  - AudioContext buffer queue with gapless scheduled playback
 *  - Real-time chunk decoding (fetch + decodeAudioData)
 *  - Seamless cross-fading (40ms) between sentence audio boundaries
 *  - AnalyserNode integration for real-time frequency spectrum sampling (getFrequencyData())
 *  - Built-in dynamic equalizer visualization driver via requestAnimationFrame
 *  - Instant interruption (interrupt()) when user submits new message
 *  - Volume and mute control with status callbacks
 */

class StreamingAudioPlayer {
    constructor(options = {}) {
        this.crossFadeDuration = options.crossFadeDuration || 0.04; // 40ms cross-fade
        this.audioCtx = null;
        this.masterGain = null;
        this.analyser = null;
        this.freqData = null;
        this.queue = [];
        this.activeSources = [];
        this.nextStartTime = 0;
        this.isPlaying = false;
        this.isMuted = false;
        this.volume = 1.0;
        this.currentSessionId = 0; // Incremented on interrupt to invalidate pending async fetches

        // Equalizer element and animation loop
        this.equalizerElement = options.equalizerElement || null;
        this.animFrameId = null;

        // Event callbacks
        this.onStatusChange = options.onStatusChange || null;
        this.onChunkStart = options.onChunkStart || null;
        this.onChunkEnd = options.onChunkEnd || null;
        this.onQueueEmpty = options.onQueueEmpty || null;
        this.onError = options.onError || null;
    }

    /**
     * Initializes AudioContext & AnalyserNode on user interaction to comply with browser autoplay policies.
     */
    initContext() {
        if (!this.audioCtx) {
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextClass) {
                console.error('Web Audio API is not supported in this browser.');
                return null;
            }
            this.audioCtx = new AudioContextClass();
            this.masterGain = this.audioCtx.createGain();
            this.masterGain.gain.setValueAtTime(this.isMuted ? 0 : this.volume, this.audioCtx.currentTime);

            // AnalyserNode setup
            this.analyser = this.audioCtx.createAnalyser();
            this.analyser.fftSize = 64; // 32 frequency bins
            this.analyser.smoothingTimeConstant = 0.8;
            this.freqData = new Uint8Array(this.analyser.frequencyBinCount);

            // Audio Graph: MasterGain -> Analyser -> Destination
            this.masterGain.connect(this.analyser);
            this.analyser.connect(this.audioCtx.destination);
        }
        if (this.audioCtx.state === 'suspended') {
            this.audioCtx.resume();
        }
        return this.audioCtx;
    }

    /**
     * Returns the current byte frequency spectrum (Uint8Array) if audio is playing.
     * @returns {Uint8Array|null}
     */
    getFrequencyData() {
        if (this.analyser && this.isPlaying) {
            this.analyser.getByteFrequencyData(this.freqData);
            return this.freqData;
        }
        return null;
    }

    /**
     * Binds a DOM element containing .bar elements for soundwave visualization.
     * @param {HTMLElement} el
     */
    attachEqualizer(el) {
        this.equalizerElement = el;
    }

    /**
     * Starts requestAnimationFrame loop to update equalizer bars based on AnalyserNode data.
     */
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
                        const val = data[i * step] || 0; // 0..255
                        // Scale height smoothly between 4px and 22px
                        const heightPx = Math.max(4, Math.round(4 + (val / 255) * 18));
                        bar.style.height = `${heightPx}px`;
                    });
                }
            }
            this.animFrameId = requestAnimationFrame(render);
        };

        this.animFrameId = requestAnimationFrame(render);
    }

    /**
     * Stops requestAnimationFrame loop and resets equalizer bars.
     */
    _stopVisualizerLoop() {
        if (this.animFrameId) {
            cancelAnimationFrame(this.animFrameId);
            this.animFrameId = null;
        }
        this._resetEqualizerBars();
    }

    /**
     * Resets equalizer bars back to default min height.
     */
    _resetEqualizerBars() {
        if (this.equalizerElement) {
            const bars = this.equalizerElement.querySelectorAll('.bar');
            bars.forEach(bar => {
                bar.style.height = '4px';
            });
        }
    }

    /**
     * Enqueues an incoming audio chunk URL or blob for playback.
     * @param {Object} chunk - { index, audio_url, sentence }
     */
    async enqueue(chunk) {
        this.initContext();
        if (!this.audioCtx) return;

        const sessionId = this.currentSessionId;
        const item = {
            id: 'chunk_' + sessionId + '_' + chunk.index,
            sessionId: sessionId,
            index: chunk.index,
            url: chunk.audio_url,
            sentence: chunk.sentence || '',
            audioBuffer: null,
            status: 'fetching'
        };

        this.queue.push(item);
        this._notifyStatus('缓冲中: 句子 #' + (chunk.index + 1), true);

        try {
            // 1. Fetch audio payload
            const response = await fetch(item.url);
            if (!response.ok) {
                throw new Error('HTTP error ' + response.status + ' fetching chunk from ' + item.url);
            }
            const arrayBuffer = await response.arrayBuffer();

            // Check if this chunk was invalidated during fetch
            if (this.currentSessionId !== sessionId) {
                return;
            }

            // 2. Decode audio payload
            try {
                const audioBuffer = await this.audioCtx.decodeAudioData(arrayBuffer);
                if (this.currentSessionId !== sessionId) {
                    return;
                }
                item.audioBuffer = audioBuffer;
                item.status = 'ready';
                // 3. Schedule playback
                this._scheduleNext();
            } catch (decodeErr) {
                console.warn('Web Audio decode failed, falling back to HTML5 Audio element:', decodeErr);
                if (this.currentSessionId !== sessionId) return;
                const fallbackAudio = new Audio(item.url);
                fallbackAudio.volume = this.isMuted ? 0 : this.volume;
                fallbackAudio.play().catch(e => console.warn('Fallback audio playback failed:', e));
                this._notifyStatus('播放中 (回退模式)', true);
                fallbackAudio.onended = () => {
                    this._notifyStatus('就绪', false);
                };
            }
        } catch (err) {
            if (this.currentSessionId === sessionId) {
                console.error('Failed to load audio chunk:', err);
                item.status = 'error';
                if (this.onError) this.onError(err, item);
            }
        }
    }

    /**
     * Schedules ready audio buffers into the Web Audio API timeline.
     */
    _scheduleNext() {
        if (!this.audioCtx || this.audioCtx.state === 'suspended') {
            this.initContext();
        }

        const now = this.audioCtx.currentTime;
        if (this.nextStartTime < now) {
            this.nextStartTime = now + 0.05; // 50ms lead-in buffer
        }

        while (this.queue.length > 0) {
            const nextItem = this.queue[0];
            if (nextItem.status !== 'ready' || !nextItem.audioBuffer) {
                // Next chunk is still downloading/decoding
                break;
            }

            this.queue.shift(); // Remove from queue
            const buffer = nextItem.audioBuffer;
            const startTime = this.nextStartTime;
            const duration = buffer.duration;

            // Create Audio Source and Gain Node for cross-fading
            const source = this.audioCtx.createBufferSource();
            source.buffer = buffer;

            const chunkGain = this.audioCtx.createGain();
            chunkGain.gain.setValueAtTime(0, startTime);
            // Cross-fade in
            chunkGain.gain.linearRampToValueAtTime(1.0, Math.min(startTime + this.crossFadeDuration, startTime + duration / 2));
            // Cross-fade out
            chunkGain.gain.setValueAtTime(1.0, Math.max(startTime, startTime + duration - this.crossFadeDuration));
            chunkGain.gain.linearRampToValueAtTime(0.01, startTime + duration);

            source.connect(chunkGain);
            chunkGain.connect(this.masterGain);

            source.start(startTime);
            this.activeSources.push({ source, chunkGain, item: nextItem, startTime, duration });
            this.isPlaying = true;
            this._startVisualizerLoop();

            // Update next scheduled start time with slight cross-fade overlap
            this.nextStartTime = startTime + duration - (this.crossFadeDuration / 2);

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
                    if (this.activeSources.length === 0 && this.queue.length === 0) {
                        this.isPlaying = false;
                        this._stopVisualizerLoop();
                        this._notifyStatus('就绪', false);
                        if (this.onQueueEmpty) this.onQueueEmpty();
                    }
                }
            };
        }
    }

    /**
     * Immediately stops active playback and clears queued audio chunks.
     * Called when a new user message is submitted or user clicks stop.
     */
    interrupt() {
        this.currentSessionId += 1; // Invalidate all pending async fetches/decodes
        this.queue = [];

        for (const s of this.activeSources) {
            try {
                s.source.stop(0);
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

    /**
     * Adjusts playback volume (0.0 to 1.0).
     */
    setVolume(value) {
        this.volume = Math.max(0, Math.min(1.0, value));
        if (this.masterGain && this.audioCtx && !this.isMuted) {
            this.masterGain.gain.setValueAtTime(this.volume, this.audioCtx.currentTime);
        }
    }

    /**
     * Toggles mute state.
     */
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
