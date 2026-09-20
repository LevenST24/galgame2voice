// Global Settings Controller: LLM Providers, Hardware Telemetry, Telegram, Audio Cache
import {
  fetchProviders,
  saveProvider,
  activateProvider as apiActivateProvider,
  testProviderConnection,
} from '../api.js';
import { formatProviderDiagnostic, BUILTIN_PRESETS } from '../settings.js';
import { state, saveGlobal } from '../store.js';
import { showToast } from '../ui.js';
import { clearMemAudioCache } from '../cache.js';

const $ = (id) => document.getElementById(id);

let _dom = {};
let _callbacks = {};
let cachedProviders = [];
let cachedPresets = [];
let currentActiveProvider = null;

export function getCurrentActiveProvider() {
  return currentActiveProvider;
}

export function initGlobalSettings(dom, callbacks = {}) {
  _dom = dom;
  _callbacks = callbacks;

  // 选项卡切换
  if (_dom.gTabs) {
    _dom.gTabs.querySelectorAll('.modal-tab-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        _dom.gTabs.querySelectorAll('.modal-tab-btn').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        const targetTab = btn.getAttribute('data-gtab');
        _dom.globalModal.querySelectorAll('.gtab-panel').forEach((p) => p.classList.add('hidden'));
        const targetPanel = $(`gp-${targetTab}`);
        if (targetPanel) targetPanel.classList.remove('hidden');
        const modalBody = _dom.globalModal ? _dom.globalModal.querySelector('.modal-body') : null;
        if (modalBody) modalBody.scrollTop = 0;
      });
    });
  }

  // 推理参数滑块数值联动
  if (_dom.gParamSpeed) {
    _dom.gParamSpeed.addEventListener('input', (e) => {
      if (_dom.gParamSpeedVal) _dom.gParamSpeedVal.textContent = e.target.value;
    });
  }
  if (_dom.gParamTopK) {
    _dom.gParamTopK.addEventListener('input', (e) => {
      if (_dom.gParamTopKVal) _dom.gParamTopKVal.textContent = e.target.value;
    });
  }
  if (_dom.gParamTopP) {
    _dom.gParamTopP.addEventListener('input', (e) => {
      if (_dom.gParamTopPVal) _dom.gParamTopPVal.textContent = e.target.value;
    });
  }
  if (_dom.gParamTemp) {
    _dom.gParamTemp.addEventListener('input', (e) => {
      if (_dom.gParamTempVal) _dom.gParamTempVal.textContent = e.target.value;
    });
  }

  // Telegram 开关
  if (_dom.gTgEnabled) {
    _dom.gTgEnabled.addEventListener('change', updateTelegramFieldsVisibility);
  }
  if (_dom.gTgSave) {
    _dom.gTgSave.addEventListener('click', saveGlobalConfig);
  }
  if (_dom.gTgTest) {
    _dom.gTgTest.addEventListener('click', testTelegram);
  }

  // 刷新状态诊断
  if (_dom.gBtnRefreshStatus) {
    _dom.gBtnRefreshStatus.addEventListener('click', () => {
      fetchSystemTelemetry();
      loadGlobalConfig();
      showToast('诊断数据已刷新', 'info');
    });
  }

  // 一键热重启 GPT-SoVITS 引擎
  if (_dom.gBtnRestartSovits) {
    _dom.gBtnRestartSovits.addEventListener('click', handleRestartSovits);
  }

  // 一键切换精度/设备并重启
  if (_dom.gBtnTogglePrecision) {
    _dom.gBtnTogglePrecision.addEventListener('click', handleTogglePrecision);
  }

  // 精度下拉菜单自动保存
  if (_dom.gPrecision) {
    _dom.gPrecision.addEventListener('change', async () => {
      const val = _dom.gPrecision.value;
      try {
        await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ inference_precision: val }),
        });
        const labelMap = {
          auto: '自动判定 (Auto)',
          fp16: 'FP16 半精度',
          fp32: 'FP32 单精度',
          cpu: 'CPU 稳定模式 (免显存)',
        };
        showToast(`已保存推理设置: ${labelMap[val] || val}。请点击【重启 SoVITS 引擎】应用。`, 'info');
        fetchSystemTelemetry();
      } catch (err) {
        console.warn('Auto-save precision failed:', err);
      }
    });
  }

  // 跳转至语音与推理设置
  const btnGoPrecision = $('gBtnGoPrecision');
  if (btnGoPrecision) {
    btnGoPrecision.addEventListener('click', () => {
      const tabBtn = _dom.gTabs ? _dom.gTabs.querySelector('[data-gtab="inference"]') : null;
      if (tabBtn) tabBtn.click();
    });
  }

  // 一键清空音频缓存
  if (_dom.gBtnClearCache) {
    _dom.gBtnClearCache.addEventListener('click', handleClearCache);
  }

  // 模型配置与连通性测试事件绑定
  if (_dom.gProvider) _dom.gProvider.addEventListener('change', onProviderChange);
  if (_dom.gBtnToggleKeyVisibility) _dom.gBtnToggleKeyVisibility.addEventListener('click', toggleKeyVisibility);
  if (_dom.gChatModelSelect) _dom.gChatModelSelect.addEventListener('change', onChatModelSelectChange);
  if (_dom.gProviderTest) _dom.gProviderTest.addEventListener('click', handleProviderTest);
  if (_dom.gProviderActivate) _dom.gProviderActivate.addEventListener('click', handleActivateProvider);

  // 恢复默认参数
  if (_dom.gResetBtn) {
    _dom.gResetBtn.addEventListener('click', () => {
      if (!confirm('确定要将全局参数恢复为推荐默认值吗？')) return;
      if (_dom.gParamSpeed) {
        _dom.gParamSpeed.value = '1.0';
        _dom.gParamSpeedVal.textContent = '1.0';
      }
      if (_dom.gParamTopK) {
        _dom.gParamTopK.value = '15';
        _dom.gParamTopKVal.textContent = '15';
      }
      if (_dom.gParamTopP) {
        _dom.gParamTopP.value = '1.0';
        _dom.gParamTopPVal.textContent = '1.0';
      }
      if (_dom.gParamTemp) {
        _dom.gParamTemp.value = '1.0';
        _dom.gParamTempVal.textContent = '1.0';
      }
      if (_dom.gParamSliceMethod) _dom.gParamSliceMethod.value = 'cut5';
      if (_dom.gPrecision) _dom.gPrecision.value = 'auto';
      if (_dom.gAudioRetention) _dom.gAudioRetention.value = '30';
      showToast('已恢复推荐数值，请点击【保存全局配置】生效', 'info');
    });
  }

  // 全局模态框关闭按键
  if (_dom.globalModal) {
    _dom.globalModal.querySelectorAll('[data-close]').forEach((btn) => {
      btn.addEventListener('click', () => {
        if (_callbacks.closeModal) _callbacks.closeModal(_dom.globalModal);
      });
    });
  }
}

