/**
 * ChatClient - Commercial-Grade Galgame Immersion & SSE Bilingual Streaming Client
 * Features:
 *  - Dual Mode (🌸 Galgame Visual Novel Immersion Mode & 💬 Classic Chat Stream Mode)
 *  - Galgame Interaction Triad: AutoModeController, SkipController, LogDrawerController
 *  - Dynamic Emotion Engine (EmotionManager) driving 6-archetype avatar emoji & breathing halo aura
 *  - Robust SSE Parser for 'event: text', 'event: audio_chunk', 'event: done', 'event: error'
 *  - Web Audio API AnalyserNode soundwave visualizer & dynamic equalizer
 *  - Top Capsule Controls: instant voice switcher, live GPT-SoVITS status & latency, volume gain, mute toggle, reset
 *  - Slide-out Frosted Backlog Drawer with per-sentence audio replay
 */

// ============================================================================
// 1. Galgame Immersion Controllers
// ============================================================================

/**
 * AutoModeController - Manages automatic dialogue progression after voice playback.
 */
class AutoModeController {
    constructor(options = {}) {
        this.enabled = false;
        this.delayMs = options.defaultDelayMs || 1500; // 1.5s reading interval
        this.timer = null;
        this.onAdvance = options.onAdvance || null;
        this.btnEl = options.btnEl || null;
        this.init();
    }

    init() {
        if (this.btnEl) {
            this.btnEl.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggle();
            });
        }
    }

    toggle() {
        this.enabled = !this.enabled;
        this.updateUI();
        if (!this.enabled && this.timer) {
            clearTimeout(this.timer);
            this.timer = null;
        }
        return this.enabled;
    }

    setEnabled(val) {
        this.enabled = !!val;
        this.updateUI();
        if (!this.enabled && this.timer) {
            clearTimeout(this.timer);
            this.timer = null;
        }
    }

    updateUI() {
        if (!this.btnEl) return;
        if (this.enabled) {
            this.btnEl.classList.add('active');
            const textEl = this.btnEl.querySelector('.btn-text');
            if (textEl) textEl.textContent = 'AUTO [ON]';
        } else {
            this.btnEl.classList.remove('active');
            const textEl = this.btnEl.querySelector('.btn-text');
            if (textEl) textEl.textContent = 'AUTO';
        }
    }

    onAudioQueueFinished() {
        if (!this.enabled) return;
        if (this.timer) clearTimeout(this.timer);
        this.timer = setTimeout(() => {
            if (this.enabled && this.onAdvance) {
                this.onAdvance();
            }
        }, this.delayMs);
    }

    cancel() {
        if (this.timer) {
            clearTimeout(this.timer);
            this.timer = null;
        }
    }
}

/**
 * SkipController - Fast skips typewriter text and waits.
 */
class SkipController {
    constructor(options = {}) {
        this.btnEl = options.btnEl || null;
        this.onSkip = options.onSkip || null;
        this.init();
    }

    init() {
        if (this.btnEl) {
            this.btnEl.addEventListener('click', (e) => {
                e.stopPropagation();
                this.skip();
            });
        }
    }

    skip() {
        if (this.onSkip) {
            this.onSkip();
        }
    }
}

/**
 * EmotionManager - Handles 6 emotion archetypes & visual aura/avatar transitions.
 */
class EmotionManager {
    static EMOTIONS = {
        gentle: { name: '温柔', emoji: '🌸😊', tag: '🌸 温柔' },
        shy: { name: '害羞', emoji: '😳', tag: '😳 害羞' },
        happy: { name: '开心', emoji: '✨😄', tag: '✨ 开心' },
        tsundere: { name: '傲娇', emoji: '😤', tag: '😤 傲娇' },
        cool: { name: '高冷', emoji: '❄️', tag: '❄️ 高冷' },
        sad: { name: '难过', emoji: '🥺', tag: '🥺 难过' },
    };

    constructor(options = {}) {
        this.backdropEl = options.backdropEl || null;
        this.avatarEmojiEl = options.avatarEmojiEl || null;
        this.badgeEl = options.badgeEl || null;
        this.currentEmotion = 'gentle';
    }

    setEmotion(emotion) {
        const emo = (emotion || 'gentle').toLowerCase();
        if (!EmotionManager.EMOTIONS[emo]) {
            return;
        }
        this.currentEmotion = emo;
        const info = EmotionManager.EMOTIONS[emo];

        const backdrop = this.backdropEl || document.querySelector('.character-backdrop');
        if (backdrop) {
            Object.keys(EmotionManager.EMOTIONS).forEach(e => {
                backdrop.classList.remove(`emotion-${e}`);
            });
            backdrop.classList.add(`emotion-${emo}`);
        }

        const avatarEmoji = this.avatarEmojiEl || document.querySelector('.avatar-emoji');
        if (avatarEmoji) {
            avatarEmoji.textContent = info.emoji;
        }

        const badge = this.badgeEl || document.getElementById('vn-emotion-badge');
        if (badge) {
            badge.setAttribute('data-emotion', emo);
            badge.textContent = info.tag;
        }
    }

