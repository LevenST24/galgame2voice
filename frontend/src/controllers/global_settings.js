// Global Settings Controller: Coordinator & compatibility facade for Providers, Telemetry, Engine
import {
  requestJson,
} from '../api.js';
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
import { initSettingsTabs } from '../settings_ui.js';
import { createSettingsDraftStore } from './settings_draft_store.js';

const $ = (id) => (typeof document !== 'undefined' ? document.getElementById?.(id) : null);

let _dom = {};
let _callbacks = {};
let providerController = null;
let telemetryController = null;
let engineController = null;
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
const RESET_CONFIG_FIELDS = ['gPrecision', 'gAudioRetention'];
let privateEndpointRevision = 0;
let privateEndpointSaved = false;
let privateEndpointSaving = false;
let precisionSavedRevision = 0;
let gpuAvailable = true;
const CONFIG_FIELDS = ['gParamSovitsUrl', 'gPrecision', 'gAudioRetention'];

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
  precisionSavedRevision += 1;
  gpuAvailable = true;

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
    onGpuAvailabilityChange: (available) => {
      gpuAvailable = available !== false;
    },
    onTelemetryHidden: () => {
      providerController?.invalidateActiveProviderTest();
    },
  });

  engineController = createEngineSettingsController(_dom, {
    isConfigLoaded: () => configLoaded,
    isGpuAvailable: () => gpuAvailable,
    showToast,
    fetchSystemTelemetry: () => telemetryController?.fetchSystemTelemetry(),
    renderEngineStatus: (status) => telemetryController?.renderEngineStatus(status),
    goToInferenceTab,
    revokeAllCachedAudioUrls: () => _callbacks.revokeAllCachedAudioUrls?.(),
    // An autosaved choice is no longer a pending edit, so later loads may refresh it.
    onPrecisionSaved: (value) => {
      precisionSavedRevision += 1;
      if (_dom.gPrecision?.value === value) dirtyConfigFields.delete('gPrecision');
    },
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

  // Global save footer button
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
  const precisionRevision = precisionSavedRevision;
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
    if (precisionRevision === precisionSavedRevision) {
      fill('gPrecision', (el) => { el.value = s.inference_precision || 'auto'; });
    }
    fill('gAudioRetention', (el) => { el.value = s.audio_retention_minutes || 30; });
    if (targetDom.gDashRetentionVal) targetDom.gDashRetentionVal.textContent = `保留时长：${s.audio_retention_minutes || 30} 分钟`;

    configLoaded = true;
  } catch (e) {
    if (coordinatorRevision !== settingsCoordinatorRevision || targetDom !== _dom || load !== configLoad) return;
    const configError = $('gConfigError');
    if (configError) { configError.textContent = e.message; configError.classList.remove('hidden'); }
  }
}

const settingsDraftStore = createSettingsDraftStore({
  isConfigSaving: () => configSaving,
  getProviderController: () => providerController,
  isConfigLoaded: () => configLoaded,
  showToast,
  reloadConfig: () => { loadGlobalConfig(); },
  getEngineDirectory: () => engineDirectory,
  getCoordinatorRevision: () => settingsCoordinatorRevision,
  setConfigSaving: (saving) => {
    configSaving = saving;
    updateSaveButtons();
  },
  getConfigRevision: () => configRevision,
  readConfigPayload: () => ({
    gpt_sovits_url: _dom.gParamSovitsUrl ? _dom.gParamSovitsUrl.value.trim() || 'http://127.0.0.1:9880' : undefined,
    inference_precision: _dom.gPrecision ? _dom.gPrecision.value : undefined,
    audio_retention_minutes: _dom.gAudioRetention ? Math.max(1, Number(_dom.gAudioRetention.value) || 30) : 30,
  }),
  runSettingsMutation,
  clearConfigDirtyIfUnchanged: (savedRevision) => {
    if (savedRevision === configRevision) {
      configDirty = false;
      dirtyConfigFields.clear();
    }
  },
  isConfigDirty: () => configDirty,
  refreshTelemetry: () => { fetchSystemTelemetry(); },
});

export async function saveGlobalConfig() {
  return settingsDraftStore.commitAll();
}