export function openGlobalSettings() {
  if (_callbacks.openModal) _callbacks.openModal(_dom.globalModal);
  loadProviders();
  loadGlobalConfig();
  fetchSystemTelemetry();
  if (_callbacks.loadSystemVersionInfo) {
    _callbacks.loadSystemVersionInfo(false).catch(() => {});
  }
  if (_dom.gPreset) {
    _dom.gPreset.value = state.global.ttsPreset || '';
  }
  if (_dom.gAutoTranslate) {
    _dom.gAutoTranslate.value = state.global.autoTranslate ? 'auto' : 'manual';
  }
}

export async function loadProviders() {
  if (_dom.gProvider) _dom.gProvider.innerHTML = '<option value="">加载模型列表…</option>';
  try {
    const data = await fetchProviders().catch(() => null);
    cachedProviders = data && data.providers ? data.providers : [];
    cachedPresets = data && data.presets && data.presets.length ? data.presets : BUILTIN_PRESETS;

    const active = cachedProviders.find((p) => p.is_active);
    currentActiveProvider = active || null;
    if (_callbacks.updateBadge) _callbacks.updateBadge();

    const presetOrder = ['gemini', 'openai', 'anthropic', 'deepseek', 'xai', 'siliconflow', 'glm', 'qwen', 'custom'];
    const providerMap = new Map();

    presetOrder.forEach((pid) => {
      const preset = cachedPresets.find((p) => p.id === pid);
      const stored = cachedProviders.find((p) => p.id === pid);
      if (preset || stored) {
        providerMap.set(pid, {
          id: pid,
          name: stored?.name || preset?.name || pid,
          chat_model: stored?.chat_model || preset?.default_chat_model || '',
          is_active: Boolean(stored?.is_active),
        });
      }
    });

    cachedPresets.forEach((p) => {
      if (!providerMap.has(p.id) && p.id !== 'custom') {
        const stored = cachedProviders.find((sp) => sp.id === p.id);
        providerMap.set(p.id, {
          id: p.id,
          name: stored?.name || p.name || p.id,
          chat_model: stored?.chat_model || p.default_chat_model || '',
          is_active: Boolean(stored?.is_active),
        });
      }
    });

    cachedProviders.forEach((p) => {
      if (!providerMap.has(p.id)) {
        providerMap.set(p.id, {
          id: p.id,
          name: p.name,
          chat_model: p.chat_model || '',
          is_active: Boolean(p.is_active),
        });
      }
    });

    const items = Array.from(providerMap.values());
    const sorted = active
      ? [items.find((i) => i.id === active.id) || active, ...items.filter((i) => i.id !== active.id)]
      : items;

    if (_dom.gProvider) {
      _dom.gProvider.innerHTML = '';
      sorted.forEach((p) => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = `${p.name} · ${p.chat_model || '未设定'}${p.is_active ? '（当前生效）' : ''}`;
        if (p.is_active) opt.selected = true;
        _dom.gProvider.appendChild(opt);
      });
      const customOpt = document.createElement('option');
      customOpt.value = '__custom__';
      customOpt.textContent = '＋ 自定义模型 / 接口…';
      if (!active && !sorted.length) customOpt.selected = true;
      _dom.gProvider.appendChild(customOpt);
    }

    onProviderChange();
  } catch (e) {
    console.error('Failed to load providers:', e);
    if (_dom.gProvider) {
      _dom.gProvider.innerHTML = '';
      BUILTIN_PRESETS.forEach((p) => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = `${p.name} · ${p.default_chat_model}`;
        _dom.gProvider.appendChild(opt);
      });
      const customOpt = document.createElement('option');
      customOpt.value = '__custom__';
      customOpt.textContent = '＋ 自定义模型 / 接口…';
      _dom.gProvider.appendChild(customOpt);
    }
    showToast(`提供商列表加载异常: ${e.message || e}`, 'error');
    onProviderChange();
  }
}

