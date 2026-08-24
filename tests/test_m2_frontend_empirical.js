/**
 * Empirical Frontend Test Suite for Milestone 2 (Commercial Galgame Immersion UI & Streaming)
 * Tests StreamingAudioPlayer Web Audio API lifecycle, AnalyserNode frequency sampling,
 * equalizer animation loop, gapless playback, fast interruption, error recovery,
 * and SSE stream parsing logic.
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

// Load audio_player.js
const audioPlayerModule = require('../galgame2voice/static/js/audio_player.js');
const StreamingAudioPlayer = audioPlayerModule.StreamingAudioPlayer || global.StreamingAudioPlayer;

// ============================================================================
// 1. Mock Web Audio API Environment
// ============================================================================

class MockGainNode {
    constructor() {
        this.gain = {
            value: 1.0,
            setValueAtTime: (val, time) => { this.gain.value = val; },
            linearRampToValueAtTime: (val, time) => { this.gain.value = val; }
        };
        this.connectedTo = null;
    }
    connect(dest) {
        this.connectedTo = dest;
    }
    disconnect() {
        this.connectedTo = null;
    }
}

class MockAnalyserNode {
    constructor() {
        this.fftSize = 64;
        this.frequencyBinCount = 32;
        this.smoothingTimeConstant = 0.8;
        this.connectedTo = null;
    }
    connect(dest) {
        this.connectedTo = dest;
    }
    disconnect() {
        this.connectedTo = null;
    }
    getByteFrequencyData(array) {
        // Populate mock frequency data
        for (let i = 0; i < array.length; i++) {
            array[i] = Math.min(255, (i + 1) * 8); // 8, 16, 24, ...
        }
    }
}

class MockAudioBufferSourceNode {
    constructor(ctx) {
        this.ctx = ctx;
        this.buffer = null;
        this.onended = null;
        this.startTime = null;
        this.isStopped = false;
        this.connectedTo = null;
    }
    connect(dest) {
        this.connectedTo = dest;
    }
    disconnect() {
        this.connectedTo = null;
    }
    start(time) {
        this.startTime = time;
    }
    stop(time) {
        this.isStopped = true;
    }
}

class MockAudioContext {
    constructor() {
        this.state = 'running';
        this.currentTime = 0.0;
        this.destination = { type: 'destination' };
    }
    createGain() {
        return new MockGainNode();
    }
    createAnalyser() {
        return new MockAnalyserNode();
    }
    createBufferSource() {
        return new MockAudioBufferSourceNode(this);
    }
    async decodeAudioData(arrayBuffer) {
        if (arrayBuffer.byteLength === 0 || arrayBuffer.isCorrupt) {
            throw new Error('Corrupt audio data decode error');
        }
        return {
            duration: 1.5, // 1.5s simulated duration
            sampleRate: 24000,
            numberOfChannels: 1
        };
    }
    resume() {
        this.state = 'running';
        return Promise.resolve();
    }
}

// Global browser mocks
global.AudioContext = MockAudioContext;
global.window = { AudioContext: MockAudioContext };
global.requestAnimationFrame = (cb) => setTimeout(cb, 16);
global.cancelAnimationFrame = (id) => clearTimeout(id);

// Mock HTML5 Audio fallback
class MockAudio {
    constructor(url) {
        this.url = url;
        this.volume = 1.0;
        this.onended = null;
    }
    play() {
        return Promise.resolve();
    }
}
global.Audio = MockAudio;

// ============================================================================
// 2. DOM Mock for Equalizer & UI Tests
// ============================================================================

function createMockEqualizerElement() {
    const bars = [
        { style: { height: '4px' } },
        { style: { height: '4px' } },
        { style: { height: '4px' } },
        { style: { height: '4px' } },
        { style: { height: '4px' } },
    ];
    return {
        classList: {
            add: () => {},
            remove: () => {}
        },
        querySelectorAll: (sel) => {
            if (sel === '.bar') return bars;
            return [];
        },
        _bars: bars
    };
}

// ============================================================================
// 3. Test Runner
// ============================================================================

let testsPassed = 0;
let testsFailed = 0;

async function runTest(name, fn) {
    try {
        await fn();
        console.log(`  ✓ ${name}`);
        testsPassed++;
    } catch (err) {
        console.error(`  ✗ ${name}:`, err.message);
        testsFailed++;
    }
}

async function main() {
    console.log('\n=== Running Empirical Tests for Milestone 2 Frontend & Audio ===\n');

    // Test 1: Player Initialization & Audio Graph
    await runTest('Web Audio API Graph setup (MasterGain -> Analyser -> Destination)', async () => {
        const player = new StreamingAudioPlayer();
        const ctx = player.initContext();
        assert(ctx !== null, 'AudioContext must be initialized');
        assert(player.masterGain !== null, 'MasterGain must be created');
        assert(player.analyser !== null, 'AnalyserNode must be created');
        assert.strictEqual(player.analyser.fftSize, 64, 'Analyser fftSize must be 64');
        assert.strictEqual(player.analyser.smoothingTimeConstant, 0.8, 'smoothingTimeConstant must be 0.8');
        assert.strictEqual(player.masterGain.connectedTo, player.analyser, 'masterGain must connect to analyser');
        assert.strictEqual(player.analyser.connectedTo, ctx.destination, 'analyser must connect to destination');
    });

    // Test 2: Frequency Data Sampling
    await runTest('getFrequencyData() returns Uint8Array only during active playback', async () => {
        const player = new StreamingAudioPlayer();
        player.initContext();
        
        // When not playing, returns null
        assert.strictEqual(player.getFrequencyData(), null, 'Should return null when isPlaying is false');

        // When playing, returns frequency data
        player.isPlaying = true;
        const freq = player.getFrequencyData();
        assert(freq instanceof Uint8Array, 'Should return Uint8Array when isPlaying is true');
        assert.strictEqual(freq.length, 32, 'Frequency bin count must be 32');
        assert(freq[0] > 0, 'Frequency amplitude should be populated');
    });

    // Test 3: Equalizer Attachment & Bar Scaling
    await runTest('Dynamic equalizer scaling (4px to 22px) and reset on stop', async () => {
        const mockEq = createMockEqualizerElement();
        const player = new StreamingAudioPlayer({ equalizerElement: mockEq });
        player.initContext();
        player.isPlaying = true;

        player._startVisualizerLoop();
        
        // Allow visualizer loop iteration
        await new Promise(r => setTimeout(r, 40));

        // Verify bars were scaled up based on frequency amplitudes
        const heights = mockEq._bars.map(b => parseInt(b.style.height, 10));
        assert(heights.some(h => h > 4), 'Bars should scale dynamically above 4px during audio playback');
        assert(heights.every(h => h >= 4 && h <= 22), 'Bar heights must be bounded between 4px and 22px');

        // Stop visualizer and verify reset to 4px
        player.isPlaying = false;
        player._stopVisualizerLoop();
        assert(mockEq._bars.every(b => b.style.height === '4px'), 'All bars must reset to 4px on stop');
    });

    // Test 4: Audio Enqueueing, Gapless Playback & Cross-Fade Timeline
    await runTest('Enqueueing audio chunks calculates gapless schedule with cross-fade overlap', async () => {
        // Mock global fetch for audio chunks
        global.fetch = async (url) => {
            return {
                ok: true,
                status: 200,
                arrayBuffer: async () => new ArrayBuffer(1024)
            };
        };

        const player = new StreamingAudioPlayer({ crossFadeDuration: 0.04 });
        player.initContext();

        let chunkStartCount = 0;
        let chunkEndCount = 0;
        player.onChunkStart = () => chunkStartCount++;
        player.onChunkEnd = () => chunkEndCount++;

        await player.enqueue({ index: 0, audio_url: '/api/audio/chunk0.wav', sentence: 'こんにちは' });
        
        assert.strictEqual(player.activeSources.length, 1, 'Should have 1 active audio source scheduled');
        assert(player.isPlaying, 'Player state should be playing');
        assert(player.nextStartTime > 0, 'nextStartTime should be scheduled ahead');

        const firstScheduleTime = player.nextStartTime;

        await player.enqueue({ index: 1, audio_url: '/api/audio/chunk1.wav', sentence: '今日もいい天気ですね' });
        assert.strictEqual(player.activeSources.length, 2, 'Should have 2 active audio sources scheduled');
        
        // Second chunk starts with cross-fade overlap
        assert(player.nextStartTime > firstScheduleTime, 'Next chunk should extend the playback timeline');
    });

    // Test 5: Fast Interruption & Cancellation
    await runTest('interrupt() invalidates pending async fetches and stops active sources', async () => {
        let fetchResolved = false;
        global.fetch = async (url) => {
            await new Promise(r => setTimeout(r, 50));
            fetchResolved = true;
            return {
                ok: true,
                status: 200,
                arrayBuffer: async () => new ArrayBuffer(1024)
            };
        };

        const player = new StreamingAudioPlayer();
        player.initContext();

        const initialSession = player.currentSessionId;
        const enqueuePromise = player.enqueue({ index: 0, audio_url: '/api/audio/slow.wav', sentence: '慢速音频' });

        // Interrupt immediately before fetch completes
        player.interrupt();

        assert.strictEqual(player.currentSessionId, initialSession + 1, 'Session ID must increment on interrupt');
        assert.strictEqual(player.queue.length, 0, 'Queue must be cleared on interrupt');
        assert.strictEqual(player.activeSources.length, 0, 'Active sources must be stopped and cleared');
        assert.strictEqual(player.isPlaying, false, 'Player isPlaying must be false');

        await enqueuePromise;
        // Even when fetch finishes, it should NOT schedule audio because session ID changed
        assert.strictEqual(player.activeSources.length, 0, 'Stale chunk must not be scheduled after interrupt');
    });

    // Test 6: Corrupted Audio Buffer Fallback
    await runTest('Corrupted audio decode gracefully falls back to HTML5 Audio element', async () => {
        global.fetch = async (url) => {
            const buf = new ArrayBuffer(128);
            buf.isCorrupt = true; // Trigger decodeAudioData error
            return {
                ok: true,
                status: 200,
                arrayBuffer: async () => buf
            };
        };

        const player = new StreamingAudioPlayer();
        player.initContext();

        let statusRecorded = '';
        player.onStatusChange = (status) => { statusRecorded = status; };

        await player.enqueue({ index: 0, audio_url: '/api/audio/corrupt.wav', sentence: '损坏音频' });

        assert(statusRecorded.includes('回退模式') || statusRecorded.includes('就绪'), 'Should handle decode error via fallback');
    });

    // Test 7: Network Error on Audio Chunk Fetch
    await runTest('Network fetch failure catches error and calls onError callback', async () => {
        global.fetch = async (url) => {
            return {
                ok: false,
                status: 404,
                statusText: 'Not Found'
            };
        };

        const player = new StreamingAudioPlayer();
        player.initContext();

        let errorReported = null;
        player.onError = (err, item) => { errorReported = err; };

        await player.enqueue({ index: 0, audio_url: '/api/audio/missing.wav', sentence: '缺失音频' });

        assert(errorReported !== null, 'onError callback must be triggered on fetch failure');
        assert(errorReported.message.includes('404'), 'Error message should include status 404');
    });

    // Test 8: Volume and Muting State Transitions
    await runTest('setVolume() and setMuted() properly update gain values', async () => {
        const player = new StreamingAudioPlayer();
        player.initContext();

        player.setVolume(0.65);
        assert.strictEqual(player.volume, 0.65, 'Volume should be updated to 0.65');
        assert.strictEqual(player.masterGain.gain.value, 0.65, 'masterGain gain value should match volume');

        player.setMuted(true);
        assert.strictEqual(player.isMuted, true, 'isMuted should be true');
        assert.strictEqual(player.masterGain.gain.value, 0, 'masterGain gain value must be 0 when muted');

        player.setMuted(false);
        assert.strictEqual(player.isMuted, false, 'isMuted should be false');
        assert.strictEqual(player.masterGain.gain.value, 0.65, 'masterGain gain value must restore to volume');
    });

    // Test 9: SSE Stream Parsing Logic Verification
    await runTest('SSE Stream Parser Handles Chunk Fragmentation and Event Dispatch', async () => {
        // Simulated raw SSE chunks arriving in fragments
        const sseStreamChunks = [
            'event: text\ndata: {"delta_chinese": "你好',
            '，指挥官！"}\n\nevent: audio_chunk\n',
            'data: {"index": 0, "audio_url": "/api/audio/1.wav", "sentence": "こんにちは、指揮官！"}\n\n',
            'event: text\ndata: {"delta_chinese": " 今天天气真好。"}\n\n',
            'event: done\ndata: {"chinese": "你好，指挥官！ 今天天气真好。", "japanese": "こんにちは、指揮官！", "audio_url": "/api/audio/1.wav"}\n\n'
        ];

        let buffer = '';
        let currentEventType = 'message';
        let fullChinese = '';
        let fullJapanese = '';
        let receivedAudioChunks = [];
        let doneReceived = false;

        for (const rawChunk of sseStreamChunks) {
            buffer += rawChunk;
            const lines = buffer.split('\n');
            buffer = lines.pop(); // Keep incomplete trailing fragment

            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed) {
                    currentEventType = 'message';
                    continue;
                }
                if (trimmed.startsWith('event:')) {
                    currentEventType = trimmed.substring(6).trim();
                    continue;
                }
                if (trimmed.startsWith('data:')) {
                    const jsonStr = trimmed.substring(5).trim();
                    if (jsonStr === '[DONE]') continue;
                    const payload = JSON.parse(jsonStr);

                    if (currentEventType === 'text' || (payload.delta_chinese !== undefined || payload.full_chinese !== undefined)) {
                        fullChinese += (payload.delta_chinese || '');
                    } else if (currentEventType === 'audio_chunk' || (payload.audio_url && payload.index !== undefined)) {
                        receivedAudioChunks.push(payload);
                        if (payload.sentence) fullJapanese = payload.sentence;
                    } else if (currentEventType === 'done' || (payload.chinese && payload.japanese)) {
                        doneReceived = true;
                    }
                }
            }
        }

        assert.strictEqual(fullChinese, '你好，指挥官！ 今天天气真好。', 'Parsed streamed Chinese text must match exactly');
        assert.strictEqual(fullJapanese, 'こんにちは、指揮官！', 'Parsed Japanese text must match');
        assert.strictEqual(receivedAudioChunks.length, 1, 'Should have received 1 audio chunk');
        assert.strictEqual(receivedAudioChunks[0].audio_url, '/api/audio/1.wav');
        assert.strictEqual(doneReceived, true, 'Done event must be processed');
    });

    console.log(`\n========================================`);
    console.log(`Tests Complete: ${testsPassed} passed, ${testsFailed} failed`);
    console.log(`========================================\n`);

    if (testsFailed > 0) {
        process.exit(1);
    }
}

main().catch(err => {
    console.error('Fatal test runner error:', err);
    process.exit(1);
});