    getEmotion() {
        return this.currentEmotion;
    }
}

/**
 * LogDrawerController - Handles slide-out Backlog drawer and per-sentence voice replay.
 */
class LogDrawerController {
    constructor(options = {}) {
        this.drawerEl = options.drawerEl || null;
        this.backdropEl = options.backdropEl || null;
        this.bodyEl = options.bodyEl || null;
        this.btnOpen = options.btnOpen || null;
        this.btnClose = options.btnClose || null;
        this.btnClose2 = options.btnClose2 || null;
        this.btnClear = options.btnClear || null;
        this.onReplayAudio = options.onReplayAudio || null;
        this.onClearHistory = options.onClearHistory || null;
        this.getHistory = options.getHistory || (() => []);
        this.characterName = options.characterName || '四季夏目';

        this.init();
    }

    init() {
        if (this.btnOpen) {
            this.btnOpen.addEventListener('click', (e) => {
                e.stopPropagation();
                this.open();
            });
        }
        if (this.btnClose) {
            this.btnClose.addEventListener('click', () => this.close());
        }
        if (this.btnClose2) {
            this.btnClose2.addEventListener('click', () => this.close());
        }
        if (this.backdropEl) {
            this.backdropEl.addEventListener('click', (e) => {
                if (e.target === this.backdropEl) this.close();
            });
        }
        if (this.btnClear && this.onClearHistory) {
            this.btnClear.addEventListener('click', () => this.onClearHistory());
        }
    }

    open() {
        this.render();
        const backdrop = this.backdropEl || document.getElementById('log-drawer-backdrop');
        const drawer = this.drawerEl || document.getElementById('vn-log-drawer');
        if (backdrop) {
            backdrop.style.display = 'block';
            requestAnimationFrame(() => {
                backdrop.classList.add('open');
                if (drawer) drawer.classList.add('open');
            });
        }
    }

    close() {
        const backdrop = this.backdropEl || document.getElementById('log-drawer-backdrop');
        const drawer = this.drawerEl || document.getElementById('vn-log-drawer');
        if (backdrop) {
            backdrop.classList.remove('open');
            if (drawer) drawer.classList.remove('open');
            setTimeout(() => {
                if (!backdrop.classList.contains('open')) {
                    backdrop.style.display = 'none';
                }
            }, 350);
        }
    }

    render() {
        const body = this.bodyEl || document.getElementById('log-drawer-body');
        if (!body) return;
        body.innerHTML = '';
        const history = this.getHistory();
        if (!history || history.length === 0) {
            body.innerHTML = '<div style="text-align: center; color: #94a3b8; padding: 40px 20px;">暂无历史对话记录</div>';
            return;
        }

        history.forEach((item, idx) => {
            const card = document.createElement('div');
            const isUser = item.role === 'user';
            card.className = `log-item-card ${isUser ? 'log-user' : 'log-assistant'}`;

            const timeStr = item.timestamp || new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            const speakerName = isUser ? '你' : (this.characterName || '四季夏目');

            let contentHtml = '';
            if (isUser) {
                contentHtml = `<div class="log-item-zh">${this.escapeHtml(item.content_chinese || item.content || '')}</div>`;
            } else {
                contentHtml = `
                    ${item.content_japanese ? `<div class="log-item-ja">${this.escapeHtml(item.content_japanese)}</div>` : ''}
                    <div class="log-item-zh">${this.escapeHtml(item.content_chinese || item.content || '')}</div>
                `;
            }

            const hasAudio = !isUser && (item.audio_url || (item.chunks && item.chunks.length > 0));

            card.innerHTML = `
                <div class="log-item-header">
                    <span class="log-item-speaker">${this.escapeHtml(speakerName)}</span>
                    <span class="log-item-time">${timeStr}</span>
                </div>
                ${contentHtml}
                ${hasAudio ? `
                    <div class="log-item-actions">
                        <button type="button" class="btn-log-replay" data-index="${idx}" title="重新播放此句语音">
                            <span>🔊 重播</span>
                        </button>
                    </div>
                ` : ''}
            `;

            if (hasAudio) {
                const replayBtn = card.querySelector('.btn-log-replay');
                if (replayBtn && this.onReplayAudio) {
                    replayBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        this.onReplayAudio(item);
                    });
                }
            }

            body.appendChild(card);
        });

        body.scrollTop = body.scrollHeight;
    }

    escapeHtml(str) {
        return (str || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }
}