export function onProviderChange() {
  if (!_dom.gProvider) return;
  const pid = _dom.gProvider.value;
  const isCustom = pid === 'custom' || pid === '__custom__';
  const realId = isCustom ? 'custom' : pid;

  const stored = cachedProviders.find((p) => p.id === realId);
  const preset = cachedPresets.find((p) => p.id === realId) || BUILTIN_PRESETS.find((p) => p.id === realId);

  // 1. 自定义提供商名称字段显隐
  if (_dom.gCustomNameField) {
    _dom.gCustomNameField.classList.toggle('hidden', !isCustom);
    if (_dom.gCustomName) {
      _dom.gCustomName.value = isCustom ? stored?.name || '自定义模型' : '';
    }
  }

  // 2. 当前生效状态徽标与描述文本
  const isActive = Boolean(stored?.is_active);
  if (_dom.gProviderActiveBadge) {
    _dom.gProviderActiveBadge.className = `badge-status-pill ${isActive ? 'badge-pill-green' : 'badge-pill-gray'}`;
    _dom.gProviderActiveBadge.textContent = isActive ? '当前生效' : '未启用';
  }
  if (_dom.gProviderActive) {
    _dom.gProviderActive.textContent = isActive ? `当前：${stored ? stored.name : realId} · ${stored?.chat_model || ''}` : '';
  }
  if (_dom.gProviderDesc) {
    _dom.gProviderDesc.textContent =
      preset?.description ||
      stored?.description ||
      (isCustom ? '本地或私有部署的 OpenAI 兼容推理服务 (Ollama / vLLM / LMStudio)' : '外部 AI 大模型服务提供商');
  }

  // 3. API Key 状态与脱敏占位符处理
  if (_dom.gApiKey) {
    _dom.gApiKey.value = '';
    _dom.gApiKey.type = 'password';
  }
  if (_dom.gBtnToggleKeyText) {
    _dom.gBtnToggleKeyText.textContent = '显示';
  }
  if (_dom.gIconKeyEye) {
    const use = _dom.gIconKeyEye.querySelector('use');
    if (use) use.setAttribute('href', '#i-eye');
  }

  const maskedKey = (stored?.api_key || '').trim();
  const hasKey = Boolean(maskedKey && maskedKey.length > 0);

  if (hasKey) {
    if (_dom.gApiKey) _dom.gApiKey.placeholder = `已配置：${maskedKey}（留空保持原密钥，输入新密钥可覆盖）`;
    if (_dom.gProviderKeyTag) _dom.gProviderKeyTag.className = 'provider-status-tag tag-configured';
    if (_dom.gKeyStatusText) _dom.gKeyStatusText.textContent = `已配置密钥 (${maskedKey})`;
    if (_dom.gApiKeyTip) _dom.gApiKeyTip.textContent = '后端已持久化该提供商密钥。如需修改，请在此输入新密钥后保存生效。';
  } else if (isCustom) {
    if (_dom.gApiKey) _dom.gApiKey.placeholder = 'sk-…（本地 Ollama / vLLM 等无鉴权服务可留空）';
    if (_dom.gProviderKeyTag) _dom.gProviderKeyTag.className = 'provider-status-tag tag-optional';
    if (_dom.gKeyStatusText) _dom.gKeyStatusText.textContent = '可选 (本地服务可免密)';
    if (_dom.gApiKeyTip) _dom.gApiKeyTip.textContent = '本地服务（如 Ollama）无需填写，公网中转或鉴权服务请输入对应凭据。';
  } else {
    if (_dom.gApiKey) _dom.gApiKey.placeholder = `请输入 ${preset?.name || pid} 的有效 API Key (必填)`;
    if (_dom.gProviderKeyTag) _dom.gProviderKeyTag.className = 'provider-status-tag tag-unconfigured';
    if (_dom.gKeyStatusText) _dom.gKeyStatusText.textContent = '未配置 API Key · 无法调用';
    if (_dom.gApiKeyTip) _dom.gApiKeyTip.textContent = '该提供商尚未配置密钥。请前往官方控制台申领并在此填入。';
  }

  // 4. Base URL 填充与一键恢复默认
  const defaultUrl = preset?.default_base_url || (isCustom ? 'http://127.0.0.1:11434/v1' : 'https://api.openai.com/v1');
  const currentUrl = stored?.api_base_url || defaultUrl;
  if (_dom.gBaseUrl) {
    _dom.gBaseUrl.value = currentUrl;
    _dom.gBaseUrl.placeholder = defaultUrl;
  }
  if (_dom.gBtnResetBaseUrl) {
    _dom.gBtnResetBaseUrl.textContent = '重置为默认';
    _dom.gBtnResetBaseUrl.title = `恢复官方默认端点: ${defaultUrl}`;
    _dom.gBtnResetBaseUrl.onclick = () => {
      if (_dom.gBaseUrl) _dom.gBaseUrl.value = defaultUrl;
      showToast(`已恢复官方默认 Base URL: ${defaultUrl}`, 'info');
    };
  }

  // 5. 对话模型选择
  const presetModels = preset?.preset_models || (stored?.chat_model ? [stored.chat_model] : []);
  if (_dom.gChatModelSelect) {
    _dom.gChatModelSelect.innerHTML = '';
    const placeholderOpt = document.createElement('option');
    placeholderOpt.value = '';
    placeholderOpt.textContent = '-- 选择预设推荐模型 --';
    _dom.gChatModelSelect.appendChild(placeholderOpt);
    presetModels.forEach((m) => {
      const opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      _dom.gChatModelSelect.appendChild(opt);
    });
    const customModelOpt = document.createElement('option');
    customModelOpt.value = '__custom_model__';
    customModelOpt.textContent = '✏️ 手动输入任意模型 ID...';
    _dom.gChatModelSelect.appendChild(customModelOpt);
  }

  const currentModel = stored?.chat_model || preset?.default_chat_model || presetModels[0] || '';
  if (_dom.gChatModel) {
    _dom.gChatModel.value = currentModel;
  }
  if (_dom.gChatModelSelect) {
    if (presetModels.includes(currentModel)) {
      _dom.gChatModelSelect.value = currentModel;
    } else if (currentModel) {
      _dom.gChatModelSelect.value = '__custom_model__';
    } else {
      _dom.gChatModelSelect.value = '';
    }
  }

  // 6. 诊断输出框重置
  if (_dom.gProviderTestResult) {
    _dom.gProviderTestResult.className = 'test-result-box hidden';
    if (_dom.gTestResultBody) _dom.gTestResultBody.textContent = '';
  }
}

