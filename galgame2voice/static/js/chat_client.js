/**
 * Galgame2Voice - High-End Anime/Galgame Cinematic Chat Client
 * 
 * Modular architecture:
 *  - AutoModeController: Auto-progression state machine
 *  - SkipController: Typewriter & audio fast-forward
 *  - EmotionManager: Emotion aura & avatar transitions
 *  - LogDrawerController: Backlog drawer & replay manager
 *  - ChatApp: Main SSE client & event orchestrator
 */

// ============================================================================
// 1. Core Immersion Controllers
// ============================================================================

class AutoModeController {
    constructor(options = {}) {
        this.enabled = false;
        this.delayMs = options.defaultDelayMs || 1500;
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
            const textEl = this.btnEl.querySelector('.btn-text') || this.btnEl;
            if (textEl) textEl.textContent = 'AUTO [ON]';
        } else {
            this.btnEl.classList.remove('active');
            const textEl = this.btnEl.querySelector('.btn-text') || this.btnEl;
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

class EmotionManager {
    static EMOTIONS = {
        gentle:   { name: '温柔', emoji: '🌸', tag: '温柔' },
        shy:      { name: '害羞', emoji: '😳', tag: '害羞' },
        happy:    { name: '开心', emoji: '✨', tag: '开心' },
        tsundere: { name: '傲娇', emoji: '😤', tag: '傲娇' },
        cool:     { name: '高冷', emoji: '❄️', tag: '高冷' },
        sad:      { name: '难过', emoji: '🥺', tag: '难过' },
        normal:   { name: '平静', emoji: '🌸', tag: '平静' }
    };

    constructor(options = {}) {
        this.backdropEl = options.backdropEl || null;
        this.avatarEmojiEl = options.avatarEmojiEl || null;
        this.badgeEl = options.badgeEl || null;
        this.currentEmotion = 'gentle';
    }

    setEmotion(emotion) {
        const emo = (emotion || 'gentle').toLowerCase();
        const info = EmotionManager.EMOTIONS[emo] || EmotionManager.EMOTIONS.gentle;
        this.currentEmotion = emo;

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
            }, 300);
        }
    }

    render() {
        const body = this.bodyEl || document.getElementById('log-drawer-body');
        if (!body) return;
        body.innerHTML = '';
        const history = this.getHistory();
        if (!history || history.length === 0) {
            body.innerHTML = '<div style="text-align: center; color: var(--c-text-faint); padding: 40px 20px; font-size: 13px;">暂无历史对话记录</div>';
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
                            ▶️ 重播
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
// 2. Application Setup & Lifecycle
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
    // 1. Elements
    const vnView = document.getElementById('vn-view');
    const chatView = document.getElementById('chat-view');
    const btnModeVn = document.getElementById('btn-mode-vn');
    const btnModeChat = document.getElementById('btn-mode-chat');

    const chatLog = document.getElementById('chat-log');
    const chatForm = document.getElementById('chat-form');
    const promptInput = document.getElementById('prompt-input');
    const sendBtn = document.getElementById('send-btn');
    const resetContextBtn = document.getElementById('reset-context-btn');

    const vnCharacterName = document.getElementById('vn-character-name');
    const vnTextJa = document.getElementById('vn-text-ja');
    const vnTextZh = document.getElementById('vn-text-zh');
    const vnAudioStatus = document.getElementById('vn-audio-status');
    const vnAudioEqualizer = document.getElementById('vn-audio-equalizer');
    const vnEmotionBadge = document.getElementById('vn-emotion-badge');
    const vnAffectionBadge = document.getElementById('vn-affection-badge');

    const btnVnAuto = document.getElementById('btn-vn-auto');
    const btnVnSkip = document.getElementById('btn-vn-skip');
    const btnVnLog = document.getElementById('btn-vn-log');
    const btnVnReplay = document.getElementById('btn-vn-replay');
    const btnVnDownload = document.getElementById('btn-vn-download');
    const btnVnHistory = document.getElementById('btn-vn-history');

    const logDrawer = document.getElementById('vn-log-drawer');
    const logDrawerBackdrop = document.getElementById('log-drawer-backdrop');
    const logDrawerBody = document.getElementById('log-drawer-body');
    const btnCloseLogDrawer = document.getElementById('btn-close-log-drawer');
    const btnCloseLogDrawer2 = document.getElementById('btn-close-log-drawer-2');
    const btnClearLogHistory = document.getElementById('btn-clear-log-history');

    const quickVoiceSelect = document.getElementById('quick-voice-select');
    const sovitsStatusBadge = document.getElementById('sovits-status-badge');
    const muteToggleBtn = document.getElementById('mute-toggle-btn');
    const volumeSlider = document.getElementById('volume-slider');
    const volumeLabel = document.getElementById('volume-label');
    const affectionLabel = document.getElementById('affection-label');

    const historyModal = document.getElementById('history-modal');
    const historyModalBody = document.getElementById('history-modal-body');
    const btnCloseHistory = document.getElementById('btn-close-history');
    const btnCloseHistory2 = document.getElementById('btn-close-history-2');
    const btnClearHistoryModal = document.getElementById('btn-clear-history-modal');

    // 2. Session & State
    let sessionId = localStorage.getItem('g2v_session_id') || ('sess_' + Math.random().toString(36).substring(2, 10));
    localStorage.setItem('g2v_session_id', sessionId);

    let currentMode = localStorage.getItem('g2v_view_mode') || 'vn';
    let isStreaming = false;
    let abortController = null;
    let lastVoiceData = null;
    let currentDialogueHistory = [];

    // Typewriter state
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

    const vnDialogueBox = document.querySelector('.vn-dialogue-box');
    if (vnDialogueBox) {
        vnDialogueBox.addEventListener('click', (e) => {
            if (!e.target.closest('button') && !e.target.closest('select')) {
                skipTypewriter();
            }
        });
    }

    // 3. Audio Player Instance
    const audioPlayer = new (window.StreamingAudioPlayer || StreamingAudioPlayer)({
        crossFadeDuration: 0.025,
        equalizerElement: vnAudioEqualizer,
        onStatusChange: (statusText, isPlaying) => {
            if (vnAudioStatus) vnAudioStatus.textContent = statusText;
            if (vnAudioEqualizer) {
                if (isPlaying) vnAudioEqualizer.classList.add('playing');
                else vnAudioEqualizer.classList.remove('playing');
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

    const unlockAudio = () => {
        audioPlayer.initContext();
        document.removeEventListener('click', unlockAudio);
        document.removeEventListener('keydown', unlockAudio);
    };
    document.addEventListener('click', unlockAudio);
    document.addEventListener('keydown', unlockAudio);

    // 4. Controllers
    const emotionManager = new EmotionManager({
        backdropEl: document.querySelector('.character-backdrop'),
        avatarEmojiEl: document.querySelector('.avatar-emoji'),
        badgeEl: vnEmotionBadge,
    });

    const autoModeController = new AutoModeController({
        defaultDelayMs: 1500,
        btnEl: btnVnAuto,
        onAdvance: () => {
            if (vnAudioStatus) vnAudioStatus.textContent = '自动推进就绪';
        }
    });

    const skipController = new SkipController({
        btnEl: btnVnSkip,
        onSkip: () => {
            skipTypewriter();
            audioPlayer.interrupt();
            if (vnAudioStatus) vnAudioStatus.textContent = '已跳过';
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

    audioPlayer.onQueueEmpty = () => {
        autoModeController.onAudioQueueFinished();
    };

    // Expose for testing & debug
    window.autoModeController = autoModeController;
    window.skipController = skipController;
    window.emotionManager = emotionManager;
    window.logDrawerController = logDrawerController;
    window.audioPlayer = audioPlayer;
    window.skipTypewriter = skipTypewriter;

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

    // 6. Volume & Mute
    if (muteToggleBtn) {
        muteToggleBtn.addEventListener('click', () => {
            const isMuted = !audioPlayer.isMuted;
            audioPlayer.setMuted(isMuted);
            muteToggleBtn.classList.toggle('muted', isMuted);
            muteToggleBtn.title = isMuted ? '点击取消静音' : '点击静音';
        });
    }

    if (volumeSlider) {
        volumeSlider.addEventListener('input', (e) => {
            const val = parseInt(e.target.value, 10);
            audioPlayer.setVolume(val / 100);
            if (volumeLabel) volumeLabel.textContent = `${val}`;
            if (audioPlayer.isMuted && val > 0) {
                audioPlayer.setMuted(false);
                if (muteToggleBtn) muteToggleBtn.classList.remove('muted');
            }
        });
    }

    // 7. Voice Profiles Dropdown
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
                    const charInline = document.querySelector('.character-name-inline');
                    if (charInline) charInline.textContent = activeProfile.name;
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
                    const charInline = document.querySelector('.character-name-inline');
                    if (charInline) charInline.textContent = selectedText;
                    logDrawerController.characterName = selectedText;
                    appendSystemMessage(`已切换角色音色为: ${selectedText}`);
                }
            } catch (err) {
                console.error('Failed to switch voice profile:', err);
            }
        });
    }

    // 8. System Status
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
                        ? (latency ? `GPT-SoVITS (${latency}ms)` : 'GPT-SoVITS 在线')
                        : 'GPT-SoVITS 离线';
                }
            }
        } catch (e) {
            sovitsStatusBadge.className = 'badge badge-danger';
            const textEl = sovitsStatusBadge.querySelector('.status-text');
            if (textEl) textEl.textContent = 'GPT-SoVITS 离线';
        }
    }

    // 9. Affection Display
    function updateAffectionDisplay(aff) {
        if (!aff) return;
        const level = aff.level || aff.affection_level || 1;
        const name = aff.level_name || '初识';
        const score = aff.score !== undefined ? aff.score : (aff.affection_score !== undefined ? aff.affection_score : 0);
        
        if (affectionLabel) {
            affectionLabel.textContent = `Lv.${level} ${name} (${score}/100)`;
        }
        if (vnAffectionBadge) {
            vnAffectionBadge.textContent = `Lv.${level}`;
        }
    }

    async function fetchInitialAffection() {
        try {
            const res = await fetch('/api/affection');
            if (res.ok) {
                const data = await res.json();
                updateAffectionDisplay(data);
                if (data.current_emotion) {
                    emotionManager.setEmotion(data.current_emotion);
                }
            }
        } catch (e) {
            console.debug('Failed to fetch initial affection:', e);
        }
    }

    // 10. Chat History
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
                                audio_url: msg.audio_url || '',
                                chunks: msg.chunks || []
                            });
                        }
                    });

                    if (lastAssistantMsg) {
                        if (vnTextJa) vnTextJa.textContent = lastAssistantMsg.content_japanese || '……';
                        if (vnTextZh) vnTextZh.textContent = `（${lastAssistantMsg.content_chinese || ''}）`;
                        pendingFullTextJa = lastAssistantMsg.content_japanese || '';
                        pendingFullTextZh = lastAssistantMsg.content_chinese || '';
                        lastVoiceData = {
                            audio_url: lastAssistantMsg.audio_url,
                            chunks: lastAssistantMsg.chunks,
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

    // 11. Download Audio
    function downloadAudioFile(audioUrl, defaultName = 'voice_master.wav') {
        if (!audioUrl) {
            alert('暂无可下载的语音母带');
            return;
        }
        const a = document.createElement('a');
        a.href = audioUrl;
        const parts = audioUrl.split('/');
        a.download = parts[parts.length - 1] || defaultName;
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
                downloadAudioFile(targetUrl, 'dialogue.wav');
            } else {
                alert('暂无可下载的语音母带');
            }
        });
    }

    if (btnVnHistory) {
        btnVnHistory.addEventListener('click', () => {
            logDrawerController.open();
        });
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
                    已开启新的会话。
                </div>
            `;
        }
        if (vnTextJa) vnTextJa.textContent = '……';
        if (vnTextZh) vnTextZh.textContent = '（已重置记忆，请开始新的对话）';
        pendingFullTextJa = '';
        pendingFullTextZh = '';
        lastVoiceData = null;
        emotionManager.setEmotion('gentle');
        logDrawerController.close();
        if (historyModal) historyModal.style.display = 'none';
    }

    if (resetContextBtn) resetContextBtn.addEventListener('click', handleResetContext);
    if (btnClearHistoryModal) btnClearHistoryModal.addEventListener('click', handleResetContext);
    if (btnCloseHistory) btnCloseHistory.addEventListener('click', () => { if (historyModal) historyModal.style.display = 'none'; });
    if (btnCloseHistory2) btnCloseHistory2.addEventListener('click', () => { if (historyModal) historyModal.style.display = 'none'; });

    // 13. Quick Chips
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

    // 14. Streaming Chat Form Submit
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

            audioPlayer.interrupt();
            autoModeController.cancel();
            skipTypewriter();

            const timeNow = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

            appendUserMessage(text);
            currentDialogueHistory.push({ role: 'user', content_chinese: text, timestamp: timeNow });

            if (vnTextJa) vnTextJa.textContent = '……正在思考中……';
            if (vnTextZh) vnTextZh.textContent = `「${text}」`;
            if (vnAudioStatus) vnAudioStatus.textContent = '正在思考并合成语音...';

            pendingFullTextJa = '';
            pendingFullTextZh = '';

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
                    buffer = lines.pop();

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

                            try {
                                const payload = JSON.parse(jsonStr);

                                if (payload.emotion) {
                                    lastEmotion = payload.emotion;
                                    emotionManager.setEmotion(payload.emotion);
                                }

                                if (currentEventType === 'text' || payload.delta_chinese !== undefined || payload.full_chinese !== undefined) {
                                    const delta = payload.delta_chinese || '';
                                    if (payload.full_chinese) {
                                        fullChinese = payload.full_chinese;
                                    } else {
                                        fullChinese += delta;
                                    }
                                    pendingFullTextZh = fullChinese;
                                    assistantHolder.update(fullChinese, fullJapanese);
                                    if (vnTextZh) vnTextZh.textContent = `（${fullChinese}）`;
                                } else if (currentEventType === 'audio_chunk' || (payload.audio_url && payload.index !== undefined)) {
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
                                } else if (currentEventType === 'done' || (payload.chinese && payload.japanese)) {
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
                                } else if (currentEventType === 'error' || payload.error) {
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

    // 15. Helper Renderers
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
        const characterName = (vnCharacterName && vnCharacterName.textContent) || '四季夏目';
        msgDiv.innerHTML = `
            <div class="msg-speaker">${escapeHtml(characterName)}</div>
            <div class="msg-ja" style="display: none;"></div>
            <div class="msg-zh">思考中...</div>
            <div class="msg-actions" style="display: none;">
                <button type="button" class="btn-play-bubble vn-action-btn" title="播放台词">▶️ 播放</button>
                <button type="button" class="btn-download-bubble vn-action-btn" title="下载母带">💾 下载</button>
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
                if (audioInfo && (audioInfo.audio_url || (audioInfo.chunks && audioInfo.chunks.length > 0))) {
                    actionsEl.style.display = 'flex';
                    playBtn.onclick = () => {
                        audioPlayer.interrupt();
                        if (audioInfo.chunks && audioInfo.chunks.length > 0) {
                            audioInfo.chunks.forEach((chunk, i) => {
                                audioPlayer.enqueue({
                                    index: chunk.index !== undefined ? chunk.index : i,
                                    audio_url: chunk.audio_url,
                                    sentence: chunk.sentence || ''
                                });
                            });
                        } else if (audioInfo.audio_url) {
                            audioPlayer.enqueue({
                                index: 0,
                                audio_url: audioInfo.audio_url,
                                sentence: audioInfo.sentence || jaEl.textContent || ''
                            });
                        }
                    };

                    if (downloadBtn) {
                        downloadBtn.onclick = () => {
                            let url = audioInfo.audio_url || (audioInfo.chunks && audioInfo.chunks[0] && audioInfo.chunks[0].audio_url) || '';
                            if (url) downloadAudioFile(url, 'full_dialogue.wav');
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

    // Initial Load
    checkSystemStatus();
    loadVoiceProfiles();
    loadHistory();
    setInterval(checkSystemStatus, 5000);
});

// Export classes for testing
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        AutoModeController,
        SkipController,
        EmotionManager,
        LogDrawerController,
    };
}
