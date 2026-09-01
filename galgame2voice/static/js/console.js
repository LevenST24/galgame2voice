        // State store
        const state = {
            settings: {},
            providers: [],
            presets: [],
            voiceProfiles: [],
            activeProfileId: 1,
            activeProvider: null,
            telemetry: null,
            autoRefreshTimer: null
        };

        // HTML Sanitizer to prevent XSS
        function escapeHtml(str) {
            if (str === null || str === undefined) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }

        // Toast Helper
        function showToast(message, type = 'info', duration = 3500) {
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            toast.className = `toast-msg toast-${type}`;
            const icons = { success: '✅', error: '❌', info: 'ℹ️', warning: '⚠️' };
            toast.innerHTML = `<span>${icons[type] || 'ℹ️'}</span> <span>${escapeHtml(message)}</span>`;
            container.appendChild(toast);
            setTimeout(() => {
                toast.style.opacity = '0';
                toast.style.transform = 'translateX(100%)';
                toast.style.transition = 'all 0.3s';
                setTimeout(() => toast.remove(), 300);
            }, duration);
        }

        // Tab Navigation
        document.querySelectorAll('.nav-item').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
                document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
                btn.classList.add('active');
                const tabId = btn.getAttribute('data-tab');
                const targetPanel = document.getElementById(`tab-${tabId}`);
                if (targetPanel) targetPanel.classList.add('active');
            });
        });

        // Setup Range Slider sync with number input
        function bindSliderAndNumber(sliderId, numId, displayId, suffix = '') {
            const slider = document.getElementById(sliderId);
            const num = document.getElementById(numId);
            const display = document.getElementById(displayId);
            if (!slider || !num) return;

            const update = (val) => {
                slider.value = val;
                num.value = val;
                if (display) display.textContent = `${val}${suffix}`;
            };

            slider.addEventListener('input', (e) => update(e.target.value));
            num.addEventListener('input', (e) => update(e.target.value));
        }

        bindSliderAndNumber('param-speed-factor', 'num-speed-factor', 'val-speed');
        bindSliderAndNumber('param-temperature', 'num-temperature', 'val-temp');
        bindSliderAndNumber('param-top-k', 'num-top-k', 'val-topk');
        bindSliderAndNumber('param-top-p', 'num-top-p', 'val-topp');
        bindSliderAndNumber('param-fragment-interval', 'num-fragment-interval', 'val-fragment', 's');

        // Toggle Key Visibility Helper
        function bindKeyToggle(btnId, inputId) {
            const btn = document.getElementById(btnId);
            const input = document.getElementById(inputId);
            if (!btn || !input) return;
            btn.addEventListener('click', () => {
                if (input.type === 'password') {
                    input.type = 'text';
                    btn.textContent = '🔒';
                } else {
                    input.type = 'password';
                    btn.textContent = '👁️';
                }
            });
        }

        bindKeyToggle('btn-toggle-prov-key', 'prov-api-key');
        bindKeyToggle('btn-toggle-tg-token', 'tg-bot-token');

        // ================= API FETCHERS & RENDERERS =================

        // 1. Fetch System Telemetry
        async function fetchSystemTelemetry() {
            try {
                const resp = await fetch('/api/system/status');
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const data = await resp.json();
                state.telemetry = data;

                // Update Raw Telemetry Box
                const rawBox = document.getElementById('raw-telemetry-json');
                if (rawBox) rawBox.textContent = JSON.stringify(data, null, 2);

                // Update Dashboard Cards
                const sovitsBadge = document.getElementById('dash-sovits-badge');
                const sovitsStatus = document.getElementById('dash-sovits-status');
                const sovitsUrl = document.getElementById('dash-sovits-url');

                if (data.gpt_sovits) {
                    sovitsUrl.textContent = data.gpt_sovits.base_url || 'http://127.0.0.1:9880';
                    if (data.gpt_sovits.status === 'reachable') {
                        sovitsBadge.className = 'badge-status badge-green';
                        sovitsBadge.textContent = '在线 (Online)';
                        sovitsStatus.textContent = `正常 (${data.gpt_sovits.latency_ms || 0} ms)`;
                    } else {
                        sovitsBadge.className = 'badge-status badge-red';
                        sovitsBadge.textContent = '离线 (Offline)';
                        sovitsStatus.textContent = '不可达 (Unreachable)';
                    }
                }

                // App Uptime & Storage
                if (data.app) {
                    const uptimeSec = Math.round(data.app.uptime_seconds || 0);
                    const hours = Math.floor(uptimeSec / 3600);
                    const mins = Math.floor((uptimeSec % 3600) / 60);
                    const secs = uptimeSec % 60;
                    document.getElementById('dash-uptime').textContent = `${hours}h ${mins}m ${secs}s`;
                    document.getElementById('dash-process-info').textContent = 
                        `PID: ${data.app.pid} | Python: ${data.app.python_version} | 内存: ${data.app.memory_usage_mb || 0} MB`;
                }

                if (data.storage) {
                    document.getElementById('dash-storage-info').textContent = 
                        `音频: ${data.storage.audio_files_count} 文件 (${data.storage.audio_dir_size_mb} MB)`;
                }

                // Telegram Telemetry
                if (data.telegram) {
                    const tgBadge = document.getElementById('dash-tg-badge');
                    const tgStatus = document.getElementById('dash-tg-status');
                    if (data.telegram.status === 'running') {
                        tgBadge.className = 'badge-status badge-green';
                        tgBadge.textContent = '运行中';
                        tgStatus.textContent = '已连接 (Online)';
                    } else if (data.telegram.status === 'standby' || data.telegram.enabled) {
                        tgBadge.className = 'badge-status badge-yellow';
                        tgBadge.textContent = '已配置';
                        tgStatus.textContent = '待命 (Standby)';
                    } else {
                        tgBadge.className = 'badge-status badge-gray';
                        tgBadge.textContent = '未启用';
                        tgStatus.textContent = 'Disabled';
                    }
                }
            } catch (err) {
                console.warn('Failed to fetch telemetry:', err);
            }
        }

        // 2. Fetch Global Config & Active Provider
        async function fetchGlobalConfig() {
            try {
                const resp = await fetch('/api/config');
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const data = await resp.json();
                state.settings = data.settings || {};
                state.activeProvider = data.active_provider || null;

                // Update Dashboard Active Provider Card
                if (state.activeProvider) {
                    document.getElementById('dash-provider-name').textContent = state.activeProvider.name;
                    document.getElementById('dash-provider-model').textContent = `${state.activeProvider.chat_model} (${state.activeProvider.id})`;
                } else if (state.settings.active_provider_id) {
                    document.getElementById('dash-provider-name').textContent = state.settings.active_provider_id;
                    document.getElementById('dash-provider-model').textContent = state.settings.active_provider_id;
                }

                // Populate Inference Parameters Form
                const s = state.settings;
                if (s.text_split_method) document.getElementById('param-text-split-method').value = s.text_split_method;
                if (s.speed_factor !== undefined) {
                    document.getElementById('param-speed-factor').value = s.speed_factor;
                    document.getElementById('num-speed-factor').value = s.speed_factor;
                    document.getElementById('val-speed').textContent = s.speed_factor;
                }
                if (s.temperature !== undefined) {
                    document.getElementById('param-temperature').value = s.temperature;
                    document.getElementById('num-temperature').value = s.temperature;
                    document.getElementById('val-temp').textContent = s.temperature;
                }
                if (s.top_k !== undefined) {
                    document.getElementById('param-top-k').value = s.top_k;
                    document.getElementById('num-top-k').value = s.top_k;
                    document.getElementById('val-topk').textContent = s.top_k;
                }
                if (s.top_p !== undefined) {
                    document.getElementById('param-top-p').value = s.top_p;
                    document.getElementById('num-top-p').value = s.top_p;
                    document.getElementById('val-topp').textContent = s.top_p;
                }
                if (s.fragment_interval !== undefined) {
                    document.getElementById('param-fragment-interval').value = s.fragment_interval;
                    document.getElementById('num-fragment-interval').value = s.fragment_interval;
                    document.getElementById('val-fragment').textContent = `${s.fragment_interval}s`;
                }
                if (s.seed !== undefined) document.getElementById('param-seed').value = s.seed;
                if (s.batch_size !== undefined) document.getElementById('param-batch-size').value = s.batch_size;
                if (s.max_history_messages !== undefined) document.getElementById('param-max-history').value = s.max_history_messages;
                if (s.audio_retention_minutes !== undefined) document.getElementById('param-audio-retention').value = s.audio_retention_minutes;
                if (s.gpt_sovits_url) document.getElementById('param-sovits-url').value = s.gpt_sovits_url;

                // Populate Telegram Form
                if (s.telegram_bot_token) document.getElementById('tg-bot-token').value = s.telegram_bot_token;
                if (s.telegram_bot_username) {
                    document.getElementById('tg-bot-username').value = s.telegram_bot_username;
                    document.getElementById('dash-tg-username').textContent = `@${s.telegram_bot_username}`;
                }
                if (s.telegram_proxy_enabled !== undefined) document.getElementById('tg-proxy-enabled').checked = Boolean(s.telegram_proxy_enabled);
                if (s.telegram_proxy_host) document.getElementById('tg-proxy-host').value = s.telegram_proxy_host;
                if (s.telegram_proxy_port) document.getElementById('tg-proxy-port').value = s.telegram_proxy_port;

            } catch (err) {
                console.error('Failed to fetch config:', err);
                showToast(`加载配置失败: ${err.message}`, 'error');
            }
        }

        // 3. Fetch Providers & Presets
        async function fetchProviders() {
            try {
                const resp = await fetch('/api/providers');
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const data = await resp.json();
                state.providers = data.providers || [];
                state.presets = data.presets || [];

                // Render Preset Selector Dropdown
                const presetSelect = document.getElementById('provider-preset-select');
                if (presetSelect) {
                    presetSelect.innerHTML = '<option value="">-- 选择预设模板快速配置 --</option>';
                    state.presets.forEach(p => {
                        const opt = document.createElement('option');
                        opt.value = p.id;
                        opt.textContent = `${p.name} (${p.default_chat_model})`;
                        presetSelect.appendChild(opt);
                    });
                }

                // Render Providers Table
                renderProvidersTable();
            } catch (err) {
                console.error('Failed to fetch providers:', err);
                showToast(`加载提供商失败: ${err.message}`, 'error');
            }
        }

        function renderProvidersTable() {
            const tbody = document.getElementById('providers-table-body');
            if (!tbody) return;
            tbody.innerHTML = '';

            if (state.providers.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">暂无提供商配置</td></tr>';
                return;
            }

            state.providers.forEach(p => {
                const tr = document.createElement('tr');
                const isActive = p.is_active || (state.settings && state.settings.active_provider_id === p.id);
                
                tr.innerHTML = `
                    <td>
                        <span class="badge-status ${isActive ? 'badge-green' : 'badge-gray'}">
                            ${isActive ? '● 当前活动' : '○ 备用'}
                        </span>
                    </td>
                    <td><code>${escapeHtml(p.id)}</code></td>
                    <td><strong>${escapeHtml(p.name)}</strong></td>
                    <td style="font-size: 12px; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(p.api_base_url)}">${escapeHtml(p.api_base_url)}</td>
                    <td><code>${escapeHtml(p.chat_model)}</code></td>
                    <td><code>${escapeHtml(p.api_key || '<无密钥>')}</code></td>
                    <td>
                        <div style="display: flex; gap: 6px;">
                            ${!isActive ? `<button class="btn-action-sm btn-action-success btn-activate-prov" data-id="${escapeHtml(p.id)}" title="设为活动提供商">启用</button>` : ''}
                            <button class="btn-action-sm btn-action-secondary btn-edit-prov" data-id="${escapeHtml(p.id)}">编辑</button>
                            <button class="btn-action-sm btn-action-danger btn-delete-prov" data-id="${escapeHtml(p.id)}">删除</button>
                        </div>
                    </td>
                `;
                tbody.appendChild(tr);
            });

            // Bind Table Action Buttons
            tbody.querySelectorAll('.btn-activate-prov').forEach(b => {
                b.addEventListener('click', () => activateProvider(b.getAttribute('data-id')));
            });
            tbody.querySelectorAll('.btn-edit-prov').forEach(b => {
                b.addEventListener('click', () => editProvider(b.getAttribute('data-id')));
            });
            tbody.querySelectorAll('.btn-delete-prov').forEach(b => {
                b.addEventListener('click', () => deleteProvider(b.getAttribute('data-id')));
            });
        }

        // 4. Fetch Voice Profiles
        async function fetchVoiceProfiles() {
            try {
                const resp = await fetch('/api/voice/profiles');
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const data = await resp.json();
                state.voiceProfiles = data.profiles || [];
                state.activeProfileId = data.active_profile_id || 1;

                renderVoiceProfiles();
            } catch (err) {
                console.error('Failed to fetch voice profiles:', err);
                showToast(`加载角色音色失败: ${err.message}`, 'error');
            }
        }

        function renderVoiceProfiles() {
            const grid = document.getElementById('voice-profiles-grid');
            if (!grid) return;
            grid.innerHTML = '';

            state.voiceProfiles.forEach(vp => {
                const isActive = (vp.id === state.activeProfileId);
                const card = document.createElement('div');
                card.className = `profile-card ${isActive ? 'active-profile' : ''}`;

                if (isActive) {
                    const voiceNameEl = document.getElementById('dash-voice-name');
                    if (voiceNameEl) voiceNameEl.textContent = vp.name;
                    const voiceWeightsEl = document.getElementById('dash-voice-weights');
                    if (voiceWeightsEl && vp.gpt_weights_path) {
                        // Handle both Windows backslash and POSIX slash paths.
                        const parts = String(vp.gpt_weights_path).split(/[\\/]/);
                        voiceWeightsEl.textContent = parts[parts.length - 1];
                    }
                }

                card.innerHTML = `
                    <div class="profile-card-header">
                        <span class="profile-card-title">${escapeHtml(vp.name)}</span>
                        <span class="badge-status ${isActive ? 'badge-green' : 'badge-gray'}">
                            ${isActive ? '● 当前活动音色' : '可选'}
                        </span>
                    </div>
                    <div class="profile-card-desc">${escapeHtml(vp.description || '暂无描述')}</div>
                    <div class="profile-meta-list">
                        <div><strong>GPT:</strong> ${escapeHtml(vp.gpt_weights_path)}</div>
                        <div><strong>SoVITS:</strong> ${escapeHtml(vp.sovits_weights_path)}</div>
                        <div><strong>参考音频:</strong> ${escapeHtml(vp.ref_audio_path || vp.refer_audio_path || '-')}</div>
                        <div><strong>语种:</strong> ${escapeHtml(vp.prompt_lang || 'ja')} ➔ ${escapeHtml(vp.text_lang || 'ja')}</div>
                    </div>
                    <div class="profile-card-actions">
                        ${!isActive ? `<button class="btn-action-sm btn-action-primary btn-switch-voice" data-id="${escapeHtml(vp.id)}">🚀 切换为此音色</button>` : `<span class="badge-status badge-indigo" style="align-self: center;">已加载在后端</span>`}
                        <button class="btn-action-sm btn-action-secondary btn-test-voice-preview" data-id="${escapeHtml(vp.id)}">🔊 试听</button>
                        <button class="btn-action-sm btn-action-secondary btn-edit-voice" data-id="${escapeHtml(vp.id)}">✏️ 编辑</button>
                        ${state.voiceProfiles.length > 1 ? `<button class="btn-action-sm btn-action-danger btn-delete-voice" data-id="${escapeHtml(vp.id)}">🗑️</button>` : ''}
                    </div>
                `;
                grid.appendChild(card);
            });

            // Bind Profile Actions
            grid.querySelectorAll('.btn-switch-voice').forEach(b => {
                b.addEventListener('click', () => switchVoiceProfile(parseInt(b.getAttribute('data-id'), 10)));
            });
            grid.querySelectorAll('.btn-test-voice-preview').forEach(b => {
                b.addEventListener('click', () => testVoicePreview(parseInt(b.getAttribute('data-id'), 10)));
            });
            grid.querySelectorAll('.btn-edit-voice').forEach(b => {
                b.addEventListener('click', () => editVoiceProfile(parseInt(b.getAttribute('data-id'), 10)));
            });
            grid.querySelectorAll('.btn-delete-voice').forEach(b => {
                b.addEventListener('click', () => deleteVoiceProfile(parseInt(b.getAttribute('data-id'), 10)));
            });
        }

        // ================= PROVIDER ACTIONS =================

        // Preset dropdown change
        document.getElementById('provider-preset-select').addEventListener('change', (e) => {
            const pid = e.target.value;
            if (!pid) return;
            const preset = state.presets.find(p => p.id === pid);
            if (!preset) return;

            document.getElementById('prov-id').value = preset.id;
            document.getElementById('prov-name').value = preset.name;
            document.getElementById('prov-base-url').value = preset.default_base_url;
            document.getElementById('prov-chat-model').value = preset.default_chat_model;
            document.getElementById('prov-stt-model').value = preset.default_stt_model || '';
            document.getElementById('prov-api-key').value = '';
            document.getElementById('prov-api-key').placeholder = 'sk-**** (留空或保持掩码则保留原密钥)';

            // Populate datalist with preset models
            const datalist = document.getElementById('chat-models-datalist');
            datalist.innerHTML = '';
            if (preset.preset_models) {
                preset.preset_models.forEach(m => {
                    const opt = document.createElement('option');
                    opt.value = m;
                    datalist.appendChild(opt);
                });
            }
            showToast(`已加载 ${preset.name} 预设模板`, 'info');
        });

        // Edit Provider
        function editProvider(provId) {
            const p = state.providers.find(x => x.id === provId);
            if (!p) return;

            document.getElementById('provider-form-title').textContent = `✏️ 编辑提供商: ${p.name}`;
            document.getElementById('prov-id').value = p.id;
            document.getElementById('prov-id').readOnly = true;
            document.getElementById('prov-name').value = p.name;
            document.getElementById('prov-base-url').value = p.api_base_url;
            document.getElementById('prov-api-key').value = p.api_key || '';
            document.getElementById('prov-chat-model').value = p.chat_model;
            document.getElementById('prov-stt-model').value = p.stt_model || '';
            document.getElementById('prov-custom-headers').value = p.custom_headers ? JSON.stringify(p.custom_headers, null, 2) : '{}';
            document.getElementById('prov-is-active').checked = Boolean(p.is_active);

            document.getElementById('provider-form-box').scrollIntoView({ behavior: 'smooth' });
        }

        // Add Provider Toggle
        document.getElementById('btn-add-provider-toggle').addEventListener('click', () => {
            document.getElementById('provider-form-title').textContent = '➕ 添加提供商配置';
            document.getElementById('prov-id').value = '';
            document.getElementById('prov-id').readOnly = false;
            document.getElementById('prov-name').value = '';
            document.getElementById('prov-base-url').value = 'https://api.openai.com/v1';
            document.getElementById('prov-api-key').value = '';
            document.getElementById('prov-chat-model').value = 'gpt-4o-mini';
            document.getElementById('prov-stt-model').value = '';
            document.getElementById('prov-custom-headers').value = '{}';
            document.getElementById('prov-is-active').checked = false;
            document.getElementById('provider-form-box').scrollIntoView({ behavior: 'smooth' });
        });

        // Activate Provider
        async function activateProvider(provId) {
            try {
                showToast(`正在激活提供商 ${provId}...`, 'info');
                const resp = await fetch(`/api/providers/${provId}/activate`, { method: 'POST' });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                showToast(`成功激活提供商 ${provId}！`, 'success');
                await fetchGlobalConfig();
                await fetchProviders();
            } catch (err) {
                showToast(`激活失败: ${err.message}`, 'error');
            }
        }

        // Delete Provider
        async function deleteProvider(provId) {
            if (!confirm(`确定要删除提供商 [${provId}] 吗？`)) return;
            try {
                const resp = await fetch(`/api/providers/${provId}`, { method: 'DELETE' });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                showToast(`成功删除提供商 ${provId}`, 'success');
                await fetchProviders();
                await fetchGlobalConfig();
            } catch (err) {
                showToast(`删除失败: ${err.message}`, 'error');
            }
        }

        // Test Provider Connection
        document.getElementById('btn-test-prov-connection').addEventListener('click', async () => {
            const provId = document.getElementById('prov-id').value.trim();
            const baseUrl = document.getElementById('prov-base-url').value.trim();
            const apiKey = document.getElementById('prov-api-key').value.trim();
            const model = document.getElementById('prov-chat-model').value.trim();

            let customHeaders = {};
            try {
                const hText = document.getElementById('prov-custom-headers').value.trim();
                if (hText) customHeaders = JSON.parse(hText);
            } catch (e) {
                showToast('自定义请求头 JSON 格式无效', 'warning');
                return;
            }

            showToast('正在探测模型服务连通性...', 'info');
            try {
                const resp = await fetch('/api/providers/test', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        id: provId,
                        provider_type: provId,
                        base_url: baseUrl,
                        api_key: apiKey,
                        model: model,
                        custom_headers: customHeaders
                    })
                });
                const result = await resp.json();
                if (result.success) {
                    showToast(`连接成功！延迟: ${result.latency_ms || 0} ms (${result.message})`, 'success');
                } else {
                    showToast(`连通性测试失败: ${result.message}`, 'error', 5000);
                }
            } catch (err) {
                showToast(`测试请求异常: ${err.message}`, 'error');
            }
        });

        // Fetch Provider Models Live Discovery
        document.getElementById('btn-fetch-prov-models').addEventListener('click', async () => {
            const provId = document.getElementById('prov-id').value.trim();
            if (!provId) {
                showToast('请先输入提供商 ID', 'warning');
                return;
            }
            showToast(`正在从 ${provId} 拉取可用模型列表...`, 'info');
            try {
                const resp = await fetch(`/api/providers/${provId}/models`);
                const data = await resp.json();
                const datalist = document.getElementById('chat-models-datalist');
                datalist.innerHTML = '';
                if (data.models && data.models.length > 0) {
                    data.models.forEach(m => {
                        const opt = document.createElement('option');
                        opt.value = m;
                        datalist.appendChild(opt);
                    });
                    showToast(`成功获取到 ${data.models.length} 个模型！`, 'success');
                } else {
                    showToast('未返回模型列表，请手动输入', 'warning');
                }
            } catch (err) {
                showToast(`获取模型列表失败: ${err.message}`, 'error');
            }
        });

        // Save / Submit Provider
        document.getElementById('provider-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const provId = document.getElementById('prov-id').value.trim();
            const name = document.getElementById('prov-name').value.trim();
            const baseUrl = document.getElementById('prov-base-url').value.trim();
            const apiKey = document.getElementById('prov-api-key').value.trim();
            const chatModel = document.getElementById('prov-chat-model').value.trim();
            const sttModel = document.getElementById('prov-stt-model').value.trim();
            const isActive = document.getElementById('prov-is-active').checked;

            let customHeaders = {};
            try {
                const hText = document.getElementById('prov-custom-headers').value.trim();
                if (hText) customHeaders = JSON.parse(hText);
            } catch (e) {
                showToast('自定义请求头 JSON 格式无效', 'error');
                return;
            }

            try {
                const resp = await fetch('/api/providers', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        id: provId,
                        name: name,
                        api_base_url: baseUrl,
                        api_key: apiKey,
                        chat_model: chatModel,
                        stt_model: sttModel,
                        is_active: isActive,
                        custom_headers: customHeaders
                    })
                });

                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                showToast(`提供商 [${name}] 保存成功！`, 'success');
                await fetchProviders();
                await fetchGlobalConfig();
            } catch (err) {
                showToast(`保存提供商失败: ${err.message}`, 'error');
            }
        });

        // ================= VOICE PROFILE ACTIONS =================

        // Switch Active Voice Profile
        async function switchVoiceProfile(profileId) {
            try {
                showToast(`正在切换音色模型 (ID: ${profileId})...`, 'info');
                const resp = await fetch('/api/voice/switch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ profile_id: profileId })
                });
                const res = await resp.json();
                if (!resp.ok) throw new Error(res.detail || `HTTP ${resp.status}`);
                showToast(`音色已成功切换为：${res.profile}！`, 'success');
                await fetchVoiceProfiles();
                await fetchSystemTelemetry();
            } catch (err) {
                showToast(`音色切换失败: ${err.message}`, 'error', 5000);
            }
        }

        // Test Voice Preview Synthesis
        async function testVoicePreview(profileId) {
            const vp = state.voiceProfiles.find(x => x.id === profileId);
            const textToSpeak = vp ? (vp.prompt_text || 'こんにちは、今日もよろしくお願いします。') : 'こんにちは！';
            showToast(`正在试听合成音色...`, 'info');
            try {
                const resp = await fetch('/api/voice/synthesize', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        text: textToSpeak,
                        speed: 1.0,
                        text_language: vp ? vp.text_lang : 'ja'
                    })
                });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const blob = await resp.blob();
                const audioUrl = URL.createObjectURL(blob);
                const audio = document.getElementById('preview-audio-player');
                // Revoke previous object URL to prevent memory leaks.
                if (audio.dataset.objectUrl) URL.revokeObjectURL(audio.dataset.objectUrl);
                audio.dataset.objectUrl = audioUrl;
                audio.src = audioUrl;
                audio.play().catch(err => console.warn('Preview playback failed:', err));
                audio.addEventListener('ended', () => {
                    if (audio.dataset.objectUrl) {
                        URL.revokeObjectURL(audio.dataset.objectUrl);
                        delete audio.dataset.objectUrl;
                        audio.src = '';
                    }
                }, { once: true });
                showToast('正在播放试听语音...', 'success');
            } catch (err) {
                showToast(`试听合成失败: ${err.message}`, 'error');
            }
        }

        // Edit Voice Profile
        function editVoiceProfile(profileId) {
            const vp = state.voiceProfiles.find(x => x.id === profileId);
            if (!vp) return;

            document.getElementById('voice-form-title').textContent = `✏️ 编辑音色: ${vp.name}`;
            document.getElementById('voice-profile-id').value = vp.id;
            document.getElementById('voice-name').value = vp.name;
            document.getElementById('voice-desc').value = vp.description || '';
            document.getElementById('voice-gpt-path').value = vp.gpt_weights_path;
            document.getElementById('voice-sovits-path').value = vp.sovits_weights_path;
            document.getElementById('voice-ref-audio').value = vp.ref_audio_path || vp.refer_audio_path || '';
            document.getElementById('voice-prompt-text').value = vp.prompt_text || vp.refer_text || '';
            document.getElementById('voice-prompt-lang').value = vp.prompt_lang || vp.refer_language || 'ja';
            document.getElementById('voice-text-lang').value = vp.text_lang || 'ja';
            document.getElementById('voice-system-prompt').value = vp.system_prompt || '';
            document.getElementById('voice-is-default').checked = Boolean(vp.is_default);

            document.getElementById('voice-form-box').scrollIntoView({ behavior: 'smooth' });
        }

        // Add Voice Profile Toggle
        document.getElementById('btn-add-profile-toggle').addEventListener('click', () => {
            document.getElementById('voice-form-title').textContent = '➕ 新增角色音色';
            document.getElementById('voice-profile-id').value = '';
            document.getElementById('voice-name').value = '';
            document.getElementById('voice-desc').value = '';
            document.getElementById('voice-gpt-path').value = 'GPT_weights_v2ProPlus/character.ckpt';
            document.getElementById('voice-sovits-path').value = 'SoVITS_weights_v2ProPlus/character.pth';
            document.getElementById('voice-ref-audio').value = 'sample.ogg';
            document.getElementById('voice-prompt-text').value = '';
            document.getElementById('voice-prompt-lang').value = 'ja';
            document.getElementById('voice-text-lang').value = 'ja';
            document.getElementById('voice-system-prompt').value = '';
            document.getElementById('voice-is-default').checked = false;
            document.getElementById('voice-form-box').scrollIntoView({ behavior: 'smooth' });
        });

        // Cancel Voice Edit
        document.getElementById('btn-cancel-voice-edit').addEventListener('click', () => {
            document.getElementById('voice-profile-form').reset();
            document.getElementById('voice-profile-id').value = '';
        });

        // Delete Voice Profile
        async function deleteVoiceProfile(profileId) {
            if (!confirm(`确定要删除此音色配置 (ID: ${profileId}) 吗？`)) return;
            try {
                const resp = await fetch(`/api/voice/profiles/${profileId}`, { method: 'DELETE' });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                showToast(`成功删除音色 ID: ${profileId}`, 'success');
                await fetchVoiceProfiles();
            } catch (err) {
                showToast(`删除音色失败: ${err.message}`, 'error');
            }
        }

        // Save Voice Profile Form Submit
        document.getElementById('voice-profile-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const profileId = document.getElementById('voice-profile-id').value;
            const name = document.getElementById('voice-name').value.trim();
            const desc = document.getElementById('voice-desc').value.trim();
            const gptPath = document.getElementById('voice-gpt-path').value.trim();
            const sovitsPath = document.getElementById('voice-sovits-path').value.trim();
            const refAudio = document.getElementById('voice-ref-audio').value.trim();
            const promptText = document.getElementById('voice-prompt-text').value.trim();
            const promptLang = document.getElementById('voice-prompt-lang').value;
            const textLang = document.getElementById('voice-text-lang').value;
            const sysPrompt = document.getElementById('voice-system-prompt').value;
            const isDefault = document.getElementById('voice-is-default').checked;

            const payload = {
                name: name,
                description: desc,
                gpt_weights_path: gptPath,
                sovits_weights_path: sovitsPath,
                refer_audio_path: refAudio,
                ref_audio_path: refAudio,
                refer_text: promptText,
                prompt_text: promptText,
                refer_language: promptLang,
                prompt_lang: promptLang,
                text_lang: textLang,
                system_prompt: sysPrompt,
                is_default: isDefault
            };

            try {
                let resp;
                if (profileId) {
                    // Update
                    resp = await fetch(`/api/voice/profiles/${profileId}`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                } else {
                    // Create
                    resp = await fetch('/api/voice/profiles', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                }

                if (!resp.ok) {
                    const errJson = await resp.json();
                    throw new Error(errJson.detail || `HTTP ${resp.status}`);
                }
                showToast(`角色音色 [${name}] 保存成功！`, 'success');
                document.getElementById('voice-profile-form').reset();
                document.getElementById('voice-profile-id').value = '';
                await fetchVoiceProfiles();
            } catch (err) {
                showToast(`保存音色失败: ${err.message}`, 'error');
            }
        });

        // ================= INFERENCE & PRESET ACTIONS =================

        // Preset Tuning Buttons
        function applyInferencePreset(type) {
            document.querySelectorAll('.btn-preset').forEach(b => b.classList.remove('active'));
            if (type === 'high_quality') {
                document.getElementById('preset-high-quality').classList.add('active');
                document.getElementById('param-text-split-method').value = 'cut3';
                updateKnob('param-speed-factor', 'num-speed-factor', 'val-speed', 1.0);
                updateKnob('param-temperature', 'num-temperature', 'val-temp', 1.0);
                updateKnob('param-top-k', 'num-top-k', 'val-topk', 15);
                updateKnob('param-top-p', 'num-top-p', 'val-topp', 1.0);
                updateKnob('param-fragment-interval', 'num-fragment-interval', 'val-fragment', 0.3, 's');
                showToast('已应用：高音质预设 (cut3, temp 1.0, top_k 15)', 'info');
            } else if (type === 'balanced') {
                document.getElementById('preset-balanced').classList.add('active');
                document.getElementById('param-text-split-method').value = 'cut1';
                updateKnob('param-speed-factor', 'num-speed-factor', 'val-speed', 1.0);
                updateKnob('param-temperature', 'num-temperature', 'val-temp', 1.0);
                updateKnob('param-top-k', 'num-top-k', 'val-topk', 15);
                updateKnob('param-top-p', 'num-top-p', 'val-topp', 1.0);
                updateKnob('param-fragment-interval', 'num-fragment-interval', 'val-fragment', 0.3, 's');
                showToast('已应用：平衡预设 (cut1, temp 1.0, top_k 15)', 'info');
            } else if (type === 'low_latency') {
                document.getElementById('preset-low-latency').classList.add('active');
                document.getElementById('param-text-split-method').value = 'cut5';
                updateKnob('param-speed-factor', 'num-speed-factor', 'val-speed', 1.05);
                updateKnob('param-temperature', 'num-temperature', 'val-temp', 0.8);
                updateKnob('param-top-k', 'num-top-k', 'val-topk', 10);
                updateKnob('param-top-p', 'num-top-p', 'val-topp', 0.9);
                updateKnob('param-fragment-interval', 'num-fragment-interval', 'val-fragment', 0.2, 's');
                showToast('已应用：极低延迟预设 (cut5, temp 0.8, top_k 10, speed 1.05)', 'info');
            }
        }

        function updateKnob(sliderId, numId, displayId, val, suffix = '') {
            document.getElementById(sliderId).value = val;
            document.getElementById(numId).value = val;
            document.getElementById(displayId).textContent = `${val}${suffix}`;
        }

        document.getElementById('preset-high-quality').addEventListener('click', () => applyInferencePreset('high_quality'));
        document.getElementById('preset-balanced').addEventListener('click', () => applyInferencePreset('balanced'));
        document.getElementById('preset-low-latency').addEventListener('click', () => applyInferencePreset('low_latency'));

        // Save Inference Parameters
        document.getElementById('inference-config-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            // Safe numeric parsers: empty/invalid input falls back to null (server keeps old value).
            const num = (id, radix = 10) => {
                const v = parseInt(document.getElementById(id).value, radix);
                return Number.isFinite(v) ? v : null;
            };
            const float = (id) => {
                const v = parseFloat(document.getElementById(id).value);
                return Number.isFinite(v) ? v : null;
            };
            const updates = {
                text_split_method: document.getElementById('param-text-split-method').value,
                speed_factor: float('num-speed-factor'),
                temperature: float('num-temperature'),
                top_k: num('num-top-k'),
                top_p: float('num-top-p'),
                fragment_interval: float('num-fragment-interval'),
                seed: num('param-seed'),
                batch_size: num('param-batch-size'),
                max_history_messages: num('param-max-history'),
                audio_retention_minutes: num('param-audio-retention'),
                gpt_sovits_url: document.getElementById('param-sovits-url').value.trim()
            };
            // Drop null entries so we never overwrite server settings with null.
            Object.keys(updates).forEach(k => { if (updates[k] === null) delete updates[k]; });

            try {
                const resp = await fetch('/api/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ settings: updates })
                });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                showToast('推理参数已保存并同步至 SQLite！', 'success');
                await fetchGlobalConfig();
                await fetchSystemTelemetry();
            } catch (err) {
                showToast(`保存推理参数失败: ${err.message}`, 'error');
            }
        });

        // ================= TELEGRAM BOT ACTIONS =================

        // Test Telegram Connection
        document.getElementById('btn-test-telegram-token').addEventListener('click', async () => {
            const token = document.getElementById('tg-bot-token').value.trim();
            const proxyEnabled = document.getElementById('tg-proxy-enabled').checked;
            const proxyHost = document.getElementById('tg-proxy-host').value.trim();
            const proxyPort = parseInt(document.getElementById('tg-proxy-port').value, 10);

            showToast('正在测试 Telegram Bot 连通性...', 'info');
            try {
                const resp = await fetch('/api/telegram/test', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        token: token,
                        proxy_enabled: proxyEnabled,
                        proxy_host: proxyHost,
                        proxy_port: proxyPort
                    })
                });
                const result = await resp.json();
                if (result.success) {
                    showToast(`${result.message} (延迟: ${result.latency_ms || 0} ms)`, 'success');
                    if (result.bot_info && result.bot_info.username) {
                        document.getElementById('tg-bot-username').value = result.bot_info.username;
                    }
                } else {
                    showToast(`Telegram 测试失败: ${result.message}`, 'error', 5000);
                }
            } catch (err) {
                showToast(`Telegram 测试异常: ${err.message}`, 'error');
            }
        });

        // Save Telegram Settings
        document.getElementById('telegram-config-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const token = document.getElementById('tg-bot-token').value.trim();
            const username = document.getElementById('tg-bot-username').value.trim();
            const proxyEnabled = document.getElementById('tg-proxy-enabled').checked;
            const proxyHost = document.getElementById('tg-proxy-host').value.trim();
            const proxyPort = parseInt(document.getElementById('tg-proxy-port').value, 10);

            const updates = {
                telegram_bot_token: token,
                telegram_bot_username: username,
                telegram_proxy_enabled: proxyEnabled,
                telegram_proxy_host: proxyHost,
                telegram_proxy_port: proxyPort
            };

            try {
                const resp = await fetch('/api/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ settings: updates })
                });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                showToast('Telegram 配置保存成功！', 'success');
                await fetchGlobalConfig();
                await fetchSystemTelemetry();
            } catch (err) {
                showToast(`保存 Telegram 配置失败: ${err.message}`, 'error');
            }
        });

        // ================= GLOBAL SHORTCUTS & REFRESH =================

        // Global Refresh Button
        document.getElementById('btn-global-refresh').addEventListener('click', async () => {
            showToast('正在刷新系统全量遥测与配置...', 'info');
            await fetchSystemTelemetry();
            await fetchGlobalConfig();
            await fetchProviders();
            await fetchVoiceProfiles();
            showToast('系统状态已是最新', 'success');
        });

        // Quick Action: Test TTS
        document.getElementById('btn-quick-test-tts').addEventListener('click', () => {
            testVoicePreview(state.activeProfileId);
        });

        // Quick Action: Test LLM
        document.getElementById('btn-quick-test-llm').addEventListener('click', () => {
            const activeId = (state.activeProvider && state.activeProvider.id) || (state.settings && state.settings.active_provider_id) || 'deepseek';
            showToast(`正在测试活动大模型 [${activeId}] 连通性...`, 'info');
            fetch('/api/providers/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: activeId })
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    showToast(`大模型连通性良好！延迟: ${res.latency_ms || 0} ms`, 'success');
                } else {
                    showToast(`大模型连接失败: ${res.message}`, 'error');
                }
            }).catch(e => showToast(`测试异常: ${e.message}`, 'error'));
        });

        // Quick Action: Reset Context
        document.getElementById('btn-reset-chat-context').addEventListener('click', async () => {
            if (!confirm('确定要清空默认会话的全部历史记录吗？')) return;
            try {
                const resp = await fetch('/api/chat/history?session_id=default', { method: 'DELETE' });
                if (resp.ok) {
                    showToast('已成功重置对话上下文！', 'success');
                } else {
                    showToast('重置对话失败 (HTTP ' + resp.status + ')', 'error');
                }
            } catch (e) {
                showToast(`重置异常: ${e.message}`, 'error');
            }
        });

        // ================= FILE EXPLORER & LOCAL BROWSER =================
        let currentFsTargetInput = null;
        let currentFsFileType = 'all';
        let currentFsPath = '';

        async function openNativeFileDialog(fileType, targetInputId) {
            const targetInput = document.getElementById(targetInputId);
            const initialDir = targetInput && targetInput.value ? targetInput.value : '';
            showToast('正在打开本地文件选择窗口...', 'info');
            try {
                const resp = await fetch('/api/voice/browse-file', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ file_type: fileType, initial_dir: initialDir })
                });
                if (resp.ok) {
                    const data = await resp.json();
                    if (data.selected_path) {
                        targetInput.value = data.selected_path;
                        showToast(`已成功选中: ${data.selected_path.split(/[\\\\/]/).pop()}`, 'success');
                    } else {
                        showToast('未选择文件或已取消', 'info');
                    }
                } else {
                    openWebFileExplorer(fileType, targetInputId);
                }
            } catch (err) {
                console.warn('Native picker failed, opening web explorer:', err);
                openWebFileExplorer(fileType, targetInputId);
            }
        }

        async function openWebFileExplorer(fileType, targetInputId) {
            currentFsTargetInput = document.getElementById(targetInputId);
            currentFsFileType = fileType;
            const modal = document.getElementById('file-explorer-modal');
            modal.style.display = 'flex';

            let initPath = currentFsTargetInput && currentFsTargetInput.value ? currentFsTargetInput.value : '';
            await loadFsDirectory(initPath);
            await loadDiscoveredModels(fileType);
        }

        async function loadFsDirectory(path) {
            const loading = document.getElementById('fs-items-loading');
            const dirList = document.getElementById('fs-directories-list');
            const fileList = document.getElementById('fs-files-list');
            const pathInput = document.getElementById('fs-current-path');
            const driveContainer = document.getElementById('drive-selector');

            loading.style.display = 'block';
            dirList.innerHTML = '';
            fileList.innerHTML = '';

            try {
                const url = `/api/voice/fs-browse?file_type=${encodeURIComponent(currentFsFileType)}` + (path ? `&path=${encodeURIComponent(path)}` : '');
                const resp = await fetch(url);
                const data = await resp.json();
                loading.style.display = 'none';

                if (data.error) {
                    dirList.innerHTML = `<div style="padding: 12px; color: #ef4444;">${escapeHtml(data.error)}</div>`;
                    return;
                }

                currentFsPath = data.current_path;
                pathInput.value = data.current_path;

                // Render drives
                driveContainer.innerHTML = '';
                (data.drives || []).forEach(drive => {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = `fs-drive-btn ${data.current_path.toUpperCase().startsWith(drive.toUpperCase()) ? 'active' : ''}`;
                    btn.textContent = drive;
                    btn.onclick = () => loadFsDirectory(drive);
                    driveContainer.appendChild(btn);
                });

                // Render directories
                if (data.directories && data.directories.length > 0) {
                    data.directories.forEach(d => {
                        const item = document.createElement('div');
                        item.className = 'fs-item fs-dir';
                        item.innerHTML = `
                            <div class="fs-item-left">
                                <span>📁</span>
                                <span class="fs-item-name">${escapeHtml(d.name)}</span>
                            </div>
                            <span style="font-size: 11px; color: var(--text-muted);">文件夹</span>
                        `;
                        item.onclick = () => loadFsDirectory(d.path);
                        dirList.appendChild(item);
                    });
                }

                // Render files
                if (data.files && data.files.length > 0) {
                    data.files.forEach(f => {
                        const item = document.createElement('div');
                        item.className = 'fs-item';
                        const sizeKb = Math.round((f.size_bytes || 0) / 1024);
                        const sizeMb = (sizeKb / 1024).toFixed(1);
                        const sizeStr = sizeMb > 1 ? `${sizeMb} MB` : `${sizeKb} KB`;

                        item.innerHTML = `
                            <div class="fs-item-left">
                                <span>${f.name.endsWith('.ckpt') || f.name.endsWith('.pth') ? '📦' : '🎵'}</span>
                                <span class="fs-item-name" style="color: var(--primary-accent); font-weight: 600;">${escapeHtml(f.name)}</span>
                            </div>
                            <span style="font-size: 11px; color: var(--text-muted);">${sizeStr}</span>
                        `;
                        item.onclick = () => {
                            if (currentFsTargetInput) {
                                currentFsTargetInput.value = f.path;
                                showToast(`已选中: ${f.name}`, 'success');
                            }
                            closeFileExplorer();
                        };
                        fileList.appendChild(item);
                    });
                } else if (!data.directories || data.directories.length === 0) {
                    fileList.innerHTML = '<div style="padding: 16px; text-align: center; color: var(--text-muted);">此目录下无匹配的文件</div>';
                }
            } catch (err) {
                loading.style.display = 'none';
                dirList.innerHTML = `<div style="padding: 12px; color: #ef4444;">加载目录失败: ${escapeHtml(err.message)}</div>`;
            }
        }

        async function loadDiscoveredModels(fileType) {
            const fastBox = document.getElementById('fs-fast-select-box');
            const fastItems = document.getElementById('fs-fast-items');
            try {
                const resp = await fetch('/api/voice/scan-models');
                if (!resp.ok) return;
                const data = await resp.json();
                let items = [];
                if (fileType === 'gpt') items = data.gpt_weights || [];
                else if (fileType === 'sovits') items = data.sovits_weights || [];
                else if (fileType === 'audio') items = data.audio_files || [];
                else items = [...(data.gpt_weights || []), ...(data.sovits_weights || []), ...(data.audio_files || [])];

                if (items.length > 0) {
                    fastBox.style.display = 'block';
                    fastItems.innerHTML = '';
                    items.forEach(it => {
                        const chip = document.createElement('button');
                        chip.type = 'button';
                        chip.className = 'btn-action-sm btn-action-secondary';
                        chip.style.fontSize = '11px';
                        chip.textContent = `${it.name.endsWith('.ckpt') ? '📦 ' : it.name.endsWith('.pth') ? '🧠 ' : '🎵 '}${it.name}`;
                        chip.title = it.path;
                        chip.onclick = () => {
                            if (currentFsTargetInput) {
                                currentFsTargetInput.value = it.path;
                                showToast(`已选中: ${it.name}`, 'success');
                            }
                            closeFileExplorer();
                        };
                        fastItems.appendChild(chip);
                    });
                } else {
                    fastBox.style.display = 'none';
                }
            } catch (e) {
                fastBox.style.display = 'none';
            }
        }

        function closeFileExplorer() {
            document.getElementById('file-explorer-modal').style.display = 'none';
        }

        // Up directory button
        document.getElementById('btn-fs-up').addEventListener('click', () => {
            if (!currentFsPath) return;
            const parts = currentFsPath.replace(/[\\\\/]+$/, '').split(/[\\\\/]/);
            if (parts.length > 1) {
                parts.pop();
                let parent = parts.join('\\');
                if (parts.length === 1 && parts[0].endsWith(':')) parent += '\\';
                loadFsDirectory(parent || '\\');
            }
        });

        document.getElementById('btn-close-file-modal').addEventListener('click', closeFileExplorer);
        document.getElementById('btn-cancel-file-modal').addEventListener('click', closeFileExplorer);
        document.getElementById('btn-native-dialog-modal').addEventListener('click', async () => {
            if (currentFsTargetInput) {
                const targetId = currentFsTargetInput.id;
                const fileType = currentFsFileType;
                closeFileExplorer();
                await openNativeFileDialog(fileType, targetId);
            }
        });

        // Event listeners for voice profile browse buttons
        document.getElementById('btn-browse-gpt').addEventListener('click', () => openNativeFileDialog('gpt', 'voice-gpt-path'));
        document.getElementById('btn-fs-gpt').addEventListener('click', () => openWebFileExplorer('gpt', 'voice-gpt-path'));

        document.getElementById('btn-browse-sovits').addEventListener('click', () => openNativeFileDialog('sovits', 'voice-sovits-path'));
        document.getElementById('btn-fs-sovits').addEventListener('click', () => openWebFileExplorer('sovits', 'voice-sovits-path'));

        document.getElementById('btn-browse-audio').addEventListener('click', () => openNativeFileDialog('audio', 'voice-ref-audio'));
        document.getElementById('btn-fs-audio').addEventListener('click', () => openWebFileExplorer('audio', 'voice-ref-audio'));

        // Auto Refresh Management
        const autoRefreshToggle = document.getElementById('auto-refresh-toggle');
        let telemetryPollInFlight = false;
        async function pollTelemetryOnce() {
            // Skip if the previous round is still in flight: a slow response
            // must never overwrite fresher state with stale data.
            if (telemetryPollInFlight) return;
            telemetryPollInFlight = true;
            try {
                await Promise.allSettled([fetchSystemTelemetry(), fetchMetricsAndCache()]);
            } finally {
                telemetryPollInFlight = false;
            }
        }
        function setupAutoRefresh() {
            if (state.autoRefreshTimer) clearInterval(state.autoRefreshTimer);
            if (autoRefreshToggle && autoRefreshToggle.checked) {
                state.autoRefreshTimer = setInterval(pollTelemetryOnce, 5000);
            }
        }

        if (autoRefreshToggle) {
            autoRefreshToggle.addEventListener('change', setupAutoRefresh);
        }

        // ================= MEMORY & AFFECTION SYSTEM =================
        async function fetchMemories() {
            try {
                const res = await fetch('/api/memory');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const memories = await res.json();
                renderMemoriesTable(memories);
            } catch (err) {
                console.error('Failed to load memories:', err);
            }
        }

        function renderMemoriesTable(memories) {
            const tbody = document.getElementById('memories-table-body');
            if (!tbody) return;
            if (!memories || memories.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; padding: 20px; color: var(--text-muted);">暂无记忆记录，快去和伴侣聊天吧～</td></tr>';
                return;
            }

            const categoryColors = {
                nickname: '#ec4899',
                preference: '#3b82f6',
                taboo: '#ef4444',
                promise: '#8b5cf6',
                identity: '#10b981',
                event: '#f59e0b',
            };

            tbody.innerHTML = memories.map(m => {
                const color = categoryColors[m.category] || '#64748b';
                const dateStr = m.updated_at ? m.updated_at.split('T')[0] : '-';
                return `
                    <tr style="border-bottom: 1px solid var(--card-border);">
                        <td style="padding: 8px 12px; color: var(--text-muted);">${escapeHtml(m.id)}</td>
                        <td style="padding: 8px 12px;"><span style="background: ${color}20; color: ${color}; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 11px;">${escapeHtml(m.category)}</span></td>
                        <td style="padding: 8px 12px; font-weight: 600; font-family: monospace;">${escapeHtml(m.fact_key)}</td>
                        <td style="padding: 8px 12px; color: var(--text-dark);">${escapeHtml(m.fact_value)}</td>
                        <td style="padding: 8px 12px; color: var(--text-muted);">${escapeHtml(m.recall_count)} 次</td>
                        <td style="padding: 8px 12px; color: var(--text-muted); font-size: 12px;">${escapeHtml(dateStr)}</td>
                        <td style="padding: 8px 12px; text-align: center;">
                            <button type="button" class="btn btn-sm btn-outline" style="color: #ef4444; border-color: #fca5a5; padding: 2px 6px; font-size: 11px;" data-del-memory="${escapeHtml(String(m.id))}">删除</button>
                        </td>
                    </tr>
                `;
            }).join('');

            // Event delegation instead of inline onclick (XSS-safe, consistent style).
            tbody.querySelectorAll('[data-del-memory]').forEach(btn => {
                btn.addEventListener('click', () => deleteMemoryItem(btn.getAttribute('data-del-memory')));
            });
        }

        async function deleteMemoryItem(memId) {
            if (!confirm(`确定要删除此条记忆记录 #${memId} 吗？`)) return;
            try {
                const res = await fetch(`/api/memory/${memId}`, { method: 'DELETE' });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                showToast('记忆记录已删除', 'success');
                await fetchMemories();
            } catch (err) {
                showToast(`删除失败: ${err.message}`, 'error');
            }
        }

        async function fetchAffection() {
            try {
                const res = await fetch('/api/affection');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const aff = await res.json();
                
                document.getElementById('aff-level-badge').innerText = `Lv.${aff.affection_level} ${aff.level_name}`;
                document.getElementById('aff-score-display').innerText = `${aff.affection_score} / 100`;
                document.getElementById('aff-emotion-display').innerText = `${aff.current_emotion}`;
                document.getElementById('aff-daily-display').innerText = `${aff.daily_points_earned} / 15 pts`;

                const slider = document.getElementById('aff-score-slider');
                slider.value = aff.affection_score;
                document.getElementById('aff-score-val').innerText = aff.affection_score;

                const emotionSelect = document.getElementById('aff-emotion-select');
                if (emotionSelect) emotionSelect.value = aff.current_emotion || 'normal';

                const nicknameInput = document.getElementById('aff-nickname-input');
                if (nicknameInput) nicknameInput.value = aff.custom_nickname || '';
            } catch (err) {
                console.error('Failed to load affection:', err);
            }
        }

        async function fetchDialogueGallery() {
            try {
                const res = await fetch('/api/affection/dialogues');
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                renderDialogueGallery(data.dialogues || []);
            } catch (err) {
                console.error('Failed to load dialogues gallery:', err);
            }
        }

        function renderDialogueGallery(dialogues) {
            const container = document.getElementById('dialogue-gallery-grid');
            if (!container) return;
            if (!dialogues || dialogues.length === 0) {
                container.innerHTML = '<div style="color: var(--text-muted); font-size: 13px;">暂无台词记录。</div>';
                return;
            }

            container.innerHTML = dialogues.map(d => {
                const statusBadge = d.is_unlocked
                    ? '<span style="background: #10b98120; color: #10b981; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600;">🔓 已解锁</span>'
                    : '<span style="background: #64748b20; color: var(--text-muted); padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600;">🔒 未解锁</span>';

                const emotionIcon = {
                    gentle: '🌸 温柔', shy: '😳 害羞', tsundere: '💢 傲娇', happy: '✨ 开心',
                    sad: '💧 低落', cold: '❄️ 高冷', normal: '😊 平静'
                }[d.emotion] || '😊';

                const blurStyle = d.is_unlocked ? '' : 'filter: blur(4px); user-select: none;';

                return `
                    <div style="background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 8px; padding: 12px 14px; display: flex; flex-direction: column; gap: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                        <div style="display: flex; justify-content: space-between; align-items: center;">
                            <span style="font-weight: 600; font-size: 13px; color: var(--text-dark);">${escapeHtml(d.title)}</span>
                            <div>${statusBadge}</div>
                        </div>
                        <div style="font-size: 11px; color: var(--text-muted);">状态: ${emotionIcon} | ${escapeHtml(d.unlock_condition)}</div>
                        <div style="${blurStyle} background: var(--muted-bg); border-radius: 6px; padding: 8px; font-size: 12px;">
                            <div style="color: var(--primary-accent); font-weight: 500; margin-bottom: 4px;">${escapeHtml(d.japanese)}</div>
                            <div style="color: var(--text-dark);">${escapeHtml(d.chinese)}</div>
                        </div>
                    </div>
                `;
            }).join('');
        }

        // Memory & Affection Event Listeners
        document.getElementById('refresh-memory-btn')?.addEventListener('click', async () => {
            await fetchMemories();
            await fetchAffection();
            await fetchDialogueGallery();
            showToast('记忆与好感度数据已刷新', 'info');
        });

        document.getElementById('aff-score-slider')?.addEventListener('input', (e) => {
            document.getElementById('aff-score-val').innerText = e.target.value;
        });

        document.getElementById('affection-form')?.addEventListener('submit', async (e) => {
            e.preventDefault();
            const score = parseInt(document.getElementById('aff-score-slider').value, 10);
            const emotion = document.getElementById('aff-emotion-select').value;
            const nickname = document.getElementById('aff-nickname-input').value.trim();

            try {
                const res = await fetch('/api/affection/update', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        affection_score: score,
                        current_emotion: emotion,
                        custom_nickname: nickname || null
                    })
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                showToast('好感度设置已保存', 'success');
                await fetchAffection();
                await fetchDialogueGallery();
            } catch (err) {
                showToast(`保存失败: ${err.message}`, 'error');
            }
        });

        document.getElementById('reset-affection-btn')?.addEventListener('click', async () => {
            if (!confirm('确定要重置当前伴侣的好感度分数与情绪吗？此操作不可逆。')) return;
            try {
                const res = await fetch('/api/affection/reset', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({})
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                showToast('好感度已重置为0', 'success');
                await fetchAffection();
                await fetchDialogueGallery();
            } catch (err) {
                showToast(`重置失败: ${err.message}`, 'error');
            }
        });

        document.getElementById('add-memory-form')?.addEventListener('submit', async (e) => {
            e.preventDefault();
            const cat = document.getElementById('new-mem-category').value;
            const key = document.getElementById('new-mem-key').value.trim();
            const val = document.getElementById('new-mem-value').value.trim();

            if (!key || !val) {
                showToast('记忆键与内容不能为空', 'warning');
                return;
            }

            try {
                const res = await fetch('/api/memory', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        category: cat,
                        fact_key: key,
                        fact_value: val,
                        confidence: 1.0
                    })
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                showToast('记忆事实录入成功', 'success');
                document.getElementById('new-mem-key').value = '';
                document.getElementById('new-mem-value').value = '';
                await fetchMemories();
            } catch (err) {
                showToast(`录入失败: ${err.message}`, 'error');
            }
        });

        document.getElementById('clear-memories-btn')?.addEventListener('click', async () => {
            if (!confirm('确定要清空所有已记录的长程事实记忆吗？此操作不可逆。')) return;
            try {
                const res = await fetch('/api/memory', { method: 'DELETE' });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                showToast('长程事实记忆库已清空', 'success');
                await fetchMemories();
            } catch (err) {
                showToast(`清空失败: ${err.message}`, 'error');
            }
        });

        // ================= TOKEN & LATENCY TELEMETRY =================
        async function fetchMetricsAndCache() {
            try {
                // 1. Fetch Overview
                const ovResp = await fetch('/api/metrics/overview');
                if (ovResp.ok) {
                    const ov = await ovResp.json();
                    
                    // Update Summary Cards
                    const totalTokensEl = document.getElementById('dash-total-tokens');
                    if (totalTokensEl) totalTokensEl.textContent = (ov.total_tokens || 0).toLocaleString();

                    const tokenBreakdownEl = document.getElementById('dash-token-breakdown');
                    if (tokenBreakdownEl) tokenBreakdownEl.textContent = `输入: ${(ov.total_prompt_tokens || 0).toLocaleString()} | 输出: ${(ov.total_completion_tokens || 0).toLocaleString()}`;

                    const costUsdEl = document.getElementById('dash-total-cost');
                    if (costUsdEl) costUsdEl.textContent = `$${(ov.estimated_cost_usd || 0).toFixed(4)}`;

                    const costCnyEl = document.getElementById('dash-total-cost-cny');
                    if (costCnyEl) costCnyEl.textContent = `≈ ¥${(ov.estimated_cost_cny || 0).toFixed(4)} CNY`;

                    const avgTtftEl = document.getElementById('dash-avg-ttft');
                    if (avgTtftEl) avgTtftEl.textContent = `${(ov.avg_ttft_ms || 0).toFixed(1)} ms`;

                    const ttftBadge = document.getElementById('dash-ttft-badge');
                    if (ttftBadge) {
                        const ttft = ov.avg_ttft_ms || 0;
                        if (ttft < 500) {
                            ttftBadge.className = 'badge-status badge-green';
                            ttftBadge.textContent = '极速';
                        } else if (ttft < 1000) {
                            ttftBadge.className = 'badge-status badge-yellow';
                            ttftBadge.textContent = '良好';
                        } else {
                            ttftBadge.className = 'badge-status badge-red';
                            ttftBadge.textContent = '较慢';
                        }
                    }

                    const ttsFirstEl = document.getElementById('dash-tts-first-chunk');
                    if (ttsFirstEl) ttsFirstEl.textContent = `TTS首句: ${(ov.avg_tts_first_chunk_ms || 0).toFixed(1)} ms`;

                    // Cache Overview Card
                    if (ov.cache_stats) {
                        const cs = ov.cache_stats;
                        const hitRateEl = document.getElementById('dash-cache-hit-rate');
                        if (hitRateEl) hitRateEl.textContent = `${(cs.hit_rate_percent || 0).toFixed(1)}%`;

                        const hitSubEl = document.getElementById('dash-cache-hit-sub');
                        if (hitSubEl) hitSubEl.textContent = `命中: ${cs.total_hits || 0} | 未命中: ${cs.total_misses || 0}`;

                        const cacheBadge = document.getElementById('dash-cache-badge');
                        if (cacheBadge) {
                            const hr = cs.hit_rate_percent || 0;
                            cacheBadge.className = `badge-status ${hr >= 70 ? 'badge-green' : (hr >= 30 ? 'badge-yellow' : 'badge-gray')}`;
                            cacheBadge.textContent = `${hr.toFixed(0)}%`;
                        }

                        // TTS Cache Manager Controls
                        const fileCountEl = document.getElementById('tts-cache-file-count');
                        if (fileCountEl) fileCountEl.textContent = `${cs.total_files || 0} 个`;

                        const diskSizeEl = document.getElementById('tts-cache-disk-size');
                        if (diskSizeEl) diskSizeEl.textContent = `${(cs.total_size_mb || 0).toFixed(2)} MB`;

                        const hitsTotalEl = document.getElementById('tts-cache-hits-total');
                        if (hitsTotalEl) hitsTotalEl.textContent = `${cs.total_hits || 0} 次`;

                        const speedupEl = document.getElementById('tts-cache-speedup-ratio');
                        if (speedupEl) {
                            const savedSec = ((cs.total_hits || 0) * 0.8).toFixed(1);
                            speedupEl.textContent = `省 GPU ~${savedSec}s`;
                        }
                    }
                }

                // 2. Fetch Providers Breakdown
                const provResp = await fetch('/api/metrics/providers');
                if (provResp.ok) {
                    const provData = await provResp.json();
                    renderProviderDistribution(provData.providers || []);
                }

                // 3. Fetch Latency Trend
                const trendResp = await fetch('/api/metrics/latency-trend?limit=30');
                if (trendResp.ok) {
                    const trendData = await trendResp.json();
                    renderLatencyChart(trendData.trend || []);
                }
            } catch (err) {
                console.warn('Failed to fetch metrics and cache stats:', err);
            }
        }

        function renderProviderDistribution(providers) {
            const container = document.getElementById('provider-distribution-bars');
            if (!container) return;
            if (!providers || providers.length === 0) {
                container.innerHTML = '<div style="font-size: 12px; color: var(--text-muted); text-align: center; padding: 12px;">暂无 Token 消耗数据</div>';
                return;
            }

            const colors = ['#6366f1', '#10b981', '#f59e0b', '#ec4899', '#3b82f6', '#8b5cf6'];
            container.innerHTML = '';

            providers.forEach((p, idx) => {
                const color = colors[idx % colors.length];
                const row = document.createElement('div');
                row.style.marginBottom = '6px';
                row.innerHTML = `
                    <div style="display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 2px;">
                        <span><strong>${escapeHtml(p.name || p.provider_id)}</strong> <span style="color: var(--text-muted);">(${p.request_count} 次)</span></span>
                        <span><strong>${p.total_tokens.toLocaleString()} tok</strong> (${p.percentage}%)</span>
                    </div>
                    <div style="background: var(--muted-bg); border-radius: 9999px; height: 6px; overflow: hidden;">
                        <div style="background: ${color}; width: ${Math.min(100, Math.max(2, p.percentage))}%; height: 100%;"></div>
                    </div>
                `;
                container.appendChild(row);
            });
        }

        function renderLatencyChart(trend) {
            const canvas = document.getElementById('latency-trend-canvas');
            if (!canvas) return;
            const ctx = canvas.getContext('2d');
            if (!ctx) return;

            const width = canvas.width;
            const height = canvas.height;
            ctx.clearRect(0, 0, width, height);

            if (!trend || trend.length === 0) {
                ctx.fillStyle = '#94a3b8';
                ctx.font = '12px sans-serif';
                ctx.textAlign = 'center';
                ctx.fillText('暂无近期的请求延迟记录', width / 2, height / 2);
                return;
            }

            // Draw grid lines
            ctx.strokeStyle = '#f1f5f9';
            ctx.lineWidth = 1;
            for (let y = 20; y < height; y += 30) {
                ctx.beginPath();
                ctx.moveTo(0, y);
                ctx.lineTo(width, y);
                ctx.stroke();
            }

            // Find maximum latency for scaling
            let maxLatency = 500;
            trend.forEach(d => {
                const m = Math.max(d.total_latency_ms || 0, d.tts_first_chunk_ms || 0, d.ttft_ms || 0);
                if (m > maxLatency) maxLatency = m;
            });
            maxLatency = Math.ceil(maxLatency * 1.15);

            const n = trend.length;
            const stepX = n > 1 ? (width - 40) / (n - 1) : width - 40;

            function drawSeries(key, strokeColor) {
                ctx.beginPath();
                ctx.strokeStyle = strokeColor;
                ctx.lineWidth = 2;
                trend.forEach((d, i) => {
                    const val = d[key] || 0;
                    const x = 20 + i * stepX;
                    const y = height - 15 - (val / maxLatency) * (height - 30);
                    if (i === 0) ctx.moveTo(x, y);
                    else ctx.lineTo(x, y);
                });
                ctx.stroke();

                // Draw dots
                ctx.fillStyle = strokeColor;
                trend.forEach((d, i) => {
                    const val = d[key] || 0;
                    const x = 20 + i * stepX;
                    const y = height - 15 - (val / maxLatency) * (height - 30);
                    ctx.beginPath();
                    ctx.arc(x, y, 2.5, 0, 2 * Math.PI);
                    ctx.fill();
                });
            }

            drawSeries('total_latency_ms', '#f59e0b');
            drawSeries('tts_first_chunk_ms', '#10b981');
            drawSeries('ttft_ms', '#6366f1');
        }

        // Bind TTS Cache Management Buttons
        document.getElementById('btn-refresh-cache-stats')?.addEventListener('click', async () => {
            await fetchMetricsAndCache();
            showToast('已刷新 TTS 缓存与性能数据', 'success');
        });

        document.getElementById('btn-clear-tts-cache')?.addEventListener('click', async () => {
            if (!confirm('确定要清空全部离线持久化音频缓存吗？清空后重复对话将需要重新进行 GPU 合成。')) return;
            try {
                const res = await fetch('/api/cache/clear', { method: 'POST' });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                showToast(`已清空音频缓存: 释放 ${data.freed_mb || 0} MB, 删除 ${data.deleted_files || 0} 个文件`, 'success');
                await fetchMetricsAndCache();
            } catch (err) {
                showToast(`清空缓存失败: ${err.message}`, 'error');
            }
        });

        // ================= INITIALIZATION =================
        // Robust init: runs immediately if DOM is already parsed, otherwise
        // waits for DOMContentLoaded. Immune to script-position / timing issues.
        async function initSettingsConsole() {
            try {
                // Parallel init: 8 independent fetches, no serial waterfall.
                await Promise.allSettled([
                    fetchSystemTelemetry(),
                    fetchMetricsAndCache(),
                    fetchGlobalConfig(),
                    fetchProviders(),
                    fetchVoiceProfiles(),
                    fetchMemories(),
                    fetchAffection(),
                    fetchDialogueGallery(),
                ]);
            } catch (err) {
                console.error('Settings console initialization error:', err);
            } finally {
                setupAutoRefresh();
            }
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', initSettingsConsole);
        } else {
            initSettingsConsole();
        }