export function toggleKeyVisibility() {
  if (!_dom.gApiKey) return;
  const isPass = _dom.gApiKey.type === 'password';
  _dom.gApiKey.type = isPass ? 'text' : 'password';
  if (_dom.gBtnToggleKeyText) {
    _dom.gBtnToggleKeyText.textContent = isPass ? '隐藏' : '显示';
  }
  if (_dom.gIconKeyEye) {
    const use = _dom.gIconKeyEye.querySelector('use');
    if (use) use.setAttribute('href', isPass ? '#i-eye-off' : '#i-eye');
  }
}

export function onChatModelSelectChange() {
  if (!_dom.gChatModelSelect || !_dom.gChatModel) return;
  const val = _dom.gChatModelSelect.value;
  if (val && val !== '__custom_model__') {
    _dom.gChatModel.value = val;
  } else if (val === '__custom_model__') {
    _dom.gChatModel.focus();
    _dom.gChatModel.select();
  }
}

export async function handleProviderTest() {
  if (!_dom.gProviderTest || !_dom.gProvider) return;
  _dom.gProviderTest.disabled = true;
  const origHtml = _dom.gProviderTest.innerHTML;
  _dom.gProviderTest.innerHTML =
    '<svg class="icon" style="width:14px;height:14px;vertical-align:-2px;"><use href="#i-activity"></use></svg><span>测试中…</span>';

  try {
    const pid = _dom.gProvider.value === '__custom__' ? 'custom' : _dom.gProvider.value;
    const baseUrl = _dom.gBaseUrl ? _dom.gBaseUrl.value.trim() : '';
    const chatModel = _dom.gChatModel ? _dom.gChatModel.value.trim() : '';
    const apiKey = _dom.gApiKey ? _dom.gApiKey.value.trim() : '';

    const payload = {
      id: pid,
      api_base_url: baseUrl,
      chat_model: chatModel,
    };
    if (apiKey && !apiKey.includes('****')) {
      payload.api_key = apiKey;
    }

    const data = await testProviderConnection(payload);

    if (_dom.gProviderTestResult) _dom.gProviderTestResult.classList.remove('hidden');

    if (data.success) {
      const lat = Math.round(data.latency_ms || 0);
      showToast(`连通性测试成功 (耗时 ${lat}ms)`, 'success');
      if (_dom.gProviderTestResult) {
        _dom.gProviderTestResult.className = 'test-result-box test-result-success';
      }
      if (_dom.gTestResultTitle) _dom.gTestResultTitle.textContent = '连通性测试通过';
      if (_dom.gTestResultLatency) _dom.gTestResultLatency.textContent = `耗时 ${lat}ms`;
      if (_dom.gTestResultBody) {
        const modelsCount = Array.isArray(data.models) ? data.models.length : 0;
        _dom.gTestResultBody.textContent =
          `已成功连接至目标端点，模型 ${chatModel || '默认'} 就绪。` +
          (modelsCount > 0 ? ` (发现 ${modelsCount} 个可用模型)` : '');
      }
    } else {
      const rawErr = data.error || data.message || '连接失败';
      const diag = formatProviderDiagnostic(pid, rawErr, data.diagnostic);
      showToast(`连通性测试未通过: ${diag.title}`, 'error');
      if (_dom.gProviderTestResult) {
        _dom.gProviderTestResult.className = 'test-result-box test-result-error';
      }
      if (_dom.gTestResultTitle) _dom.gTestResultTitle.textContent = diag.title;
      if (_dom.gTestResultLatency) _dom.gTestResultLatency.textContent = '失败';
      if (_dom.gTestResultBody) _dom.gTestResultBody.textContent = diag.guidance;
    }
  } catch (e) {
    showToast(`测试请求异常: ${e.message || e}`, 'error');
    if (_dom.gProviderTestResult) {
      _dom.gProviderTestResult.className = 'test-result-box test-result-error';
      _dom.gProviderTestResult.classList.remove('hidden');
      if (_dom.gTestResultTitle) _dom.gTestResultTitle.textContent = '网络请求异常';
      if (_dom.gTestResultLatency) _dom.gTestResultLatency.textContent = '异常';
      if (_dom.gTestResultBody) _dom.gTestResultBody.textContent = `请求发送失败: ${e.message || e}`;
    }
  } finally {
    _dom.gProviderTest.disabled = false;
    _dom.gProviderTest.innerHTML = origHtml;
  }
}

