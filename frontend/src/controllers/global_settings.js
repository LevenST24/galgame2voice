// Global Settings Controller: Coordinator & compatibility facade for Providers, Telemetry, Engine, Telegram
import {
  requestJson,
} from '../api.js';
import { state, saveGlobal } from '../store.js';
import { showToast } from '../ui.js';
import { initEngineDirectory } from './engine_directory.js';
import { runSettingsMutation, saveConfirmedConfig } from './config_recovery.js';
import {
  createProviderSettingsController,
} from './provider_settings.js';
import {
  createTelemetrySettingsController,
} from './telemetry_settings.js';
import {
  createEngineSettingsController,
} from './engine_settings.js';
import {
  createTelegramSettingsController,
} from './telegram_settings.js';
import { initSettingsTabs } from '../settings_ui.js';

const $ = (id) => (typeof document !== 'undefined' ? document.getElementById?.(id) : null);

let _dom = {};
let _callbacks = {};
let providerController = null;
let telemetryController = null;
let engineController = null;
let telegramController = null;
let engineDirectory = null;
let tabsCleanup = null;
let coordinatorCleanups = [];
let settingsCoordinatorRevision = 0;
let configSaving = false;
let configDirty = false;
let configRevision = 0;
let configLoad = 0;
let configLoaded = false;
const dirtyConfigFields = new Set();
const RESET_CONFIG_FIELDS = ['gParamSpeed', 'gParamTopK', 'gParamTopP', 'gParamTemp', 'gParamSliceMethod',
  'gPrecision', 'gAudioRetention'];
let privateEndpointRevision = 0;
let privateEndpointSaved = false;
let privateEndpointSaving = false;
const CONFIG_FIELDS = ['gParamSovitsUrl', 'gPrecision', 'gParamSliceMethod', 'gParamSpeed', 'gParamTopK',
  'gParamTopP', 'gParamTemp', 'gParamFragmentInterval', 'gAudioRetention', 'gTgEnabled',
  'gTgToken', 'gTgChatId', 'gTgProxyHost', 'gTgProxyPort', 'gTgProxyEnabled'];

function _bindCoordinator(element, type, handler) {
  if (!element || typeof element.addEventListener !== 'function') return;
  const bound = (...args) => handler(...args);
  element.addEventListener(type, bound);
  coordinatorCleanups.push(() => {
    if (typeof element.removeEventListener === 'function') {
      element.removeEventListener(type, bound);
    }
  });
}

export function getCurrentActiveProvider() {
  return providerController ? providerController.getCurrentActiveProvider() : null;
}

/** 模型列表是否已读取过（成功或失败）；之前没有活跃模型不代表「未配置」。 */
export function haveProvidersLoaded() {
  return providerController ? providerController.haveProvidersLoaded() : false;
}

export function hasProviderReadError() {
  return providerController ? providerController.hasProviderReadError() : false;
}

export function loadProviders(options) {
  return providerController ? providerController.loadProviders(options) : Promise.resolve();
}

export function onProviderChange() {
  return providerController?.onProviderChange();
}

export function toggleKeyVisibility() {
  return providerController?.toggleKeyVisibility();
}

export function onChatModelSelectChange() {
  return providerController?.onChatModelSelectChange();
}

export function handleProviderTest() {
  return providerController ? providerController.handleProviderTest() : Promise.resolve();
}

export function handleActivateProvider() {
  return providerController ? providerController.handleActivateProvider() : Promise.resolve(false);
}

export function updateTelegramFieldsVisibility() {
  return telegramController?.updateTelegramFieldsVisibility();
}

export function testTelegram() {
  return telegramController ? telegramController.testTelegram() : Promise.resolve();
}

export function handleRestartSovits(precision) {
  return engineController ? engineController.handleRestartSovits(precision) : Promise.resolve();
}

export function fetchSystemTelemetry() {
  return telemetryController ? telemetryController.fetchSystemTelemetry() : Promise.resolve();
}

