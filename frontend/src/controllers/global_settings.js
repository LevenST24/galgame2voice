// Global Settings Controller: LLM Providers, Hardware Telemetry, Telegram, Audio Cache
import {
  fetchProviders,
  testProviderConnection,
  getErrorMessage,
  requestJson,
} from '../api.js';
import { formatProviderDiagnostic, BUILTIN_PRESETS } from '../settings.js';
import { state, saveGlobal } from '../store.js';
import { showToast } from '../ui.js';
import { clearMemAudioCache } from '../cache.js';
import { initEngineDirectory } from './engine_directory.js';
import { waitForEngine } from './engine_recovery.js';
import { runSettingsMutation, saveConfirmedConfig } from './config_recovery.js';
import { saveConfirmedProvider, activateConfirmedProvider } from './provider_recovery.js';
import { initSettingsTabs, onModalChange } from '../settings_ui.js';

const $ = (id) => document.getElementById(id);
const UNCONFIRMED_RESTART_MESSAGE = '重启请求结果未确认；当前引擎已在线。计算方式以「启动计算方式」显示的状态为准。';

let _dom = {};
let _callbacks = {};
let cachedProviders = [];
let cachedPresets = [];
let currentActiveProvider = null;
let providersLoaded = false;
let providerReadError = false;
let providerDirty = false;
let providerRevision = 0;
let providerLoad = 0;
let providerSaving = false;
let configSaving = false;
const dirtyProviderFields = new Set();
const PROVIDER_FIELDS = ['gApiKey', 'gBaseUrl', 'gChatModel', 'gChatModelSelect', 'gReasoningEffort', 'gCustomName'];
let engineDirectory = null;
let configDirty = false;
let configRevision = 0;
let configLoad = 0;
// The form starts with HTML defaults. Saving it before the server's values arrived
// would overwrite the real settings (and stop a running Telegram bot), so saving waits
// for one successful load, and a load fills every field the user has not edited.
let configLoaded = false;
const dirtyConfigFields = new Set();
const RESET_CONFIG_FIELDS = ['gParamSpeed', 'gParamTopK', 'gParamTopP', 'gParamTemp', 'gParamSliceMethod',
  'gPrecision', 'gAudioRetention'];
let restarting = false;
let precisionRevision = 0;
let engineStatusRevision = 0;
let engineRestartUnconfirmed = false;
let telemetryTimer = null;
let telemetryGeneration = 0;
let telemetryActive = false;
let telemetryController = null;
let unsubscribeTelemetry = null;
let privateEndpointRevision = 0;
let privateEndpointSaved = false;
let privateEndpointSaving = false;
const CONFIG_FIELDS = ['gParamSovitsUrl', 'gPrecision', 'gParamSliceMethod', 'gParamSpeed', 'gParamTopK',
  'gParamTopP', 'gParamTemp', 'gParamFragmentInterval', 'gAudioRetention', 'gTgEnabled',
  'gTgToken', 'gTgChatId', 'gTgProxyHost', 'gTgProxyPort', 'gTgProxyEnabled'];

export function getCurrentActiveProvider() {
  return currentActiveProvider;
}

/** 模型列表是否已读取过（成功或失败）；之前没有活跃模型不代表「未配置」。 */
export function haveProvidersLoaded() {
  return providersLoaded;
}

export function hasProviderReadError() {
  return providerReadError;
}

function markProviderDirty(field) {
  providerDirty = true;
  providerRevision += 1;
  dirtyProviderFields.add(field);
  if (field === 'gChatModelSelect') dirtyProviderFields.add('gChatModel');
}

function updateSaveButtons() {
  const busy = providerSaving || configSaving;
  if (_dom.gProviderActivate) _dom.gProviderActivate.disabled = busy;
  if (_dom.gTgSave) _dom.gTgSave.disabled = busy;
}