export async function handleActivateProvider() {
  if (!_dom.gProviderActivate || !_dom.gProvider) return;
  _dom.gProviderActivate.disabled = true;
  const origHtml = _dom.gProviderActivate.innerHTML;
  _dom.gProviderActivate.innerHTML =
    '<svg class="icon" style="width:14px;height:14px;vertical-align:-2px;"><use href="#i-check"></use></svg><span>保存并启用中…</span>';

  try {
    const selected = _dom.gProvider.value;
    const isCustom = selected === '__custom__' || selected === 'custom';
    const pid = isCustom ? 'custom' : selected;

    const baseUrl = _dom.gBaseUrl ? _dom.gBaseUrl.value.trim() : '';
    const chatModel = _dom.gChatModel ? _dom.gChatModel.value.trim() : '';
    const apiKey = _dom.gApiKey ? _dom.gApiKey.value.trim() : '';
    const customName = _dom.gCustomName ? _dom.gCustomName.value.trim() : '';

    if (!baseUrl) {
      showToast('请填写 API Base URL', 'error');
      return;
    }
    if (!chatModel) {
      showToast('请填写或选择模型名称', 'error');
      return;
    }

    const stored = cachedProviders.find((p) => p.id === pid);
    const preset = cachedPresets.find((p) => p.id === pid) || BUILTIN_PRESETS.find((p) => p.id === pid);

    const hasExistingKey = Boolean(stored?.api_key && stored.api_key.length > 0);
    if (!isCustom && !hasExistingKey && !apiKey) {
      showToast(`请先输入 ${preset?.name || pid} 的 API Key 再启用`, 'error');
      if (_dom.gApiKey) _dom.gApiKey.focus();
      return;
    }

    const payload = {
      id: pid,
      name: isCustom ? customName || '自定义模型' : preset?.name || stored?.name || pid,
      api_base_url: baseUrl,
      chat_model: chatModel,
      is_active: true,
    };
    if (apiKey && !apiKey.includes('****')) {
      payload.api_key = apiKey;
    }

    await saveProvider(payload);

    const actResult = await apiActivateProvider(pid);
    const active = actResult.active_provider;

    currentActiveProvider = active || { name: payload.name, chat_model: chatModel };
    if (_callbacks.updateBadge) _callbacks.updateBadge();

    showToast(`已成功保存并启用：${payload.name} · ${chatModel}`, 'success');
    await loadProviders();
  } catch (e) {
    console.error('Failed to activate provider:', e);
    showToast(`模型启用失败: ${e.message || e}`, 'error');
  } finally {
    _dom.gProviderActivate.disabled = false;
    _dom.gProviderActivate.innerHTML = origHtml;
  }
}