function updateSaveButtons() {
  const busy = (providerController?.isProviderSaving() ?? false) || configSaving;
  if (_dom.gProviderActivate) _dom.gProviderActivate.disabled = busy;
  if (_dom.gTgSave) _dom.gTgSave.disabled = busy;
}

function settingsVisible() {
  return _dom.globalModal && !_dom.globalModal.hidden && !_dom.globalModal.classList.contains('hidden');
}

function goToInferenceTab() {
  const tabBtn = _dom.gTabs ? _dom.gTabs.querySelector('[data-gtab="inference"]') : null;
  if (tabBtn) tabBtn.click();
}

export function initGlobalSettings(dom, callbacks = {}) {
  // 1. Release previous controllers and coordinator listeners
  telemetryController?.dispose();
  engineController?.dispose();
  telegramController?.dispose();
  providerController?.dispose();
  engineDirectory?.dispose?.();
  if (typeof tabsCleanup === 'function') {
    tabsCleanup();
    tabsCleanup = null;
  }
  for (const cleanup of coordinatorCleanups) {
    try { cleanup(); } catch (_) {}
  }
  coordinatorCleanups = [];

  // 2. Set coordinator state
  settingsCoordinatorRevision += 1;
  _dom = dom || {};
  _callbacks = callbacks || {};
  configSaving = false;
  configDirty = false;
  configRevision = 0;
  configLoaded = false;
  dirtyConfigFields.clear();
  privateEndpointRevision += 1;
  privateEndpointSaved = false;
  privateEndpointSaving = false;

  // 3. Instantiate domain controllers
  providerController = createProviderSettingsController(_dom, {
    updateBadge: () => _callbacks.updateBadge?.(),
    updateSaveButtons,
    isConfigSaving: () => configSaving,
    isConnectionPreferencePending: connectionPreferencePending,
    settingsVisible,
    showToast,
  });

  telemetryController = createTelemetrySettingsController(_dom, {
    settingsVisible,
    isRestarting: () => engineController?.isRestarting() ?? false,
    isEngineRestartUnconfirmed: () => engineController?.isEngineRestartUnconfirmed() ?? false,
    loadGlobalConfig,
    showToast,
    onTelemetryHidden: () => {
      providerController?.invalidateActiveProviderTest();
      telegramController?.invalidateActiveTelegramTest();
    },
  });

  engineController = createEngineSettingsController(_dom, {
    isConfigLoaded: () => configLoaded,
    showToast,
    fetchSystemTelemetry: () => telemetryController?.fetchSystemTelemetry(),
    renderEngineStatus: (status) => telemetryController?.renderEngineStatus(status),
    goToInferenceTab,
    revokeAllCachedAudioUrls: () => _callbacks.revokeAllCachedAudioUrls?.(),
  });

  telegramController = createTelegramSettingsController(_dom, {
    showToast,
    updateSaveButtons,
  });

  updateSaveButtons();

  // 4. Bind coordinator-owned controls
  if (_dom.gAllowPrivateEndpoints) {
    _bindCoordinator(_dom.gAllowPrivateEndpoints, 'change', savePrivateEndpointPreference);
  }

  CONFIG_FIELDS.forEach((field) => {
    for (const event of ['input', 'change']) {
      if (_dom[field]) {
        _bindCoordinator(_dom[field], event, () => {
          configDirty = true;
          configRevision += 1;
          dirtyConfigFields.add(field);
        });
      }
    }
  });

  engineDirectory = initEngineDirectory(_dom, showToast, () => _callbacks.onSovitsDirectorySaved?.());

  // Tabs
  tabsCleanup = initSettingsTabs(_dom.gTabs, (name) => _callbacks.onSettingsTabChange?.(name));

  // Settings cross-navigation
  for (const id of ['gOpenSessionSettings', 'gOpenCharacterSettings']) {
    const el = $(id);
    if (el) {
      _bindCoordinator(el, 'click', () => {
        if (_callbacks.onSettingsTabChange) {
          if (id === 'gOpenSessionSettings') _dom.gTabs?.querySelector('[data-gtab="session"]')?.click();
          return;
        }
        _callbacks.closeModal?.(_dom.globalModal);
        _callbacks.onOpenSessionSettings?.();
      });
    }
  }

  const openReading = $('gOpenChatExperience');
  if (openReading) {
    _bindCoordinator(openReading, 'click', () => {
      if (_callbacks.onSettingsTabChange) return;
      _callbacks.closeModal?.(_dom.globalModal);
      _callbacks.onOpenReadingSettings?.();
    });
  }

  // Sliders numeric display linkage
  if (_dom.gParamSpeed) {
    _bindCoordinator(_dom.gParamSpeed, 'input', (e) => {
      if (_dom.gParamSpeedVal) _dom.gParamSpeedVal.textContent = e.target.value;
    });
  }
  if (_dom.gParamTopK) {
    _bindCoordinator(_dom.gParamTopK, 'input', (e) => {
      if (_dom.gParamTopKVal) _dom.gParamTopKVal.textContent = e.target.value;
    });
  }
  if (_dom.gParamTopP) {
    _bindCoordinator(_dom.gParamTopP, 'input', (e) => {
      if (_dom.gParamTopPVal) _dom.gParamTopPVal.textContent = e.target.value;
    });
  }
  if (_dom.gParamTemp) {
    _bindCoordinator(_dom.gParamTemp, 'input', (e) => {
      if (_dom.gParamTempVal) _dom.gParamTempVal.textContent = e.target.value;
    });
  }

  // Telegram save footer button
  if (_dom.gTgSave) {
    _bindCoordinator(_dom.gTgSave, 'click', saveGlobalConfig);
  }

  // Diagnostic Chips
  const diagModelChip = $('gDiagModelChip');
  if (diagModelChip) {
    diagModelChip.style.cursor = 'pointer';
    _bindCoordinator(diagModelChip, 'click', () => {
      const tabBtn = _dom.gTabs ? _dom.gTabs.querySelector('[data-gtab="models"]') : null;
      if (tabBtn) tabBtn.click();
    });
  }
  const diagVoiceChip = $('gDiagVoiceChip');
  if (diagVoiceChip) {
    diagVoiceChip.style.cursor = 'pointer';
    _bindCoordinator(diagVoiceChip, 'click', () => {
      if (_callbacks.onSettingsTabChange) {
        _dom.gTabs?.querySelector('[data-gtab="session"]')?.click();
        return;
      }
      _callbacks.closeModal?.(_dom.globalModal);
      _callbacks.onOpenSessionSettings?.();
    });
  }

  // Reset parameters
  if (_dom.gResetBtn) {
    _bindCoordinator(_dom.gResetBtn, 'click', () => {
      if (typeof confirm === 'function' && !confirm('确定要将全局参数恢复为推荐默认值吗？')) return;
      configDirty = true;
      configRevision += 1;
      RESET_CONFIG_FIELDS.forEach((field) => dirtyConfigFields.add(field));
      if (_dom.gParamSpeed) {
        _dom.gParamSpeed.value = '1.0';
        if (_dom.gParamSpeedVal) _dom.gParamSpeedVal.textContent = '1.0';
      }
      if (_dom.gParamTopK) {
        _dom.gParamTopK.value = '15';
        if (_dom.gParamTopKVal) _dom.gParamTopKVal.textContent = '15';
      }
      if (_dom.gParamTopP) {
        _dom.gParamTopP.value = '1.0';
        if (_dom.gParamTopPVal) _dom.gParamTopPVal.textContent = '1.0';
      }
      if (_dom.gParamTemp) {
        _dom.gParamTemp.value = '1.0';
        if (_dom.gParamTempVal) _dom.gParamTempVal.textContent = '1.0';
      }
      if (_dom.gParamSliceMethod) _dom.gParamSliceMethod.value = 'cut5';
      if (_dom.gPrecision) _dom.gPrecision.value = 'auto';
      if (_dom.gAudioRetention) _dom.gAudioRetention.value = '30';
      showToast('已恢复推荐数值，请点击【保存全局配置】生效', 'info');
    });
  }

  // Modal close buttons
  if (_dom.globalModal) {
    _dom.globalModal.querySelectorAll('[data-close]').forEach((btn) => {
      _bindCoordinator(btn, 'click', () => {
        if (_callbacks.closeModal) _callbacks.closeModal(_dom.globalModal);
      });
    });
  }
}