export function initGlobalSettings(dom, callbacks = {}) {
  unsubscribeTelemetry?.();
  stopTelemetryRefresh();
  _dom = dom;
  _callbacks = callbacks;
  engineStatusRevision += 1;
  engineRestartUnconfirmed = false;
  precisionRevision += 1;
  providerDirty = false;
  providerRevision += 1;
  providerLoad += 1;
  dirtyProviderFields.clear();
  providerSaving = configSaving = false;
  updateSaveButtons();
  configDirty = false;
  configRevision = 0;
  configLoad = 0;
  configLoaded = false;
  dirtyConfigFields.clear();
  privateEndpointRevision += 1;
  privateEndpointSaved = false;
  privateEndpointSaving = false;
  dom.gAllowPrivateEndpoints?.addEventListener('change', savePrivateEndpointPreference);
  CONFIG_FIELDS.forEach((field) => {
    for (const event of ['input', 'change']) dom[field]?.addEventListener(event, () => {
      configDirty = true;
      configRevision += 1;
      dirtyConfigFields.add(field);
    });
  });
  engineDirectory = initEngineDirectory(dom, showToast, () => _callbacks.onSovitsDirectorySaved?.());
  const telemetryDocument = document;
  const syncTelemetryVisibility = () => {
    if (settingsVisible() && !telemetryDocument.hidden) startTelemetryRefresh();
    else stopTelemetryRefresh();
  };
  const unsubscribeModal = onModalChange(syncTelemetryVisibility);
  telemetryDocument.addEventListener?.('visibilitychange', syncTelemetryVisibility);
  unsubscribeTelemetry = () => {
    unsubscribeModal();
    telemetryDocument.removeEventListener?.('visibilitychange', syncTelemetryVisibility);
  };

  // 选项卡切换
  initSettingsTabs(_dom.gTabs, name => _callbacks.onSettingsTabChange?.(name));
  for (const id of ['gOpenSessionSettings', 'gOpenCharacterSettings']) $(id)?.addEventListener('click', () => {
    if (_callbacks.onSettingsTabChange) {
      if (id === 'gOpenSessionSettings') _dom.gTabs?.querySelector('[data-gtab="session"]')?.click();
      return;
    }
    _callbacks.closeModal?.(_dom.globalModal);
    _callbacks.onOpenSessionSettings?.();
  });
  $('gOpenChatExperience')?.addEventListener('click', () => {
    if (_callbacks.onSettingsTabChange) return;
    _callbacks.closeModal?.(_dom.globalModal);
    _callbacks.onOpenReadingSettings?.();
  });

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
      const revision = ++precisionRevision;
      const val = _dom.gPrecision.value;
      try {
        await saveConfirmedConfig({ inference_precision: val });
        if (revision !== precisionRevision) return;
        const labelMap = {
          auto: '自动判定 (Auto)',
          fp16: 'FP16 半精度',
          fp32: 'FP32 单精度',
          cpu: 'CPU 稳定模式 (免显存)',
        };
        showToast(`已保存推理设置: ${labelMap[val] || val}。请点击【重启 SoVITS 引擎】应用。`, 'info');
        fetchSystemTelemetry();
      } catch (err) {
        if (revision !== precisionRevision) return;
        showToast(`推理设置尚未保存：${err.message}。当前选择已保留。`, 'error');
      }
    });
  }

  // 跳转至语音与推理设置
  const goToInferenceTab = () => {
    const tabBtn = _dom.gTabs ? _dom.gTabs.querySelector('[data-gtab="inference"]') : null;
    if (tabBtn) tabBtn.click();
  };
  const btnGoPrecision = $('gBtnGoPrecision');
  if (btnGoPrecision) {
    btnGoPrecision.addEventListener('click', goToInferenceTab);
  }
  const btnGoSovitsSetup = $('gBtnGoSovitsSetup');
  if (btnGoSovitsSetup) {
    btnGoSovitsSetup.addEventListener('click', () => {
      goToInferenceTab();
      _dom.gSovitsDirectory?.focus?.();
    });
  }
  const diagModelChip = $('gDiagModelChip');
  if (diagModelChip) {
    diagModelChip.style.cursor = 'pointer';
    diagModelChip.addEventListener('click', () => {
      const tabBtn = _dom.gTabs ? _dom.gTabs.querySelector('[data-gtab="models"]') : null;
      if (tabBtn) tabBtn.click();
    });
  }
  const diagVoiceChip = $('gDiagVoiceChip');
  if (diagVoiceChip) {
    diagVoiceChip.style.cursor = 'pointer';
    diagVoiceChip.addEventListener('click', () => {
      if (_callbacks.onSettingsTabChange) {
        _dom.gTabs?.querySelector('[data-gtab="session"]')?.click();
        return;
      }
      _callbacks.closeModal?.(_dom.globalModal);
      _callbacks.onOpenSessionSettings?.();
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
  for (const field of PROVIDER_FIELDS) {
    for (const event of ['input', 'change']) _dom[field]?.addEventListener(event, () => markProviderDirty(field));
  }

  // 恢复默认参数
  if (_dom.gResetBtn) {
    _dom.gResetBtn.addEventListener('click', () => {
      if (!confirm('确定要将全局参数恢复为推荐默认值吗？')) return;
      configDirty = true;
      configRevision += 1;
      RESET_CONFIG_FIELDS.forEach((field) => dirtyConfigFields.add(field));
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

export function openGlobalSettings(tabName = null) {
  if (_callbacks.openModal) _callbacks.openModal(_dom.globalModal);
  if (typeof tabName === 'string' && tabName && _dom.gTabs) {
    const tabBtn = _dom.gTabs.querySelector(`[data-gtab="${tabName}"]`);
    if (tabBtn) tabBtn.click();
  } else {
    _callbacks.onSettingsTabChange?.(_dom.gTabs?.querySelector('.active')?.dataset.gtab);
  }
  loadProviders();
  loadGlobalConfig();
  if (settingsVisible()) startTelemetryRefresh();
  else if (!document.hidden) fetchSystemTelemetry();
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

function settingsVisible() {
  return _dom.globalModal && !_dom.globalModal.hidden && !_dom.globalModal.classList.contains('hidden');
}

function stopTelemetryRefresh() {
  clearTimeout(telemetryTimer);
  telemetryTimer = null;
  telemetryActive = false;
  telemetryController?.abort();
  telemetryController = null;
  telemetryGeneration += 1;
  engineStatusRevision += 1;
}

function startTelemetryRefresh() {
  if (telemetryActive || document.hidden || !settingsVisible()) return;
  telemetryActive = true;
  const generation = ++telemetryGeneration;
  const refresh = async () => {
    // Restart owns its own readiness polling. Status refreshes never restart or
    // reconfigure the engine, and closed dialogs do not leave timers behind.
    if (!restarting && !document.hidden) await fetchSystemTelemetry();
    if (generation !== telemetryGeneration) return;
    if (!settingsVisible()) { stopTelemetryRefresh(); return; }
    telemetryTimer = setTimeout(refresh, 5000);
  };
  refresh();
}

export async function loadProviders({ formRevision = providerRevision } = {}) {
  const load = ++providerLoad;
  const hadOptions = Boolean(_dom.gProvider?.children.length);
  if (_dom.gProvider && !hadOptions) _dom.gProvider.innerHTML = '<option value="">加载模型列表…</option>';
  try {
    const data = await fetchProviders();
    if (load !== providerLoad) return;
    cachedProviders = data && data.providers ? data.providers : [];
    cachedPresets = data && data.presets && data.presets.length ? data.presets : BUILTIN_PRESETS;

    const active = cachedProviders.find((p) => p.is_active);
    currentActiveProvider = active || null;
    providersLoaded = true;
    providerReadError = false;
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

    const keepDraft = providerDirty || providerRevision !== formRevision;
    const selection = keepDraft ? _dom.gProvider?.value : null;
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
      if (selection && [..._dom.gProvider.children].some(option => option.value === selection)) {
        _dom.gProvider.value = selection;
      }
    }

    renderLoadedProvider(keepDraft);
  } catch (e) {
    if (load !== providerLoad) return;
    console.error('Failed to load providers:', e);
    providersLoaded = true;
    providerReadError = true;
    if (_callbacks.updateBadge) _callbacks.updateBadge();
    if (hadOptions) {
      showToast(`列表暂时无法刷新，当前输入已保留：${e.message}`, 'error');
      return;
    }
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
    renderLoadedProvider(providerDirty || providerRevision !== formRevision);
  }
}

export function onProviderChange() {
  providerDirty = false;
  providerRevision += 1;
  dirtyProviderFields.clear();
  renderProviderForm();
}

function renderLoadedProvider(keepDraft) {
  renderProviderForm(keepDraft);
  if (!keepDraft) {
    providerDirty = false;
    dirtyProviderFields.clear();
  }
}

function renderProviderForm(keepDraft = false) {
  if (!_dom.gProvider) return;
  const fill = (field, apply) => {
    if (_dom[field] && !(keepDraft && dirtyProviderFields.has(field))) apply(_dom[field]);
  };
  const pid = _dom.gProvider.value;
  const isCustom = pid === 'custom' || pid === '__custom__';
  const realId = isCustom ? 'custom' : pid;

  const stored = cachedProviders.find((p) => p.id === realId);
  const preset = cachedPresets.find((p) => p.id === realId) || BUILTIN_PRESETS.find((p) => p.id === realId);

  // 1. 自定义提供商名称字段显隐
  if (_dom.gCustomNameField) {
    _dom.gCustomNameField.classList.toggle('hidden', !isCustom);
    fill('gCustomName', el => { el.value = isCustom ? stored?.name || '自定义模型' : ''; });
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
  const keepKey = keepDraft && dirtyProviderFields.has('gApiKey');
  fill('gApiKey', el => { el.value = ''; el.type = 'password'; });
  if (_dom.gBtnToggleKeyText && !keepKey) {
    _dom.gBtnToggleKeyText.textContent = '显示';
  }
  if (_dom.gIconKeyEye && !keepKey) {
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
  fill('gBaseUrl', el => { el.value = currentUrl; el.placeholder = defaultUrl; });
  if (_dom.gBtnResetBaseUrl) {
    _dom.gBtnResetBaseUrl.textContent = '重置为默认';
    _dom.gBtnResetBaseUrl.title = `恢复官方默认端点: ${defaultUrl}`;
    _dom.gBtnResetBaseUrl.onclick = () => {
      if (_dom.gBaseUrl) {
        _dom.gBaseUrl.value = defaultUrl;
        markProviderDirty('gBaseUrl');
      }
      showToast(`已恢复官方默认 Base URL: ${defaultUrl}`, 'info');
    };
  }

  // 5. 对话模型选择
  const presetModels = preset?.preset_models || (stored?.chat_model ? [stored.chat_model] : []);
  const keepModelSelect = keepDraft && dirtyProviderFields.has('gChatModelSelect');
  if (_dom.gChatModelSelect && !keepModelSelect) {
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

  const currentModel = keepDraft && dirtyProviderFields.has('gChatModel') ? _dom.gChatModel.value
    : stored?.chat_model || preset?.default_chat_model || presetModels[0] || '';
  fill('gChatModel', el => { el.value = currentModel; });
  if (_dom.gChatModelSelect && !keepModelSelect) {
    if (presetModels.includes(currentModel)) {
      _dom.gChatModelSelect.value = currentModel;
    } else if (currentModel) {
      _dom.gChatModelSelect.value = '__custom_model__';
    } else {
      _dom.gChatModelSelect.value = '';
    }
  }

  // 6. 思考强度设置
  fill('gReasoningEffort', el => { el.value = stored ? (stored.reasoning_effort || '') : (preset?.default_reasoning_effort || ''); });

  // 7. 诊断输出框重置
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

export async function savePrivateEndpointPreference() {
  if (!_dom.gAllowPrivateEndpoints || privateEndpointSaving) return;
  const revision = ++privateEndpointRevision;
  const dom = _dom;
  const allowed = dom.gAllowPrivateEndpoints.checked;
  privateEndpointSaving = true;
  dom.gAllowPrivateEndpoints.disabled = true;
  if (dom.gPrivateEndpointsStatus) dom.gPrivateEndpointsStatus.textContent = '正在保存连接范围…';
  try {
    await saveConfirmedConfig({ allow_private_llm_endpoints: allowed });
    if (revision !== privateEndpointRevision) return;
    privateEndpointSaved = allowed;
    if (dom.gPrivateEndpointsStatus) dom.gPrivateEndpointsStatus.textContent = allowed
      ? '已保存：允许连接你填写的本机或局域网模型地址。仅填写你信任的服务地址。'
      : '已保存：仅允许公网模型地址。本机或局域网模型需要先开启此选项。';
  } catch (error) {
    if (revision !== privateEndpointRevision) return;
    dom.gAllowPrivateEndpoints.checked = privateEndpointSaved;
    if (dom.gPrivateEndpointsStatus) dom.gPrivateEndpointsStatus.textContent = `连接范围尚未确认保存，已恢复上次确认的选择。请重试：${error.message}`;
    showToast(`连接范围保存失败：${error.message}`, 'error');
  } finally {
    if (revision === privateEndpointRevision) {
      privateEndpointSaving = false;
      dom.gAllowPrivateEndpoints.disabled = false;
    }
  }
}

function connectionPreferencePending() {
  if (!privateEndpointSaving) return false;
  showToast('正在保存模型连接范围，请完成后再测试或启用模型。', 'info');
  return true;
}

export async function handleProviderTest() {
  if (!_dom.gProviderTest || !_dom.gProvider) return;
  if (connectionPreferencePending()) return;
  _dom.gProviderTest.disabled = true;
  const origHtml = _dom.gProviderTest.innerHTML;
  _dom.gProviderTest.innerHTML =
    '<svg class="icon" style="width:14px;height:14px;vertical-align:-2px;"><use href="#i-activity"></use></svg><span>测试中…</span>';

  try {
    const pid = _dom.gProvider.value === '__custom__' ? 'custom' : _dom.gProvider.value;
    const baseUrl = _dom.gBaseUrl ? _dom.gBaseUrl.value.trim() : '';
    const chatModel = _dom.gChatModel ? _dom.gChatModel.value.trim() : '';
    const apiKey = _dom.gApiKey ? _dom.gApiKey.value.trim() : '';
    const reasoningEffort = _dom.gReasoningEffort ? _dom.gReasoningEffort.value : null;

    const payload = {
      id: pid,
      api_base_url: baseUrl,
      chat_model: chatModel,
      reasoning_effort: reasoningEffort || null,
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
  return saveAndActivateProvider();
}

// The footer save reuses this; it reports success itself and must say the rest was not saved either.
async function saveAndActivateProvider({ fromFooter = false } = {}) {
  if (!_dom.gProviderActivate || !_dom.gProvider) return false;
  if (providerSaving || configSaving && !fromFooter) return false;
  if (connectionPreferencePending()) return false;
  const savedRevision = providerRevision;
  providerSaving = true;
  updateSaveButtons();
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
    const reasoningEffort = _dom.gReasoningEffort ? _dom.gReasoningEffort.value : null;
    const customName = _dom.gCustomName ? _dom.gCustomName.value.trim() : '';

    if (!baseUrl) {
      throw new Error('请填写 API Base URL');
    }
    if (!chatModel) {
      throw new Error('请填写或选择模型名称');
    }

    const stored = cachedProviders.find((p) => p.id === pid);
    const preset = cachedPresets.find((p) => p.id === pid) || BUILTIN_PRESETS.find((p) => p.id === pid);

    const hasExistingKey = Boolean(stored?.api_key && stored.api_key.length > 0);
    if (!isCustom && !hasExistingKey && !apiKey) {
      if (_dom.gApiKey) _dom.gApiKey.focus();
      throw new Error(`请先输入 ${preset?.name || pid} 的 API Key 再启用`);
    }

    const payload = {
      id: pid,
      name: isCustom ? customName || '自定义模型' : preset?.name || stored?.name || pid,
      api_base_url: baseUrl,
      chat_model: chatModel,
      reasoning_effort: reasoningEffort || null,
      is_active: true,
    };
    if (apiKey && !apiKey.includes('****')) {
      payload.api_key = apiKey;
    }

    const saved = await saveConfirmedProvider(payload);
    // is_active is committed with the fields by POST /providers. A second
    // activation can fail after success, or overwrite a later tab's selection.
    const active = saved?.provider?.id === pid && saved.provider.is_active === true
      ? saved.provider : (await activateConfirmedProvider(payload)).active_provider;

    currentActiveProvider = active || { name: payload.name, chat_model: chatModel };
    if (_callbacks.updateBadge) _callbacks.updateBadge();

    if (savedRevision === providerRevision) {
      providerDirty = false;
      dirtyProviderFields.clear();
    }
    await loadProviders({ formRevision: savedRevision });
    if (!fromFooter) showToast(`已成功保存并启用：${payload.name} · ${chatModel}${providerDirty ? '；当前修改尚未保存。' : ''}`, 'success');
    return true;
  } catch (e) {
    console.error('Failed to activate provider:', e);
    const message = e.code === 'PROVIDER_UNCONFIRMED' ? e.message : `模型设置未保存或启用失败：${e.message || e}`;
    showToast(`${message}${fromFooter ? '；其他设置也还没有保存' : ''}`, 'error');
    return false;
  } finally {
    providerSaving = false;
    updateSaveButtons();
    _dom.gProviderActivate.innerHTML = origHtml;
  }
}

function renderEngineStatus(status) {
  const ready = status.state === 'ready' && status.ready === true;
  const loading = status.state === 'starting' || status.state === 'loading_slow';
  const checking = status.state === 'checking';
  if (_dom.gDashSovitsBadge) {
    _dom.gDashSovitsBadge.className = `badge-status-pill badge-pill-${ready ? 'green' : loading || checking ? 'yellow' : 'red'}`;
    _dom.gDashSovitsBadge.textContent = ready ? '在线' : status.state === 'loading_slow' ? '加载较慢'
      : loading ? '启动中' : checking ? '待确认' : status.state === 'failed' ? '启动失败' : '离线';
  }
  if (_dom.gDashSovitsStatus) {
    _dom.gDashSovitsStatus.textContent = ready && engineRestartUnconfirmed && !status.message?.includes('重启请求结果未确认')
      ? UNCONFIRMED_RESTART_MESSAGE
      : status.message;
  }
  if (_dom.gDashSovitsUrl && status.base_url) _dom.gDashSovitsUrl.textContent = status.base_url;
}

export async function fetchSystemTelemetry() {
  if (document.hidden || (_dom.globalModal && !settingsVisible())) return;
  telemetryController?.abort();
  const controller = new AbortController();
  telemetryController = controller;
  const revision = ++engineStatusRevision;
  const current = () => telemetryController === controller && !controller.signal.aborted && revision === engineStatusRevision;
  try {
    let statusRes = null, engineRes = null;
    const renderStatus = data => {
      if (!current() || !data) return;
      // 1. GPT-SoVITS
      if (data.gpt_sovits && _dom.gDashSovitsStatus && revision === engineStatusRevision && !restarting) {
        const isOnline = data.gpt_sovits.status === 'reachable';
        renderEngineStatus(engineRes?.state ? engineRes : {
          state: isOnline ? 'ready' : 'unavailable', ready: isOnline,
          message: isOnline ? `运行正常 (${data.gpt_sovits.latency_ms || 0}ms)` : '服务未响应（可点下方配置或重启）',
        });
        if (_dom.gDashSovitsUrl) _dom.gDashSovitsUrl.textContent = engineRes?.base_url || data.gpt_sovits.base_url || 'http://127.0.0.1:9880';
      }
      // 2. Precision & GPU
      if (data.hardware && revision === engineStatusRevision && !restarting) {
        const prec = data.hardware.precision_source === 'managed_launch'
          ? (data.hardware.inference_precision || '').toUpperCase() : '';
        const isCpu = prec === 'CPU';
        const isFp16 = prec === 'FP16';
        const known = isCpu || isFp16 || prec === 'FP32';
        const labels = { CPU: 'CPU 模式', FP16: 'FP16 半精度', FP32: 'FP32 单精度' };
        if (_dom.gDashPrecisionBadge) {
          if (!known) {
            _dom.gDashPrecisionBadge.className = 'badge-status-pill badge-pill-yellow';
            _dom.gDashPrecisionBadge.textContent = '待确认';
          } else if (isCpu) {
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
          _dom.gDashPrecisionVal.textContent = labels[prec] || '计算方式待确认';
        }
        if (_dom.gBtnTogglePrecisionText) {
          if (!known) {
            _dom.gBtnTogglePrecisionText.textContent = '按已保存设置启动';
          } else if (isCpu) {
            _dom.gBtnTogglePrecisionText.textContent = '切为 GPU(FP16)';
          } else if (isFp16) {
            _dom.gBtnTogglePrecisionText.textContent = '切为 FP32 重启';
          } else {
            _dom.gBtnTogglePrecisionText.textContent = '切为 CPU 模式';
          }
        }
        if (_dom.gDashDeviceVal) {
          if (!known) {
            _dom.gDashDeviceVal.textContent = '启动配置待确认；外部引擎请在对应程序中查看。';
          } else if (data.hardware.precision_pending_restart === true) {
            _dom.gDashDeviceVal.textContent = `已保存：${labels[data.hardware.next_inference_precision] || '新的计算方式'}；重启引擎后生效。`;
          } else if (isCpu) {
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
    };
    const options = { signal: controller.signal };
    const statusRead = requestJson('/api/system/status', options, { timeoutMs: 15000 }).catch(() => null).then(data => {
      statusRes = data;
      renderStatus(data);
    });
    const cacheRead = requestJson('/api/cache/stats', options, { timeoutMs: 15000 }).catch(() => null).then(data => {
      if (current() && data && _dom.gDashCacheVal) {
        _dom.gDashCacheVal.textContent = `${data.total_entries || 0} 个文件 · ${(data.total_size_mb || 0).toFixed(1)} MB`;
      }
    });
    const engineRead = requestJson('/api/system/engine-status', options, { timeoutMs: 10000 }).catch(() => null).then(data => {
      engineRes = data;
      if (current() && data?.state && !restarting) {
        renderEngineStatus(data);
      }
    });
    await Promise.all([statusRead, cacheRead, engineRead]);
    if (!statusRes && revision === engineStatusRevision && !restarting && _dom.gDashSovitsStatus) {
      if (engineRes?.state) renderEngineStatus(engineRes);
      else {
        _dom.gDashSovitsStatus.textContent = '无法刷新状态，请保留启动窗口；若已关闭，请重新双击启动。';
        if (_dom.gDashSovitsBadge) {
          _dom.gDashSovitsBadge.textContent = '待检查';
          _dom.gDashSovitsBadge.className = 'badge-status-pill badge-pill-yellow';
        }
      }
    }
  } catch (e) {
    if (!controller.signal.aborted) console.warn('Failed to fetch telemetry:', e);
  } finally {
    if (telemetryController === controller) telemetryController = null;
    controller.abort();
  }
}

export function updateTelegramFieldsVisibility() {
  const enabled = _dom.gTgEnabled ? _dom.gTgEnabled.checked : false;
  if (_dom.gTgFieldsGroup) {
    _dom.gTgFieldsGroup.style.opacity = enabled ? '1' : '0.45';
    _dom.gTgFieldsGroup.style.pointerEvents = enabled ? 'auto' : 'none';
    _dom.gTgFieldsGroup.inert = !enabled;
  }
}

export async function loadGlobalConfig() {
  engineDirectory?.load();
  const load = ++configLoad;
  const privateRevision = privateEndpointRevision;
  try {
    const fetchVoice = _callbacks.fetchVoiceProfiles
      ? _callbacks.fetchVoiceProfiles().catch(() => null)
      : Promise.resolve(null);
    const [cfg] = await Promise.all([requestJson('/api/config'), fetchVoice]);
    if (load !== configLoad) return;
    const configError = $('gConfigError');
    if (configError) { configError.textContent = ''; configError.classList.add('hidden'); }
    const s = cfg.settings || {};
    if (privateRevision === privateEndpointRevision && !privateEndpointSaving) {
      privateEndpointSaved = Boolean(s.allow_private_llm_endpoints);
      if (_dom.gAllowPrivateEndpoints) _dom.gAllowPrivateEndpoints.checked = privateEndpointSaved;
      if (_dom.gPrivateEndpointsStatus) _dom.gPrivateEndpointsStatus.textContent = privateEndpointSaved
        ? '已允许本机或局域网模型。更改后自动保存；仅填写你信任的服务地址。'
        : '使用 Ollama、LM Studio 或局域网模型时开启；更改后自动保存。仅填写你信任的服务地址。';
    }
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
    // Fill every field the user has not edited; edited fields keep their pending values.
    const fill = (field, apply) => {
      if (_dom[field] && !dirtyConfigFields.has(field)) apply(_dom[field]);
    };

    // 语音与推理参数
    fill('gParamSovitsUrl', (el) => { el.value = s.gpt_sovits_url || 'http://127.0.0.1:9880'; });
    fill('gPrecision', (el) => { el.value = s.inference_precision || 'auto'; });
    fill('gParamSliceMethod', (el) => { el.value = s.text_split_method || 'cut5'; });
    fill('gParamSpeed', (el) => {
      el.value = s.speed_factor !== undefined ? s.speed_factor : 1.0;
      if (_dom.gParamSpeedVal) _dom.gParamSpeedVal.textContent = el.value;
    });
    fill('gParamTopK', (el) => {
      el.value = s.top_k || 15;
      if (_dom.gParamTopKVal) _dom.gParamTopKVal.textContent = el.value;
    });
    fill('gParamTopP', (el) => {
      el.value = s.top_p !== undefined ? s.top_p : 1.0;
      if (_dom.gParamTopPVal) _dom.gParamTopPVal.textContent = el.value;
    });
    fill('gParamTemp', (el) => {
      el.value = s.temperature !== undefined ? s.temperature : 1.0;
      if (_dom.gParamTempVal) _dom.gParamTempVal.textContent = el.value;
    });
    fill('gParamFragmentInterval', (el) => {
      el.value = s.fragment_interval !== undefined ? s.fragment_interval : 0.3;
    });
    fill('gAudioRetention', (el) => { el.value = s.audio_retention_minutes || 30; });
    if (_dom.gDashRetentionVal) _dom.gDashRetentionVal.textContent = `保留时长: ${s.audio_retention_minutes || 30} 分钟`;

    // Telegram
    fill('gTgEnabled', (el) => {
      el.checked = Boolean(s.telegram_enabled);
      updateTelegramFieldsVisibility();
    });
    const token = s.telegram_bot_token || '';
    const masked = token.includes('****');
    fill('gTgToken', (el) => {
      el.value = '';
      el.placeholder = masked ? '已保存（输入新 Token 可覆盖）' : '未配置，如 123456:ABC-DEF…';
    });
    fill('gTgChatId', (el) => { el.value = s.telegram_chat_id || s.telegram_admin_ids || ''; });
    fill('gTgProxyHost', (el) => { el.value = s.telegram_proxy_host || ''; });
    fill('gTgProxyPort', (el) => { el.value = s.telegram_proxy_port || ''; });
    fill('gTgProxyEnabled', (el) => { el.checked = Boolean(s.telegram_proxy_enabled); });
    configLoaded = true;
  } catch (e) {
    if (load !== configLoad) return;
    const configError = $('gConfigError');
    if (configError) { configError.textContent = e.message; configError.classList.remove('hidden'); }
  }
}

export async function saveGlobalConfig() {
  if (configSaving || providerSaving) return;
  if (!configLoaded) {
    showToast('还没有读取到当前配置，保存会用默认值覆盖已有设置。正在重新读取，请稍后再保存。', 'error');
    loadGlobalConfig();
    return;
  }
  configSaving = true;
  updateSaveButtons();
  let modelSaved = false;
  let modelResult;
  try {
    const saveModel = providerDirty;
    const savedRevision = configRevision;
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
      telegram_enabled: _dom.gTgEnabled ? _dom.gTgEnabled.checked : false,
      telegram_chat_id: _dom.gTgChatId ? _dom.gTgChatId.value.trim() || undefined : undefined,
      telegram_admin_ids: _dom.gTgChatId ? _dom.gTgChatId.value.trim() || undefined : undefined,
      telegram_proxy_enabled: _dom.gTgProxyEnabled ? _dom.gTgProxyEnabled.checked : false,
      telegram_proxy_host: _dom.gTgProxyHost ? _dom.gTgProxyHost.value.trim() || '127.0.0.1' : '127.0.0.1',
      telegram_proxy_port: _dom.gTgProxyPort ? Number(_dom.gTgProxyPort.value) || 10809 : 10809,
    };
    const token = _dom.gTgToken ? _dom.gTgToken.value.trim() : '';
    if (token) payload.telegram_bot_token = token;

    // Start the provider save now so it captures this click's model draft, but
    // reserve the config write before later autosaves can overtake it.
    modelResult = saveModel ? saveAndActivateProvider({ fromFooter: true }).then(saved => {
      modelSaved = saved;
      return saved;
    }) : Promise.resolve(true);
    // The earlier config write can fail and skip this queued callback while
    // the independent provider request is already in flight.
    modelResult.catch(() => {});
    const saved = await runSettingsMutation(async save => {
      if (saveModel && !await modelResult) return false;
      await save(payload);
      return true;
    });
    if (!saved) return;
    if (savedRevision === configRevision) {
      configDirty = false;
      dirtyConfigFields.clear();
    }

    if (_dom.gPreset) {
      saveGlobal({ ttsPreset: _dom.gPreset.value });
    }
    if (_dom.gAutoTranslate) {
      saveGlobal({ autoTranslate: _dom.gAutoTranslate.value === 'auto' });
    }

    showToast(configDirty || providerDirty
      ? (saveModel ? '模型设置已保存并启用，之前的全局配置已保存；当前修改尚未保存。' : '之前的配置已保存；当前修改尚未保存。')
      : (saveModel ? '全局配置和模型设置已保存，模型已启用。' : '全局配置已保存'), 'success');
    loadGlobalConfig();
    fetchSystemTelemetry();
  } catch (e) {
    if (modelResult) await modelResult.catch(() => false);
    showToast(`${modelSaved ? '模型设置已保存并启用；全局配置保存失败' : '保存失败'}: ${e.message || e}`, 'error');
  } finally {
    configSaving = false;
    updateSaveButtons();
  }
}

export async function testTelegram() {
  if (_dom.gTgTest?.disabled) return;
  if (_dom.gTgTest) _dom.gTgTest.disabled = true;
  try {
    const payload = {
      proxy_enabled: _dom.gTgProxyEnabled ? _dom.gTgProxyEnabled.checked : false,
      proxy_host: _dom.gTgProxyHost ? _dom.gTgProxyHost.value.trim() || '127.0.0.1' : '127.0.0.1',
      proxy_port: _dom.gTgProxyPort ? Number(_dom.gTgProxyPort.value) || 10809 : 10809,
    };
    const token = _dom.gTgToken ? _dom.gTgToken.value.trim() : '';
    if (token) payload.token = token;
    const data = await requestJson('/api/telegram/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }, { timeoutMs: 60000 });
    showToast(data.message || (data.success ? '连接成功' : '连接失败'), data.success ? 'success' : 'error');
  } catch (e) {
    showToast(`测试失败: ${e.message || e}`, 'error');
  } finally {
    if (_dom.gTgTest) _dom.gTgTest.disabled = false;
  }
}

export async function handleRestartSovits(precision) {
  if (restarting) return;
  restarting = true;
  engineStatusRevision += 1;
  renderEngineStatus({ state: 'starting', ready: false, message: '正在确认设置并准备语音引擎；文字聊天可继续使用。' });
  if (_dom.gBtnRestartSovits) {
    _dom.gBtnRestartSovits.disabled = true;
    _dom.gBtnRestartSovits.innerHTML = '<svg class="icon"><use href="#i-play"></use></svg><span>正在准备引擎…</span>';
  }
  if (_dom.gBtnTogglePrecision) _dom.gBtnTogglePrecision.disabled = true;
  try {
    const payload = {};
    // Before the config loads, the dropdown shows its default; the engine keeps its saved precision.
    const selected = typeof precision === 'string' ? precision : (configLoaded ? _dom.gPrecision?.value : '');
    const revision = precisionRevision;
    if (selected) payload.precision = selected;
    let sent = false;
    let acknowledged = true;
    let data;
    try {
      data = await runSettingsMutation(() => {
        sent = true;
        return requestJson('/api/system/restart_sovits', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
        });
      });
    } catch (error) {
      if (!sent || error.name === 'AbortError' || (error.status && error.status < 500)) throw error;
      acknowledged = false;
      engineRestartUnconfirmed = true;
      data = { message: '重启请求结果未确认，正在核对当前引擎状态；请稍候，无需再次点击重启。' };
      renderEngineStatus({ state: 'checking', ready: false, message: data.message });
    }
    if (acknowledged) engineRestartUnconfirmed = false;
    if (acknowledged && _dom.gPrecision && selected && revision === precisionRevision) _dom.gPrecision.value = selected;
    // The card and busy button already explain recovery. One final toast avoids
    // covering the card with stacked progress messages on a narrow screen.
    if (acknowledged) showToast(data.message || '引擎开始加载，请稍候。', 'info');
    let result = await waitForEngine({ onProgress: (status) => {
      renderEngineStatus(status);
      if (_dom.gBtnRestartSovits) _dom.gBtnRestartSovits.innerHTML = `<svg class="icon"><use href="#i-play"></use></svg><span>${status.ready ? '引擎已就绪' : status.state === 'checking' ? '正在核对状态…' : status.state === 'loading_slow' ? '加载时间较长…' : '正在加载模型…'}</span>`;
    } });
    if (!acknowledged && result.ready) result = { ...result, message: UNCONFIRMED_RESTART_MESSAGE };
    renderEngineStatus(result);
    showToast(result.message, result.ready && acknowledged ? 'success' : 'info');
  } catch (err) {
    const unconfirmed = err.code === 'CONFIG_WRITE_SKIPPED' || [401, 403, 404, 429].includes(err.status);
    renderEngineStatus({ state: err.engineState || (unconfirmed ? 'checking' : 'failed'), ready: false, message: err.message });
    const needDir = /引擎目录|选择文件夹|api_v2\.py|运行环境/i.test(err.message || '');
    showToast(err.message, 'error', needDir ? {
      actionLabel: '选择引擎文件夹',
      onAction: () => {
        const tabBtn = _dom.gTabs ? _dom.gTabs.querySelector('[data-gtab="inference"]') : null;
        if (tabBtn) tabBtn.click();
        _dom.gSovitsDirectory?.focus?.();
      },
    } : null);
  } finally {
    restarting = false;
    if (_dom.gBtnRestartSovits) {
      _dom.gBtnRestartSovits.disabled = false;
      _dom.gBtnRestartSovits.innerHTML = '<svg class="icon"><use href="#i-play"></use></svg><span>重启 SoVITS 引擎</span>';
    }
    if (_dom.gBtnTogglePrecision) _dom.gBtnTogglePrecision.disabled = false;
    fetchSystemTelemetry();
  }
}

async function handleTogglePrecision() {
  const badgeText = _dom.gDashPrecisionBadge?.textContent.trim().toUpperCase();
  if (!['CPU', 'FP16', 'FP32'].includes(badgeText)) {
    await handleRestartSovits();
    return;
  }
  let targetPrec = 'fp16';
  if (badgeText === 'FP16') {
    targetPrec = 'fp32';
  } else if (badgeText === 'FP32') {
    targetPrec = 'cpu';
  } else {
    targetPrec = 'fp16';
  }

  await handleRestartSovits(targetPrec);
}

async function handleClearCache() {
  if (_dom.gBtnClearCache.disabled) return;
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
    const data = await requestJson('/api/cache/clear', { method: 'POST' });
    showToast(`缓存已清空：删除 ${data.deleted_files || 0} 个文件，释放 ${data.freed_mb || 0} MB`, 'success');
    fetchSystemTelemetry();
  } catch (err) {
    showToast(`清空缓存失败: ${err.message}`, 'error');
  } finally {
    _dom.gBtnClearCache.disabled = false;
  }
}