export async function fetchSystemTelemetry() {
  try {
    const [statusRes, cacheRes] = await Promise.all([
      fetch('/api/system/status').catch(() => null),
      fetch('/api/cache/stats').catch(() => null),
    ]);
    if (statusRes && statusRes.ok) {
      const data = await statusRes.json();
      // 1. GPT-SoVITS
      if (data.gpt_sovits && _dom.gDashSovitsStatus) {
        const isOnline = data.gpt_sovits.status === 'reachable';
        _dom.gDashSovitsBadge.className = `badge-status-pill ${isOnline ? 'badge-pill-green' : 'badge-pill-red'}`;
        _dom.gDashSovitsBadge.textContent = isOnline ? '在线' : '离线';
        _dom.gDashSovitsStatus.textContent = isOnline ? `运行正常 (${data.gpt_sovits.latency_ms || 0}ms)` : '服务未响应';
        _dom.gDashSovitsUrl.textContent = data.gpt_sovits.base_url || 'http://127.0.0.1:9880';
      }
      // 2. Precision & GPU
      if (data.hardware) {
        const prec = (data.hardware.inference_precision || '').toUpperCase();
        const isCpu = prec === 'CPU';
        const isFp16 = prec === 'FP16';
        if (_dom.gDashPrecisionBadge) {
          if (isCpu) {
            _dom.gDashPrecisionBadge.className = 'badge-status-pill badge-pill-yellow';
            _dom.gDashPrecisionBadge.textContent = 'CPU';
          } else if (isFp16) {
            _dom.gDashPrecisionBadge.className = 'badge-status-pill badge-pill-indigo';
            _dom.gDashPrecisionBadge.textContent = 'FP16';
          } else {
            _dom.gDashPrecisionBadge.className = 'badge-status-pill badge-pill-green';
            _dom.gDashPrecisionBadge.textContent = 'FP32';
          }
        }
        if (_dom.gDashPrecisionVal) {
          if (isCpu) {
            _dom.gDashPrecisionVal.textContent = '🛡️ CPU 稳定模式';
          } else if (isFp16) {
            _dom.gDashPrecisionVal.textContent = '⚡ FP16 半精度';
          } else {
            _dom.gDashPrecisionVal.textContent = '🛡️ FP32 单精度';
          }
        }
        if (_dom.gBtnTogglePrecisionText) {
          if (isCpu) {
            _dom.gBtnTogglePrecisionText.textContent = '切为 GPU(FP16)';
          } else if (isFp16) {
            _dom.gBtnTogglePrecisionText.textContent = '切为 FP32 重启';
          } else {
            _dom.gBtnTogglePrecisionText.textContent = '切为 CPU 模式';
          }
        }
        if (_dom.gDashDeviceVal) {
          if (isCpu) {
            _dom.gDashDeviceVal.textContent = '免显存占用 · 依托物理大内存';
          } else {
            _dom.gDashDeviceVal.textContent = data.hardware.gpu_name ? data.hardware.gpu_name : '硬件加速中';
          }
        }
        if (_dom.gDashHardwareVal) {
          const procMem =
            data.app && data.app.memory_usage_mb !== undefined && data.app.memory_usage_mb !== null
              ? `${Math.round(data.app.memory_usage_mb)} MB`
              : '正常';
          _dom.gDashHardwareVal.textContent = `服务内存: ${procMem}`;
        }
        const hostRamEl = $('gDashHostRam');
        if (hostRamEl) {
          if (data.hardware.system_memory_gb && data.hardware.system_memory_avail_gb) {
            hostRamEl.textContent = `系统可用: ${data.hardware.system_memory_avail_gb.toFixed(1)}G / ${data.hardware.system_memory_gb.toFixed(1)}G`;
          } else {
            hostRamEl.textContent = '系统内存: 良好';
          }
        }
      }
      // 3. Uptime & PID
      if (data.app && _dom.gDashUptimeVal) {
        const sec = Math.round(data.app.uptime_seconds || 0);
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        const s = sec % 60;
        _dom.gDashUptimeVal.textContent = `PID: ${data.app.pid || '-'} · 运行: ${h}h ${m}m ${s}s`;
      }
    }
    if (cacheRes && cacheRes.ok && _dom.gDashCacheVal) {
      const cData = await cacheRes.json();
      _dom.gDashCacheVal.textContent = `${cData.total_entries || 0} 个文件 · ${(cData.total_size_mb || 0).toFixed(1)} MB`;
    }
  } catch (e) {
    console.warn('Failed to fetch telemetry:', e);
  }
}

export function updateTelegramFieldsVisibility() {
  const enabled = _dom.gTgEnabled ? _dom.gTgEnabled.checked : false;
  if (_dom.gTgFieldsGroup) {
    _dom.gTgFieldsGroup.style.opacity = enabled ? '1' : '0.45';
    _dom.gTgFieldsGroup.style.pointerEvents = enabled ? 'auto' : 'none';
  }
}