export function openGlobalSettings(tabName = null) {
  providerController?.invalidateActiveProviderTest();
  providerController?.resetTestResultBox();
  telegramController?.invalidateActiveTelegramTest();
  if (_callbacks.openModal) _callbacks.openModal(_dom.globalModal);
  if (typeof tabName === 'string' && tabName && _dom.gTabs) {
    const tabBtn = _dom.gTabs.querySelector(`[data-gtab="${tabName}"]`);
    if (tabBtn) tabBtn.click();
  } else {
    _callbacks.onSettingsTabChange?.(_dom.gTabs?.querySelector('.active')?.dataset.gtab);
  }
  loadProviders();
  loadGlobalConfig();
  if (settingsVisible()) telemetryController?.startTelemetryRefresh();
  else if (typeof document !== 'undefined' && !document.hidden) telemetryController?.fetchSystemTelemetry();
  if (_callbacks.loadSystemVersionInfo) {
    _callbacks.loadSystemVersionInfo(false).catch(() => {});
  }
  if (_dom.gPreset) {
    _dom.gPreset.value = state.global.ttsPreset || '';
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

export async function loadGlobalConfig() {
  engineDirectory?.load();
  const coordinatorRevision = settingsCoordinatorRevision;
  const targetDom = _dom;
  const load = ++configLoad;
  const privateRevision = privateEndpointRevision;
  try {
    const fetchVoice = _callbacks.fetchVoiceProfiles
      ? _callbacks.fetchVoiceProfiles().catch(() => null)
      : Promise.resolve(null);
    const [cfg] = await Promise.all([requestJson('/api/config'), fetchVoice]);
    if (coordinatorRevision !== settingsCoordinatorRevision || targetDom !== _dom || load !== configLoad) return;
    const configError = $('gConfigError');
    if (configError) { configError.textContent = ''; configError.classList.add('hidden'); }
    const s = cfg.settings || {};
    if (privateRevision === privateEndpointRevision && !privateEndpointSaving) {
      privateEndpointSaved = Boolean(s.allow_private_llm_endpoints);
      if (targetDom.gAllowPrivateEndpoints) targetDom.gAllowPrivateEndpoints.checked = privateEndpointSaved;
      if (targetDom.gPrivateEndpointsStatus) targetDom.gPrivateEndpointsStatus.textContent = privateEndpointSaved
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
      if (targetDom[field] && !dirtyConfigFields.has(field)) apply(targetDom[field]);
    };

    // 语音与推理参数
    fill('gParamSovitsUrl', (el) => { el.value = s.gpt_sovits_url || 'http://127.0.0.1:9880'; });
    fill('gPrecision', (el) => { el.value = s.inference_precision || 'auto'; });
    fill('gParamSliceMethod', (el) => { el.value = s.text_split_method || 'cut5'; });
    fill('gParamSpeed', (el) => {
      el.value = s.speed_factor !== undefined ? s.speed_factor : 1.0;
      if (targetDom.gParamSpeedVal) targetDom.gParamSpeedVal.textContent = el.value;
    });
    fill('gParamTopK', (el) => {
      el.value = s.top_k || 15;
      if (targetDom.gParamTopKVal) targetDom.gParamTopKVal.textContent = el.value;
    });
    fill('gParamTopP', (el) => {
      el.value = s.top_p !== undefined ? s.top_p : 1.0;
      if (targetDom.gParamTopPVal) targetDom.gParamTopPVal.textContent = el.value;
    });
    fill('gParamTemp', (el) => {
      el.value = s.temperature !== undefined ? s.temperature : 1.0;
      if (targetDom.gParamTempVal) targetDom.gParamTempVal.textContent = el.value;
    });
    fill('gParamFragmentInterval', (el) => {
      el.value = s.fragment_interval !== undefined ? s.fragment_interval : 0.3;
    });
    fill('gAudioRetention', (el) => { el.value = s.audio_retention_minutes || 30; });
    if (targetDom.gDashRetentionVal) targetDom.gDashRetentionVal.textContent = `保留时长: ${s.audio_retention_minutes || 30} 分钟`;

    // Telegram
    telegramController?.fillTelegramConfig(s, dirtyConfigFields);

    configLoaded = true;
  } catch (e) {
    if (coordinatorRevision !== settingsCoordinatorRevision || targetDom !== _dom || load !== configLoad) return;
    const configError = $('gConfigError');
    if (configError) { configError.textContent = e.message; configError.classList.remove('hidden'); }
  }
}

export async function saveGlobalConfig() {
  if (configSaving || (providerController?.isProviderSaving() ?? false)) return;
  if (!configLoaded) {
    showToast('还没有读取到当前配置，保存会用默认值覆盖已有设置。正在重新读取，请稍后再保存。', 'error');
    loadGlobalConfig();
    return;
  }
  const tgValidation = telegramController?.validateTelegram() || { valid: true };
  if (!tgValidation.valid) {
    showToast(tgValidation.message, 'error');
    updateSaveButtons();
    return;
  }

  const coordinatorRevision = settingsCoordinatorRevision;
  const targetProviderController = providerController;
  configSaving = true;
  updateSaveButtons();
  let modelSaved = false;
  let modelResult;
  try {
    const saveModel = targetProviderController?.isProviderDirty() ?? false;
    const savedRevision = configRevision;
    const tgPayload = telegramController?.getTelegramPayload() || {};
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
      ...tgPayload,
    };

    // Start the provider save now so it captures this click's model draft, but
    // reserve the config write before later autosaves can overtake it.
    modelResult = saveModel && targetProviderController ? targetProviderController.saveAndActivateProvider({ fromFooter: true }).then(saved => {
      modelSaved = saved;
      return saved;
    }) : Promise.resolve(true);
    // The earlier config write can fail and skip this queued callback while
    // the independent provider request is already in flight.
    modelResult.catch(() => {});
    const saved = await runSettingsMutation(async (save) => {
      if (saveModel && !await modelResult) return false;
      await save(payload);
      return true;
    });
    if (coordinatorRevision !== settingsCoordinatorRevision || targetProviderController?.disposed) return;
    if (!saved) return;
    if (savedRevision === configRevision) {
      configDirty = false;
      dirtyConfigFields.clear();
    }

    if (_dom.gPreset) {
      saveGlobal({ ttsPreset: _dom.gPreset.value });
    }

    const currentProviderDirty = targetProviderController?.isProviderDirty() ?? false;
    showToast(configDirty || currentProviderDirty
      ? (saveModel ? '模型设置已保存并启用，之前的全局配置已保存；当前修改尚未保存。' : '之前的配置已保存；当前修改尚未保存。')
      : (saveModel ? '全局配置和模型设置已保存，模型已启用。' : '全局配置已保存'), 'success');
    loadGlobalConfig();
    fetchSystemTelemetry();
  } catch (e) {
    if (modelResult) await modelResult.catch(() => false);
    if (coordinatorRevision !== settingsCoordinatorRevision || targetProviderController?.disposed) return;
    showToast(`${modelSaved ? '模型设置已保存并启用；全局配置保存失败' : '保存失败'}: ${e.message || e}`, 'error');
  } finally {
    if (coordinatorRevision === settingsCoordinatorRevision) {
      configSaving = false;
      updateSaveButtons();
    }
  }
}