// ============================================================================
// 2. Main DOM & Lifecycle Initialization
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
    // 1. DOM Elements
    const vnView = document.getElementById('vn-view');
    const chatView = document.getElementById('chat-view');
    const btnModeVn = document.getElementById('btn-mode-vn');
    const btnModeChat = document.getElementById('btn-mode-chat');

    const chatLog = document.getElementById('chat-log');
    const chatForm = document.getElementById('chat-form');
    const promptInput = document.getElementById('prompt-input');
    const sendBtn = document.getElementById('send-btn');
    const resetContextBtn = document.getElementById('reset-context-btn');

    // Visual Novel Stage Elements
    const vnDialogueBox = document.querySelector('.vn-dialogue-box');
    const vnCharacterName = document.getElementById('vn-character-name');
    const vnTextJa = document.getElementById('vn-text-ja');
    const vnTextZh = document.getElementById('vn-text-zh');
    const vnAudioStatus = document.getElementById('vn-audio-status');
    const vnAudioEqualizer = document.getElementById('vn-audio-equalizer');
    const vnEmotionBadge = document.getElementById('vn-emotion-badge');
    const btnVnAuto = document.getElementById('btn-vn-auto');
    const btnVnSkip = document.getElementById('btn-vn-skip');
    const btnVnLog = document.getElementById('btn-vn-log');
    const btnVnReplay = document.getElementById('btn-vn-replay');
    const btnVnDownload = document.getElementById('btn-vn-download');
    const btnVnHistory = document.getElementById('btn-vn-history');

    // Log Drawer Elements
    const logDrawer = document.getElementById('vn-log-drawer');
    const logDrawerBackdrop = document.getElementById('log-drawer-backdrop');
    const logDrawerBody = document.getElementById('log-drawer-body');
    const btnCloseLogDrawer = document.getElementById('btn-close-log-drawer');
    const btnCloseLogDrawer2 = document.getElementById('btn-close-log-drawer-2');
    const btnClearLogHistory = document.getElementById('btn-clear-log-history');

    // Header Capsule Controls
    const quickVoiceSelect = document.getElementById('quick-voice-select');
    const sovitsStatusBadge = document.getElementById('sovits-status-badge');
    const muteToggleBtn = document.getElementById('mute-toggle-btn');
    const volumeSlider = document.getElementById('volume-slider');
    const volumeLabel = document.getElementById('volume-label');

    // Legacy History Modal Elements (Retained for compatibility)
    const historyModal = document.getElementById('history-modal');
    const historyModalBody = document.getElementById('history-modal-body');
    const btnCloseHistory = document.getElementById('btn-close-history');
    const btnCloseHistory2 = document.getElementById('btn-close-history-2');
    const btnClearHistoryModal = document.getElementById('btn-clear-history-modal');

    // 2. State & Session
    let sessionId = localStorage.getItem('g2v_session_id') || ('sess_' + Math.random().toString(36).substring(2, 10));
    localStorage.setItem('g2v_session_id', sessionId);

    let currentMode = localStorage.getItem('g2v_view_mode') || 'vn'; // 'vn' | 'chat'
    let isStreaming = false;
    let abortController = null;
    let lastVoiceData = null;
    let currentDialogueHistory = [];

    // Typewriter State & Skip Support
    let typewriterTimer = null;
    let pendingFullTextJa = '';
    let pendingFullTextZh = '';

    function skipTypewriter() {
        if (typewriterTimer) {
            clearTimeout(typewriterTimer);
            typewriterTimer = null;
        }
        if (pendingFullTextJa && vnTextJa) {
            vnTextJa.textContent = pendingFullTextJa;
        }
        if (pendingFullTextZh && vnTextZh) {
            vnTextZh.textContent = `（${pendingFullTextZh}）`;
        }
    }

    if (vnDialogueBox) {
        vnDialogueBox.addEventListener('click', (e) => {
            if (!e.target.closest('button') && !e.target.closest('select')) {
                skipTypewriter();
            }
        });
    }
    if (vnView) {
        vnView.addEventListener('click', (e) => {
            if (!e.target.closest('button') && !e.target.closest('select')) {
                skipTypewriter();
            }
        });
    }

    // 3. Audio Player Instance & Equalizer Attachment
    const audioPlayer = new StreamingAudioPlayer({
        crossFadeDuration: 0.025, // 25ms optimal zero DC-offset cross-fade
        equalizerElement: vnAudioEqualizer,
        onStatusChange: (statusText, isPlaying) => {
            if (vnAudioStatus) {
                vnAudioStatus.textContent = statusText;
            }
            if (vnAudioEqualizer) {
                if (isPlaying) {
                    vnAudioEqualizer.classList.add('playing');
                } else {
                    vnAudioEqualizer.classList.remove('playing');
                }
            }
        },
        onError: (err) => {
            console.error('Audio playback error:', err);
            if (vnAudioStatus) vnAudioStatus.textContent = '音频异常';
        }
    });

    if (vnAudioEqualizer) {
        audioPlayer.attachEqualizer(vnAudioEqualizer);
    }

    // Auto-unlock Web Audio context on user interaction
    const unlockAudio = () => {
        audioPlayer.initContext();
        document.removeEventListener('click', unlockAudio);
        document.removeEventListener('keydown', unlockAudio);
    };
    document.addEventListener('click', unlockAudio);
    document.addEventListener('keydown', unlockAudio);

    // 4. Instantiate Controllers
    const emotionManager = new EmotionManager({
        backdropEl: document.querySelector('.character-backdrop'),
        avatarEmojiEl: document.querySelector('.avatar-emoji'),
        badgeEl: vnEmotionBadge,
    });

    const autoModeController = new AutoModeController({
        defaultDelayMs: 1500,
        btnEl: btnVnAuto,
        onAdvance: () => {
            console.log('[AutoMode] Dialogue finished, auto-advance ready');
            if (vnAudioStatus) vnAudioStatus.textContent = '自动推进就绪';
        }
    });

    const skipController = new SkipController({
        btnEl: btnVnSkip,
        onSkip: () => {
            skipTypewriter();
            audioPlayer.interrupt();
            if (vnAudioStatus) vnAudioStatus.textContent = '已跳过等待';
        }
    });

    const logDrawerController = new LogDrawerController({
        drawerEl: logDrawer,
        backdropEl: logDrawerBackdrop,
        bodyEl: logDrawerBody,
        btnOpen: btnVnLog,
        btnClose: btnCloseLogDrawer,
        btnClose2: btnCloseLogDrawer2,
        btnClear: btnClearLogHistory,
        getHistory: () => currentDialogueHistory,
        characterName: (vnCharacterName && vnCharacterName.textContent) || '四季夏目',
        onReplayAudio: (item) => {
            audioPlayer.interrupt();
            if (item.chunks && item.chunks.length > 0) {
                item.chunks.forEach((chunk, i) => {
                    audioPlayer.enqueue({
                        index: chunk.index !== undefined ? chunk.index : i,
                        audio_url: chunk.audio_url,
                        sentence: chunk.sentence || ''
                    });
                });
            } else if (item.audio_url) {
                audioPlayer.enqueue({
                    index: 0,
                    audio_url: item.audio_url,
                    sentence: item.content_japanese || ''
                });
            }
        },
        onClearHistory: handleResetContext
    });

    // Wire audioPlayer queue empty event to autoModeController
    audioPlayer.onQueueEmpty = () => {
        autoModeController.onAudioQueueFinished();
    };

    // Expose instances for verification & debugging
    window.autoModeController = autoModeController;
    window.skipController = skipController;
    window.emotionManager = emotionManager;
    window.logDrawerController = logDrawerController;
    window.audioPlayer = audioPlayer;

    // 5. Dual View Mode Switching
    function setViewMode(mode) {
        currentMode = mode;
        localStorage.setItem('g2v_view_mode', mode);
        if (mode === 'vn') {
            if (vnView) vnView.style.display = 'flex';
            if (chatView) chatView.style.display = 'none';
            if (btnModeVn) btnModeVn.classList.add('active');
            if (btnModeChat) btnModeChat.classList.remove('active');
        } else {
            if (vnView) vnView.style.display = 'none';
            if (chatView) chatView.style.display = 'flex';
            if (btnModeVn) btnModeVn.classList.remove('active');
            if (btnModeChat) btnModeChat.classList.add('active');
            if (chatLog) chatLog.scrollTop = chatLog.scrollHeight;
        }
    }

    if (btnModeVn) btnModeVn.addEventListener('click', () => setViewMode('vn'));
    if (btnModeChat) btnModeChat.addEventListener('click', () => setViewMode('chat'));
    setViewMode(currentMode);

    // 6. Volume & Mute Controls
    if (muteToggleBtn) {
        muteToggleBtn.addEventListener('click', () => {
            const isMuted = !audioPlayer.isMuted;
            audioPlayer.setMuted(isMuted);
            muteToggleBtn.textContent = isMuted ? '🔇' : '🔊';
            muteToggleBtn.title = isMuted ? '点击取消静音' : '点击静音';
        });
    }

    if (volumeSlider) {
        volumeSlider.addEventListener('input', (e) => {
            const val = parseInt(e.target.value, 10);
            const factor = val / 100;
            audioPlayer.setVolume(factor);
            if (volumeLabel) volumeLabel.textContent = `${val}%`;
            if (audioPlayer.isMuted && val > 0) {
                audioPlayer.setMuted(false);
                if (muteToggleBtn) {
                    muteToggleBtn.textContent = '🔊';
                    muteToggleBtn.title = '点击静音';
                }
            }
        });
    }

    // 7. Quick Character Dropdown
    async function loadVoiceProfiles() {
        if (!quickVoiceSelect) return;
        try {
            const resp = await fetch('/api/voice/profiles');
            if (resp.ok) {
                const data = await resp.json();
                const profiles = data.profiles || [];
                const activeId = data.active_profile_id;

                quickVoiceSelect.innerHTML = '';
                profiles.forEach(p => {
                    const opt = document.createElement('option');
                    opt.value = p.id;
                    opt.textContent = p.name;
                    if (p.id === activeId) opt.selected = true;
                    quickVoiceSelect.appendChild(opt);
                });

                const activeProfile = profiles.find(p => p.id === activeId) || profiles.find(p => p.is_default) || profiles[0];
                if (activeProfile) {
                    if (vnCharacterName) vnCharacterName.textContent = activeProfile.name;
                    logDrawerController.characterName = activeProfile.name;
                }
            }
        } catch (err) {
            console.warn('Failed to load voice profiles:', err);
        }
    }

    if (quickVoiceSelect) {
        quickVoiceSelect.addEventListener('change', async (e) => {
            const profileId = parseInt(e.target.value, 10);
            if (!profileId) return;
            try {
                const resp = await fetch('/api/voice/switch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ profile_id: profileId })
                });
                if (resp.ok) {
                    const selectedText = quickVoiceSelect.options[quickVoiceSelect.selectedIndex].text;
                    if (vnCharacterName) vnCharacterName.textContent = selectedText;
                    logDrawerController.characterName = selectedText;
                    appendSystemMessage(`已切换角色音色为: ${selectedText}`);
                }
            } catch (err) {
                console.error('Failed to switch voice profile:', err);
            }
        });
    }

    // 8. System Health Status with Live Latency Check
    async function checkSystemStatus() {
        if (!sovitsStatusBadge) return;
        try {
            const resp = await fetch('/api/system/status');
            if (resp.ok) {
                const data = await resp.json();
                const isOnline = data.gpt_sovits && (data.gpt_sovits.status === 'reachable' || data.gpt_sovits.status === 'healthy' || data.gpt_sovits.status === 'online');
                const latency = data.gpt_sovits && data.gpt_sovits.latency_ms ? Math.round(data.gpt_sovits.latency_ms) : null;

                sovitsStatusBadge.className = 'badge ' + (isOnline ? 'badge-success' : 'badge-danger');
                const textEl = sovitsStatusBadge.querySelector('.status-text');
                if (textEl) {
                    textEl.textContent = isOnline
                        ? (latency ? `GPT-SoVITS 在线 (${latency}ms)` : 'GPT-SoVITS 在线')
                        : 'GPT-SoVITS 离线';
                }
            } else {
                throw new Error('Non-200 response');
            }
        } catch (e) {
            sovitsStatusBadge.className = 'badge badge-danger';
            const textEl = sovitsStatusBadge.querySelector('.status-text');
            if (textEl) textEl.textContent = 'GPT-SoVITS 离线';
        }
    }

    // 9. Load Chat History
    async function loadHistory() {
        try {
            const resp = await fetch(`/api/chat/history?session_id=${encodeURIComponent(sessionId)}&limit=50`);
            if (resp.ok) {
                const data = await resp.json();
                const messages = data.messages || [];
                currentDialogueHistory = messages;
                if (messages.length > 0 && chatLog) {
                    chatLog.innerHTML = '';
                    let lastAssistantMsg = null;
                    messages.forEach(msg => {
                        if (msg.role === 'user') {
                            appendUserMessage(msg.content_chinese || msg.content || '');
                        } else if (msg.role === 'assistant') {
                            lastAssistantMsg = msg;
                            renderCompletedAssistantMessage({
                                chinese: msg.content_chinese || '',
                                japanese: msg.content_japanese || '',
                                audio_url: msg.audio_url || ''
                            });
                        }
                    });

                    // Set Visual Novel state to last assistant message
                    if (lastAssistantMsg) {
                        if (vnTextJa) vnTextJa.textContent = lastAssistantMsg.content_japanese || '……';
                        if (vnTextZh) vnTextZh.textContent = `（${lastAssistantMsg.content_chinese || ''}）`;
                        pendingFullTextJa = lastAssistantMsg.content_japanese || '';
                        pendingFullTextZh = lastAssistantMsg.content_chinese || '';
                        lastVoiceData = {
                            audio_url: lastAssistantMsg.audio_url,
                            sentence: lastAssistantMsg.content_japanese
                        };
                        if (lastAssistantMsg.emotion) {
                            emotionManager.setEmotion(lastAssistantMsg.emotion);
                        }
                    }
                }
            }
        } catch (e) {
            console.warn('Failed to load chat history:', e);
        }
        await fetchInitialAffection();
    }

    // Affection Display Helper
    function updateAffectionDisplay(aff) {
        if (!aff) return;
        const label = `Lv.${aff.level || 1} ${aff.level_name || '初识'} (${aff.score !== undefined ? aff.score : 0}/100)`;
        const headerBadge = document.getElementById('affection-label');
        if (headerBadge) headerBadge.textContent = label;
        const vnBadge = document.getElementById('vn-affection-badge');
        if (vnBadge) vnBadge.textContent = `❤️ Lv.${aff.level || 1}`;
    }

    async function fetchInitialAffection() {
        try {
            const res = await fetch('/api/affection');
            if (res.ok) {
                const data = await res.json();
                updateAffectionDisplay(data);
                if (data.current_emotion && emotionManager) {
                    emotionManager.setEmotion(data.current_emotion);
                }
            }
        } catch (e) {
            console.debug('Failed to fetch initial affection:', e);
        }
    }

    // 10. Replay & Download Master Audio
    function downloadAudioFile(audioUrl, defaultName = 'voice_master.wav') {
        if (!audioUrl) {
            console.warn('No audio URL provided for download.');
            return;
        }
        const a = document.createElement('a');
        a.href = audioUrl;
        const parts = audioUrl.split('/');
        const filename = parts[parts.length - 1] || defaultName;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }

    if (btnVnReplay) {
        btnVnReplay.addEventListener('click', () => {
            if (lastVoiceData) {
                audioPlayer.interrupt();
                if (lastVoiceData.chunks && lastVoiceData.chunks.length > 0) {
                    lastVoiceData.chunks.forEach((chunk, i) => {
                        audioPlayer.enqueue({
                            index: chunk.index !== undefined ? chunk.index : i,
                            audio_url: chunk.audio_url,
                            sentence: chunk.sentence || ''
                        });
                    });
                } else if (lastVoiceData.audio_url) {
                    audioPlayer.enqueue({
                        index: 0,
                        audio_url: lastVoiceData.audio_url,
                        sentence: lastVoiceData.sentence || ''
                    });
                }
            }
        });
    }

    if (btnVnDownload) {
        btnVnDownload.addEventListener('click', () => {
            if (lastVoiceData) {
                let targetUrl = lastVoiceData.audio_url;
                if (!targetUrl && lastVoiceData.chunks && lastVoiceData.chunks.length > 0) {
                    targetUrl = lastVoiceData.chunks[0].audio_url;
                }
                if (targetUrl) {
                    downloadAudioFile(targetUrl, 'full_dialogue.wav');
                } else {
                    alert('暂无可下载的语音母带');
                }
            } else {
                alert('暂无可下载的语音母带');
            }
        });
    }

    // 11. Legacy Backlog History Modal & Backwards-Compatibility
    if (btnVnHistory) {
        btnVnHistory.addEventListener('click', () => {
            logDrawerController.open();
        });
    }

    function closeHistoryModal() {
        if (historyModal) historyModal.style.display = 'none';
        logDrawerController.close();
    }

    if (btnCloseHistory) btnCloseHistory.addEventListener('click', closeHistoryModal);
    if (btnCloseHistory2) btnCloseHistory2.addEventListener('click', closeHistoryModal);

    function renderHistoryModal() {
        logDrawerController.render();
    }

    // 12. Reset Context
    async function handleResetContext() {
        if (!confirm('确定要清空当前对话上下文并开启新会话吗？')) return;
        audioPlayer.interrupt();
        autoModeController.cancel();
        try {
            await fetch(`/api/chat/history?session_id=${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
        } catch (e) {
            console.warn('Failed to delete remote history:', e);
        }

        sessionId = 'sess_' + Math.random().toString(36).substring(2, 10);
        localStorage.setItem('g2v_session_id', sessionId);
        currentDialogueHistory = [];

        if (chatLog) {
            chatLog.innerHTML = `
                <div class="message system-message glass-card">
                    🌸 已开启新的对话会话。双语流式对话与低延迟日配语音合成引擎已就绪。
                </div>
            `;
        }
        if (vnTextJa) vnTextJa.textContent = '……';
        if (vnTextZh) vnTextZh.textContent = '（已重置记忆，请开始新的对话）';
        pendingFullTextJa = '';
        pendingFullTextZh = '';
        lastVoiceData = null;
        emotionManager.setEmotion('gentle');
        closeHistoryModal();
    }

    if (resetContextBtn) resetContextBtn.addEventListener('click', handleResetContext);
    if (btnClearHistoryModal) btnClearHistoryModal.addEventListener('click', handleResetContext);

    // 13. Quick Chips Click
    document.querySelectorAll('.quick-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            const prompt = chip.getAttribute('data-prompt');
            if (prompt && promptInput && chatForm) {
                promptInput.value = prompt;
                promptInput.focus();
                chatForm.dispatchEvent(new Event('submit', { cancelable: true }));
            }
        });
    });

    // 14. Submit & SSE Streaming
    if (chatForm) {
        chatForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const text = promptInput ? promptInput.value.trim() : '';
            if (!text || isStreaming) return;

            if (promptInput) {
                promptInput.value = '';
                promptInput.disabled = true;
            }
            if (sendBtn) sendBtn.disabled = true;
            isStreaming = true;

            // Stop ongoing audio playback, timer, and reset
            audioPlayer.interrupt();
            autoModeController.cancel();
            skipTypewriter();

            const timeNow = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

            // Append user message
            appendUserMessage(text);
            currentDialogueHistory.push({ role: 'user', content_chinese: text, timestamp: timeNow });

            // Visual Novel mode loading state
            if (vnTextJa) vnTextJa.textContent = '……正在思考中……';
            if (vnTextZh) vnTextZh.textContent = `「${text}」`;
            if (vnAudioStatus) vnAudioStatus.textContent = '正在思考并合成语音...';

            pendingFullTextJa = '';
            pendingFullTextZh = '';

            // Prepare placeholder assistant message in Chat View
            const assistantHolder = createAssistantMessageHolder();
            if (chatLog) {
                chatLog.appendChild(assistantHolder.element);
                chatLog.scrollTop = chatLog.scrollHeight;
            }

            abortController = new AbortController();

            let fullChinese = '';
            let fullJapanese = '';
            let lastAudioUrl = '';
            let lastEmotion = 'gentle';
            let currentMessageChunks = [];

            try {
                const resp = await fetch('/api/chat/stream', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        session_id: sessionId,
                        prompt: text,
                        stream: true
                    }),
                    signal: abortController.signal
                });

                if (!resp.ok) {
                    throw new Error(`HTTP error ${resp.status}`);
                }

                const reader = resp.body.getReader();
                const decoder = new TextDecoder('utf-8');
                let buffer = '';
                let currentEventType = 'message';

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;

                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop(); // Keep incomplete line

                    for (const line of lines) {
                        const trimmed = line.trim();
                        if (!trimmed) {
                            currentEventType = 'message';
                            continue;
                        }

                        // 1. Check SSE Event Line
                        if (trimmed.startsWith('event:')) {
                            currentEventType = trimmed.substring(6).trim();
                            continue;
                        }

                        // 2. Check SSE Data Line
                        if (trimmed.startsWith('data:')) {
                            const jsonStr = trimmed.substring(5).trim();
                            if (jsonStr === '[DONE]') continue;

                            try {
                                const payload = JSON.parse(jsonStr);

                                // Check for emotion update
                                if (payload.emotion) {
                                    lastEmotion = payload.emotion;
                                    emotionManager.setEmotion(payload.emotion);
                                }

                                // Case A: Text Event (Chinese streaming delta/full)
                                if (currentEventType === 'text' || payload.delta_chinese !== undefined || payload.full_chinese !== undefined) {
                                    const delta = payload.delta_chinese || '';
                                    if (payload.full_chinese) {
                                        fullChinese = payload.full_chinese;
                                    } else {
                                        fullChinese += delta;
                                    }
                                    pendingFullTextZh = fullChinese;

                                    assistantHolder.update(fullChinese, fullJapanese);

                                    if (vnTextZh) {
                                        vnTextZh.textContent = `（${fullChinese}）`;
                                    }
                                }
                                // Case B: Audio Chunk Event (Sentence ready for TTS playback)
                                else if (currentEventType === 'audio_chunk' || (payload.audio_url && payload.index !== undefined)) {
                                    lastAudioUrl = payload.audio_url;
                                    const chunkObj = {
                                        index: payload.index !== undefined ? payload.index : currentMessageChunks.length,
                                        audio_url: payload.audio_url,
                                        sentence: payload.sentence || ''
                                    };
                                    currentMessageChunks.push(chunkObj);

                                    if (payload.sentence) {
                                        fullJapanese = payload.sentence;
                                        pendingFullTextJa = fullJapanese;
                                        if (vnTextJa) vnTextJa.textContent = fullJapanese;
                                    }
                                    lastVoiceData = {
                                        audio_url: payload.audio_url,
                                        chunks: currentMessageChunks,
                                        sentence: fullJapanese
                                    };
                                    assistantHolder.update(fullChinese, fullJapanese);

                                    audioPlayer.enqueue(chunkObj);
                                }
                                // Case C: Final Done Event
                                else if (currentEventType === 'done' || (payload.chinese && payload.japanese)) {
                                    if (payload.chinese) fullChinese = payload.chinese;
                                    if (payload.japanese) fullJapanese = payload.japanese;
                                    if (payload.emotion) {
                                        lastEmotion = payload.emotion;
                                        emotionManager.setEmotion(payload.emotion);
                                    }
                                    if (payload.affection) {
                                        updateAffectionDisplay(payload.affection);
                                    }
                                    if (payload.total_audio_url) {
                                        lastAudioUrl = payload.total_audio_url;
                                    } else if (payload.audio_url) {
                                        lastAudioUrl = payload.audio_url;
                                    }
                                    if (payload.chunks && payload.chunks.length > 0) {
                                        currentMessageChunks = payload.chunks;
                                    }

                                    pendingFullTextJa = fullJapanese;
                                    pendingFullTextZh = fullChinese;

                                    lastVoiceData = {
                                        audio_url: lastAudioUrl,
                                        chunks: currentMessageChunks,
                                        sentence: fullJapanese
                                    };

                                    if (vnTextJa && fullJapanese) vnTextJa.textContent = fullJapanese;
                                    if (vnTextZh && fullChinese) vnTextZh.textContent = `（${fullChinese}）`;
                                }
                                // Case D: Error Event
                                else if (currentEventType === 'error' || payload.error) {
                                    const errMsg = payload.error || payload.message || '未知异常';
                                    console.error('SSE backend error:', errMsg);
                                    appendSystemMessage(`❌ 服务异常: ${errMsg}`);
                                }
                            } catch (pErr) {
                                console.warn('JSON parse error on SSE line:', pErr, jsonStr);
                            }
                        }
                    }
                }

                // Finished response stream
                assistantHolder.finish({
                    audio_url: lastAudioUrl,
                    chunks: currentMessageChunks,
                    sentence: fullJapanese
                });

                const assistantEntry = {
                    role: 'assistant',
                    content_chinese: fullChinese,
                    content_japanese: fullJapanese,
                    audio_url: lastAudioUrl,
                    chunks: currentMessageChunks,
                    emotion: lastEmotion,
                    timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
                };
                currentDialogueHistory.push(assistantEntry);

                if (vnTextJa) vnTextJa.textContent = fullJapanese || fullChinese || '……';
                if (vnTextZh) vnTextZh.textContent = fullJapanese ? `（${fullChinese}）` : '';

            } catch (err) {
                if (err.name !== 'AbortError') {
                    console.error('Chat stream error:', err);
                    appendSystemMessage(`❌ 对话请求失败: ${err.message}`);
                    if (vnTextJa) vnTextJa.textContent = '发生网络或服务异常，请重试';
                }
            } finally {
                isStreaming = false;
                if (promptInput) {
                    promptInput.disabled = false;
                    promptInput.focus();
                }
                if (sendBtn) sendBtn.disabled = false;
            }
        });
    }

    // 15. Message DOM Builders
    function appendUserMessage(text) {
        if (!chatLog) return;
        const msgDiv = document.createElement('div');
        msgDiv.className = 'message user-message';
        msgDiv.textContent = text;
        chatLog.appendChild(msgDiv);
        chatLog.scrollTop = chatLog.scrollHeight;
    }

    function appendSystemMessage(text) {
        if (!chatLog) return;
        const msgDiv = document.createElement('div');
        msgDiv.className = 'message system-message glass-card';
        msgDiv.textContent = text;
        chatLog.appendChild(msgDiv);
        chatLog.scrollTop = chatLog.scrollHeight;
    }

    function createAssistantMessageHolder() {
        const msgDiv = document.createElement('div');
        msgDiv.className = 'message assistant-message';
        const characterName = (vnCharacterName && vnCharacterName.textContent) || 'AI 伴侣';
        msgDiv.innerHTML = `
            <div style="font-size: 11px; font-weight: 700; color: var(--primary-purple); margin-bottom: 4px;">
                ${escapeHtml(characterName)}
            </div>
            <div class="msg-ja" style="font-size: 15px; font-weight: 700; color: #0f172a; margin-bottom: 4px; display: none;"></div>
            <div class="msg-zh" style="font-size: 13px; color: #475569;">思考中...</div>
            <div class="msg-actions" style="margin-top: 8px; display: none;">
                <button type="button" class="btn-play-bubble vn-action-btn" title="连续播放所有语音分句">▶️ 播放台词</button>
                <button type="button" class="btn-download-bubble vn-action-btn" title="下载合并母带音频 (WAV)">💾 下载母带</button>
            </div>
        `;

        const jaEl = msgDiv.querySelector('.msg-ja');
        const zhEl = msgDiv.querySelector('.msg-zh');
        const actionsEl = msgDiv.querySelector('.msg-actions');
        const playBtn = msgDiv.querySelector('.btn-play-bubble');
        const downloadBtn = msgDiv.querySelector('.btn-download-bubble');

        return {
            element: msgDiv,
            update: (zh, ja) => {
                if (zh) zhEl.textContent = zh;
                if (ja) {
                    jaEl.textContent = ja;
                    jaEl.style.display = 'block';
                }
            },
            finish: (audioInfo) => {
                if (audioInfo) {
                    actionsEl.style.display = 'flex';
                    actionsEl.style.gap = '8px';
                    playBtn.onclick = () => {
                        audioPlayer.interrupt();
                        if (typeof audioInfo === 'object' && audioInfo.chunks && audioInfo.chunks.length > 0) {
                            audioInfo.chunks.forEach((chunk, i) => {
                                audioPlayer.enqueue({
                                    index: chunk.index !== undefined ? chunk.index : i,
                                    audio_url: chunk.audio_url,
                                    sentence: chunk.sentence || ''
                                });
                            });
                        } else if (typeof audioInfo === 'object' && audioInfo.audio_url) {
                            audioPlayer.enqueue({
                                index: 0,
                                audio_url: audioInfo.audio_url,
                                sentence: audioInfo.sentence || jaEl.textContent || ''
                            });
                        } else if (typeof audioInfo === 'string') {
                            audioPlayer.enqueue({
                                index: 0,
                                audio_url: audioInfo,
                                sentence: jaEl.textContent || ''
                            });
                        }
                    };

                    if (downloadBtn) {
                        downloadBtn.onclick = () => {
                            let url = '';
                            if (typeof audioInfo === 'object') {
                                url = audioInfo.audio_url || (audioInfo.chunks && audioInfo.chunks[0] && audioInfo.chunks[0].audio_url) || '';
                            } else if (typeof audioInfo === 'string') {
                                url = audioInfo;
                            }
                            if (url) {
                                downloadAudioFile(url, 'full_dialogue.wav');
                            } else {
                                alert('暂无可下载的语音母带');
                            }
                        };
                    }
                }
            }
        };
    }

    function renderCompletedAssistantMessage(data) {
        if (!chatLog) return;
        const holder = createAssistantMessageHolder();
        holder.update(data.chinese, data.japanese);
        holder.finish({
            audio_url: data.audio_url,
            chunks: data.chunks || [],
            sentence: data.japanese
        });
        chatLog.appendChild(holder.element);
    }

    function escapeHtml(str) {
        return (str || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    // 16. Initial Load & Polling
    checkSystemStatus();
    loadVoiceProfiles();
    loadHistory();
    setInterval(checkSystemStatus, 5000);
});

// Export classes for module environments (Node / Jest / Testing)
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        AutoModeController,
        SkipController,
        EmotionManager,
        LogDrawerController,
    };
}