export async function loadGlobalConfig() {
  try {
    const fetchVoice = _callbacks.fetchVoiceProfiles
      ? _callbacks.fetchVoiceProfiles().catch(() => null)
      : Promise.resolve(null);
    const [cfgRes] = await Promise.all([fetch('/api/config'), fetchVoice]);
    const cfg = await cfgRes.json();
    const s = cfg.settings || {};
    const provider = cfg.active_provider;
    const voiceProfiles = _callbacks.getVoiceProfiles ? _callbacks.getVoiceProfiles() : [];
    const activeProfileId = _callbacks.getActiveProfileId ? _callbacks.getActiveProfileId() : null;
    const activeVoice = voiceProfiles.find((p) => p.id === activeProfileId);

    const isOk = cfg.status === 'ok';
    const providerStr = provider ? `${provider.name} · ${provider.chat_model || '-'}` : '未配置';
    const voiceStr = activeVoice ? activeVoice.name : '未加载';

    const diagBackend = $('gDiagBackend');
    const diagBackendDot = $('gDiagBackendDot');
    const diagModel = $('gDiagModel');
    const diagModelDot = $('gDiagModelDot');
    const diagVoice = $('gDiagVoice');
    const diagVoiceDot = $('gDiagVoiceDot');

    if (diagBackend) diagBackend.textContent = isOk ? '运行正常' : String(cfg.status);
    if (diagBackendDot) diagBackendDot.className = `diag-dot ${isOk ? 'ok' : ''}`;
    if (diagModel) {
      diagModel.textContent = providerStr;
      diagModel.title = providerStr;
    }
    if (diagModelDot) diagModelDot.className = `diag-dot ${provider ? 'ok' : ''}`;
    if (diagVoice) {
      diagVoice.textContent = voiceStr;
      diagVoice.title = voiceStr;
    }
    if (diagVoiceDot) diagVoiceDot.className = `diag-dot ${activeVoice ? 'ok' : ''}`;

    // 语音与推理参数
    if (_dom.gParamSovitsUrl) _dom.gParamSovitsUrl.value = s.gpt_sovits_url || 'http://127.0.0.1:9880';
    if (_dom.gPrecision) _dom.gPrecision.value = s.inference_precision || 'auto';
    if (_dom.gParamSliceMethod) _dom.gParamSliceMethod.value = s.text_split_method || 'cut5';
    if (_dom.gParamSpeed) {
      _dom.gParamSpeed.value = s.speed_factor !== undefined ? s.speed_factor : 1.0;
      if (_dom.gParamSpeedVal) _dom.gParamSpeedVal.textContent = _dom.gParamSpeed.value;
    }
    if (_dom.gParamTopK) {
      _dom.gParamTopK.value = s.top_k || 15;
      if (_dom.gParamTopKVal) _dom.gParamTopKVal.textContent = _dom.gParamTopK.value;
    }
    if (_dom.gParamTopP) {
      _dom.gParamTopP.value = s.top_p !== undefined ? s.top_p : 1.0;
      if (_dom.gParamTopPVal) _dom.gParamTopPVal.textContent = _dom.gParamTopP.value;
    }
    if (_dom.gParamTemp) {
      _dom.gParamTemp.value = s.temperature !== undefined ? s.temperature : 1.0;
      if (_dom.gParamTempVal) _dom.gParamTempVal.textContent = _dom.gParamTemp.value;
    }
    if (_dom.gParamFragmentInterval) {
      _dom.gParamFragmentInterval.value = s.fragment_interval !== undefined ? s.fragment_interval : 0.3;
    }
    if (_dom.gAudioRetention) _dom.gAudioRetention.value = s.audio_retention_minutes || 30;
    if (_dom.gDashRetentionVal) _dom.gDashRetentionVal.textContent = `保留时长: ${s.audio_retention_minutes || 30} 分钟`;

    // STT 与 Telegram
    if (_dom.gSttEngine) _dom.gSttEngine.value = s.stt_engine || 'browser';
    if (_dom.gTgEnabled) {
      _dom.gTgEnabled.checked = Boolean(s.telegram_enabled);
      updateTelegramFieldsVisibility();
    }
    const token = s.telegram_bot_token || '';
    const masked = token.includes('****');
    if (_dom.gTgToken) {
      _dom.gTgToken.value = '';
      _dom.gTgToken.placeholder = masked ? '已保存（输入新 Token 可覆盖）' : '未配置，如 123456:ABC-DEF…';
    }
    if (_dom.gTgChatId) _dom.gTgChatId.value = s.telegram_chat_id || s.telegram_admin_ids || '';
    if (_dom.gTgProxyHost) _dom.gTgProxyHost.value = s.telegram_proxy_host || '';
    if (_dom.gTgProxyPort) _dom.gTgProxyPort.value = s.telegram_proxy_port || '';
    if (_dom.gTgProxyEnabled) _dom.gTgProxyEnabled.checked = Boolean(s.telegram_proxy_enabled);

    // 记忆与人设
    if (_dom.gMemoryEnabled) _dom.gMemoryEnabled.checked = s.memory_enabled !== false;
    if (_dom.gUserNickname) _dom.gUserNickname.value = s.user_nickname || '';
    if (_dom.gDefaultSystemPrompt) _dom.gDefaultSystemPrompt.value = s.system_prompt || '';
  } catch (e) {
    if (_dom.gStatus) _dom.gStatus.textContent = '状态加载失败（后端未启动？）';
  }
}

export async function saveGlobalConfig() {
  if (_dom.gTgSave) _dom.gTgSave.disabled = true;
  try {
    const payload = {
      gpt_sovits_url: _dom.gParamSovitsUrl ? _dom.gParamSovitsUrl.value.trim() || 'http://127.0.0.1:9880' : undefined,
      inference_precision: _dom.gPrecision ? _dom.gPrecision.value : undefined,
      text_split_method: _dom.gParamSliceMethod ? _dom.gParamSliceMethod.value : undefined,
      speed_factor: _dom.gParamSpeed ? Number(_dom.gParamSpeed.value) : undefined,
      top_k: _dom.gParamTopK ? Number(_dom.gParamTopK.value) : undefined,
      top_p: _dom.gParamTopP ? Number(_dom.gParamTopP.value) : undefined,
      temperature: _dom.gParamTemp ? Number(_dom.gParamTemp.value) : undefined,
      fragment_interval: _dom.gParamFragmentInterval ? Number(_dom.gParamFragmentInterval.value) || 0.3 : undefined,
      audio_retention_minutes: _dom.gAudioRetention ? Math.max(1, Number(_dom.gAudioRetention.value) || 30) : 30,
      stt_engine: _dom.gSttEngine ? _dom.gSttEngine.value : undefined,
      telegram_enabled: _dom.gTgEnabled ? _dom.gTgEnabled.checked : false,
      telegram_chat_id: _dom.gTgChatId ? _dom.gTgChatId.value.trim() || undefined : undefined,
      telegram_admin_ids: _dom.gTgChatId ? _dom.gTgChatId.value.trim() || undefined : undefined,
      telegram_proxy_enabled: _dom.gTgProxyEnabled ? _dom.gTgProxyEnabled.checked : false,
      telegram_proxy_host: _dom.gTgProxyHost ? _dom.gTgProxyHost.value.trim() || '127.0.0.1' : '127.0.0.1',
      telegram_proxy_port: _dom.gTgProxyPort ? Number(_dom.gTgProxyPort.value) || 10809 : 10809,
      memory_enabled: _dom.gMemoryEnabled ? _dom.gMemoryEnabled.checked : true,
      user_nickname: _dom.gUserNickname ? _dom.gUserNickname.value.trim() : undefined,
      system_prompt: _dom.gDefaultSystemPrompt ? _dom.gDefaultSystemPrompt.value.trim() || undefined : undefined,
    };
    const token = _dom.gTgToken ? _dom.gTgToken.value.trim() : '';
    if (token) payload.telegram_bot_token = token;

    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);

    if (_dom.gPreset) {
      saveGlobal({ ttsPreset: _dom.gPreset.value });
    }
    if (_dom.gAutoTranslate) {
      saveGlobal({ autoTranslate: _dom.gAutoTranslate.value === 'auto' });
    }

    showToast('全局配置已成功保存并实时生效', 'success');
    loadGlobalConfig();
    fetchSystemTelemetry();
  } catch (e) {
    showToast(`保存失败: ${e.message || e}`, 'error');
  } finally {
    if (_dom.gTgSave) _dom.gTgSave.disabled = false;
  }
}

