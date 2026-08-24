/**
 * ChatClient - Commercial-Grade Galgame Immersion & SSE Bilingual Streaming Client
 * Features:
 *  - Dual Mode (🌸 Galgame Visual Novel Immersion Mode & 💬 Classic Chat Stream Mode)
 *  - Robust SSE Parser for 'event: text', 'event: audio_chunk', 'event: done', 'event: error'
 *  - Web Audio API AnalyserNode soundwave visualizer & dynamic equalizer
 *  - Click-to-skip text display / typewriter animation
 *  - Top Capsule Controls: instant voice switcher, live GPT-SoVITS status & latency, volume gain, mute toggle, reset
 *  - Full dialogue Backlog history modal and sentence replay
 */

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
    const btnVnReplay = document.getElementById('btn-vn-replay');
    const btnVnDownload = document.getElementById('btn-vn-download');
    const btnVnHistory = document.getElementById('btn-vn-history');

    // Header Capsule Controls
    const quickVoiceSelect = document.getElementById('quick-voice-select');
    const sovitsStatusBadge = document.getElementById('sovits-status-badge');
    const muteToggleBtn = document.getElementById('mute-toggle-btn');
    const volumeSlider = document.getElementById('volume-slider');
    const volumeLabel = document.getElementById('volume-label');

    // History Modal Elements
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
        vnDialogueBox.addEventListener('click', skipTypewriter);
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

    // 4. Dual View Mode Switching
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

    // 5. Volume & Mute Controls
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

    // 6. Quick Character Dropdown
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
                if (activeProfile && vnCharacterName) {
                    vnCharacterName.textContent = activeProfile.name;
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
                    appendSystemMessage(`已切换角色音色为: ${selectedText}`);
                }
            } catch (err) {
                console.error('Failed to switch voice profile:', err);
            }
        });
    }

    // 7. System Health Status with Live Latency Check
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

    // 8. Load Chat History
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
                    }
                }
            }
        } catch (e) {
            console.warn('Failed to load chat history:', e);
        }
    }

    // 9. Replay & Download Master Audio
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

    // 10. Backlog History Modal
    if (btnVnHistory) {
        btnVnHistory.addEventListener('click', () => {
            renderHistoryModal();
            if (historyModal) historyModal.style.display = 'flex';
        });
    }

    function closeHistoryModal() {
        if (historyModal) historyModal.style.display = 'none';
    }

    if (btnCloseHistory) btnCloseHistory.addEventListener('click', closeHistoryModal);
    if (btnCloseHistory2) btnCloseHistory2.addEventListener('click', closeHistoryModal);

    function renderHistoryModal() {
        if (!historyModalBody) return;
        historyModalBody.innerHTML = '';
        if (!currentDialogueHistory || currentDialogueHistory.length === 0) {
            historyModalBody.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 20px;">暂无历史对话记录</div>';
            return;
        }
        currentDialogueHistory.forEach(item => {
            const row = document.createElement('div');
            row.style.padding = '10px 14px';
            row.style.borderRadius = '10px';
            row.style.background = item.role === 'user' ? 'rgba(236, 72, 153, 0.08)' : 'rgba(255, 255, 255, 0.9)';
            row.style.border = '1px solid #e2e8f0';

            const sender = item.role === 'user' ? '你' : ((vnCharacterName && vnCharacterName.textContent) || 'AI 伴侣');
            row.innerHTML = `
                <div style="font-size: 11px; font-weight: 700; color: ${item.role === 'user' ? 'var(--primary-pink)' : 'var(--primary-purple)'}; margin-bottom: 4px;">
                    ${sender}:
                </div>
                ${item.content_japanese ? `<div style="font-size: 13px; font-weight: 600; color: #0f172a;">${escapeHtml(item.content_japanese)}</div>` : ''}
                <div style="font-size: 12px; color: ${item.content_japanese ? '#64748b' : '#1e293b'};">${escapeHtml(item.content_chinese || item.content || '')}</div>
            `;
            historyModalBody.appendChild(row);
        });
        historyModalBody.scrollTop = historyModalBody.scrollHeight;
    }

    // 11. Reset Context
    async function handleResetContext() {
        if (!confirm('确定要清空当前对话上下文并开启新会话吗？')) return;
        audioPlayer.interrupt();
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
        closeHistoryModal();
    }

    if (resetContextBtn) resetContextBtn.addEventListener('click', handleResetContext);
    if (btnClearHistoryModal) btnClearHistoryModal.addEventListener('click', handleResetContext);

    // 12. Quick Chips Click
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

    // 13. Submit & SSE Streaming
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

            // Stop any ongoing audio playback and reset visualizer
            audioPlayer.interrupt();
            skipTypewriter();

            // Append user message
            appendUserMessage(text);
            currentDialogueHistory.push({ role: 'user', content_chinese: text });

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
                currentDialogueHistory.push({
                    role: 'assistant',
                    content_chinese: fullChinese,
                    content_japanese: fullJapanese,
                    audio_url: lastAudioUrl,
                    chunks: currentMessageChunks
                });

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

    // 14. Message DOM Builders
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

    // 15. Initial Load & Polling
    checkSystemStatus();
    loadVoiceProfiles();
    loadHistory();
    setInterval(checkSystemStatus, 5000);
});