export async function testTelegram() {
  if (_dom.gTgTest) _dom.gTgTest.disabled = true;
  try {
    const payload = {
      proxy_enabled: _dom.gTgProxyEnabled ? _dom.gTgProxyEnabled.checked : false,
      proxy_host: _dom.gTgProxyHost ? _dom.gTgProxyHost.value.trim() || '127.0.0.1' : '127.0.0.1',
      proxy_port: _dom.gTgProxyPort ? Number(_dom.gTgProxyPort.value) || 10809 : 10809,
    };
    const token = _dom.gTgToken ? _dom.gTgToken.value.trim() : '';
    if (token) payload.token = token;
    const res = await fetch('/api/telegram/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    showToast(data.message || (data.success ? '连接成功' : '连接失败'), data.success ? 'success' : 'error');
  } catch (e) {
    showToast(`测试失败: ${e.message || e}`, 'error');
  } finally {
    if (_dom.gTgTest) _dom.gTgTest.disabled = false;
  }
}

async function handleRestartSovits() {
  _dom.gBtnRestartSovits.disabled = true;
  _dom.gBtnRestartSovits.innerHTML = '<svg class="icon"><use href="#i-play"></use></svg><span>正在热重启...</span>';
  try {
    const payload = {};
    if (_dom.gPrecision && _dom.gPrecision.value) {
      payload.precision = _dom.gPrecision.value;
    }
    const res = await fetch('/api/system/restart_sovits', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    showToast(data.message || 'GPT-SoVITS 重启指令已发送', 'success');
    setTimeout(() => {
      fetchSystemTelemetry();
      _dom.gBtnRestartSovits.disabled = false;
      _dom.gBtnRestartSovits.innerHTML = '<svg class="icon"><use href="#i-play"></use></svg><span>重启 SoVITS 引擎</span>';
    }, 2500);
  } catch (err) {
    showToast(`重启失败: ${err.message}`, 'error');
    _dom.gBtnRestartSovits.disabled = false;
    _dom.gBtnRestartSovits.innerHTML = '<svg class="icon"><use href="#i-play"></use></svg><span>重启 SoVITS 引擎</span>';
  }
}

async function handleTogglePrecision() {
  const badgeText = _dom.gDashPrecisionBadge ? _dom.gDashPrecisionBadge.textContent.trim().toUpperCase() : 'FP16';
  let targetPrec = 'fp16';
  let targetLabel = 'FP16 半精度';
  if (badgeText === 'FP16') {
    targetPrec = 'fp32';
    targetLabel = 'FP32 单精度';
  } else if (badgeText === 'FP32') {
    targetPrec = 'cpu';
    targetLabel = 'CPU 模式 (免显存)';
  } else {
    targetPrec = 'fp16';
    targetLabel = 'FP16 半精度';
  }

  _dom.gBtnTogglePrecision.disabled = true;
  if (_dom.gBtnRestartSovits) _dom.gBtnRestartSovits.disabled = true;
  if (_dom.gBtnTogglePrecisionText) _dom.gBtnTogglePrecisionText.textContent = `正在切换为 ${targetPrec.toUpperCase()}...`;
  try {
    if (_dom.gPrecision) _dom.gPrecision.value = targetPrec;
    const res = await fetch('/api/system/restart_sovits', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ precision: targetPrec }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    showToast(data.message || `已按 ${targetLabel} 重启语音引擎`, 'success');
    setTimeout(() => {
      fetchSystemTelemetry();
      _dom.gBtnTogglePrecision.disabled = false;
      if (_dom.gBtnRestartSovits) _dom.gBtnRestartSovits.disabled = false;
    }, 2500);
  } catch (err) {
    showToast(`切换精度重启失败: ${err.message}`, 'error');
    _dom.gBtnTogglePrecision.disabled = false;
    if (_dom.gBtnRestartSovits) _dom.gBtnRestartSovits.disabled = false;
  }
}

async function handleClearCache() {
  if (!confirm('确定要清空全部 TTS 离线音频缓存吗？清空后新请求将重新合成。')) return;
  _dom.gBtnClearCache.disabled = true;
  try {
    clearMemAudioCache();
    if (_callbacks.revokeAllCachedAudioUrls) {
      _callbacks.revokeAllCachedAudioUrls();
    }
    if ('caches' in window) {
      await caches.delete('gal2voice-audio-v1').catch(() => {});
    }
    const res = await fetch('/api/cache/clear', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    showToast(`缓存已清空：删除 ${data.deleted_files || 0} 个文件，释放 ${data.freed_mb || 0} MB`, 'success');
    fetchSystemTelemetry();
  } catch (err) {
    showToast(`清空缓存失败: ${err.message}`, 'error');
  } finally {
    _dom.gBtnClearCache.disabled = false;
  }
}
